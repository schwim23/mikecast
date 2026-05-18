"""
MikeCast — xAI Grok adaptive search planning.

Calls Grok-3 (with live web search) to identify today's breaking stories
and generate targeted Google News queries for each category.

NOTE: Grok is used ONLY for generating search query strings — NOT for
returning article content. LLMs hallucinate plausible-sounding article
facts (fake URLs, scores, player names) when asked to return structured
article data. All actual article content must come from real RSS/API fetches.

Returns (dynamic_queries, trending_context, trending) where:
  - dynamic_queries: {category: [query, ...]} to supplement static CATEGORIES
  - trending_context: short paragraph summarising today's breaking news
    (passed to scoring agents to weight fresh stories higher)
  - trending: list of up to 5 dicts {topic, x_url} for the top AI/tech/world
    breaking stories, each linked to the most popular X post about that story

Gracefully skips (returns ({}, "", [])) if XAI_API_KEY is not set or any error occurs.
"""

import json
import logging
from urllib.parse import quote_plus

from mc_config import TODAY_DISPLAY, XAI_API_KEY

logger = logging.getLogger(__name__)

_CATEGORIES = ["AI & Tech", "Business & Markets", "Companies", "NY Sports"]

_SYSTEM_PROMPT = (
    "You are a news research assistant with live web search and direct access to X (Twitter) posts. "
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
    '  "trending_stories": [\n'
    '    {"topic": "short phrase describing the story", "x_url": "https://x.com/...", "x_query": "keyword1 keyword2 keyword3"}\n'
    '  ]\n'
    '}\n\n'
    "Each search query must be specific enough to find the story directly. "
    "Focus on what is genuinely NEW and breaking today, not evergreen topics.\n\n"
    'Also return a "trending_stories" key: the 5 most important trending stories in '
    "AI, tech, and world news RIGHT NOW. For each story:\n"
    '- "topic": a short descriptive phrase (10-15 words) describing WHAT the story is\n'
    '- "x_url": the URL of the single most popular or most relevant X post about this '
    "story (real https://x.com/... URL you can verify; set to \"\" if not found)\n"
    '- "x_query": 3-5 specific keywords optimized for searching this story on X '
    '(e.g. "OpenAI GPT-5 launch" or "Fed rate cut March 2026") — used as search fallback\n\n'
    "IMPORTANT: Only include a trending story if you are highly confident it actually occurred "
    "today. If you are uncertain whether a specific event happened (e.g. a product launch, "
    "a rate decision, a policy announcement), omit it rather than include it. "
    "Do not speculate or extrapolate from prior trends."
)


def plan_daily_searches() -> tuple[dict[str, list[str]], str, list[dict]]:
    """
    Call xAI Grok-3 to identify today's breaking stories and generate
    targeted search queries for each MikeCast category.

    Returns:
        (dynamic_queries, trending_context, trending)
        - dynamic_queries: {category: [query, ...]} — appended to static CATEGORIES
        - trending_context: short paragraph for scoring agents (may be "")
        - trending: up to 5 dicts {topic, x_url} for top AI/tech/world stories
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
            max_tokens=1500,
            temperature=0.3,
            response_format={"type": "json_object"},
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
        trending: list[dict] = []
        for item in raw_trending[:5]:
            if isinstance(item, dict) and item.get("topic", "").strip():
                topic   = item["topic"].strip()
                x_url   = item.get("x_url", "").strip()
                x_query = item.get("x_query", "").strip()
                # Build search URL: prefer verified post, then x_query keywords,
                # then full topic phrase. Drop &f=live — "Top" results surface
                # popular posts better than real-time-only filter.
                if not x_url:
                    search_term = x_query if x_query else topic
                    x_url = "https://x.com/search?q=" + quote_plus(search_term)
                trending.append({"topic": topic, "x_url": x_url})

        total_queries = sum(len(v) for v in cleaned.values())
        logger.info(
            "Grok planning complete: %d dynamic queries across %d categories, "
            "%d trending stories.",
            total_queries, len(cleaned), len(trending),
        )
        # When Grok parses fine but yields zero queries AND zero trending, the
        # response shape is almost certainly off (wrong category keys, missing
        # arrays). Surface the raw response so future flakes are diagnosable
        # without rerunning a 7-minute pipeline.
        if total_queries == 0 and not trending:
            logger.warning(
                "Grok returned 0 queries and 0 trending — raw response below for diagnosis:\n%s",
                raw[:2000],
            )

        trending_context = _build_trending_context(cleaned)
        return cleaned, trending_context, trending

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
