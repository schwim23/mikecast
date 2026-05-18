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

# Labels that mean "in the future" — anything else (LAST NIGHT, YESTERDAY,
# "N DAYS AGO") is dropped from the upcoming-games block.
_UPCOMING_LABELS = {"TODAY", "TONIGHT", "TOMORROW", "TOMORROW NIGHT"}

# Subset of _UPCOMING_LABELS that triggers the writer's mandatory-include
# rule. A team with a game on this list MUST appear in the NY Sports section
# of the day's briefing.
_MANDATORY_UPCOMING_LABELS = {"TONIGHT", "TOMORROW", "TOMORROW NIGHT"}


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
        "articles above mention a recent game outcome, standings position, or player "
        "injury. If they do, call fetch_sports_box_score, fetch_sports_standings, or "
        "fetch_team_injury_report to retrieve the primary-source fact. If they don't, "
        "OMIT that team entirely.\n\n"
        "CRITICAL — game timing: the fetch_sports_box_score tool returns BOTH a raw UTC "
        "`date` field AND human-readable `date_et` + `relative_to_today` fields (e.g. "
        "'TONIGHT', 'TOMORROW NIGHT', 'LAST NIGHT'). When you write timing into the "
        "verified-facts string, use the `relative_to_today` label verbatim — NEVER infer "
        "'tonight' or 'tomorrow' from the raw UTC date or from article copy. The label "
        "is anchored to today in Eastern Time and is the only trustworthy source.\n\n"
        "Return ONLY a JSON object keyed by team name with short factual prose for each "
        "team you verified. Example (note how timing comes straight from relative_to_today):\n"
        "{\n"
        '  "Yankees": "Yankees beat Red Sox 7-3 LAST NIGHT at home. Next game TOMORROW NIGHT vs Orioles.",\n'
        '  "Knicks": "Knicks 50-32, 3rd in Eastern Conference. Next game TOMORROW NIGHT vs Cavaliers."\n'
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


def fetch_all_ny_upcoming_games() -> list[dict]:
    """
    Unconditionally fetch the next scheduled game for each NY team via ESPN.

    Bypasses the LLM Researcher: the writers used to depend on article coverage
    for upcoming-game info, which silently dropped imminent games (e.g. a Knicks
    playoff game) whenever today's article batch happened not to mention that
    team. This function calls fetch_box_score_tool directly for all four NY
    teams so the next-game info flows through deterministically.

    Returns a list of dicts shaped like::

        {
            "team": "Knicks",
            "opponent": "Cavaliers",
            "date_et": "Tuesday, May 19 at 7:00 PM ET",
            "relative_to_today": "TOMORROW NIGHT",
            "raw_utc": "2026-05-19T23:00Z",
        }

    Teams with no future game (off-season, no schedule data, ESPN unreachable,
    or whose next game is already in the past) are silently omitted.
    """
    upcoming: list[dict] = []
    for team in _NY_TEAMS:
        try:
            result = fetch_box_score_tool._run(team=team)
        except Exception as exc:
            logger.warning("[Upcoming Games] %s fetch failed (non-fatal): %s", team, exc)
            continue
        if not result.get("ok"):
            continue
        ng = result.get("next_game") or {}
        rel = (ng.get("relative_to_today") or "").strip()
        # Drop past-tense labels and anything without timing info we trust.
        if not rel or rel not in _UPCOMING_LABELS and not rel.startswith("IN "):
            continue
        upcoming.append({
            "team": team,
            "opponent": ng.get("opponent", ""),
            "date_et": ng.get("date_et", ""),
            "relative_to_today": rel,
            "raw_utc": ng.get("date", ""),
        })
    logger.info("[Upcoming Games] Found upcoming games for %d/%d NY teams: %s",
                len(upcoming), len(_NY_TEAMS),
                [g["team"] for g in upcoming])
    return upcoming


def format_upcoming_games_block(upcoming: list[dict]) -> str:
    """
    Render the upcoming-games list as a prompt block for the writers.
    Returns "" if empty so callers can concatenate safely.

    Includes an explicit MANDATORY rule: any team whose game is TONIGHT,
    TOMORROW, or TOMORROW NIGHT must appear in the briefing's NY Sports section
    even if no article in today's batch covers that team.
    """
    if not upcoming:
        return ""
    lines = [
        "=== NY SPORTS — UPCOMING GAMES (verified via ESPN, anchored to today in ET) ==="
    ]
    has_mandatory = False
    for g in upcoming:
        rel = g.get("relative_to_today", "")
        opp = g.get("opponent", "") or "TBD"
        when = g.get("date_et", "")
        team = g.get("team", "")
        is_mandatory = rel in _MANDATORY_UPCOMING_LABELS
        marker = " [MANDATORY-INCLUDE]" if is_mandatory else ""
        has_mandatory = has_mandatory or is_mandatory
        lines.append(f"  • {team}: {rel} ({when}) vs {opp}{marker}")
    if has_mandatory:
        lines.append(
            "MANDATORY: every team above marked [MANDATORY-INCLUDE] MUST appear in the NY "
            "Sports section of your output — one sentence is enough (who, opponent, when). "
            "This applies EVEN IF no article in today's batch mentions that team; the "
            "upcoming-game info above is primary-source from ESPN and is the briefing's "
            "ground truth. Use the relative_to_today label (TONIGHT / TOMORROW / TOMORROW "
            "NIGHT) verbatim — never substitute your own timing guess."
        )
    else:
        lines.append(
            "These are primary-source from ESPN. Use the relative_to_today label "
            "verbatim if you mention any of these games. Never substitute your own "
            "timing guess from article copy."
        )
    return "\n".join(lines)
