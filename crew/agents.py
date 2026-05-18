"""
CrewAI agent definitions for the MikeCast pipeline.

Each agent's backstory pulls verbatim from the existing prompts in
mc_config.py and mc_generate.py so that all hallucination guardrails carry
over (the "only discuss articles in input" rule, the team-affiliation rule,
the never-patch-NY-Sports invariant, etc.).

The agents are intentionally narrow — each one is responsible for exactly
one step of the legacy pipeline. The crews in planning_crew.py / research_crew.py
/ writing_crew.py / critic_crew.py wire them together.
"""

from __future__ import annotations

from crewai import Agent

from mc_config import CATEGORY_SCORER_PROMPTS, SPORTS_TRUSTED_SOURCES

from crew.llm import (
    claude_writer_llm,
    openai_critic_llm,
    openai_helper_llm,
    openai_scorer_llm,
)
from crew.tools import (
    cluster_tool,
    collect_all_news_tool,
    dedup_tool,
    enrich_tool,
    fetch_box_score_tool,
    fetch_injuries_tool,
    fetch_standings_tool,
    filter_sports_trusted_tool,
    filter_stale_tool,
    process_picks_tool,
    score_tool,
    select_top_tool,
    validate_claim_tool,
    xai_grok_search_tool,
)


# ---------------------------------------------------------------------------
# Shared text fragments (lifted verbatim from mc_generate.py)
# ---------------------------------------------------------------------------

_HALLUCINATION_GUARD = (
    "CRITICAL RULE: Only discuss stories explicitly present in the provided articles. "
    "Do NOT mention trades, signings, scores, injuries, business events, or any facts "
    "from your training knowledge that are not in the input articles. "
    "If a category has few articles, keep that segment short — never invent news to fill time."
)

_TEAM_RULE = (
    "SPORTS TEAM RULE: If an article mentions a player's name but does NOT explicitly state "
    "which team they play for, do NOT name their team. Do not use training knowledge to infer "
    "team affiliations, positions, or stats. Say only what the article says."
)

_STORYTELLING_RULE = (
    "STORYTELLING RULE: For every story, fully explain what happened — the outcome, the "
    "numbers, the key people, the decision. Do NOT tease or trail off ('we'll have to "
    "wait and see', 'the implications could be huge'). Tell the listener what the article "
    "actually says happened."
)


# ---------------------------------------------------------------------------
# Step 0 — Planning Crew
# ---------------------------------------------------------------------------

def make_planner() -> Agent:
    return Agent(
        role="Daily Search Planner",
        goal=(
            "Identify today's most important breaking stories across AI & Tech, "
            "Business & Markets, Companies, and NY Sports, and generate targeted "
            "search queries the Research Crews can use to find primary coverage."
        ),
        backstory=(
            "You are a wire-service editor with access to live web and X (Twitter) "
            "search via xAI Grok. You only flag stories you are highly confident "
            "actually happened today. You never speculate or extrapolate from prior "
            "trends — if you can't verify a story, you omit it."
        ),
        tools=[xai_grok_search_tool],
        llm=openai_helper_llm(),
        verbose=False,
        allow_delegation=False,
    )


# ---------------------------------------------------------------------------
# Steps 1–6 — Research Crew (one Researcher per non-sports category)
# ---------------------------------------------------------------------------

def make_research_orchestrator() -> Agent:
    """
    Runs the deterministic collection + dedup + cluster pipeline.
    This agent is mostly a tool-driver — it doesn't reason about content,
    it just calls collect_all_news → deduplicate → filter_stale → cluster
    so the per-category Researchers can score what's left.
    """
    return Agent(
        role="Collection Orchestrator",
        goal=(
            "Run the full source collection (NYT + RSS + HN + Reddit + ESPN + "
            "Google News), deduplicate against the rolling 7-day history, drop "
            "stale entries, and cluster same-story duplicates. Hand the cleaned "
            "category dict to the Researchers."
        ),
        backstory=(
            "You are a senior news ops engineer. You don't write copy — you "
            "coordinate the fetch + dedup pipeline so downstream agents work on "
            "a clean set of unique stories."
        ),
        tools=[collect_all_news_tool, dedup_tool, filter_stale_tool, cluster_tool],
        llm=openai_helper_llm(),
        verbose=False,
        allow_delegation=False,
    )


