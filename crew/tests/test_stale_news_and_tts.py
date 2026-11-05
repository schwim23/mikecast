"""
Regression tests for the 2026-11-05 episode:

  1. Garbled audio — ElevenLabs hallucinated ~7s of babble for the 14-char line
     "[MIKE] End of an era.". mc_audio now sends previous/next text and length-checks
     every clip (retry, then drop short throwaway lines).
  2. Stale news — ABC's April 20 "Tim Cook is stepping down" ran as breaking news.
     Google News items were undated (parser looked for <pubdate>, the tag is <pubDate>)
     and all shared the publisher's homepage URL; the writers and scorer also didn't
     know Ternus was already Apple's CEO.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_audio  # noqa: E402
import mc_collect  # noqa: E402
import mc_facts  # noqa: E402


# ---------------------------------------------------------------------------
# 1. ElevenLabs short-segment guard
# ---------------------------------------------------------------------------

_SCRIPT = (
    "[ELIZABETH]\nABC News reports something big happened at a very large company today, "
    "and analysts are watching closely.\n\n"
    "[MIKE]\nEnd of an era.\n\n"
    "[ELIZABETH]\nElsewhere, a streaming service raised the price of its ad tier by two dollars.\n"
)


def _run_tts(durations_by_text: dict[str, list[float]], tmp_path: Path):
    """Run generate_elevenlabs_audio with ElevenLabs + ffmpeg mocked.

    durations_by_text maps a segment's text to the clip durations successive calls return.
    Returns (ok, posted payloads, blobs handed to concat)."""
    posted: list[dict] = []
    calls: dict[str, int] = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        posted.append(json)
        n = calls.get(json["text"], 0)
        calls[json["text"]] = n + 1
        resp = MagicMock()
        resp.content = f"{json['text']}#{n}".encode()
        resp.raise_for_status = lambda: None
        return resp

    def fake_duration(blob: bytes) -> float:
        text, n = blob.decode().rsplit("#", 1)
        seq = durations_by_text.get(text, [len(text) * 0.065])
        return seq[min(int(n), len(seq) - 1)]

    concat_input: list[bytes] = []

    def fake_concat(segments, output_path):
        concat_input.extend(segments)
        return True

    with patch.object(mc_audio, "ELEVENLABS_API_KEY", "k"), \
         patch.object(mc_audio, "ELEVENLABS_VOICE_MIKE", "vm"), \
         patch.object(mc_audio, "ELEVENLABS_VOICE_ELIZABETH", "ve"), \
         patch.object(mc_audio, "ELEVENLABS_VOICE_JESSE", "vj"), \
         patch.object(mc_audio.requests, "post", side_effect=fake_post), \
         patch.object(mc_audio, "_mp3_blob_duration_secs", side_effect=fake_duration), \
         patch.object(mc_audio, "_concat_mp3_segments", side_effect=fake_concat), \
         patch.object(mc_audio, "_normalize_loudness"), \
         patch.object(mc_audio, "_stamp_mp3_duration"), \
         patch.object(mc_audio.time, "sleep"):
        out = tmp_path / "ep.mp3"
        out.write_bytes(b"x")  # stat() in the success log line
        ok = mc_audio.generate_elevenlabs_audio(_SCRIPT, out)
    return ok, posted, concat_input


class TestElevenLabsGuard:

    def test_sends_neighbouring_text_as_context(self, tmp_path):
        _, posted, _ = _run_tts({}, tmp_path)
        mike = next(p for p in posted if p["text"] == "End of an era.")
        assert "something big happened" in mike["previous_text"]
        assert "streaming service" in mike["next_text"]
        assert "previous_text" not in posted[0]  # first segment has nothing before it

    def test_hallucinated_short_line_is_retried_then_dropped(self, tmp_path):
        ok, posted, blobs = _run_tts({"End of an era.": [7.0, 6.5]}, tmp_path)
        assert ok
        assert sum(p["text"] == "End of an era." for p in posted) == 2   # original + 1 retry
        assert not any(b.startswith(b"End of an era.") for b in blobs)   # dropped
        assert len(blobs) == 2                                           # both real segments kept

    def test_retry_that_comes_back_clean_is_kept(self, tmp_path):
        ok, _, blobs = _run_tts({"End of an era.": [7.0, 0.8]}, tmp_path)
        assert ok and b"End of an era.#1" in blobs

    def test_normal_clips_are_not_retried(self, tmp_path):
        _, posted, blobs = _run_tts({}, tmp_path)
        assert len(posted) == 3 and len(blobs) == 3

    def test_long_suspicious_segment_is_kept_not_dropped(self, tmp_path):
        long_line = ("Elsewhere, a streaming service raised the price of its ad tier by two "
                     "dollars.")
        assert len(long_line) <= mc_audio._TTS_DROPPABLE_MAX_CHARS  # sanity: this one IS droppable
        real = ("ABC News reports something big happened at a very large company today, and "
                "analysts are watching closely.")
        assert len(real) > mc_audio._TTS_DROPPABLE_MAX_CHARS
        ok, posted, blobs = _run_tts({real: [60.0, 60.0]}, tmp_path)
        assert ok and sum(p["text"] == real for p in posted) == 2   # retried once
        assert any(b.startswith(real.encode()) for b in blobs)      # but never dropped

    def test_limit_catches_the_real_incident(self):
        with patch.object(mc_audio, "_mp3_blob_duration_secs", return_value=7.0):
            assert mc_audio._tts_duration_suspicious("End of an era.", b"")[0]
        # 2026-11-05 segment 2: 663 chars, ~43s — must pass
        with patch.object(mc_audio, "_mp3_blob_duration_secs", return_value=43.0):
            assert not mc_audio._tts_duration_suspicious("x" * 663, b"")[0]


# ---------------------------------------------------------------------------
# 2a. Google News parsing
# ---------------------------------------------------------------------------

_GNEWS_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
  <title>Apple CEO Tim Cook is stepping down - ABC News</title>
  <link>https://news.google.com/rss/articles/CBMiAAA?oc=5</link>
  <pubDate>Mon, 20 Apr 2026 21:00:00 GMT</pubDate>
  <description>Apple CEO Tim Cook is stepping down</description>
  <source url="https://abcnews.com">ABC News</source>
</item>
<item>
  <title>FTC opens probe into AI safety - ABC News</title>
  <link>https://news.google.com/rss/articles/CBMiBBB?oc=5</link>
  <pubDate>Wed, 04 Nov 2026 11:00:00 GMT</pubDate>
  <description>FTC opens probe</description>
  <source url="https://abcnews.com">ABC News</source>
</item>
</channel></rss>"""


