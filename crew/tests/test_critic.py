"""
Regression tests for crew/critic_crew.py.

Pins the NEVER_PATCH invariant and the fact-check observability path that
replaced the old NY-Sports-patches-via-Fact-Checker pattern.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crew.critic_crew import (  # noqa: E402
    _NEVER_PATCH_NORMALIZED,
    _WEAK_THRESHOLD,
    _extract_ny_sports_text,
    _split_sentences,
    fact_check_ny_sports,
    run_critic_pass,
)


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

class TestInvariants:

    def test_ny_sports_in_never_patch(self):
        """If this ever changes, sports hallucinations come back."""
        assert "ny sports" in _NEVER_PATCH_NORMALIZED

    def test_weak_threshold_is_7(self):
        # Matches mc_critic legacy + the score reasoning in the patcher prompt.
        assert _WEAK_THRESHOLD == 7


# ---------------------------------------------------------------------------
# NY Sports section extraction
# ---------------------------------------------------------------------------

_HTML_FRAGMENT = """
<h2 style="color:#4fc3f7">AI &amp; TECH</h2>
<p>OpenAI announced a new model.</p>
<h2 style="color:#4fc3f7">NY SPORTS</h2>
<p>The Yankees lost 7-6 to the Mets in the Subway Series finale.</p>
<p>Jalen Brunson scored 38 points as the Knicks beat the 76ers 144-114.</p>
<h2 style="color:#ffb74d">KEY TRENDS &amp; INSIGHTS</h2>
<p>Some trends.</p>
"""


class TestExtractNYSports:

    def test_extracts_inner_text_only(self):
        text = _extract_ny_sports_text(_HTML_FRAGMENT)
        assert "Yankees lost 7-6" in text
        assert "Brunson scored 38" in text
        # Other sections must not leak in
        assert "OpenAI" not in text
        assert "trends" not in text.lower()

    def test_missing_section_returns_empty(self):
        assert _extract_ny_sports_text("<h2>AI &amp; TECH</h2><p>x</p>") == ""


# ---------------------------------------------------------------------------
# Sentence splitting — needs substance for fact-checking
# ---------------------------------------------------------------------------

class TestSplitSentences:

    def test_keeps_substantive_sentences(self):
        text = (
            "The Yankees lost 7-6 to the Mets at Citi Field. "
            "Jalen Brunson scored 38 points last night. "
            "Sports."
        )
        sentences = _split_sentences(text)
        assert len(sentences) == 2
        assert any("Yankees" in s for s in sentences)
        assert any("Brunson" in s for s in sentences)
        # Trivial "Sports." gets dropped (too short, no digits/names)
        assert not any(s.strip() == "Sports" for s in sentences)

    def test_drops_speaker_tags(self):
        text = "[JESSE] The Yankees lost 7-6 to the Mets at Citi Field."
        sentences = _split_sentences(text)
        assert sentences
        assert not any("[JESSE]" in s for s in sentences)

    def test_filters_short_fragments(self):
        # All < 8 words → all dropped
        assert _split_sentences("Hi there. Sports today.") == []


# ---------------------------------------------------------------------------
# Fact-check pass — short-circuits when no NY Sports articles
# ---------------------------------------------------------------------------

class TestFactCheckShortCircuits:

    def test_no_ny_sports_articles_skips(self):
        # validate_claim_tool must NOT be called — assert by patching it
        with patch("crew.critic_crew.validate_claim_tool") as tool_mock:
            unsupported = fact_check_ny_sports(_HTML_FRAGMENT, "", {"NY Sports": []})
            assert unsupported == 0
            tool_mock._run.assert_not_called()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
