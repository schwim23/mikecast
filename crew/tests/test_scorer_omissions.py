"""
Regression tests for scorer omissions in mc_collect.score_and_rank_articles.

Incident (2026-09-22..24, found in the article_scorer eval): gpt-4o returned
valid JSON that scored only the first ~7 articles per category and silently
omitted the rest, leaving ~1/3 of each run at the default score 50 with no
warning (6/21, 17/51, 15/43 unscored in prod). The scorer now retries the
omitted articles once and warns about anything still unscored.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import mc_collect  # noqa: E402


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(mc_collect, "OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.delenv("MIKECAST_DUMP_EVAL_FIXTURE", raising=False)


def _articles(n):
    return {"Tech": [{"title": f"Story {i}", "description": "d", "url": f"https://example.com/{i}",
                      "source": "Example Wire"} for i in range(n)]}


def _response(ids):
    body = json.dumps([{"id": i, "score": 80, "reason": f"reason {i}"} for i in ids])
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=body))])


def _ids_in_prompt(kwargs):
    prompt = kwargs["messages"][1]["content"]
    return [int(line[1:line.index("]")]) for line in prompt.splitlines() if line.startswith("[")]


def test_omitted_articles_are_retried():
    calls = []

    def fake_completion(**kwargs):
        ids = _ids_in_prompt(kwargs)
        calls.append(ids)
        return _response(ids[:3] if len(calls) == 1 else ids)  # first call omits all but 3

    with patch("litellm.completion", side_effect=fake_completion):
        result = mc_collect.score_and_rank_articles(_articles(8))

    assert calls == [list(range(1, 9)), list(range(4, 9))]
    assert all(a["score_reason"] and a["score"] == 80 for a in result["Tech"])


def test_still_omitted_after_retry_warns_and_keeps_default(caplog):
    def fake_completion(**kwargs):
        return _response(_ids_in_prompt(kwargs)[:1])  # always scores only one article

    with patch("litellm.completion", side_effect=fake_completion):
        result = mc_collect.score_and_rank_articles(_articles(4))

    scored = [a for a in result["Tech"] if a["score_reason"]]
    assert len(scored) == 2  # one from the first call, one from the retry
    assert sum(1 for a in result["Tech"] if a["score"] == 50 and not a["score_reason"]) == 2
    assert "still omitted 2 articles" in caplog.text


def test_complete_response_makes_one_call():
    with patch("litellm.completion", side_effect=lambda **kw: _response(_ids_in_prompt(kw))) as m:
        mc_collect.score_and_rank_articles(_articles(5))
    assert m.call_count == 1
