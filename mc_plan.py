"""
MikeCast — xAI Grok adaptive search planning.

Calls Grok-3 (with live web search) to identify today's breaking stories
and generate targeted Google News queries for each category.

NOTE: Grok is used ONLY for generating search query strings — NOT for
returning article content. LLMs hallucinate plausible-sounding article
facts (fake URLs, scores, player names) when asked to return structured
article data. All actual article content must come from real RSS/API fetches.

Returns (dynamic_queries, trending_context, trending_stories) where:
  - dynamic_queries: {category: [query, ...]} to supplement static CATEGORIES
  - trending_context: short paragraph summarising today's breaking news
    (passed to scoring agents to weight fresh stories higher)
  - trending_stories: list of up to 5 short descriptive phrases for top
    cross-category breaking stories (used to build the Trending section)

Gracefully skips (returns ({}, "", [])) if XAI_API_KEY is not set or any error occurs.
"""

import json
import logging

from mc_config import TODAY_DISPLAY, XAI_API_KEY

logger = logging.getLogger(__name__)

_CATEGORIES = ["AI & Tech", "Business & Markets", "Companies", "NY Sports"]

_SYSTEM_PROMPT = (
    "You are a news research assistant with live web search. "
    "Identify the most important breaking stories happening RIGHT NOW across four categories. "
    "Return ONLY valid JSON — no preamble, no markdown, no explanation outside the JSON object."
)

_USER_PROMPT = (
    f"Today is {TODAY_DISPLAY}. Search the web right now.\n\n"
    "For each category below, identify the top 3-5 most important BREAKING stories "
    "happening today — things that have genuinely moved or matter in the last 24 hours.\n\n"
    "Categories: AI & Tech, Business & Markets, Companies, NY Sports\n\n"
    'Return ONLY this JSON structure:\n'
    '{\n'
    '  "AI & Tech": ["query1", "query2", ...],\n'
    '  "Business & Markets": ["query1", "query2", ...],\n'
    '  "Companies": ["query1", "query2", ...],\n'
    '  "NY Sports": ["query1", "query2", ...],\n'
    '  "trending_stories": ["phrase1", "phrase2", ...]\n'
    '}\n\n'
    "Each query must be specific enough to find the story directly "
    "(e.g. 'OpenAI GPT-5 release today' not just 'OpenAI'). "
    "Focus on what is genuinely NEW and breaking today, not evergreen topics.\n\n"
    'Also return a "trending_stories" key: a list of the 5 most important CROSS-CATEGORY '
    "breaking stories happening right now. Each entry is a short descriptive phrase "
    "(10-15 words) describing WHAT the story is — not a search query. "
    'Example: "Senate passes sweeping AI regulation bill with bipartisan support"'
)


def plan_daily_searches() -> tuple[dict[str, list[str]], str, list[str]]:
    """
    Call xAI Grok-3 to identify today's breaking stories and generate
    targeted search queries for each MikeCast category.

    Returns:
        (dynamic_queries, trending_context, trending_stories)
        - dynamic_queries: {category: [query, ...]} — appended to static CATEGORIES
        - trending_context: short paragraph for scoring agents (may be "")
        - trending_stories: up to 5 descriptive phrases for top breaking stories
        On any failure, returns ({}, "", []).
    """
    if not XAI_API_KEY:
        logger.info("XAI_API_KEY not set — skipping adaptive search planning.")
        return {}, "", []

    try:
        from openai import OpenAI
        client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

        logger.info("Calling xAI Grok-3 for today's breaking stories…")
        resp = client.chat.completions.create(
            model="grok-3",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": _USER_PROMPT},
            ],
            max_tokens=1000,
            temperature=0.3,
        )

        raw = resp.choices[0].message.content.strip()

        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(
                line for line in lines
                if not line.strip().startswith("```")
            ).strip()

        dynamic_queries: dict[str, list[str]] = json.loads(raw)

        cleaned: dict[str, list[str]] = {}
        for cat in _CATEGORIES:
            queries = dynamic_queries.get(cat, [])
            if isinstance(queries, list):
                valid = [q for q in queries if isinstance(q, str) and q.strip()]
                if valid:
                    cleaned[cat] = valid

        raw_trending = dynamic_queries.pop("trending_stories", [])
        trending_stories = [s for s in raw_trending if isinstance(s, str) and s.strip()][:5]

        total_queries = sum(len(v) for v in cleaned.values())
        logger.info(
            "Grok planning complete: %d dynamic queries across %d categories, "
            "%d trending stories.",
            total_queries, len(cleaned), len(trending_stories),
        )

        trending_context = _build_trending_context(cleaned)
        return cleaned, trending_context, trending_stories

    except Exception as exc:
        logger.warning("xAI Grok planning failed (non-fatal): %s", exc)
        return {}, "", []


def _build_trending_context(dynamic_queries: dict[str, list[str]]) -> str:
    if not dynamic_queries:
        return ""
    parts: list[str] = []
    for cat, queries in dynamic_queries.items():
        if queries:
            joined = "; ".join(queries[:3])
            parts.append(f"{cat}: {joined}")
    if not parts:
        return ""
    return (
        "Today's breaking stories (from live web search via Grok):\n"
        + "\n".join(f"  • {p}" for p in parts)
    )
