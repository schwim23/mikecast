"""
Regression tests for crew/tools.py.

Each test pins behavior we already had to fix once during shadow validation —
the goal is to ensure those bugs can't quietly come back. Tests run without
network access: HTTP is mocked, OpenAI is skipped via env, and ESPN payloads
are loaded from inline fixtures that mirror real responses.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

# Make repo root importable when running pytest from anywhere
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crew.tools import (  # noqa: E402
    _arg,
    fetch_box_score_tool,
    fetch_injuries_tool,
    fetch_standings_tool,
    validate_claim_tool,
)


# ---------------------------------------------------------------------------
# _arg helper
# ---------------------------------------------------------------------------

class TestArgHelper:
    """CrewAI 0.86 passes tool args in several shapes — _arg flattens them."""

    def test_direct_kwarg(self):
        assert _arg({"team": "Yankees"}, "team") == "Yankees"

    def test_under_input_key(self):
        # Some LiteLLM tool paths nest args under 'input'
        assert _arg({"input": {"team": "Knicks"}}, "team") == "Knicks"

    def test_self_named_dict(self):
        # CrewAI sometimes passes {"team": {"team": "Giants"}}
        assert _arg({"team": {"team": "Giants"}}, "team") == "Giants"

    def test_anonymous_dict_value(self):
        # Or hides the schema dict under some other kwarg name
        assert _arg({"x": {"team": "Devils"}}, "team") == "Devils"

    def test_default_returned_when_missing(self):
        assert _arg({}, "team", default="fallback") == "fallback"
        assert _arg({"other": 1}, "team", default=None) is None

    def test_skips_dict_values_that_dont_contain_name(self):
        assert _arg({"x": {"unrelated": "Yankees"}}, "team", default="d") == "d"


# ---------------------------------------------------------------------------
# ESPN score parsing — the bug that wasted hours
# ---------------------------------------------------------------------------

# Real-shape ESPN payload (truncated; matches site.api/.../teams/{slug}/schedule).
_ESPN_SCHEDULE_PAYLOAD = {
    "events": [
        {
            "date": "2026-05-17T17:40Z",
            "competitions": [
                {
                    "status": {"type": {"completed": True, "description": "Final"}},
                    "competitors": [
                        # ESPN sometimes returns score as a plain string
                        {"homeAway": "home", "score": "7",
                         "team": {"displayName": "New York Mets"}},
                        # …and sometimes as an object (this is the bug shape)
                        {"homeAway": "away",
                         "score": {"value": 6, "displayValue": "6"},
                         "team": {"displayName": "New York Yankees"}},
                    ],
                }
            ],
        }
    ],
    "team": {"nextEvent": []},
}


class TestFetchSportsBoxScore:

    def test_unknown_team_returns_error(self):
        result = fetch_box_score_tool._run(team="Mariners")
        assert result["ok"] is False
        assert "unknown team" in result["error"].lower()

    def test_missing_team_arg_returns_error(self):
        result = fetch_box_score_tool._run()
        assert result["ok"] is False
        assert "missing" in result["error"].lower() or "invalid" in result["error"].lower()

    def test_handles_score_as_string_and_dict(self):
        """Regression: ESPN's score-as-dict shape used to crash with
        'dict' object has no attribute 'strip'."""
        with patch("crew.tools._espn_get", return_value=_ESPN_SCHEDULE_PAYLOAD):
            result = fetch_box_score_tool._run(team="Yankees")
        assert result["ok"] is True
        # Home Mets had a string score; away Yankees had a dict score —
        # both must come out as plain strings.
        assert result["home"]["score"] == "7"
        assert result["away"]["score"] == "6"
        assert result["home"]["name"] == "New York Mets"
        assert result["away"]["name"] == "New York Yankees"
        assert result["status"] == "Final"

    def test_no_completed_games_returns_error(self):
        empty_payload = {"events": [], "team": {"nextEvent": []}}
        with patch("crew.tools._espn_get", return_value=empty_payload):
            result = fetch_box_score_tool._run(team="Giants")
        assert result["ok"] is False
        assert "no completed games" in result["error"].lower()


# ---------------------------------------------------------------------------
# Standings — uses site.web.api.espn.com (regression: site.api returns redirect)
# ---------------------------------------------------------------------------

_ESPN_STANDINGS_PAYLOAD = {
    "children": [
        {
            "standings": {
                "entries": [
                    {
                        "team": {"displayName": "New York Yankees"},
                        "stats": [
                            {"name": "wins", "displayValue": "30"},
                            {"name": "losses", "displayValue": "20"},
                            {"name": "winPercent", "displayValue": ".600"},
                        ],
                    }
                ]
            }
        }
    ]
}

# What the broken site.api endpoint used to return (just a redirect placeholder).
_ESPN_SITE_API_STANDINGS_REDIRECT = {"fullViewLink": "https://example.com/standings"}


class TestFetchSportsStandings:

    def test_unknown_league_returns_error(self):
        result = fetch_standings_tool._run(league="cricket")
        assert result["ok"] is False
        assert "unknown league" in result["error"].lower()

    def test_uses_web_api_for_standings(self):
        """Regression: the site.api endpoint only returns {fullViewLink} for
        /standings, so we must hit site.web.api."""
        with patch("crew.tools._espn_web_get", return_value=_ESPN_STANDINGS_PAYLOAD) as web_mock, \
             patch("crew.tools._espn_get", return_value=_ESPN_SITE_API_STANDINGS_REDIRECT) as site_mock:
            result = fetch_standings_tool._run(league="mlb")
        assert web_mock.called, "standings must use _espn_web_get, not _espn_get"
        assert result["ok"] is True
        assert len(result["teams"]) == 1
        assert result["teams"][0]["wins"] == "30"


# ---------------------------------------------------------------------------
# Injuries — league-wide filtered by team displayName (regression)
# ---------------------------------------------------------------------------

_ESPN_INJURIES_PAYLOAD = {
    "injuries": [
        {
            "id": "1", "displayName": "New York Yankees",
            "injuries": [
                {
                    "athlete": {"displayName": "Aaron Judge"},
                    "status": "Day-To-Day",
                    "longComment": "Wrist soreness, listed as day-to-day."
                },
                {
                    "athlete": {"displayName": "Gerrit Cole"},
                    "status": "60-Day-IL",
                    "shortComment": "Elbow surgery recovery."
                },
            ],
        },
        {
            "id": "2", "displayName": "Boston Red Sox",
            "injuries": [
                {"athlete": {"displayName": "Some Sox Player"}, "status": "10-Day-IL"}
            ],
        },
    ]
}


class TestFetchTeamInjuryReport:

    def test_filters_by_team_displayname_substring(self):
        """Regression: ESPN's league-wide payload uses top-level 'displayName',
        not nested 'team.abbreviation'. Filtering by 'team' kwarg must
        substring-match against displayName."""
        with patch("crew.tools._espn_get", return_value=_ESPN_INJURIES_PAYLOAD):
            result = fetch_injuries_tool._run(team="Yankees")
        assert result["ok"] is True
        assert len(result["injuries"]) == 2
        athletes = {i["athlete"] for i in result["injuries"]}
        assert "Aaron Judge" in athletes
        assert "Gerrit Cole" in athletes
        # Red Sox player must NOT leak in
        assert not any("Sox" in i["athlete"] for i in result["injuries"])


# ---------------------------------------------------------------------------
# validate_claim_against_articles — fast-fail paths (no network)
# ---------------------------------------------------------------------------

class TestValidateClaim:

    def test_no_articles_returns_no_support(self):
        result = validate_claim_tool._run(claim="The Yankees won.", articles=[])
        assert result["ok"] is True
        assert result["supported"] == "no"

    def test_missing_claim_returns_error(self):
        result = validate_claim_tool._run(claim="", articles=[{"title": "x"}])
        assert result["ok"] is False

    def test_handles_non_list_articles(self):
        result = validate_claim_tool._run(claim="x", articles="not a list")
        assert result["ok"] is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
