"""
Unit tests for mc_video pure helpers — caption chunking + cold-open selection.

These are the two bits of reel logic with real bug-surface that don't need the
network, ElevenLabs, or moviepy (moviepy is imported lazily inside the render
functions, so importing mc_video here is cheap). The render/publish paths are
exercised by the manual live-run step, not unit tests.
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from mc_video import (  # noqa: E402
    REEL_MAX_SEGMENTS,
    VIDEO_H,
    VIDEO_W,
    _select_cold_open,
    _truncate_to_words,
    _truncate_words,
    chunk_caption,
    render_slate_frame,
)


class TestChunkCaption:

    def test_empty_and_whitespace_yield_no_cues(self):
        assert chunk_caption("") == []
        assert chunk_caption("   \n  ") == []

    def test_short_text_is_a_single_cue(self):
        assert chunk_caption("Good morning everyone") == ["Good morning everyone"]

    def test_no_cue_exceeds_max_words(self):
        text = " ".join(f"word{i}" for i in range(50))
        for max_words in (5, 8, 12):
            cues = chunk_caption(text, max_words=max_words)
            assert cues, "expected at least one cue"
            assert all(len(c.split()) <= max_words for c in cues)

    def test_all_words_preserved_in_order(self):
        # Captions must never drop or reorder words vs. the spoken audio.
        text = "The AI industry is getting more concentrated by the day and that matters."
        cues = chunk_caption(text, max_words=4)
        assert " ".join(cues).split() == text.split()

    def test_breaks_on_sentence_boundary_when_long_enough(self):
        # After a 4+ word sentence, a cue should end at the period rather than
        # running the next sentence into the same cue.
        cues = chunk_caption("One two three four. Five six seven.", max_words=8)
        assert cues[0] == "One two three four."

    def test_short_sentence_does_not_force_a_break(self):
        # Fewer than 4 words before the period → keep accumulating (avoids
        # one- or two-word flicker cues).
        cues = chunk_caption("Hi there. Now the rest keeps going here", max_words=8)
        assert cues[0].startswith("Hi there. Now")


class TestSelectColdOpen:

    def _segs(self, n, words_each=10):
        body = " ".join(["word"] * words_each)
        speakers = ["MIKE", "ELIZABETH", "JESSE"]
        return [(speakers[i % 3], body) for i in range(n)]

    def test_always_returns_at_least_one_segment(self):
        # Even a single huge segment that blows the budget must yield one.
        big = [("MIKE", " ".join(["word"] * 5000))]
        assert _select_cold_open(big, target_secs=75) == big

    def test_respects_max_segments_cap(self):
        segs = self._segs(20, words_each=1)  # tiny words → budget never trips
        chosen = _select_cold_open(segs, target_secs=1000, max_segments=4)
        assert len(chosen) == 4

    def test_stops_near_word_budget(self):
        # target 4s * 2.5 words/s = 10-word budget; 5-word segments → ~2-3 segs,
        # never the whole list.
        segs = self._segs(20, words_each=5)
        chosen = _select_cold_open(segs, target_secs=4, max_segments=REEL_MAX_SEGMENTS)
        assert 1 <= len(chosen) < len(segs)

    def test_returns_whole_leading_segments_unchanged(self):
        segs = self._segs(10, words_each=6)
        chosen = _select_cold_open(segs, target_secs=30)
        assert chosen == segs[: len(chosen)]

    def test_truncates_overflowing_segment_instead_of_dropping(self):
        # A short intro followed by a long segment must NOT collapse to the intro
        # only — the long segment is truncated to fit, not dropped. (This is the
        # 15s-intro-only regression the SOC-Reel-Audio fix addresses.)
        intro = ("MIKE", "Good morning. Welcome back.")           # 4 words
        long_body = " ".join(f"Sentence number {i} here." for i in range(60))  # ~240 words
        segs = [intro, ("ELIZABETH", long_body)]
        chosen = _select_cold_open(segs, target_secs=20)          # 20 * 2.5 = 50-word budget
        assert len(chosen) == 2                                    # long segment kept, not dropped
        assert chosen[0] == intro
        trimmed = chosen[1][1]
        assert len(trimmed.split()) < len(long_body.split())      # it was truncated
        assert trimmed.rstrip().endswith(".")                     # cut on a sentence boundary


class TestTruncateToWords:

    def test_keeps_whole_sentences_within_budget(self):
        text = "One two three. Four five six. Seven eight nine."
        out = _truncate_to_words(text, 6)          # ~2 sentences fit
        assert out == "One two three. Four five six."

    def test_always_returns_at_least_the_first_sentence(self):
        text = "This first sentence is already longer than the tiny budget. Second."
        out = _truncate_to_words(text, 3)
        assert out.startswith("This first sentence")

    def test_no_punctuation_returns_the_whole_text(self):
        assert _truncate_to_words("just some words no periods", 2) == "just some words no periods"


class TestTruncateWords:

    def test_short_text_passes_through_unchanged(self):
        assert _truncate_words("OpenAI ships a new model", 60) == "OpenAI ships a new model"

    def test_long_text_truncates_at_word_boundary_with_ellipsis(self):
        text = "OpenAI announces new frontier model with major reasoning gains"
        out = _truncate_words(text, 60)
        assert out.endswith("…")
        assert not out.rstrip("…").endswith(" ")
        # never splits a word — everything before the ellipsis is whole words from the source
        assert text.startswith(out.rstrip("…").rstrip())

    def test_empty_text_returns_empty(self):
        assert _truncate_words("", 60) == ""


class TestRenderSlateFrame:
    """The 'Today's Top Stories' opening slate — headline text separate from
    the reel's live captions, so the day's highlights survive muted/at-a-glance
    viewing the way the static card used to before reels replaced it."""

    def test_returns_full_size_rgb_frame(self):
        headlines = ["Story one", "Story two", "Story three"]
        frame = render_slate_frame(bg=_solid_bg(), logo=None, headlines=headlines, date_str="Jan 1, 2026")
        assert frame.shape == (VIDEO_H, VIDEO_W, 3)

    def test_handles_no_headlines_without_crashing(self):
        frame = render_slate_frame(bg=_solid_bg(), logo=None, headlines=[], date_str="Jan 1, 2026")
        assert frame.shape == (VIDEO_H, VIDEO_W, 3)

    def test_handles_a_long_headline_without_crashing(self):
        long_headline = "A" * 200
        frame = render_slate_frame(bg=_solid_bg(), logo=None, headlines=[long_headline], date_str="Jan 1, 2026")
        assert frame.shape == (VIDEO_H, VIDEO_W, 3)


def _solid_bg():
    from PIL import Image
    return Image.new("RGB", (VIDEO_W, VIDEO_H), (10, 14, 32))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