def make_category_researcher(category: str) -> Agent:
    """A scorer + enricher specialist for one of the non-sports categories."""
    scoring_prompt = CATEGORY_SCORER_PROMPTS.get(category, "")
    return Agent(
        role=f"{category} Researcher",
        goal=(
            f"Score and rank the {category} articles by newsworthiness and "
            f"credibility, then enrich the top stories with a one-sentence "
            "'why it matters' insight grounded only in article facts."
        ),
        backstory=(
            f"You are an editor specialising in {category} for a New-York "
            "based tech executive's daily briefing. Your scoring rubric:\n\n"
            f"{scoring_prompt}\n\n"
            f"{_HALLUCINATION_GUARD}"
        ),
        tools=[score_tool, select_top_tool, enrich_tool],
        llm=openai_scorer_llm(),
        verbose=False,
        allow_delegation=False,
    )


# ---------------------------------------------------------------------------
# Steps 1–6 — Dedicated NY Sports Crew (Gatekeeper + Researcher + Fact-Checker)
# ---------------------------------------------------------------------------

_TRUSTED_SOURCES_LIST = ", ".join(sorted(SPORTS_TRUSTED_SOURCES))


def make_sports_gatekeeper() -> Agent:
    return Agent(
        role="NY Sports Gatekeeper",
        goal=(
            "Drop every sports article from a publisher not on the trusted-sources "
            "allowlist BEFORE the Researcher sees it. Untrusted aggregators (AOL, "
            "random blogs) recirculate stale fan-speculation content that has caused "
            "hallucinated scores and trades in past briefings."
        ),
        backstory=(
            "You are an obsessive newsroom standards editor. The only publishers "
            f"you allow for NY sports are: {_TRUSTED_SOURCES_LIST}. "
            "If an article has no source field, you drop it (fail-closed)."
        ),
        tools=[filter_sports_trusted_tool, filter_stale_tool],
        llm=openai_helper_llm(),
        verbose=False,
        allow_delegation=False,
    )


def make_sports_researcher() -> Agent:
    scoring_prompt = CATEGORY_SCORER_PROMPTS.get("NY Sports", "")
    return Agent(
        role="NY Sports Researcher",
        goal=(
            "Score and rank Yankees / Knicks / Giants / Devils stories. When a "
            "specific game outcome, standings position, or player status is "
            "implied but not stated in the gathered articles, use the ESPN tools "
            "to verify primary-source facts — never fill gaps with training knowledge."
        ),
        backstory=(
            "You are a sports desk editor with 20 years on the NY beat. You are "
            "the ONLY agent permitted to call the ESPN box-score, standings, and "
            "injury-report tools. Your scoring rubric:\n\n"
            f"{scoring_prompt}\n\n"
            f"{_HALLUCINATION_GUARD} {_TEAM_RULE}"
        ),
        tools=[
            score_tool,
            select_top_tool,
            enrich_tool,
            fetch_box_score_tool,
            fetch_standings_tool,
            fetch_injuries_tool,
        ],
        llm=openai_scorer_llm(),
        verbose=False,
        allow_delegation=False,
        # Cap LLM reasoning iterations. Worst case is 4 teams × 3 ESPN tools +
        # planning + synthesis ≈ 14 steps. We give a little headroom and stop
        # well below CrewAI's default of 25, so a quota-exhausted retry loop
        # can no longer eat 12+ minutes before failing.
        max_iter=15,
        max_execution_time=180,
    )


def make_sports_fact_checker() -> Agent:
    return Agent(
        role="NY Sports Fact-Checker",
        goal=(
            "For every sentence in the NY Sports section, verify the specific "
            "claim is directly supported by the source articles. Reject any "
            "patched draft that introduces an unsupported claim."
        ),
        backstory=(
            "You are a strict fact-checker. You break the writer's draft into "
            "individual factual sentences and check each one against the source "
            "articles using validate_claim_against_articles. You err on the "
            "side of cutting unsupported lines rather than including them."
        ),
        tools=[validate_claim_tool],
        llm=openai_helper_llm(),
        verbose=False,
        allow_delegation=False,
    )


# ---------------------------------------------------------------------------
# Step 7 — Picks Crew
# ---------------------------------------------------------------------------

def make_picks_processor() -> Agent:
    return Agent(
        role="Mike's Picks Processor",
        goal=(
            "Load every pending Mike's Pick from mikes_picks.json, fetch or extract "
            "its content (URL / PDF / text), produce a clean summary, and mark it "
            "processed so it isn't repeated tomorrow."
        ),
        backstory=(
            "You preserve the existing on-disk format of mikes_picks.json exactly "
            "so the dashboard and ingestion CLI keep working."
        ),
        tools=[process_picks_tool],
        llm=openai_helper_llm(),
        verbose=False,
        allow_delegation=False,
    )


