"""
MikeCast — xAI Grok adaptive search planning + live article fetching.

Two Grok calls per run:

  1. fetch_grok_articles() — asks Grok to use its live web search to return
     actual article facts (title, summary, url, source) for all four categories.
     These are injected directly into the pipeline as verified, current articles.
     Sports articles come EXCLUSIVELY from this call + NYT (no Google News RSS
     or Reddit for sports, which pull stale/unreliable content).

  2. plan_daily_searches() — asks Grok for targeted Google News query strings
     for AI & Tech, Business & Markets, and Companies (not Sports).
     Returns (dynamic_queries, trending_context).

Both calls gracefully skip if XAI_API_KEY is not set or any error occurs.
"""

import json
import logging
from datetime import datetime, timezone

from mc_config import TODAY_DISPLAY, XAI_API_KEY

logger = logging.getLogger(__name__)

_CATEGORIES = ["AI & Tech", "Business & Markets", "Companies", "NY Sports"]


# ---------------------------------------------------------------------------
# Call 1: Grok fetches actual article content via live search
# ---------------------------------------------------------------------------

_ARTICLES_SYSTEM = (
    "You are a live news research assistant. Use your real-time web search to find "
    "today's most important news stories. Return ONLY valid JSON — no preamble, "
    "no markdown, no explanation outside the JSON."
)

_ARTICLES_USER = (
    f"Today is {TODAY_DISPLAY}. Search the web RIGHT NOW for today's top news.\n\n"
    "For each category return the 4-6 most important stories published or updated "
    "in the last 24 hours. For NY Sports, focus on New York teams: Yankees, Knicks, "
    "Giants, Devils — report only what actually happened TODAY (scores, injuries, "
    "trades, signings, game previews). Do not include old news.\n\n"
    "Return ONLY this exact JSON structure:\n"
    "{\n"
    '  "AI & Tech": [\n'
    '    {"title": "...", "summary": "2-3 sentence factual summary", '
    '"url": "https://...", "source": "Publisher Name"},\n'
    "    ...\n"
    "  ],\n"
    '  "Business & Markets": [...],\n'
    '  "Companies": [...],\n'
    '  "NY Sports": [...]\n'
    "}\n\n"
    "Rules:\n"
    "- Every article must have a real URL from a real news source.\n"
    "- Summary must contain only facts you found on the web today — no training knowledge.\n"
    "- If there is no NY Sports news today for a team, omit that team entirely.\n"
    "- Do not include articles older than 48 hours."
)


def fetch_grok_articles() -> dict[str, list[dict]]:
    """
    Call Grok-3 with live search to get actual article facts for all categories.

    Returns {category: [article_dicts]} ready to inject into the pipeline.
    Each article dict has: title, description, url, source, published.
    On any failure returns {}.
    """
    if not XAI_API_KEY:
        logger.info("XAI_API_KEY not set — skipping Grok article fetch.")
        return {}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

        logger.info("Calling Grok-3 for live article content…")
        resp = client.chat.completions.create(
            model="grok-3",
            messages=[
                {"role": "system", "content": _ARTICLES_SYSTEM},
                {"role": "user",   "content": _ARTICLES_USER},
            ],
            max_tokens=2000,
            temperature=0.2,
        )

        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()

        data: dict = json.loads(raw)
        now_iso = datetime.now(timezone.utc).isoformat()

        result: dict[str, list[dict]] = {}
        for cat in _CATEGORIES:
            articles = data.get(cat, [])
            if not isinstance(articles, list):
                continue
            cleaned = []
            for a in articles:
                title   = str(a.get("title",   "")).strip()
                summary = str(a.get("summary", "")).strip()
                url     = str(a.get("url",     "")).strip()
                source  = str(a.get("source",  "xAI Grok")).strip()
                if title and url and url.startswith("http"):
                    cleaned.append({
                        "title":       title,
                        "description": summary,
                        "url":         url,
                        "source":      source,
                        "published":   now_iso,
                        "grok_verified": True,
                    })
            if cleaned:
                result[cat] = cleaned
                logger.info("Grok articles [%s]: %d", cat, len(cleaned))

        total = sum(len(v) for v in result.values())
        logger.info("Grok article fetch complete: %d articles across %d categories.", total, len(result))
        return result

    except Exception as exc:
        logger.warning("Grok article fetch failed (non-fatal): %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Call 2: Grok generates targeted Google News queries (non-sports only)
# ---------------------------------------------------------------------------

_QUERIES_SYSTEM = (
    "You are a news research assistant with live web search. "
    "Identify the most important breaking stories happening RIGHT NOW. "
    "Return ONLY valid JSON — no preamble, no markdown, no explanation outside the JSON object."
)

_QUERIES_USER = (
    f"Today is {TODAY_DISPLAY}. Search the web right now.\n\n"
    "For each category below, identify the top 3-5 most important BREAKING stories "
    "happening today — things that have genuinely moved or matter in the last 24 hours.\n\n"
    "Categories: AI & Tech, Business & Markets, Companies\n\n"
    "(NY Sports is handled separately — do not include it here.)\n\n"
    'Return ONLY this JSON structure:\n'
    '{\n'
    '  "AI & Tech": ["specific query 1", "specific query 2", ...],\n'
    '  "Business & Markets": ["specific query 1", ...],\n'
    '  "Companies": ["specific query 1", ...]\n'
    '}\n\n'
    "Each query must be specific enough to find the story directly "
    "(e.g. 'OpenAI GPT-5 release today' not just 'OpenAI'). "
    "Focus on what is genuinely NEW and breaking today, not evergreen topics."
)


def plan_daily_searches() -> tuple[dict[str, list[str]], str]:
    """
    Call Grok-3 to identify today's breaking stories and generate targeted
    Google News search queries for AI & Tech, Business & Markets, and Companies.
    (NY Sports is excluded — covered by fetch_grok_articles() + NYT.)

    Returns:
        (dynamic_queries, trending_context)
        - dynamic_queries: {category: [query, ...]} — appended to static CATEGORIES
        - trending_context: short paragraph for scoring agents (may be "")
        On any failure, returns ({}, "").
    """
    if not XAI_API_KEY:
        logger.info("XAI_API_KEY not set — skipping adaptive search planning.")
        return {}, ""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")

        logger.info("Calling Grok-3 for today's search queries…")
        resp = client.chat.completions.create(
            model="grok-3",
            messages=[
                {"role": "system", "content": _QUERIES_SYSTEM},
                {"role": "user",   "content": _QUERIES_USER},
            ],
            max_tokens=600,
            temperature=0.3,
        )

        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()

        dynamic_queries: dict[str, list[str]] = json.loads(raw)

        # Validate: only non-sports categories, ensure list of strings
        non_sports = ["AI & Tech", "Business & Markets", "Companies"]
        cleaned: dict[str, list[str]] = {}
        for cat in non_sports:
            queries = dynamic_queries.get(cat, [])
            if isinstance(queries, list):
                valid = [q for q in queries if isinstance(q, str) and q.strip()]
                if valid:
                    cleaned[cat] = valid

        total_queries = sum(len(v) for v in cleaned.values())
        logger.info(
            "Grok query planning complete: %d queries across %d categories.",
            total_queries, len(cleaned),
        )

        trending_context = _build_trending_context(cleaned)
        return cleaned, trending_context

    except Exception as exc:
        logger.warning("xAI Grok planning failed (non-fatal): %s", exc)
        return {}, ""


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
