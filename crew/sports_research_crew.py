"""
Steps 1–6 for NY Sports — dedicated crew with primary-source verification.

The general Research Crew already runs the Sports Gatekeeper (the
filter_sports_by_trusted_sources filter — drops AOL et al.) and scoring.
What this module adds is the part the legacy pipeline does NOT do:

    Researcher — given the NY Sports articles for today, identifies any
                 claim that's implied but not explicitly stated, and
                 calls ESPN (box scores / standings / injuries) for the
                 four NY teams to ground those claims in primary sources.

The output is a dict {team_name: verified_facts_block} that the Writing
Crew prepends to its NY Sports prompt context. The legacy in-article
content is preserved unchanged.

The Fact-Checker runs LATER, after the writers produce their NY Sports
drafts — see critic_crew.run_critic_pass().
"""

from __future__ import annotations

import json
import logging
import re

from crewai import Agent, Crew, Process, Task

from crew.agents import make_sports_researcher
from crew.tools import (
    fetch_box_score_tool,
    fetch_injuries_tool,
    fetch_standings_tool,
)
from mc_config import TODAY, TODAY_DISPLAY

logger = logging.getLogger("mikecast.crew.sports")


_NY_TEAMS = ["Yankees", "Knicks", "Giants", "Devils"]

# A team's most recent completed game is only worth surfacing if it happened
# within this many days — otherwise the "score" is stale (e.g. the Knicks'
# last game two weeks ago once their season ended).
_RECENT_RESULT_WINDOW_DAYS = 4

# How soon an upcoming game must be to count as the team "having a game". This
# bounds the next-game lookahead so OFF-SEASON / next-season games — which ESPN
# may list months out — are NOT surfaced. (Mike: no next-game line in the
# offseason.) ~8 days covers a weekly NFL cadence plus a little slack while
# still excluding anything season-distant.
_UPCOMING_WINDOW_DAYS = 8


def _build_sports_briefing(ny_sports_articles: list[dict]) -> str:
    """Compact NY Sports article snippets for the Researcher's prompt."""
    if not ny_sports_articles:
        return "(no NY Sports articles in today's batch)"
    lines: list[str] = []
    for i, art in enumerate(ny_sports_articles[:30], 1):
        title = (art.get("title") or "").replace("[Updated] ", "")
        desc = (art.get("description") or "")[:200]
        source = art.get("source") or ""
        lines.append(f"[{i}] {title}\n     Source: {source}\n     {desc}")
    return "\n\n".join(lines)