class TestGoogleNewsParsing:

    def _parse(self):
        resp = MagicMock(content=_GNEWS_RSS)
        with patch.object(mc_collect, "_safe_request", return_value=resp):
            return mc_collect.search_news_web("apple", max_results=5)

    def test_pubdate_is_parsed(self):
        arts = self._parse()
        assert arts[0]["published"] == "Mon, 20 Apr 2026 21:00:00 GMT"

    def test_each_article_gets_its_own_url(self):
        arts = self._parse()
        assert arts[0]["url"] != arts[1]["url"]
        assert all(a["url"].startswith("https://news.google.com/rss/articles/") for a in arts)
        assert all(a["source_url"] == "https://abcnews.com" for a in arts)

    def test_stale_filter_now_drops_the_april_story(self):
        arts = self._parse()
        kept = mc_collect.filter_stale_articles({"Companies": arts}, max_age_days=3)["Companies"]
        assert not any("Tim Cook" in a["title"] for a in kept)


# ---------------------------------------------------------------------------
# 2b. Company CEO facts
# ---------------------------------------------------------------------------

class TestCompanyFacts:

    def test_resolve_picks_current_and_filters_stopgap_predecessor(self):
        rows = [
            {"co": "Q312", "name": "Tim Cook", "start": "2011-08-24", "end": "2026-08-31"},
            {"co": "Q312", "name": "John Ternus", "start": "2026-09-01", "end": None},
            {"co": "Q21708200", "name": "Sam Altman", "start": "2023-11-22", "end": None},
            {"co": "Q21708200", "name": "Emmett Shear", "start": "2023-11-19", "end": "2023-11-21"},
        ]
        out = mc_facts._resolve_companies(rows, "2026-11-05")
        cur, prev = out["Q312"]
        assert [h.name for h in cur] == ["John Ternus"] and prev.name == "Tim Cook"
        assert out["Q21708200"][1] is None  # 2-day interim CEO isn't worth naming

    def test_unlabelled_ceo_is_ignored(self):
        rows = [{"co": "Q312", "name": "Q106028933", "start": "2026-09-01", "end": None}]
        assert "Q312" not in mc_facts._resolve_companies(rows, "2026-11-05")

    def test_prompt_block_says_ternus_is_ceo_and_has_old_news_rule(self):
        block = mc_facts.prompt_block()  # offline -> COMPANY_FALLBACK
        assert "John Ternus is the current CEO of Apple" in block
        assert "Tim Cook is the PREVIOUS Apple CEO" in block
        assert "OLD-NEWS RULE" in block

    def test_fallback_covers_every_company(self):
        assert {q for q, _ in mc_facts.COMPANIES} == set(mc_facts.COMPANY_FALLBACK)

    def test_company_titles_are_never_rewritten(self):
        # fix_stale_titles only knows OFFICES; "former Apple CEO Tim Cook" must survive.
        text = "Former Apple CEO Tim Cook spoke at the event."
        assert mc_facts.fix_stale_titles(text)[0] == text


