"""
MikeCast — CrewAI tool library.

Every tool here is a thin wrapper around an existing function in
mc_collect / mc_plan / mc_utils, with two exceptions:

  * fetch_sports_box_score / fetch_sports_standings / fetch_team_injury_report
    are new ESPN endpoint helpers used exclusively by the NY Sports Researcher.
  * validate_claim_against_articles is a new GPT-4o-mini fact-check primitive
    used by the NY Sports Fact-Checker (and available to the Critic Crew).

All tools expose Pydantic argument schemas so CrewAI's tool-calling stays
deterministic.  Tools never raise — they return a structured failure shape
({"ok": False, "error": "..."} or an empty list) so an agent can decide to
retry or move on.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python <3.9 fallback, kept for parity with mc_config
    from backports.zoneinfo import ZoneInfo  # type: ignore

from mc_collect import (
    cluster_articles as _cluster_articles,
    collect_all_news as _collect_all_news,
    deduplicate as _deduplicate,
    enrich_top_stories as _enrich_top_stories,
    fetch_espn_rss_feeds as _fetch_espn_rss_feeds,
    fetch_hacker_news_top as _fetch_hn_top,
    fetch_nyt_top_stories as _fetch_nyt_top,
    fetch_reddit_rss as _fetch_reddit_rss,
    filter_sports_by_trusted_sources as _filter_sports_trusted,
    filter_stale_articles as _filter_stale,
    process_picks as _process_picks,
    score_and_rank_articles as _score_and_rank,
    search_news_via_nyt_article_search as _nyt_article_search,
    search_news_web as _gnews_search,
    select_top_articles as _select_top,
)
from mc_config import OPENAI_API_KEY, OPENAI_HELPER_MODEL, TODAY

logger = logging.getLogger("mikecast.crew.tools")

_ET = ZoneInfo("America/New_York")


def _localize_espn_date(raw: str) -> dict:
    """
    Convert an ESPN UTC ISO timestamp (e.g. ``'2026-05-19T23:00Z'``) into an
    ET-localized view the writer can read directly:

        {
          "date_et": "Monday, May 19 at 7:00 PM ET",
          "relative_to_today": "TOMORROW NIGHT",
        }

    The writer is anchored on TODAY (computed in America/New_York). Without
    this conversion, raw UTC strings reach the prompt unchanged and the LLM
    routinely mis-renders "tomorrow night" as "tonight". Returns ``{}`` if
    ``raw`` is empty or unparseable so callers can fall back gracefully.
    """
    if not raw:
        return {}
    try:
        dt_utc = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        dt_et = dt_utc.astimezone(_ET)
        delta_days = (dt_et.date() - datetime.strptime(TODAY, "%Y-%m-%d").date()).days
    except (ValueError, TypeError):
        return {}

    evening = dt_et.hour >= 16  # 4 PM ET or later → call it "night"
    if delta_days == 0:
        relative = "TONIGHT" if evening else "TODAY"
    elif delta_days == 1:
        relative = "TOMORROW NIGHT" if evening else "TOMORROW"
    elif delta_days == -1:
        relative = "LAST NIGHT" if evening else "YESTERDAY"
    elif delta_days > 1:
        relative = f"IN {delta_days} DAYS"
    else:
        relative = f"{abs(delta_days)} DAYS AGO"

    return {
        "date_et": dt_et.strftime("%A, %B %-d at %-I:%M %p ET"),
        "relative_to_today": relative,
    }


# ---------------------------------------------------------------------------
# Tool input unpacking helper
# ---------------------------------------------------------------------------
# CrewAI 0.86 doesn't always unpack args_schema fields into _run kwargs. It can
# pass the whole JSON payload as a single positional arg (a dict) OR forward it
# under an "input" / "kwargs" key. This helper hides that wart so tool _run
# bodies stay clean.

def _arg(kwargs: dict, name: str, default=None):
    """Pull a named arg out of CrewAI's unpredictable kwargs shape."""
    # Direct hit (newer CrewAI, when unpacking works).
    if name in kwargs and not isinstance(kwargs[name], dict):
        return kwargs[name]
    # Nested under 'input' (legacy litellm-tool path).
    if isinstance(kwargs.get("input"), dict) and name in kwargs["input"]:
        return kwargs["input"][name]
    # The whole payload sometimes arrives under the schema field name itself
    # as the un-unpacked dict.
    if isinstance(kwargs.get(name), dict) and name in kwargs[name]:
        return kwargs[name][name]
    # CrewAI 0.86 sometimes passes the entire schema dict as the first kwarg
    # value — search every dict value once.
    for v in kwargs.values():
        if isinstance(v, dict) and name in v and not isinstance(v[name], dict):
            return v[name]
    return default