def run_sports_research(top_articles: dict[str, list[dict]]) -> dict[str, str]:
    """
    Run the NY Sports Researcher against today's NY Sports articles. Use the
    ESPN tools to verify per-team primary-source facts.

    Returns a dict {team_name: verified_facts_string}. The string is in
    free-form prose ready for prepending to the writer's NY Sports prompt
    context. Teams with no verifiable activity are omitted.

    Falls back to an empty dict if the Researcher call fails or if no NY
    Sports articles exist for today.
    """
    ny_sports = top_articles.get("NY Sports") or []
    if not ny_sports:
        logger.info("[Sports Research] No NY Sports articles — skipping ESPN verification.")
        return {}

    researcher: Agent = make_sports_researcher()
    article_block = _build_sports_briefing(ny_sports)

    task_description = (
        f"Today is {TODAY_DISPLAY} ({TODAY}).\n\n"
        f"Today's NY Sports articles (already filtered to trusted sources):\n\n"
        f"{article_block}\n\n"
        f"For each of these four NY teams — {', '.join(_NY_TEAMS)} — decide whether the "
        "articles above point to a development in the LAST 24 HOURS: a game result, a "
        "trade/signing, a coaching or roster move, or a player injury. If they do, call "
        "fetch_sports_box_score, fetch_sports_standings, or fetch_team_injury_report to "
        "retrieve the primary-source fact. If they don't, OMIT that team entirely.\n\n"
        "CRITICAL — game timing: the fetch_sports_box_score tool returns BOTH a raw UTC "
        "`date` field AND human-readable `date_et` + `relative_to_today` fields (e.g. "
        "'TONIGHT', 'TOMORROW NIGHT', 'LAST NIGHT'). When you write timing into the "
        "verified-facts string, use the `relative_to_today` label verbatim — NEVER infer "
        "'tonight' or 'tomorrow' from the raw UTC date or from article copy. The label "
        "is anchored to today in Eastern Time and is the only trustworthy source.\n\n"
        "Report the last game's score and the next game (if the team is in season). Do NOT "
        "volunteer standings, records, seeds, or streaks unless a tool returned that exact "
        "figure and an article raised it — stale training-data 'color' is banned. Return "
        "ONLY a JSON object keyed by team name with short factual prose for each team you "
        "verified. Example (note how timing comes straight from relative_to_today):\n"
        "{\n"
        '  "Yankees": "Yankees beat Red Sox 7-3 LAST NIGHT at home. Next game TOMORROW NIGHT vs Orioles.",\n'
        '  "Devils": "Devils lost to Rangers 4-2 LAST NIGHT. Next game TOMORROW vs Islanders."\n'
        "}\n\n"
        "If you cannot verify anything, return {}.\n"
        "Use ONLY facts returned by the ESPN tools — never your training knowledge."
    )

    task = Task(
        description=task_description,
        expected_output='A JSON object: {team_name: "verified facts string"} — possibly empty.',
        agent=researcher,
        tools=[fetch_box_score_tool, fetch_standings_tool, fetch_injuries_tool],
    )

    crew = Crew(
        agents=[researcher],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )

    try:
        result = crew.kickoff()
        # CrewAI result is a CrewOutput; .raw / str() should give us the JSON
        raw = getattr(result, "raw", None) or str(result)
        raw = raw.strip()
        # Strip code fences if any
        if raw.startswith("```"):
            raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("```")).strip()
        verified = json.loads(raw)
        if not isinstance(verified, dict):
            logger.warning("[Sports Research] Non-dict output: %r — discarding", raw[:200])
            return {}
        # Sanitize: only return entries with non-empty string values
        cleaned = {
            str(k): str(v).strip()
            for k, v in verified.items()
            if isinstance(v, str) and v.strip()
        }
        logger.info("[Sports Research] Verified primary-source facts for %d team(s): %s",
                    len(cleaned), list(cleaned.keys()))
        return cleaned
    except Exception as exc:
        logger.warning("[Sports Research] Researcher run failed (non-fatal): %s", exc)
        return {}


def format_verified_facts_block(verified: dict[str, str]) -> str:
    """
    Render the {team: facts} dict as a prompt block for the writers.
    Returns "" if empty so callers can concatenate safely.
    """
    if not verified:
        return ""
    lines = ["=== NY SPORTS — PRIMARY-SOURCE FACTS (verified via ESPN) ==="]
    for team, facts in verified.items():
        lines.append(f"  • {team}: {facts}")
    lines.append(
        "Treat these facts as ground truth alongside the articles. Do not contradict "
        "them. Do not invent additional team/player details beyond what is here or in "
        "the articles. Game-timing words like TONIGHT, TOMORROW NIGHT, LAST NIGHT, "
        "YESTERDAY are anchored to today's date in Eastern Time — copy them verbatim "
        "into your script and never substitute your own timing guess from article copy."
    )
    return "\n".join(lines)


def _days_ago_from_label(rel: str) -> int | None:
    """
    How many days ago a completed-game label refers to (0 = today). Returns
    None for labels that aren't in the past (future games) or that we can't
    parse. Mirrors the vocabulary _localize_espn_date emits.
    """
    rel = (rel or "").strip().upper()
    if rel in ("TODAY", "TONIGHT"):
        return 0
    if rel in ("LAST NIGHT", "YESTERDAY"):
        return 1
    m = re.match(r"(\d+)\s+DAYS AGO$", rel)
    if m:
        return int(m.group(1))
    return None


