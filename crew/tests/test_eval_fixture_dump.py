"""
Regression tests for the MIKECAST_DUMP_EVAL_FIXTURE=1 code path in mc_collect.py.

Incident (2026-09-22, commit 0052e69): commit 097c30b added dump_fixture()
calls referencing TODAY without importing it from mc_config. The bug was
invisible in every normal dev/test run because dump_fixture() is a no-op
unless MIKECAST_DUMP_EVAL_FIXTURE=1 is set — but that flag IS set by the
prod Docker image's default CMD (`--dump-eval-fixture`, see Dockerfile), so
it crashed the real 6:30 AM run mid-scoring: no briefing generated,
published, or emailed that day.

Lesson: any code path gated behind an env var / CLI flag that only turns on
in production is effectively untested dead code until something exercises
it with that flag set. These tests turn the flag on and force execution of
the scorer/enricher dump_fixture() call sites so a broken reference (typo,
missing import, renamed field) fails in CI instead of at 6:30 AM in prod.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch, MagicMock

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_collect  # noqa: E402


@pytest.fixture
def dump_fixture_enabled(monkeypatch):
    """Mirrors the prod Docker CMD (`--dump-eval-fixture` -> this env var)."""
    monkeypatch.setenv("MIKECAST_DUMP_EVAL_FIXTURE", "1")
    # score_and_rank_articles / enrich_top_stories both early-return before
    # ever reaching dump_fixture() if OPENAI_API_KEY is unset — prod always
    # has a real key, so the test must too (a fake one is fine; the LLM
    # call itself is mocked out below).
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(mc_collect, "OPENAI_API_KEY", "test-key-not-real")


def _sample_categorised():
    return {
        "Tech": [
            {"title": "Sample story", "description": "A sample description.",
             "url": "https://example.com/a", "source": "Example Wire"},
        ],
    }


class TestArticleScorerFixtureDump:
    """score_and_rank_articles must reach dump_fixture() without raising."""

    def test_dump_fixture_called_with_no_exception(self, dump_fixture_enabled):
        with patch("litellm.completion", side_effect=Exception("network disabled in test")), \
             patch.object(mc_collect, "dump_fixture") as mock_dump:
            result = mc_collect.score_and_rank_articles(_sample_categorised())

        assert "Tech" in result
        mock_dump.assert_called_once()
        role, date, payload = mock_dump.call_args[0]
        assert role == "article_scorer"
        assert date  # must resolve to a real value, not raise before the call
        assert "input" in payload and "output" in payload


class TestArticleEnricherFixtureDump:
    """enrich_top_stories must reach dump_fixture() without raising."""

    def test_dump_fixture_called_with_no_exception(self, dump_fixture_enabled):
        articles = {"Tech": [
            {"title": "Sample story", "description": "A sample description.",
             "url": "https://example.com/a", "score": 90},
        ]}
        with patch("litellm.completion", side_effect=Exception("network disabled in test")), \
             patch.object(mc_collect, "requests") as mock_requests, \
             patch.object(mc_collect, "dump_fixture") as mock_dump:
            mock_requests.get.side_effect = Exception("network disabled in test")
            mc_collect.enrich_top_stories(articles, top_n=8)

        mock_dump.assert_called_once()
        role, date, payload = mock_dump.call_args[0]
        assert role == "article_enricher"
        assert date
        assert "input" in payload and "output" in payload
