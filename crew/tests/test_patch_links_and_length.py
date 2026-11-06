"""
Regression tests for the 2026-11-06 episode:

  1. Missing source links — the critic patcher rewrote Business & Markets and Companies
     without ever seeing the article URLs, so both email sections shipped with no links.
  2. Short podcast — 1004/1105 words vs the 1150-1250 target (5.5 min); the prompt said
     "not a word longer. Brevity matters." and nothing checked the result.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_critic  # noqa: E402
from crew import critic_crew, writing_crew  # noqa: E402

ARTS = [
    {"title": "Fed holds rates steady", "description": "The Fed held.", "source": "Spectrum News",
     "url": "https://news.google.com/rss/articles/AAA"},
    {"title": "Jobs report Friday", "description": "Preview.", "source": "CNBC",
     "url": "https://www.cnbc.com/2026/11/05/october-jobs-report-preview.html"},
]


# ---------------------------------------------------------------------------
# 1. Patch links
# ---------------------------------------------------------------------------

class TestPatchLinks:

    def test_article_lines_include_source_and_url(self):
        lines = mc_critic.patch_article_lines(ARTS)
        assert "Source: Spectrum News" in lines[0] and "URL: https://news.google.com/rss/articles/AAA" in lines[0]

    def test_both_patch_prompts_carry_urls_and_link_rule(self):
        seen = {}
        with patch.object(critic_crew, "make_section_patcher", return_value=None), \
             patch.object(critic_crew, "_kickoff_single_task",
                          side_effect=lambda a, d, e: seen.setdefault("crew", d) and ""):
            critic_crew._run_patcher("Business & Markets", ARTS, "thin")
        assert ARTS[1]["url"] in seen["crew"] and "source link" in seen["crew"]

        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "x"})()})()]

        class _Client:
            def __init__(self, *a, **k):
                self.chat = type("Ch", (), {"completions": self})()

            def create(self, **kw):
                seen["legacy"] = kw["messages"][0]["content"]
                return _Resp()

        with patch("openai.OpenAI", _Client):
            mc_critic._regenerate_html_section("Business & Markets", ARTS, "thin")
        assert ARTS[0]["url"] in seen["legacy"] and "source link" in seen["legacy"]

    def test_valid_links_kept_and_markdown_converted(self):
        frag = (f'<h3>Fed</h3><p>Held. <a href="{ARTS[0]["url"]}" style="x">Spectrum News</a></p>'
                f'<p>Jobs. [CNBC]({ARTS[1]["url"]})</p>')
        out = mc_critic.enforce_patch_links(frag, ARTS)
        assert out.count("<a ") == 2 and "[CNBC]" not in out

    def test_invented_url_is_unwrapped(self):
        frag = (f'<p>A <a href="{ARTS[0]["url"]}">Spectrum</a></p>'
                '<p>B <a href="https://www.reuters.com/made-up">Reuters</a></p>')
        out = mc_critic.enforce_patch_links(frag, ARTS)
        assert "made-up" not in out and "Reuters" in out and out.count("<a ") == 1

    def test_escaped_ampersand_in_href_still_matches(self):
        arts = [{"url": "https://x.com/a?b=1&c=2"}]
        assert mc_critic.enforce_patch_links('<p><a href="https://x.com/a?b=1&amp;c=2">X</a></p>', arts)

    def test_unlinked_patch_is_rejected(self):
        assert mc_critic.enforce_patch_links("<h3>Fed</h3><p>No links here.</p>", ARTS) is None
        assert mc_critic.enforce_patch_links("", ARTS) is None

    def _run_pass(self, patch_html: str) -> tuple[str, list]:
        html = ('<h2>BUSINESS &amp; MARKETS</h2><p>Orig <a href="u">S</a></p>\n'
                '<h2>NY SPORTS</h2><p>x</p>')
        critique = {"category_scores": {"BUSINESS & MARKETS": 5}, "issues": {}, "overall_passed": False}
        with patch.object(critic_crew, "_run_scorer", return_value=(critique, {})), \
             patch.object(critic_crew, "_run_patcher", return_value=patch_html), \
             patch.object(critic_crew, "fact_check_ny_sports"):
            out, _, _, m = critic_crew.run_critic_pass(
                html, "s", "c", {"Business & Markets": ARTS}, [], [])
        return out, m["patched_categories"]

    def test_crew_critic_keeps_original_when_patch_has_no_links(self):
        out, patched = self._run_pass("<p>Rewritten, no links</p>")
        assert "Orig" in out and "Rewritten" not in out and patched == []

    def test_crew_critic_applies_linked_patch(self):
        out, patched = self._run_pass(f'<p>Rewritten <a href="{ARTS[0]["url"]}">Spectrum</a></p>')
        assert "Rewritten" in out and "Orig" not in out and patched == ["BUSINESS & MARKETS"]


# ---------------------------------------------------------------------------
# 2. Podcast length
# ---------------------------------------------------------------------------

def _script(n_words: int, tags=True) -> str:
    body = " ".join(["word"] * (n_words // 3))
    if not tags:
        return " ".join(["word"] * n_words)
    rest = n_words - 2 * (n_words // 3)
    return f"[MIKE]\n{body}\n\n[ELIZABETH]\n{body}\n\n[JESSE]\n{' '.join(['word'] * rest)}"


class TestPodcastLength:

    def test_prompts_no_longer_say_brevity(self):
        for fn in (writing_crew._single_voice_task, writing_crew._conversational_task):
            desc, _ = fn({"AI & Tech": []}, [], [], None)
            assert "not a word longer" not in desc and "Brevity matters" not in desc
            assert "FLOOR" in desc and "at least 160" in desc

    def test_spoken_word_count_ignores_tags(self):
        assert writing_crew.spoken_word_count("[MIKE]\nhello there\n[JESSE]\nhi") == 3

    def test_long_enough_draft_is_not_retried(self):
        with patch.object(writing_crew, "_kickoff_single_task") as k:
            d = _script(1150)
            assert writing_crew._expand_if_short(None, "t", d, "conv", True) == d
            k.assert_not_called()

    def test_short_draft_is_expanded(self):
        with patch.object(writing_crew, "_kickoff_single_task", return_value=_script(1200)) as k:
            out = writing_crew._expand_if_short(None, "TASK", _script(1004), "conv", True)
        assert writing_crew.spoken_word_count(out) == 1200
        prompt = k.call_args[0][1]
        assert prompt.startswith("TASK") and "NY SPORTS" in prompt and "1004 words" in prompt

    def test_expansion_rejected_if_runaway_shorter_or_loses_a_host(self):
        short = _script(1004)
        for bad in (_script(1600), _script(900), _script(1200).replace("[JESSE]", "[MIKE]"), ""):
            with patch.object(writing_crew, "_kickoff_single_task", return_value=bad):
                assert writing_crew._expand_if_short(None, "t", short, "conv", True) == short

    def test_single_voice_needs_no_tags(self):
        with patch.object(writing_crew, "_kickoff_single_task", return_value=_script(1200, tags=False)):
            out = writing_crew._expand_if_short(None, "t", _script(1000, tags=False), "single", False)
        assert writing_crew.spoken_word_count(out) == 1200

    def test_run_writing_applies_backstop(self):
        calls = []

        def fake(agent, desc, exp):
            calls.append(desc)
            if "YOUR DRAFT" in desc:
                return _script(1200, tags="[ELIZABETH]" in desc)
            if "3-host" in desc:
                return _script(1000)
            if "single-host" in desc:
                return _script(1000, tags=False)
            return "EXECUTIVE SUMMARY\nx"

        with patch.object(writing_crew, "_kickoff_single_task", side_effect=fake), \
             patch.object(writing_crew, "_filter_trending_to_articles", return_value=[]), \
             patch.object(writing_crew, "make_html_writer"), \
             patch.object(writing_crew, "make_single_voice_writer"), \
             patch.object(writing_crew, "make_conversational_writer"):
            _, single, conv = writing_crew.run_writing({"AI & Tech": []}, [], [])
        assert writing_crew.spoken_word_count(single) == 1200
        assert writing_crew.spoken_word_count(conv) == 1200
        assert sum("YOUR DRAFT" in c for c in calls) == 2