def _format_score_summary(team: str, home: dict, away: dict) -> str:
    """
    Render a completed game as e.g. "New York Yankees lost to Boston Red Sox
    6-1" (winner's score first). Falls back to a bare matchup line if the team
    or scores can't be resolved. Returns "" if there isn't enough to say.
    """
    h_name, a_name = (home.get("name") or "").strip(), (away.get("name") or "").strip()
    h_score, a_score = str(home.get("score") or "").strip(), str(away.get("score") or "").strip()
    if not h_name or not a_name:
        return ""
    needle = team.strip().lower()
    if needle in h_name.lower():
        ny, opp, ny_s, opp_s = h_name, a_name, h_score, a_score
    elif needle in a_name.lower():
        ny, opp, ny_s, opp_s = a_name, h_name, a_score, h_score
    else:
        # Can't tell which side is the NY team — state the raw matchup.
        return f"{a_name} {a_score}, {h_name} {h_score}".strip().rstrip(",")
    try:
        ny_i, opp_i = int(ny_s), int(opp_s)
    except (TypeError, ValueError):
        return f"{ny} vs {opp}".strip()
    if ny_i == opp_i:
        return f"{ny} tied {opp} {ny_s}-{opp_s}"
    verb = "beat" if ny_i > opp_i else "lost to"
    hi, lo = (ny_s, opp_s) if ny_i > opp_i else (opp_s, ny_s)
    return f"{ny} {verb} {opp} {hi}-{lo}"


def _build_last_game(team: str, result: dict) -> dict | None:
    """Extract a recent completed game (score) from a box-score result, or None
    if the last game is older than the recency window."""
    rel = (result.get("relative_to_today") or "").strip()
    days = _days_ago_from_label(rel)
    if days is None or days > _RECENT_RESULT_WINDOW_DAYS:
        return None
    summary = _format_score_summary(team, result.get("home") or {}, result.get("away") or {})
    if not summary:
        return None
    return {"summary": summary, "relative_to_today": rel, "date_et": result.get("date_et", "")}


def _days_until_label(rel: str) -> int | None:
    """
    How many days until an upcoming-game label (0 = today). Returns None for
    labels that aren't in the future or that we can't parse. Mirrors the
    vocabulary _localize_espn_date emits for future games.
    """
    rel = (rel or "").strip().upper()
    if rel in ("TODAY", "TONIGHT"):
        return 0
    if rel in ("TOMORROW", "TOMORROW NIGHT"):
        return 1
    m = re.match(r"IN (\d+)\s+DAYS?$", rel)
    if m:
        return int(m.group(1))
    return None


def _build_next_game(ng: dict) -> dict | None:
    """Extract a genuine, near-term upcoming game from a box-score result's
    next_game, or None. Past/stale labels are rejected, and games beyond
    _UPCOMING_WINDOW_DAYS (i.e. off-season / next-season) are dropped so the
    briefing doesn't announce a game months away."""
    rel = (ng.get("relative_to_today") or "").strip()
    days = _days_until_label(rel)
    if days is None or days > _UPCOMING_WINDOW_DAYS:
        return None
    return {
        "opponent": ng.get("opponent", "") or "TBD",
        "relative_to_today": rel,
        "date_et": ng.get("date_et", ""),
    }


