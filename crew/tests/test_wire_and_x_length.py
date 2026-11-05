"""
Regression tests (2026-11-05):

  * X 403s — _weighted_len counted every character as 1, but X weighs emoji, "…"
    and most non-Latin symbols as 2. On long-copy days the tweet was really 281-283
    and X rejected it with a bare 403 "You are not permitted to perform this action".
  * Reuters/AP — feeds.reuters.com / feeds.apnews.com stopped resolving (0 articles
    every run since at least 2026-10-01); replaced with Google News site: searches.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_collect  # noqa: E402
import mc_config  # noqa: E402
import mc_social  # noqa: E402

# The exact x_text that was rejected on 2026-11-05 (true X length 283).
_FAILED_1105 = (
    "Tim Cook is stepping down as Apple CEO — John Ternus takes over. Wall Street rallies as "
    "Democrats sweep Congress, data center stocks tumble. Plus OpenAI poaches an Anthropic exec "
    "amid new AI scrutiny. 🏀Knicks top Bulls, Luka heats up for LA."
)
_FAILED_1101 = (
    "OpenAI trims its fundraising target to $20B, Intel stock craters 29%, and the Fed holds rates "
    "steady despite sticky inflation. Plus Meta settles teen addiction lawsuits & Knicks eye a new "
    "Bridges trade partner. #AI #NYSports"
)


class TestXWeightedLength:

    def test_emoji_and_ellipsis_weigh_two(self):
        assert mc_social._weighted_len("a") == 1
        assert mc_social._weighted_len("…") == 2
        assert mc_social._weighted_len("🏀") == 2
        assert mc_social._weighted_len("— ’") == 3   # em dash / curly quote are weight 1

    def test_urls_weigh_23(self):
        assert mc_social._weighted_len("https://mikecast.io/?date=2026-11-05") == 23

    def test_template_header_counts_emoji(self):
        # "🎙️" is 2 code points (mic + VS16); counting both as 2 over-counts by 2 — safe.
        assert mc_social._weighted_len("🎙️") >= 2

    def test_previously_rejected_tweets_now_fit(self):
        for copy, d in ((_FAILED_1105, "2026-11-05"), (_FAILED_1101, "2026-11-01")):
            text = mc_social._fit_x(copy, mc_social.deep_link(d), "November 05, 2026")
            assert mc_social._weighted_len(text) <= mc_social.X_MAX
            assert text.startswith("🎙️ MikeCast Daily") and "https://mikecast.io/" in text


class TestWireSources:

    def test_dead_feed_hosts_are_gone(self):
        src = open(os.path.join(_ROOT, "mc_config.py")).read()
        assert "https://feeds.reuters.com" not in src and "https://feeds.apnews.com" not in src

    def test_wire_queries_cover_reuters_and_ap(self):
        labels = {label for label, *_ in mc_config.WIRE_GNEWS_QUERIES}
        assert labels == {"Reuters", "Associated Press"}
        for _label, q, cat, n in mc_config.WIRE_GNEWS_QUERIES:
            assert q.startswith("site:") and cat in mc_config.CATEGORIES and n > 0

    def test_fetch_groups_by_category(self):
        fake = [{"title": "t", "url": "https://news.google.com/rss/articles/x", "source": "Reuters"}]
        with patch.object(mc_collect, "search_news_web", return_value=fake) as m, \
             patch.object(mc_collect.time, "sleep"):
            out = mc_collect.fetch_wire_via_google_news()
        assert m.call_count == len(mc_config.WIRE_GNEWS_QUERIES)
        assert set(out) == {cat for *_, cat, _n in mc_config.WIRE_GNEWS_QUERIES}

    def test_ap_news_label_is_tier_1(self):
        assert mc_config.SOURCE_TIERS.get("AP News") == 1
