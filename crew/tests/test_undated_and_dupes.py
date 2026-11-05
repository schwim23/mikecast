"""
Tests for the 2026-11-05 follow-ups to the payments rollout:

1. filter_stale_articles now DROPS undated / unparseable articles (it used to
   keep them, which is how the April Tim Cook story got in).
2. merge_duplicate_stories removes same-event duplicates that title-only
   clustering missed (two outlets on Block's DOJ settlement, the same Stripe
   launch twice, and Visa earnings in two categories).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_collect  # noqa: E402

PAY = "Payments & Commerce"


def _rfc(days_ago: float) -> str:
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=days_ago))


# ---------------------------------------------------------------------------
# 1. Undated articles are dropped
# ---------------------------------------------------------------------------

class TestUndatedDropped:

    def _run(self, arts):
        return mc_collect.filter_stale_articles({PAY: arts}, max_age_days=3)[PAY]

    def test_fresh_rfc_and_iso_dates_kept(self):
        iso = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        kept = self._run([
            {"title": "rfc", "published": _rfc(0.5), "source": "Finextra"},
            {"title": "iso", "published": iso, "source": "NYT"},
        ])
        assert [a["title"] for a in kept] == ["rfc", "iso"]

    def test_old_article_dropped(self):
        assert self._run([{"title": "old", "published": _rfc(10), "source": "X"}]) == []

    def test_missing_empty_none_and_garbage_dates_dropped(self, caplog):
        arts = [
            {"title": "missing", "source": "A"},
            {"title": "empty", "published": "   ", "source": "A"},
            {"title": "none", "published": None, "source": "B"},
            {"title": "garbage", "published": "yesterday-ish", "source": "B"},
            {"title": "fresh", "published": _rfc(1), "source": "C"},
        ]
        with caplog.at_level("WARNING", logger="mikecast"):
            kept = self._run(arts)
        assert [a["title"] for a in kept] == ["fresh"]
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "4 undated" in msg and "A=2" in msg and "B=2" in msg


# ---------------------------------------------------------------------------
# 2. Duplicate-story merge
# ---------------------------------------------------------------------------

def _art(title, source, score, desc=""):
    return {"title": title, "source": source, "score": score, "description": desc}


def _fake_llm(groups):
    """Patch _LLMClient so the helper model 'returns' *groups* and records the prompt."""
    seen = {}

    def create(model, **kw):
        seen["prompt"] = kw["messages"][0]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=groups))])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return patch.object(mc_collect, "_LLMClient", return_value=fake), seen


def _scored():
    return {
        "Business & Markets": [
            _art("Visa beats quarterly estimates on cross-border volume", "CNBC", 70),
            _art("Stocks edge higher as yields slip", "AP", 65),
        ],
        "Companies": [_art("Retailer cuts 1,200 jobs", "WSJ", 60)],
        PAY: [
            _art("Stripe Opens Public Preview of Stablecoin Wallet Payouts in 160 Markets", "TokenPost", 90),
            _art("Block faces $765m DoJ settlement", "Finextra", 85),
            _art("Stripe Lets US Businesses Send Stablecoins to Wallets in 160-Plus Markets", "The Defiant", 80),
            _art("Block reaches $964M DOJ pact", "Payments Dive", 88),
            _art("Visa Q4 revenue tops estimates", "Payments Dive", 75),
            _art("Revolut hits $115bn valuation", "Finextra", 70),
        ],
        "NY Sports": [_art("Knicks beat Celtics", "ESPN", 90), _art("Knicks recap", "ESPN", 80)],
    }


def _ids(prompt, *needles):
    """Map title substrings to the candidate ids shown in the prompt."""
    out = []
    for n in needles:
        line = next(l for l in prompt.splitlines() if n in l)
        out.append(int(line.split(":", 1)[0]))
    return out


class TestMergeDuplicateStories:

    def _merge(self, scored, groups_fn):
        # Two-step: run once to capture ids in the prompt, then answer with groups.
        p, seen = _fake_llm("[]")
        with patch.object(mc_collect, "OPENAI_API_KEY", "k"), p:
            mc_collect.merge_duplicate_stories(scored, total=30)
        groups = groups_fn(seen["prompt"])
        p2, _ = _fake_llm(json.dumps(groups))
        with patch.object(mc_collect, "OPENAI_API_KEY", "k"), p2:
            return mc_collect.merge_duplicate_stories(scored, total=30), seen["prompt"]

    def test_keeps_highest_score_in_each_group_and_across_categories(self):
        out, prompt = self._merge(_scored(), lambda pr: [
            _ids(pr, "Stablecoin Wallet Payouts", "Send Stablecoins"),
            _ids(pr, "$765m", "$964M"),
            _ids(pr, "Visa beats", "Visa Q4"),
        ])
        pay = [a["title"] for a in out[PAY]]
        assert "Stripe Lets US Businesses Send Stablecoins to Wallets in 160-Plus Markets" not in pay
        assert "Stripe Opens Public Preview of Stablecoin Wallet Payouts in 160 Markets" in pay
        assert "Block reaches $964M DOJ pact" in pay          # 88 beats 85
        assert "Block faces $765m DoJ settlement" not in pay
        assert "Visa Q4 revenue tops estimates" in pay        # 75 beats 70 (cross-category)
        assert not any("Visa" in a["title"] for a in out["Business & Markets"])
        assert len(out["Companies"]) == 1

    def test_ny_sports_never_sent_or_touched(self):
        out, prompt = self._merge(_scored(), lambda pr: [])
        assert "Knicks" not in prompt
        assert len(out["NY Sports"]) == 2

    def test_prompt_includes_source_and_description(self):
        scored = {PAY: [_art("A", "Finextra", 1, "about the deal"), _art("B", "PYMNTS", 2)]}
        _, prompt = self._merge(scored, lambda pr: [])
        assert "Finextra" in prompt and "about the deal" in prompt and f"[{PAY}]" in prompt

    def test_fails_open_on_bad_output(self):
        for bad in ("not json", '{"a": 1}', "[[999, 1000]]", "[[0]]", "[\"x\"]"):
            p, _ = _fake_llm(bad)
            scored = _scored()
            with patch.object(mc_collect, "OPENAI_API_KEY", "k"), p:
                out = mc_collect.merge_duplicate_stories(scored, total=30)
            assert {k: len(v) for k, v in out.items()} == {k: len(v) for k, v in _scored().items()}, bad

    def test_no_api_key_is_a_noop(self):
        scored = _scored()
        with patch.object(mc_collect, "OPENAI_API_KEY", ""):
            assert mc_collect.merge_duplicate_stories(scored) is scored

    def test_dropped_slot_backfilled_by_selection(self):
        # 4 payments stories, two are the same event → selection should still
        # fill from the remaining distinct stories, not leave the dupe in.
        scored = {PAY: [_art(f"s{i}", "src", 100 - i) for i in range(6)]}
        out, _ = self._merge(scored, lambda pr: [_ids(pr, "s0", "s1")])
        titles = [a["title"] for a in mc_collect.select_top_articles(out, total=3)[PAY]]
        assert titles == ["s0", "s2", "s3"]
