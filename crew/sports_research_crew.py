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
