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

from mc_config import CATEGORY_SCORER_PROMPTS

from crew.llm import (
    claude_writer_llm,
    openai_critic_llm,
    openai_helper_llm,
    openai_scorer_llm,
)
from crew.tools import (
    fetch_box_score_tool,
    fetch_injuries_tool,
    fetch_standings_tool,
    process_picks_tool,
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

_TONE_RULE = (
    "TONE RULE: Warm, informed, never condescending. The voice talks ABOUT the news, "
    "not AT the listener. NEVER use judgmental hyperbole aimed at the audience: no "
    "\"what are you doing with your life\", no \"if you don't watch you're missing out\", "
    "no \"you HAVE to\", no \"come on, people\", no \"are you kidding me\". NO profanity. "
    "NO politically charged jabs at people or groups. NO insult-as-joke framing. "
    "Enthusiasm is welcome; making the listener feel stupid for not watching, knowing, "
    "or caring about something is not. If a host wants to express excitement about a "
    "game, say WHY it matters (stakes, matchup, history), not that the listener is a "
    "loser for missing it."
)

_TTS_FRIENDLY_RULE = (
    "TTS RULE: This script is read aloud by ElevenLabs voices. Write numbers, times, "
    "scores, and dollar amounts the way a host SAYS them, not the way they'd be written:\n"
    "  • 7:05 PM ET  →  'seven-oh-five PM Eastern'\n"
    "  • 8:00 PM ET  →  'eight PM Eastern' or 'eight o\\'clock Eastern'\n"
    "  • 122-113      →  'one twenty-two to one thirteen'\n"
    "  • $10.9 billion →  'ten point nine billion dollars'\n"
    "  • 3,800 repos  →  'thirty-eight hundred repos'\n"
    "Do NOT use bare colons in times. Do NOT use bare hyphens between two numbers (it "
    "gets read as 'dash'). Do NOT include URLs. No markdown — no asterisks, no "
    "underscores, no square brackets except the [MIKE]/[ELIZABETH]/[JESSE] speaker tags "
    "themselves. No stage directions, no parenthetical asides like (laughing) or (clears "
    "throat). Only spoken words."
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
# NY Sports specialist crew (Researcher + Fact-Checker)
# ---------------------------------------------------------------------------
# Note: a Gatekeeper / Category-Researcher / Collection-Orchestrator agent used
# to live here. They were never instantiated by the live pipeline — the sports
# trusted-source filter and the per-category scorer both run as deterministic
# Python in research_crew.py / mc_collect.py. Adding LLM agents for either was
# pure latency and prompt-context cost. If you reintroduce them, give them work
# the legacy code cannot do (e.g. dynamic source discovery, cross-category
# context). Don't add an agent whose only job is to drive one deterministic
# function call.


def make_sports_researcher() -> Agent:
    scoring_prompt = CATEGORY_SCORER_PROMPTS.get("NY Sports", "")
    return Agent(
        role="NY Sports Researcher",
        goal=(
            "Decide which of the four NY teams (Yankees, Knicks, Giants, Devils) "
            "have a recent game outcome, standings position, or player status "
            "implied in today's gathered articles. For each such team, call the "
            "matching ESPN tool to retrieve the primary-source fact. Skip teams "
            "with no implied claim — never fill gaps with training knowledge."
        ),
        backstory=(
            "You are a sports desk editor with 20 years on the NY beat. You are "
            "the ONLY agent permitted to call the ESPN box-score, standings, and "
            "injury-report tools. Your scoring rubric:\n\n"
            f"{scoring_prompt}\n\n"
            f"{_HALLUCINATION_GUARD} {_TEAM_RULE}"
        ),
        # ONLY the ESPN tools — the legacy scorer/selector/enricher already ran
        # in research_crew.py before the Researcher sees these articles. Listing
        # them here just inflated every tool-choice prompt without changing
        # behavior, since the Task overrides the tool list anyway.
        tools=[fetch_box_score_tool, fetch_standings_tool, fetch_injuries_tool],
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
    """
    Used by critic_crew.run_critic_pass for observability on the NY Sports
    section. Sentences flagged 'no' by validate_claim_against_articles are
    logged at WARNING — NY Sports is still NEVER auto-patched, but operators
    get a daily signal when Claude drifts from source material.
    """
    return Agent(
        role="NY Sports Fact-Checker",
        goal=(
            "For every factual sentence in the NY Sports section of today's "
            "draft, verify the claim is directly supported by the source "
            "articles. Log unsupported sentences so operators see drift; the "
            "section itself is never auto-patched."
        ),
        backstory=(
            "You are a strict fact-checker. You break the writer's draft into "
            "individual factual sentences and check each one against the source "
            "articles using validate_claim_against_articles. You err on the "
            "side of flagging unsupported lines rather than passing them."
        ),
        tools=[validate_claim_tool],
        llm=openai_helper_llm(),
        verbose=False,
        allow_delegation=False,
        max_iter=20,
        max_execution_time=120,
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
            f"{_TONE_RULE}\n\n"
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
            f"{_STORYTELLING_RULE}\n\n{_HALLUCINATION_GUARD}\n\n{_TEAM_RULE}\n\n"
            f"{_TONE_RULE}\n\n{_TTS_FRIENDLY_RULE}"
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
            f"{_STORYTELLING_RULE}\n\n{_HALLUCINATION_GUARD}\n\n{_TEAM_RULE}\n\n"
            f"{_TONE_RULE}\n\n{_TTS_FRIENDLY_RULE}\n\n"
            "JESSE in particular: enthusiasm yes, condescension at the listener no. "
            "If the Knicks are playing a big game, say what's at stake — not that the "
            "listener doesn't have a life if they're not tuned in."
        ),
        tools=[],
        llm=claude_writer_llm(),
        verbose=False,
        allow_delegation=False,
    )


# ---------------------------------------------------------------------------
# Step 11 — Distribution Crew (social copywriter)
# ---------------------------------------------------------------------------

def make_social_copywriter() -> Agent:
    """
    Writes short-form platform copy (one X post + one Instagram caption) that
    teases the day's briefing and links back to that day's episode. Uses the same
    Claude writer LLM as the briefing writers and inherits the hallucination
    guardrail — it may only reference the headlines it is handed.
    """
    return Agent(
        role="Social Media Copywriter",
        goal=(
            "Turn today's MikeCast briefing into one punchy X (Twitter) post and one "
            "Instagram caption that make a busy reader want to open the episode. Both "
            "must be accurate to the supplied headlines and drive to the episode link."
        ),
        backstory=(
            "You are a sharp social copywriter for MikeCast, a daily AI-and-news "
            "briefing. You write tight, specific, scroll-stopping copy — you name the "
            "real companies and stories, never vague hype.\n\n"
            f"{_HALLUCINATION_GUARD}\n\n"
            "HARD CONSTRAINTS:\n"
            "  • X post: <=230 characters of body text (a link is appended separately, "
            "so leave room). One or two of the day's biggest hooks, then a soft CTA. "
            "Up to 2 relevant hashtags. Do NOT include a URL — the orchestrator appends it.\n"
            "  • Instagram caption: <=1800 characters. Lead with the strongest hook, then "
            "2-4 short lines on the top stories, then 'Full briefing at mikecast.io'. Up to "
            "8 hashtags at the very end. Do NOT include a URL in the caption (IG doesn't "
            "linkify them).\n"
            "  • No engagement-bait ('tag a friend', 'comment below', 'you won't believe'). "
            "No invented numbers, quotes, or stories — only what the headlines say.\n\n"
            "Return STRICT JSON only, no prose and no code fences:\n"
            '{"x_text": "...", "ig_caption": "..."}'
        ),
        tools=[],
        llm=claude_writer_llm(temperature=0.6, max_tokens=1200),
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
