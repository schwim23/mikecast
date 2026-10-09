"""
Tests for crew/grounding_check.py (log-only grounding check), the 'n/a' + background
additions to validate_claim_against_articles, and the synthetic bare-names eval fixture.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crew.grounding_check import check_grounding, collect_sentences, with_sports_sources  # noqa: E402

HTML = """<html><body>
<h2>AI &amp; TECH</h2><p>OpenAI released a new reasoning model on Monday for enterprise customers.</p>
<h2>NY SPORTS</h2><p>The Yankees beat the Rays five to two on Sunday night in the Bronx.</p>
<h2>WHAT TO WATCH</h2><p>Watch whether Intel shares hold their six percent gain this week.</p>
</body></html>"""

CONV = """[MIKE]
Welcome back to MikeCast, I'm Mike with Elizabeth and Jesse today.
[ELIZABETH]
Intel stock jumped nearly six percent after Trump confirmed the Apple deal.
[JESSE]
The Yankees beat the Rays five to two on Sunday night in the Bronx.
[MIKE]
Thanks for listening everybody, we will see you tomorrow morning."""

ARTICLES = {
    "AI & Tech": [{"title": "OpenAI releases reasoning model", "description": "For enterprise."}],
    "Companies": [{"title": "Intel rips 6% after Trump confirms Apple deal", "description": ""}],
    "NY Sports": [{"title": "Yankees beat Rays", "description": "5-2"}],
}


class TestCollect:

    def test_skips_ny_sports_and_jesse(self):
        items = collect_sentences(HTML, CONV, ARTICLES)
        text = " ".join(s for _, s, _ in items)
        assert "Yankees" not in text
        assert "OpenAI released" in text and "Intel stock jumped" in text

    def test_every_sentence_checked_against_all_articles(self):
        # A story filed under a different header than its article's category must still be found.
        for _, _, arts in collect_sentences(HTML, CONV, ARTICLES):
            assert len(arts) == 3

    def test_source_links_dont_merge_into_sentences(self):
        html = ('<h2>AI &amp; TECH</h2><p>OpenAI released a new reasoning model on Monday for enterprise. '
                '<a href="x">OpenAI Ships New Reasoning Model For Enterprise Customers</a></p>'
                '<p>Intel stock jumped six percent after the Apple deal was confirmed.</p>')
        sents = [s for _, s, _ in collect_sentences(html, "", ARTICLES)]
        assert not any("Ships" in s for s in sents)
        assert any(s.startswith("Intel stock") for s in sents)
        assert not any(s.endswith(" .") for s in sents)

    def test_sports_sources_added(self):
        out = with_sports_sources(ARTICLES, {"Yankees": "Beat TB 5-2"}, None)
        assert out["NY Sports"][-1]["source"] == "ESPN"
        assert ARTICLES["NY Sports"] == [{"title": "Yankees beat Rays", "description": "5-2"}]  # not mutated


class TestCheck:

    def _run(self, verdicts):
        it = iter(verdicts)
        fake = MagicMock(side_effect=lambda **kw: {"ok": True, "supported": next(it), "reasoning": "r"})
        with patch("crew.tools.validate_claim_tool._run", fake):
            return check_grounding(HTML, CONV, ARTICLES), fake

    def test_counts_and_background(self, caplog):
        with caplog.at_level(logging.WARNING):
            r, fake = self._run(["analysis", "no", "unclear", "n/a", "n/a", "yes"])
        assert r["checked"] == fake.call_count
        assert (r["unsupported"], r["unclear"]) == (1, 1)
        assert r["not_claims"] == 2 and r["analysis"] == 1
        assert fake.call_args.kwargs["allow_analysis"] is True
        assert "[Grounding] UNSUPPORTED" in caplog.text
        assert "Donald Trump is the current President" in fake.call_args.kwargs["background"]
        from crew.grounding_check import GROUNDING_MODEL
        assert fake.call_args.kwargs["model"] == GROUNDING_MODEL

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("MIKECAST_GROUNDING_CHECK", "0")
        with patch("crew.tools.validate_claim_tool._run") as fake:
            assert check_grounding(HTML, CONV, ARTICLES)["checked"] == 0
        fake.assert_not_called()

    def test_never_raises(self):
        with patch("crew.grounding_check.collect_sentences", side_effect=RuntimeError("boom")):
            assert check_grounding(HTML, CONV, ARTICLES)["checked"] == 0

    def test_tool_failures_not_counted(self):
        with patch("crew.tools.validate_claim_tool._run", return_value={"ok": False, "supported": "unclear"}):
            r = check_grounding(HTML, CONV, ARTICLES)
        assert r["checked"] == 0 and r["unsupported"] == 0


class TestValidateClaimPrompt:

    def _messages(self, **kw):
        from crew.tools import validate_claim_tool
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content='{"supported": "n/a", "evidence": "", "reasoning": "banter"}'))]
        with patch("crew.model_compat.api_key_for_model", return_value="k"), \
             patch("litellm.completion", return_value=resp) as comp:
            out = validate_claim_tool._run(claim="Alright Jesse, take it away.", articles=[{"title": "t"}], **kw)
        return out, comp.call_args.kwargs["messages"]

    def test_na_passes_through_and_is_offered(self):
        out, msgs = self._messages()
        assert out["supported"] == "n/a"
        assert '"n/a"' in msgs[0]["content"]
        assert "ALSO KNOWN TRUE" not in msgs[1]["content"]
        assert '"analysis"' not in msgs[0]["content"]  # opt-in only: other callers count just "no"

    def test_analysis_opt_in_and_source_names(self):
        from crew.tools import validate_claim_tool
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content='{"supported": "analysis"}'))]
        with patch("crew.model_compat.api_key_for_model", return_value="k"), \
             patch("litellm.completion", return_value=resp) as comp:
            out = validate_claim_tool._run(claim="c", articles=[{"title": "t", "source": "TechCrunch"}],
                                           allow_analysis=True)
        msgs = comp.call_args.kwargs["messages"]
        assert out["supported"] == "analysis" and '"analysis"' in msgs[0]["content"]
        assert "(TechCrunch) t" in msgs[1]["content"]

    def test_model_override(self):
        from crew.tools import validate_claim_tool
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(content='{"supported": "yes"}'))]
        with patch("crew.model_compat.api_key_for_model", return_value="k"), \
             patch("litellm.completion", return_value=resp) as comp:
            validate_claim_tool._run(claim="c", articles=[{"title": "t"}], model="openai/x")
        assert comp.call_args.kwargs["model"] == "openai/x"

    def test_background_included(self):
        _, msgs = self._messages(background="- Donald Trump is the current President")
        assert "ALSO KNOWN TRUE" in msgs[1]["content"] and "Donald Trump" in msgs[1]["content"]


class TestBareNamesFixture:

    def test_loads_and_has_no_titles(self):
        from eval.run_eval import load_fixture
        fx = load_fixture("writer", "bare-names")
        text = " ".join(f"{a['title']} {a['description']}"
                        for arts in fx["input"]["top_articles"].values() for a in arts)
        # The point of the fixture: officeholders appear by bare name only.
        assert not re.search(r"\b(President|Vice President|Governor|Gov\.|Mayor|Chair|Pope)\s+[A-Z]", text)
        for name in ("Trump", "Vance", "Warsh", "Powell", "Biden", "Hochul", "Mamdani", "Leo XIV"):
            assert name in text

    def test_scorer_catches_planted_stale_titles(self):
        from eval.score import score_stale_titles
        r = score_stale_titles("<p>former President Trump met Fed Chair Powell.</p>",
                               "President Biden spoke.", "former Fed Chair Powell spoke.")
        assert r["count"] == 3
