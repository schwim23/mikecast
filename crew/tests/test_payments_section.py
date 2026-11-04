"""
Tests for the Payments & Commerce section (added 2026-11-04).

Pins the "new category" checklist (every place a category must be registered)
and the critic fix for HTML-escaped "&" section headers, which the new section
would otherwise inherit (AI & Tech / Business & Markets already had it).
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_config  # noqa: E402
import mc_plan  # noqa: E402
from crew import writing_crew  # noqa: E402
from crew.critic_crew import run_critic_pass  # noqa: E402
from mc_collect import select_top_articles  # noqa: E402

PAY = "Payments & Commerce"


class TestCategoryRegistration:

    def test_payments_is_a_category(self):
        assert PAY in mc_config.CATEGORIES
        assert any("Adyen" in q for q in mc_config.CATEGORIES[PAY])
        assert any("Stripe" in q for q in mc_config.CATEGORIES[PAY])

    def test_every_category_has_scorer_prompt(self):
        # A missing key means the category is scored with no instructions.
        assert set(mc_config.CATEGORIES) == set(mc_config.CATEGORY_SCORER_PROMPTS)

    def test_every_category_has_nyt_queries(self):
        assert set(mc_config.CATEGORIES) == set(mc_config.NYT_SEARCH_QUERIES)

    def test_planner_knows_every_category(self):
        assert set(mc_plan._CATEGORIES) == set(mc_config.CATEGORIES)
        assert f'"{PAY}"' in mc_plan._USER_PROMPT

    def test_every_category_has_a_section_header(self):
        for cat in mc_config.CATEGORIES:
            assert cat.upper() in writing_crew._SECTION_HEADERS

    def test_payments_feeds_map_to_payments(self):
        assert mc_config.PAYMENTS_RSS_FEEDS
        for _name, url, cat, n in mc_config.PAYMENTS_RSS_FEEDS:
            assert cat == PAY and url.startswith("https://") and n > 0


class TestSectionParsing:

    def test_payments_gets_its_own_h2(self):
        text = (
            "COMPANIES\nA retailer cut jobs.\n\n"
            "PAYMENTS & COMMERCE\nStripe launched an agent toolkit.\n\n"
            "NY SPORTS\nThe Knicks won."
        )
        html = writing_crew._text_to_html_sections(text)
        assert "PAYMENTS &amp; COMMERCE</h2>" in html
        companies, rest = html.split("PAYMENTS &amp; COMMERCE</h2>")
        assert "Stripe" not in companies
        assert "Stripe" in rest.split("NY SPORTS</h2>")[0]


class TestSelection:

    def test_payments_gets_minimum_slots(self):
        cats = {c: [{"title": f"{c}-{i}", "score": 90 - i} for i in range(20)]
                for c in mc_config.CATEGORIES}
        cats[PAY] = [{"title": f"pay-{i}", "score": 50} for i in range(4)]
        sel = select_top_articles(cats, total=mc_config.TOP_ARTICLES_TOTAL)
        assert len(sel[PAY]) >= 3


class TestWriterPrompts:

    def _args(self):
        return ({PAY: [{"title": "x", "url": "https://e.com", "description": "d", "source": "s"}]},
                [], [], {}, [])

    def test_all_three_writers_include_payments(self):
        for fn in (writing_crew._html_task, writing_crew._single_voice_task,
                   writing_crew._conversational_task):
            desc, _ = fn(*self._args())
            assert "PAYMENTS & COMMERCE" in desc

    def test_podcast_length_targets(self):
        for fn in (writing_crew._single_voice_task, writing_crew._conversational_task):
            desc, expected = fn(*self._args())
            assert "1150–1250 words" in desc and "1150–1250 words" in expected

    def test_mike_voices_payments_in_3host(self):
        desc, _ = writing_crew._conversational_task(*self._args())
        assert "[MIKE] PAYMENTS & COMMERCE" in desc


class TestCriticEscapedAmpersand:
    """The page renders 'PAYMENTS &amp; COMMERCE'; the scorer echoes 'PAYMENTS & COMMERCE'.
    Before the fix the patch regex never matched any '&' section."""

    _HTML = (
        '<h2 style="color:#4fc3f7">COMPANIES</h2><p>Retail story.</p>'
        '<h2 style="color:#4fc3f7">PAYMENTS &amp; COMMERCE</h2><p>Thin.</p>'
        '<h2 style="color:#4fc3f7">NY SPORTS</h2><p>Knicks won.</p>'
        '<div style="text-align:center">footer</div>'
    )

    def test_weak_payments_section_is_patched(self):
        critique = {"category_scores": {"PAYMENTS & COMMERCE": 4}, "issues": {}, "overall_passed": False}
        articles = {PAY: [{"title": "Stripe toolkit", "url": "https://e.com", "description": "d"}]}
        with patch("crew.critic_crew._run_scorer", return_value=(critique, {})), \
             patch("crew.critic_crew._run_patcher", return_value="<p>Patched payments.</p>"), \
             patch("crew.critic_crew.fact_check_ny_sports"):
            html, _, _, metrics = run_critic_pass(self._HTML, "", "", articles, [], [])
        assert "Patched payments." in html and "Thin." not in html
        assert "Retail story." in html and "Knicks won." in html
        assert metrics["patched_categories"] == ["PAYMENTS & COMMERCE"]