def fetch_all_ny_team_updates() -> list[dict]:
    """
    Unconditionally fetch each NY team's most recent result (score) AND next
    game from ESPN, bypassing the LLM Researcher.

    The writers used to get sports info only when an article happened to mention
    a team, so a team's score / next-game silently vanished whenever the day's
    (AI/tech-heavy) article batch missed it — even though the Yankees play
    almost daily in season. This pulls ground truth for all four NY teams
    deterministically so the briefing can always state, at minimum, the score
    and when the next game is.

    Returns a list of per-team dicts::

        {
          "team": "Yankees",
          "last_game": {            # None if no recent completed game
            "summary": "New York Yankees lost to Boston Red Sox 6-1",
            "relative_to_today": "LAST NIGHT",
            "date_et": "Friday, June 26 at 7:05 PM ET",
          },
          "next_game": {            # None if no upcoming game
            "opponent": "Boston Red Sox",
            "relative_to_today": "TODAY",
            "date_et": "Saturday, June 27 at 1:00 PM ET",
          },
          "mandatory": True,        # writer MUST surface this team
        }

    A team is omitted only when it has neither a recent result nor an upcoming
    game (off-season, ESPN unreachable). `mandatory` is True whenever the team
    has a recent result or an upcoming game — i.e. it "has a game" — which the
    writers must report (score + next game).
    """
    updates: list[dict] = []
    for team in _NY_TEAMS:
        try:
            result = fetch_box_score_tool._run(team=team)
        except Exception as exc:
            logger.warning("[Team Updates] %s fetch failed (non-fatal): %s", team, exc)
            continue
        if not result.get("ok"):
            continue
        last_game = _build_last_game(team, result)
        next_game = _build_next_game(result.get("next_game") or {})
        if not last_game and not next_game:
            continue
        updates.append({
            "team": team,
            "last_game": last_game,
            "next_game": next_game,
            # A recent result or any upcoming game means the team "has a game"
            # worth guaranteeing in the briefing.
            "mandatory": bool(last_game or next_game),
        })
    logger.info("[Team Updates] Built updates for %d/%d NY teams; mandatory: %s",
                len(updates), len(_NY_TEAMS),
                [u["team"] for u in updates if u["mandatory"]])
    return updates


def format_ny_team_updates_block(updates: list[dict]) -> str:
    """
    Render the per-team results + upcoming-games list as a prompt block for the
    writers. Returns "" if empty so callers can concatenate safely.

    Includes an explicit MANDATORY rule: any team with a game (recent result or
    upcoming) MUST appear in the NY Sports section — stating at minimum the last
    score and the next game — even if no article in today's batch covers it.
    """
    if not updates:
        return ""
    lines = [
        "=== NY SPORTS — RESULTS & UPCOMING GAMES (verified via ESPN, anchored to today in ET) ===",
        "Only in-season NY teams appear below (a recent result or an upcoming game within "
        "the season window). Each in-season team lists its last score and its NEXT game. "
        "Any of the four NY teams (Knicks / Devils / Yankees / Giants) NOT listed here is "
        "out of season or idle — do NOT invent a score, a next game, or any update for it.",
    ]
    has_mandatory = False
    for u in updates:
        team = u.get("team", "")
        lg, ng = u.get("last_game"), u.get("next_game")
        parts: list[str] = []
        if lg:
            parts.append(f"Last game ({lg['relative_to_today']}): {lg['summary']}.")
        if ng:
            parts.append(f"Next game {ng['relative_to_today']} vs {ng['opponent']} ({ng['date_et']}).")
        marker = " [MANDATORY-INCLUDE]" if u.get("mandatory") else ""
        has_mandatory = has_mandatory or bool(u.get("mandatory"))
        lines.append(f"  • {team}: {' '.join(parts)}{marker}")
    if has_mandatory:
        lines.append(
            "MANDATORY: every team marked [MANDATORY-INCLUDE] MUST appear in the NY Sports "
            "section. State at minimum the score of their last game and when their next game "
            "is — EVEN IF no article in today's batch mentions that team. These facts are "
            "primary-source from ESPN and are the briefing's ground truth. Copy the relative "
            "timing words (TODAY / TONIGHT / TOMORROW / TOMORROW NIGHT / LAST NIGHT / "
            "YESTERDAY) verbatim — never substitute your own timing guess."
        )
    else:
        lines.append(
            "These are primary-source from ESPN. If you mention any of these games, copy the "
            "relative timing words verbatim and never substitute your own timing guess."
        )
    return "\n".join(lines)