# ---------------------------------------------------------------------------
# Shared schemas
# ---------------------------------------------------------------------------

class _QueryInput(BaseModel):
    query: str = Field(..., description="The search query string.")
    max_results: int = Field(5, description="Maximum number of results to return.")


class _SectionInput(BaseModel):
    section: str = Field(..., description="NYT Top Stories section, e.g. technology, business, sports, home.")


class _CategoryArticlesInput(BaseModel):
    categorised: dict[str, list[dict]] = Field(
        ...,
        description="Mapping of {category_name: [article_dict, ...]}.",
    )


# ---------------------------------------------------------------------------
# Collection tools (existing — wrapped)
# ---------------------------------------------------------------------------

class NYTTopStoriesTool(BaseTool):
    name: str = "nyt_top_stories"
    description: str = (
        "Fetch up to 8 New York Times Top Stories for a section "
        "(technology, business, sports, home). Returns a list of article dicts."
    )
    args_schema: type[BaseModel] = _SectionInput

    def _run(self, section: str) -> list[dict]:  # type: ignore[override]
        try:
            return _fetch_nyt_top(section)
        except Exception as exc:
            logger.warning("nyt_top_stories(%s) failed: %s", section, exc)
            return []


class NYTArticleSearchTool(BaseTool):
    name: str = "nyt_article_search"
    description: str = (
        "Search the NYT Article Search API for articles published in the last 24 hours."
    )
    args_schema: type[BaseModel] = _QueryInput

    def _run(self, query: str, max_results: int = 5) -> list[dict]:  # type: ignore[override]
        try:
            return _nyt_article_search(query, max_results=max_results)
        except Exception as exc:
            logger.warning("nyt_article_search(%s) failed: %s", query, exc)
            return []


class GoogleNewsSearchTool(BaseTool):
    name: str = "google_news_search"
    description: str = (
        "Search Google News RSS for articles published in the last 24 hours. "
        "Returns a list of article dicts."
    )
    args_schema: type[BaseModel] = _QueryInput

    def _run(self, query: str, max_results: int = 5) -> list[dict]:  # type: ignore[override]
        try:
            return _gnews_search(query, max_results=max_results)
        except Exception as exc:
            logger.warning("google_news_search(%s) failed: %s", query, exc)
            return []


class HackerNewsTool(BaseTool):
    name: str = "hn_search"
    description: str = "Fetch the current Hacker News front page (top stories)."
    args_schema: type[BaseModel] = BaseModel  # no args

    def _run(self) -> list[dict]:  # type: ignore[override]
        try:
            return _fetch_hn_top()
        except Exception as exc:
            logger.warning("hn_search failed: %s", exc)
            return []


class RedditFetchTool(BaseTool):
    name: str = "reddit_fetch"
    description: str = (
        "Fetch the configured Reddit subreddit Atom feeds. "
        "Returns {category: [article_dicts]}."
    )
    args_schema: type[BaseModel] = BaseModel  # no args

    def _run(self) -> dict[str, list[dict]]:  # type: ignore[override]
        try:
            return _fetch_reddit_rss()
        except Exception as exc:
            logger.warning("reddit_fetch failed: %s", exc)
            return {}


class ESPNFetchTool(BaseTool):
    name: str = "espn_fetch"
    description: str = (
        "Fetch the configured ESPN RSS feeds (NBA, MLB, NFL, NHL). "
        "Returns a flat list of article dicts."
    )
    args_schema: type[BaseModel] = BaseModel  # no args

    def _run(self) -> list[dict]:  # type: ignore[override]
        try:
            return _fetch_espn_rss_feeds()
        except Exception as exc:
            logger.warning("espn_fetch failed: %s", exc)
            return []


# ---------------------------------------------------------------------------
# Batched collection (preferred — runs the full Phase 1/2/3 parallel pipeline)
# ---------------------------------------------------------------------------

