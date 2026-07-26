"""
Regression tests for mc_social._graph_post — the Instagram Graph retry wrapper.

A transient Meta 500 (`is_transient: true`, e.g. code 2 "An unexpected error has
occurred. Please retry your request later.") once dropped the entire daily IG post
because the create/publish call ran a single attempt with no retry. These tests
lock in: retry-then-succeed on transient, no-retry on permanent, bounded attempts.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_social  # noqa: E402


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.text = str(body)
    r.json = lambda: body
    return r


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    # Zero the sleep delays so tests are instant.
    monkeypatch.setattr(mc_social, "_IG_RETRY_DELAYS", (0, 0, 0))


class TestGraphPostRetry:

    def test_retries_transient_then_succeeds(self):
        seq = [
            _resp(500, {"error": {"is_transient": True, "code": 2}}),
            _resp(500, {"error": {"is_transient": True, "code": 2}}),
            _resp(200, {"id": "OK123"}),
        ]
        with patch("mc_social.requests.post", side_effect=seq) as p:
            resp = mc_social._graph_post("http://x", {}, label="test")
        assert resp.status_code == 200
        assert resp.json()["id"] == "OK123"
        assert p.call_count == 3

    def test_permanent_error_is_not_retried(self):
        # 400 with a normal (non-transient) OAuth error → return immediately.
        with patch("mc_social.requests.post",
                   side_effect=[_resp(400, {"error": {"code": 190, "message": "bad token"}})]) as p:
            resp = mc_social._graph_post("http://x", {}, label="test")
        assert resp.status_code == 400
        assert p.call_count == 1

    def test_all_transient_exhausts_and_returns_last(self):
        with patch("mc_social.requests.post",
                   side_effect=[_resp(500, {"error": {"is_transient": True}})] * 4) as p:
            resp = mc_social._graph_post("http://x", {}, label="test")
        assert resp.status_code == 500
        assert p.call_count == 4  # 1 initial + 3 retries

    def test_returns_none_when_every_attempt_raises(self):
        with patch("mc_social.requests.post", side_effect=RuntimeError("boom")) as p:
            resp = mc_social._graph_post("http://x", {}, label="test")
        assert resp is None
        assert p.call_count == 4


class TestIgTransientClassifier:

    def test_5xx_is_transient(self):
        assert mc_social._ig_transient(_resp(503, {})) is True

    def test_is_transient_flag(self):
        assert mc_social._ig_transient(_resp(400, {"error": {"is_transient": True}})) is True

    def test_code_2_is_transient(self):
        assert mc_social._ig_transient(_resp(400, {"error": {"code": 2}})) is True

    def test_normal_400_is_permanent(self):
        assert mc_social._ig_transient(_resp(400, {"error": {"code": 190}})) is False


class TestBuildIgCaption:
    """The IG caption's own 'Today's top stories' bullet block — its own text,
    separate from the reel video, mirroring what the tweet body does on X. This
    is the caption-side counterpart to render_slate_frame's on-video slate."""

    def test_includes_headline_bullets(self):
        caption = mc_social._build_ig_caption(
            "Big day in tech and sports.", date_display="Jan 1, 2026",
            headlines=["Story one happened", "Story two happened", "Story three happened"],
        )
        assert "Today's top stories:" in caption
        assert "• Story one happened" in caption
        assert "• Story two happened" in caption
        assert "• Story three happened" in caption

    def test_caps_at_three_headlines(self):
        caption = mc_social._build_ig_caption(
            "Hook.", date_display="Jan 1, 2026",
            headlines=["One", "Two", "Three", "Four"],
        )
        assert "• Four" not in caption

    def test_no_headlines_omits_the_block(self):
        caption = mc_social._build_ig_caption("Hook.", date_display="Jan 1, 2026", headlines=None)
        assert "Today's top stories:" not in caption

    def test_headline_block_still_precedes_cta(self):
        caption = mc_social._build_ig_caption(
            "Hook.", date_display="Jan 1, 2026", headlines=["Story one"],
        )
        assert caption.index("Story one") < caption.index("Full briefing")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