# ---------------------------------------------------------------------------
# Step 8 — Writing Crew (Claude)
# ---------------------------------------------------------------------------

def make_html_writer() -> Agent:
    return Agent(
        role="HTML Briefing Writer",
        goal=(
            "Compose the daily HTML briefing email (1200–1800 words) covering "
            "Executive Summary, Top Stories by category, Key Trends, and What To Watch. "
            "Every story paragraph must end with a [Source Name](URL) link using the "
            "exact URL from the article data."
        ),
        backstory=(
            "You are MikeCast, a sharp daily briefing writer for a New-York tech "
            "executive. Professional yet engaging — like a smart friend who reads "
            "everything so the executive doesn't have to.\n\n"
            f"{_STORYTELLING_RULE}\n\n{_HALLUCINATION_GUARD}\n\n{_TEAM_RULE}\n\n"
            "Output the plain section text (no <html> wrapper) — the orchestrator "
            "wraps it in the styled template. Use section headers in ALL CAPS: "
            "EXECUTIVE SUMMARY, AI & TECH, BUSINESS & MARKETS, COMPANIES, NY SPORTS, "
            "KEY TRENDS & INSIGHTS, WHAT TO WATCH."
        ),
        tools=[],
        llm=claude_writer_llm(),
        verbose=False,
        allow_delegation=False,
    )


def make_single_voice_writer() -> Agent:
    return Agent(
        role="Single-Voice Podcast Writer",
        goal=(
            "Write a 10–14 minute single-host podcast script (target 1800–2000 words) "
            "in natural spoken language. Intro, AI & Tech, Business & Markets, "
            "Companies, NY Sports, Mike's Picks (if any), outro. No stage directions, "
            "no URLs."
        ),
        backstory=(
            "You write for MikeCast's solo host — smart, conversational, energetic, "
            "like a knowledgeable friend over coffee.\n\n"
            f"{_STORYTELLING_RULE}\n\n{_HALLUCINATION_GUARD}\n\n{_TEAM_RULE}"
        ),
        tools=[],
        llm=claude_writer_llm(),
        verbose=False,
        allow_delegation=False,
    )


def make_conversational_writer() -> Agent:
    return Agent(
        role="3-Voice Conversational Podcast Writer",
        goal=(
            "Write a 10–14 minute 3-host script tagged with [MIKE], [ELIZABETH], [JESSE]. "
            "MIKE intro + sign-off only. ELIZABETH covers AI & Tech, Business & Markets, "
            "Companies. JESSE covers NY Sports only. Hand-offs are explicit. "
            "Every speaker tag on its own line."
        ),
        backstory=(
            "You write for a 3-host news podcast. MIKE — warm, authoritative host. "
            "ELIZABETH — sharp tech/business correspondent. JESSE — quick-witted, "
            "NY-sports-obsessed.\n\n"
            f"{_STORYTELLING_RULE}\n\n{_HALLUCINATION_GUARD}\n\n{_TEAM_RULE}"
        ),
        tools=[],
        llm=claude_writer_llm(),
        verbose=False,
        allow_delegation=False,
    )


# ---------------------------------------------------------------------------
# Step 8b — Critic Crew (Scorer + Patcher)
# ---------------------------------------------------------------------------

def make_section_scorer() -> Agent:
    return Agent(
        role="Section Quality Scorer",
        goal=(
            "Score each category section of the HTML briefing 1-10 on depth, "
            "analysis, and substance. A score below 7 means the section needs to "
            "be rewritten by the Section Patcher."
        ),
        backstory=(
            "You are a quality editor for a daily news briefing. You score sections "
            "1-10 (7+ = acceptable). You return strict JSON: "
            '{"category_scores": {...}, "issues": {...}, "overall_passed": bool}.'
        ),
        tools=[],
        llm=openai_critic_llm(),
        verbose=False,
        allow_delegation=False,
    )


def make_section_patcher() -> Agent:
    return Agent(
        role="Section Patcher",
        goal=(
            "Rewrite a single weak HTML section using ONLY the articles for that "
            "category. Output <h3> + <p> fragments with deeper analysis (3-4 "
            "sentences per story) and specific facts from the source articles. "
            "Never patch the NY Sports section — its draft is final."
        ),
        backstory=(
            "You write replacement HTML fragments when a section scores below 7. "
            "You must use only the article inputs given — no training knowledge.\n\n"
            f"{_HALLUCINATION_GUARD}\n\n{_TEAM_RULE}"
        ),
        tools=[],
        llm=claude_writer_llm(temperature=0.3),
        verbose=False,
        allow_delegation=False,
    )
