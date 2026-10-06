"""
Regression tests for mc_facts.py — officeholder facts + the stale-title backstop.

Incident (2026-10-06): the podcast called the sitting president "former
President Trump" because the writer model's training data predates the 2024
election. These tests pin the live Wikidata lookup (mocked), its fallbacks,
the prompt injection, and the deterministic fixer.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_facts  # noqa: E402
from mc_facts import (  # noqa: E402
    FALLBACK, OFFICES, Holder, _resolve, fix_stale_titles,
    fix_stale_titles_in_outputs, get_officeholders, prompt_block,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    get_officeholders.cache_clear()
    yield
    get_officeholders.cache_clear()


def fixed(text: str) -> str:
    return fix_stale_titles(text)[0]


def _rows_from_fallback() -> list[dict]:
    """Wikidata-shaped rows equivalent to FALLBACK, plus noise the resolver must ignore."""
    rows = []
    for qid, (cur, prev) in FALLBACK.items():
        rows.append({"office": qid, "name": cur.name, "start": cur.since, "end": None})
        if prev:
            rows.append({"office": qid, "name": prev.name, "start": "2000-01-01", "end": prev.since})
    rows.append({"office": "Q11696", "name": "Q131583299", "start": "2020-01-01", "end": None})  # unlabeled item
    rows.append({"office": "Q11696", "name": "Barack Obama", "start": "2009-01-20", "end": "2017-01-20"})
    return rows


# ---------------------------------------------------------------------------
# Resolution / sources
# ---------------------------------------------------------------------------

class TestResolve:

    def test_matches_fallback(self):
        assert _resolve(_rows_from_fallback(), "2026-10-06") == FALLBACK

    def test_predecessor_is_most_recent_ended_term(self):
        cur, prev = _resolve(_rows_from_fallback(), "2026-10-06")["Q11696"]
        assert (cur.name, prev.name) == ("Donald Trump", "Joe Biden")

    def test_two_current_holders_rejected(self):
        rows = _rows_from_fallback() + [{"office": "Q785304", "name": "Kingpin", "start": "2020-01-01", "end": None}]
        assert "Q785304" not in _resolve(rows, "2026-10-06")

    def test_future_start_not_current(self):
        rows = _rows_from_fallback() + [{"office": "Q785304", "name": "Someone Else", "start": "2030-01-01", "end": None}]
        assert _resolve(rows, "2026-10-06")["Q785304"][0].name == "Zohran Mamdani"

    def test_reelection_statements_collapse(self):
        rows = _rows_from_fallback() + [{"office": "Q2347975", "name": "Kathy Hochul", "start": "2023-01-01", "end": None}]
        cur, _ = _resolve(rows, "2026-10-06")["Q2347975"]
        assert cur == Holder("Kathy Hochul", "2021-08-24")


class TestSources:

    def test_wikidata_used_and_cached_when_complete(self, monkeypatch):
        monkeypatch.setenv("MIKECAST_FACTS_OFFLINE", "0")
        rows = [r for r in _rows_from_fallback() if not (r["office"] == "Q2666591" and r["end"] is None)]
        rows.append({"office": "Q2666591", "name": "New Chair", "start": "2026-09-01", "end": None})
        with patch.object(mc_facts, "_fetch_wikidata_rows", return_value=rows), \
             patch.object(mc_facts, "_save_s3_cache") as save:
            holders = get_officeholders()
        assert holders["Q2666591"][0].name == "New Chair"
        save.assert_called_once()

    def test_disagreement_with_fallback_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("MIKECAST_FACTS_OFFLINE", "0")
        rows = [r for r in _rows_from_fallback() if not (r["office"] == "Q785304" and r["end"] is None)]
        rows.append({"office": "Q785304", "name": "New Mayor", "start": "2026-09-01", "end": None})
        with patch.object(mc_facts, "_fetch_wikidata_rows", return_value=rows), \
             patch.object(mc_facts, "_save_s3_cache"), caplog.at_level(logging.WARNING):
            get_officeholders()
        assert "update FALLBACK" in caplog.text and "New Mayor" in caplog.text

    def test_wikidata_down_uses_s3_cache_then_fallback(self, monkeypatch, caplog):
        monkeypatch.setenv("MIKECAST_FACTS_OFFLINE", "0")
        cached = {"Q11696": (Holder("Cached President", "2025-01-20"), None)}
        with patch.object(mc_facts, "_fetch_wikidata_rows", side_effect=RuntimeError("timeout")), \
             patch.object(mc_facts, "_load_s3_cache", return_value=(cached, "2026-10-01T00:00:00+00:00")), \
             caplog.at_level(logging.WARNING):
            holders = get_officeholders()
        assert holders["Q11696"][0].name == "Cached President"
        assert holders["Q11699"] == FALLBACK["Q11699"]
        assert "Wikidata lookup failed" in caplog.text

    def test_never_raises(self, monkeypatch):
        monkeypatch.setenv("MIKECAST_FACTS_OFFLINE", "0")
        with patch.object(mc_facts, "_fetch_wikidata_rows", side_effect=ValueError("bad json")), \
             patch.object(mc_facts, "_load_s3_cache", side_effect=RuntimeError("no creds")):
            assert get_officeholders() == FALLBACK

    def test_stale_fallback_warns(self, caplog):
        with patch.object(mc_facts, "FALLBACK_REVIEWED", date(2000, 1, 1)), caplog.at_level(logging.WARNING):
            get_officeholders()
        assert "last reviewed 2000-01-01" in caplog.text

    def test_every_office_has_fallback(self):
        assert {o.qid for o in OFFICES} == set(FALLBACK)


# ---------------------------------------------------------------------------
# Fixer (offline → FALLBACK data)
# ---------------------------------------------------------------------------

class TestCurrentHolders:

    def test_former_president_trump(self):
        assert fixed("Today former President Trump said") == "Today President Trump said"

    def test_former_at_sentence_start(self):
        assert fixed("Big day. Former President Donald J. Trump signed it.") == \
            "Big day. President Donald J. Trump signed it."

    def test_lowercase_and_us_prefix(self):
        assert fixed("the former U.S. president Trump") == "the U.S. president Trump"

    def test_ex_and_elect(self):
        assert fixed("ex-President Trump and President-elect Trump") == \
            "President Trump and President Trump"

    def test_other_offices(self):
        assert fixed("former Vice President JD Vance") == "Vice President JD Vance"
        assert fixed("former Fed Chair Kevin Warsh") == "Fed Chair Kevin Warsh"
        assert fixed("ex-Gov. Hochul") == "Gov. Hochul"

    def test_lowercase_word_not_taken_as_given_name(self):
        text = "the former President said Trump"
        assert fixed(text) == text

    def test_correct_text_untouched(self):
        text = "President Trump met with Vice President Vance and former President Biden."
        assert fix_stale_titles(text) == (text, [])


class TestPreviousHolders:

    def test_president_biden_gets_former(self):
        assert fixed("a bill President Biden signed") == "a bill former President Biden signed"

    def test_sentence_start_capitalized(self):
        assert fixed("[ELIZABETH]\nPresident Joe Biden spoke.") == \
            "[ELIZABETH]\nFormer President Joe Biden spoke."

    def test_already_qualified_untouched(self):
        for text in ("former President Biden", "Former President Biden",
                     "then-President Biden", "Vice President Biden", "ex-President Biden"):
            assert fixed(text) == text, text

    def test_fed_chair_powell(self):
        assert fixed("said Fed Chair Jerome Powell") == "said former Fed Chair Jerome Powell"
        assert fixed("Vice Chair Powell") == "Vice Chair Powell"

    def test_already_former_with_multiword_title_untouched(self):
        # Regression: "Chair" is also a title alternative, so this used to become
        # "former Fed former Chair Powell" (caught by eval/fixtures/synthetic/bare-names).
        for text in ("former Fed Chair Powell", "Former Fed Chair Jerome Powell",
                     "former Federal Reserve Chair Powell", "the then-Fed Chair Powell"):
            assert fix_stale_titles(text) == (text, []), text

    def test_pope_flagged_not_changed(self):
        text, changes = fix_stale_titles("Pope Francis said")
        assert text == "Pope Francis said"
        assert any("FLAGGED" in c for c in changes)
        assert fix_stale_titles("the late Pope Francis")[1] == []


class TestPlumbing:

    def test_outputs_wrapper(self):
        out = fix_stale_titles_in_outputs(a="former President Trump", b="", c="fine")
        assert out == {"a": "President Trump", "b": "", "c": "fine"}

    def test_prompt_block_content(self):
        block = prompt_block()
        assert "Donald Trump is the current President of the United States" in block
        assert "former President Biden" in block
        assert "Pope Francis was the previous Pope" in block
        assert "TITLE RULE" in block

    def test_writers_get_prompt_block(self):
        from crew.agents import (
            make_conversational_writer, make_html_writer,
            make_section_patcher, make_single_voice_writer, make_social_copywriter,
        )
        block = prompt_block()
        for make in (make_html_writer, make_single_voice_writer, make_conversational_writer,
                     make_section_patcher, make_social_copywriter):
            assert block in make().backstory, make.__name__