class _CollectAllInput(BaseModel):
    dynamic_queries: dict[str, list[str]] | None = Field(
        default=None,
        description="Optional dynamic queries from the planning crew, keyed by category.",
    )


class CollectAllNewsTool(BaseTool):
    name: str = "collect_all_news"
    description: str = (
        "Run the full MikeCast collection pipeline (NYT + RSS + HN + Reddit + ESPN + "
        "Google News across all categories in parallel). Returns "
        "{category: [article_dicts]}. Optionally accepts dynamic_queries from the "
        "planning crew to supplement the static query list."
    )
    args_schema: type[BaseModel] = _CollectAllInput

    def _run(self, dynamic_queries: dict[str, list[str]] | None = None) -> dict[str, list[dict]]:  # type: ignore[override]
        try:
            return _collect_all_news(dynamic_queries=dynamic_queries)
        except Exception as exc:
            logger.warning("collect_all_news failed: %s", exc)
            return {}


# ---------------------------------------------------------------------------
# Pipeline-stage tools (dedup, cluster, score, select, enrich, sports filter)
# ---------------------------------------------------------------------------

class DedupTool(BaseTool):
    name: str = "dedupe_against_history"
    description: str = (
        "Remove duplicate articles within the current batch and against the rolling "
        "7-day history file. Updates the history. Returns the deduplicated dict."
    )
    args_schema: type[BaseModel] = _CategoryArticlesInput

    def _run(self, categorised: dict[str, list[dict]]) -> dict[str, list[dict]]:  # type: ignore[override]
        try:
            return _deduplicate(categorised)
        except Exception as exc:
            logger.warning("dedupe_against_history failed: %s", exc)
            return categorised


class FilterStaleTool(BaseTool):
    name: str = "filter_stale_articles"
    description: str = (
        "Drop articles whose publication date is older than max_age_days. "
        "Articles without a parseable date are kept."
    )

    class _Input(BaseModel):
        categorised: dict[str, list[dict]]
        max_age_days: int = 3

    args_schema: type[BaseModel] = _Input

    def _run(self, categorised: dict[str, list[dict]], max_age_days: int = 3) -> dict[str, list[dict]]:  # type: ignore[override]
        try:
            return _filter_stale(categorised, max_age_days=max_age_days)
        except Exception as exc:
            logger.warning("filter_stale_articles failed: %s", exc)
            return categorised


class FilterSportsTrustedTool(BaseTool):
    name: str = "filter_sports_by_trusted_sources"
    description: str = (
        "For NY Sports articles only, drop entries from publishers not on the "
        "SPORTS_TRUSTED_SOURCES allowlist. All other categories pass through."
    )
    args_schema: type[BaseModel] = _CategoryArticlesInput

    def _run(self, categorised: dict[str, list[dict]]) -> dict[str, list[dict]]:  # type: ignore[override]
        try:
            return _filter_sports_trusted(categorised)
        except Exception as exc:
            logger.warning("filter_sports_by_trusted_sources failed: %s", exc)
            return categorised


class ClusterTool(BaseTool):
    name: str = "cluster_articles"
    description: str = (
        "Group articles that cover the same story within each category and keep "
        "only the most informative headline per cluster (GPT-4o-mini)."
    )
    args_schema: type[BaseModel] = _CategoryArticlesInput

    def _run(self, categorised: dict[str, list[dict]]) -> dict[str, list[dict]]:  # type: ignore[override]
        try:
            return _cluster_articles(categorised)
        except Exception as exc:
            logger.warning("cluster_articles failed: %s", exc)
            return categorised


class ScoreTool(BaseTool):
    name: str = "score_and_rank_articles"
    description: str = (
        "Score each article 1-100 using a category-specific GPT-4o agent. "
        "Returns each category's articles sorted by score descending."
    )

    class _Input(BaseModel):
        categorised: dict[str, list[dict]]
        trending_context: str = ""

    args_schema: type[BaseModel] = _Input

    def _run(self, categorised: dict[str, list[dict]], trending_context: str = "") -> dict[str, list[dict]]:  # type: ignore[override]
        try:
            return _score_and_rank(categorised, trending_context=trending_context)
        except Exception as exc:
            logger.warning("score_and_rank_articles failed: %s", exc)
            return categorised