class TestResurfacedLeadershipFilter:
    """Deterministic backstop — the prompt rule alone still let 2 of 3 writers run the story."""

    INCIDENT = {"title": "[Updated] Apple CEO Tim Cook is stepping down, John Ternus set to replace him - ABC News",
                "description": "Apple CEO Tim Cook is stepping down, John Ternus set to replace him"}

    def test_drops_the_incident_article(self):
        from datetime import date
        assert mc_facts.resurfaced_leadership_reason(self.INCIDENT, today=date(2026, 11, 5))

    def test_keeps_new_stories_and_past_tense_mentions(self):
        from datetime import date
        for title in (
            "Can new Apple CEO John Ternus fix the tech giant's AI woes?",
            "Apple Q4 earnings: CEO John Ternus, who succeeded Tim Cook in September, addresses demand",
            "Tim Cook, who stepped down as CEO in August, joins a board as Ternus settles in",
            "Tim Cook is stepping down from the Nike board",   # no successor named
        ):
            assert mc_facts.resurfaced_leadership_reason({"title": title}, today=date(2026, 11, 5)) is None, title

    def test_recent_handover_is_not_filtered(self):
        from datetime import date
        assert mc_facts.resurfaced_leadership_reason(self.INCIDENT, today=date(2026, 9, 5)) is None

    def test_filter_runs_on_categorised(self):
        out = mc_facts.filter_resurfaced_leadership_news(
            {"Companies": [self.INCIDENT, {"title": "Apple ships a new iPad", "description": ""}]})
        assert [a["title"] for a in out["Companies"]] == ["Apple ships a new iPad"]

    def test_wired_into_both_pipelines(self):
        root = Path(_ROOT)
        assert "filter_resurfaced_leadership_news(deduped)" in (root / "crew" / "research_crew.py").read_text()
        assert "filter_resurfaced_leadership_news(deduped)" in (root / "mikecast_briefing.py").read_text()