class SelectTopTool(BaseTool):
    name: str = "select_top_articles"
    description: str = (
        "Trim each category proportionally so the total across all categories "
        "is approximately `total`. Each category gets at least 3 slots."
    )

    class _Input(BaseModel):
        categorised: dict[str, list[dict]]
        total: int = 25

    args_schema: type[BaseModel] = _Input

    def _run(self, categorised: dict[str, list[dict]], total: int = 25) -> dict[str, list[dict]]:  # type: ignore[override]
        try:
            return _select_top(categorised, total=total)
        except Exception as exc:
            logger.warning("select_top_articles failed: %s", exc)
            return categorised


class EnrichTool(BaseTool):
    name: str = "enrich_top_stories"
    description: str = (
        "For the top N articles by score across all categories, fetch the article "
        "body and add a 'why_it_matters' insight via GPT-4o-mini."
    )

    class _Input(BaseModel):
        categorised: dict[str, list[dict]]
        top_n: int = 15

    args_schema: type[BaseModel] = _Input

    def _run(self, categorised: dict[str, list[dict]], top_n: int = 15) -> dict[str, list[dict]]:  # type: ignore[override]
        try:
            return _enrich_top_stories(categorised, top_n=top_n)
        except Exception as exc:
            logger.warning("enrich_top_stories failed: %s", exc)
            return categorised


class ProcessPicksTool(BaseTool):
    name: str = "process_picks"
    description: str = (
        "Load all pending Mike's Picks from mikes_picks.json, summarise them, "
        "and mark them as processed. Returns a list of pick summary dicts."
    )
    args_schema: type[BaseModel] = BaseModel  # no args

    def _run(self) -> list[dict]:  # type: ignore[override]
        try:
            return _process_picks()
        except Exception as exc:
            logger.warning("process_picks failed: %s", exc)
            return []


# ---------------------------------------------------------------------------
# Planning tool (xAI Grok)
# ---------------------------------------------------------------------------

class XAIGrokPlanTool(BaseTool):
    name: str = "xai_grok_search"
    description: str = (
        "Call xAI Grok-3 (with live web + X search) to identify today's breaking "
        "stories and generate targeted Google News queries per category. Returns "
        "{dynamic_queries, trending_context, trending}. Falls back to empty values "
        "if XAI_API_KEY is unset or Grok fails."
    )
    args_schema: type[BaseModel] = BaseModel  # no args

    def _run(self) -> dict[str, Any]:  # type: ignore[override]
        from mc_plan import plan_daily_searches
        try:
            queries, ctx, trending = plan_daily_searches()
            return {"dynamic_queries": queries, "trending_context": ctx, "trending": trending}
        except Exception as exc:
            logger.warning("xai_grok_search failed: %s", exc)
            return {"dynamic_queries": {}, "trending_context": "", "trending": []}


# ---------------------------------------------------------------------------
# NEW: ESPN sports primary-source tools (NY Sports Researcher only)
# ---------------------------------------------------------------------------

# Map NY team names → (ESPN sport, ESPN league, team slug).
# ESPN's site API team slugs are the lowercase abbreviation (nyy, ny, nyg, njd).
_TEAM_TO_ESPN: dict[str, tuple[str, str, str]] = {
    "yankees":           ("baseball",  "mlb", "nyy"),
    "new york yankees":  ("baseball",  "mlb", "nyy"),
    "knicks":            ("basketball", "nba", "ny"),
    "new york knicks":   ("basketball", "nba", "ny"),
    "giants":            ("football",  "nfl", "nyg"),
    "new york giants":   ("football",  "nfl", "nyg"),
    "devils":            ("hockey",    "nhl", "njd"),
    "new jersey devils": ("hockey",    "nhl", "njd"),
}

_LEAGUE_TO_SPORT: dict[str, str] = {
    "mlb": "baseball", "nba": "basketball", "nfl": "football", "nhl": "hockey",
}

_ESPN_TIMEOUT = 10


from mc_utils import _safe_request


def _espn_get(path: str) -> dict | None:
    """
    GET an ESPN site-api endpoint; return parsed JSON or None on failure.
    Use for news, schedules, team-level lookups.
    """
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path.lstrip('/')}"
    resp = _safe_request(url, timeout=_ESPN_TIMEOUT)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:
        logger.debug("ESPN %s json parse failed: %s", url, exc)
        return None


def _espn_web_get(path: str) -> dict | None:
    """
    GET an ESPN web-api endpoint; standings live here, not on site.api.espn.
    The site.api version of /standings only returns {fullViewLink: ...}.
    """
    url = f"https://site.web.api.espn.com/apis/v2/sports/{path.lstrip('/')}"
    resp = _safe_request(url, timeout=_ESPN_TIMEOUT)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception as exc:
        logger.debug("ESPN web %s json parse failed: %s", url, exc)
        return None


class FetchSportsBoxScoreTool(BaseTool):
    name: str = "fetch_sports_box_score"
    description: str = (
        "Fetch the most recent completed game for a NY team (Yankees, Knicks, "
        "Giants, Devils) from ESPN. Returns {ok, team, status, home, away, "
        "date (raw UTC), date_et (human-readable ET), relative_to_today "
        "('LAST NIGHT' / 'YESTERDAY' / etc.), next_game: {opponent, date, "
        "date_et, relative_to_today}} or {ok: False, error}. When writing "
        "about game timing, always use relative_to_today verbatim — never "
        "parse the raw UTC date yourself."
    )

    class _Input(BaseModel):
        team: str = Field(..., description="Team name, e.g. 'Yankees', 'Knicks', 'Giants', 'Devils'.")

    args_schema: type[BaseModel] = _Input

    def _run(self, *args, **kwargs) -> dict:  # type: ignore[override]
        team = _arg(kwargs, "team", "")
        if not isinstance(team, str) or not team.strip():
            return {"ok": False, "error": "missing or invalid team argument"}
        slug = _TEAM_TO_ESPN.get(team.strip().lower())
        if not slug:
            return {"ok": False, "error": f"unknown team: {team}"}
        sport, league, team_abbr = slug

        data = _espn_get(f"{sport}/{league}/teams/{team_abbr}/schedule")
        if not data:
            return {"ok": False, "error": "espn unreachable"}

        events = data.get("events") or data.get("team", {}).get("nextEvent", [])
        completed = [
            e for e in events
            if (e.get("competitions") or [{}])[0].get("status", {}).get("type", {}).get("completed")
        ]
        # Future events ordered earliest-first. ESPN's /teams/{abbr}/schedule
        # endpoint puts BOTH past and future games in `events`; the `nextEvent`
        # field only exists on the bare /teams/{abbr} endpoint, so the old code
        # at the bottom of this function never found a next game. Compute it
        # here from the same `events` array.
        future = sorted(
            [e for e in events
             if not (e.get("competitions") or [{}])[0].get("status", {}).get("type", {}).get("completed")],
            key=lambda e: e.get("date", ""),
        )
        if not completed:
            return {"ok": False, "error": "no completed games found", "team": team}

        last = completed[-1]
        comp = (last.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []

        def _name(c):
            team = c.get("team") or {}
            return str(team.get("displayName") or team.get("name") or "").strip()

        def _score(c):
            # ESPN returns score sometimes as a string ("7", "10-3"), sometimes
            # as an object {"value": 7, "displayValue": "7"}. Handle both.
            s = c.get("score")
            if isinstance(s, dict):
                s = s.get("displayValue") or s.get("value") or ""
            return str(s or "").strip()

        home_idx = next((i for i, c in enumerate(competitors) if c.get("homeAway") == "home"), 0)
        away_idx = 1 - home_idx if len(competitors) == 2 else 0

        home, away = competitors[home_idx], competitors[away_idx]

        next_game = None
        if future:
            ne = future[0]
            ne_comp = (ne.get("competitions") or [{}])[0]
            # `team` here is the user-supplied short name (e.g. "Yankees");
            # ESPN's competitor displayName is the full club name (e.g.
            # "New York Yankees"). The two never compare equal directly, so
            # the previous `!= team` filter silently let the team itself slip
            # in as the "opponent". Use a case-insensitive substring check.
            team_needle = team.strip().lower()
            opponents = [
                _name(c) for c in (ne_comp.get("competitors") or [])
                if team_needle not in _name(c).lower()
            ]
            ne_raw = ne.get("date", "")
            next_game = {
                "opponent": opponents[0] if opponents else "",
                "date": ne_raw,
                **_localize_espn_date(ne_raw),
            }

        last_raw = last.get("date", "")
        return {
            "ok": True,
            "team": team,
            "date": last_raw,
            **_localize_espn_date(last_raw),
            "status": comp.get("status", {}).get("type", {}).get("description", ""),
            "home": {"name": _name(home), "score": _score(home)},
            "away": {"name": _name(away), "score": _score(away)},
            "next_game": next_game,
        }


class FetchSportsStandingsTool(BaseTool):
    name: str = "fetch_sports_standings"
    description: str = (
        "Fetch current league standings from ESPN. league must be one of "
        "'mlb', 'nba', 'nfl', 'nhl'. Returns {ok, league, teams: [{name, wins, losses, ...}]}."
    )

    class _Input(BaseModel):
        league: str = Field(..., description="One of 'mlb', 'nba', 'nfl', 'nhl'.")

    args_schema: type[BaseModel] = _Input

    def _run(self, *args, **kwargs) -> dict:  # type: ignore[override]
        league = _arg(kwargs, "league", "")
        if not isinstance(league, str) or not league.strip():
            return {"ok": False, "error": "missing or invalid league argument"}
        league = league.strip().lower()
        sport = _LEAGUE_TO_SPORT.get(league)
        if not sport:
            return {"ok": False, "error": f"unknown league: {league}"}

        # Use site.web.api.espn.com — the site.api version of /standings only
        # returns {"fullViewLink": "..."} and no actual standings rows.
        data = _espn_web_get(f"{sport}/{league}/standings")
        if not data:
            return {"ok": False, "error": "espn unreachable", "league": league}

        rows: list[dict] = []
        for child in (data.get("children") or [data]):
            for entry in (child.get("standings", {}).get("entries") or []):
                team = entry.get("team", {})
                stats = {s.get("name"): s.get("displayValue") for s in entry.get("stats") or []}
                rows.append({
                    "name": team.get("displayName", ""),
                    "wins": stats.get("wins"),
                    "losses": stats.get("losses"),
                    "ties": stats.get("ties"),
                    "win_pct": stats.get("winPercent"),
                    "games_back": stats.get("gamesBehind"),
                })
        return {"ok": True, "league": league, "teams": rows}


class FetchTeamInjuryReportTool(BaseTool):
    name: str = "fetch_team_injury_report"
    description: str = (
        "Fetch the current injury report for a NY team from ESPN. "
        "Returns {ok, team, injuries: [{athlete, status, note}, ...]}."
    )

    class _Input(BaseModel):
        team: str = Field(..., description="Team name, e.g. 'Yankees', 'Knicks', 'Giants', 'Devils'.")

    args_schema: type[BaseModel] = _Input

    def _run(self, *args, **kwargs) -> dict:  # type: ignore[override]
        team = _arg(kwargs, "team", "")
        if not isinstance(team, str) or not team.strip():
            return {"ok": False, "error": "missing or invalid team argument"}
        slug = _TEAM_TO_ESPN.get(team.strip().lower())
        if not slug:
            return {"ok": False, "error": f"unknown team: {team}"}
        sport, league, team_abbr = slug

        # The per-team injuries endpoint does not exist on site.api for most
        # leagues. The league-wide endpoint returns one entry per team — pull
        # ours out of that list.
        data = _espn_get(f"{sport}/{league}/injuries")
        if not data:
            return {"ok": False, "error": "espn unreachable", "team": team}

        # The league-wide payload has one entry per team. The team identifier
        # is the top-level `displayName` (e.g. "New York Yankees") — there is
        # NO nested `team.abbreviation`. We match by case-insensitive substring
        # so "Yankees" matches "New York Yankees".
        team_match = team.strip().lower()
        injuries: list[dict] = []
        for team_entry in (data.get("injuries") or []):
            entry_name = (team_entry.get("displayName") or "").lower()
            if team_match not in entry_name:
                continue
            for inj in (team_entry.get("injuries") or []):
                athlete = (inj.get("athlete") or {}).get("displayName") or ""
                status = inj.get("status") or ""
                note = inj.get("longComment") or inj.get("shortComment") or ""
                injuries.append({"athlete": athlete, "status": status, "note": note[:280]})

        return {"ok": True, "team": team, "injuries": injuries}


# ---------------------------------------------------------------------------
# NEW: claim validation against source articles (Fact-Checker)
# ---------------------------------------------------------------------------

class ValidateClaimTool(BaseTool):
    name: str = "validate_claim_against_articles"
    description: str = (
        "Ask GPT-4o-mini whether a specific factual claim is directly supported by the "
        "provided source articles. Returns "
        "{ok: bool, supported: 'yes'|'no'|'unclear', evidence: 'quoted snippet', reasoning: '...'}. "
        "Use for fact-checking writer output before it reaches the listener."
    )

    class _Input(BaseModel):
        claim: str = Field(..., description="A single factual sentence to verify.")
        articles: list[dict] = Field(
            ...,
            description="The exhaustive list of source articles the claim must be backed by.",
        )

    args_schema: type[BaseModel] = _Input

    def _run(self, *args, **kwargs) -> dict:  # type: ignore[override]
        claim = _arg(kwargs, "claim", "")
        articles = _arg(kwargs, "articles", []) or []
        if not isinstance(claim, str) or not claim.strip():
            return {"ok": False, "supported": "unclear", "evidence": "", "reasoning": "missing claim"}
        if not isinstance(articles, list):
            return {"ok": False, "supported": "unclear", "evidence": "", "reasoning": "articles must be a list"}
        if not OPENAI_API_KEY:
            return {"ok": False, "supported": "unclear", "evidence": "", "reasoning": "OPENAI_API_KEY unset"}
        if not articles:
            return {"ok": True, "supported": "no", "evidence": "", "reasoning": "no source articles provided"}

        from openai import OpenAI
        client = OpenAI()

        # Compact article block (title + description only — bodies are too long for a fast checker)
        lines: list[str] = []
        for i, art in enumerate(articles[:40], 1):
            title = (art.get("title") or "").replace("[Updated] ", "")
            desc = (art.get("description") or "")[:300]
            lines.append(f"[{i}] {title}\n     {desc}")
        article_block = "\n".join(lines)

        system = (
            "You are a strict fact-checker for a news podcast. Given a single factual claim "
            "and a list of source article snippets, determine whether the claim is DIRECTLY "
            "supported by at least one article. A claim is supported only if its specific "
            "subject, predicate, and any numeric or named details all appear in an article. "
            "Generic topical overlap is NOT support. "
            "Return ONLY valid JSON: "
            '{"supported": "yes" | "no" | "unclear", '
            '"evidence": "<short snippet from the supporting article, or empty>", '
            '"reasoning": "<one sentence>"}'
        )
        user = f"CLAIM:\n{claim}\n\nSOURCE ARTICLES:\n{article_block}"

        try:
            # Strip the LiteLLM "openai/" prefix if present — the openai SDK
            # doesn't accept provider-prefixed model strings directly.
            model = OPENAI_HELPER_MODEL.replace("openai/", "", 1)
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                max_tokens=200,
                temperature=0,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content.strip()
            parsed = json.loads(raw)
            return {
                "ok": True,
                "supported": parsed.get("supported", "unclear"),
                "evidence": parsed.get("evidence", "")[:400],
                "reasoning": parsed.get("reasoning", "")[:400],
            }
        except Exception as exc:
            logger.warning("validate_claim_against_articles failed: %s", exc)
            return {"ok": False, "supported": "unclear", "evidence": "", "reasoning": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Tool registry — used by agent definitions in crew/agents.py
# ---------------------------------------------------------------------------

# Collection / planning tools
nyt_top_stories_tool       = NYTTopStoriesTool()
nyt_article_search_tool    = NYTArticleSearchTool()
google_news_search_tool    = GoogleNewsSearchTool()
hn_search_tool             = HackerNewsTool()
reddit_fetch_tool          = RedditFetchTool()
espn_fetch_tool            = ESPNFetchTool()
collect_all_news_tool      = CollectAllNewsTool()
xai_grok_search_tool       = XAIGrokPlanTool()

# Pipeline-stage tools
dedup_tool                 = DedupTool()
filter_stale_tool          = FilterStaleTool()
filter_sports_trusted_tool = FilterSportsTrustedTool()
cluster_tool               = ClusterTool()
score_tool                 = ScoreTool()
select_top_tool            = SelectTopTool()
enrich_tool                = EnrichTool()
process_picks_tool         = ProcessPicksTool()

# NY Sports primary-source tools
fetch_box_score_tool       = FetchSportsBoxScoreTool()
fetch_standings_tool       = FetchSportsStandingsTool()
fetch_injuries_tool        = FetchTeamInjuryReportTool()

# Fact-check tool
validate_claim_tool        = ValidateClaimTool()
