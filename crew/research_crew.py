"""
Steps 1–6 — Research Crew (non-sports categories).

Replaces the procedural pipeline in mc_collect.py:

    collect_all_news → deduplicate → filter_stale → filter_sports_trusted
                     → cluster_articles → score_and_rank_articles
                     → select_top_articles → enrich_top_stories

We keep the dict-transformation plumbing in Python and only invoke real
CrewAI agents where they add judgment value. The legacy functions are
already battle-tested, parallel-safe, and well-instrumented — wrapping
them in LLM-driven tasks would add latency without quality gain.

NY Sports is handled separately in sports_research_crew.py, which runs
the Gatekeeper + Researcher (with ESPN tools) + Fact-Checker.
"""

from __future__ import annotations

import logging

from mc_collect import (
    cluster_articles,
    collect_all_news,
    deduplicate,
    enrich_top_stories,
    filter_sports_by_trusted_sources,
    filter_stale_articles,
    score_and_rank_articles,
    select_top_articles,
)

logger = logging.getLogger("mikecast.crew.research")


def run_research(
    dynamic_queries: dict[str, list[str]] | None = None,
    trending_context: str = "",
    *,
    total_target: int = 25,
    enrich_top_n: int = 15,
) -> tuple[dict[str, list[dict]], dict[str, int]]:
    """
    Run the full research pipeline (Steps 1–6) for ALL categories, including
    NY Sports. The NY Sports section is then refined by sports_research_crew.

    Returns (top_articles, stats) — top_articles is
    {category: [ranked + enriched article dicts]}; stats is
    {"raw_collected": int, "deduped": int, "selected": int} for metrics.
    """
    logger.info("[Research Crew] Step 1: collect_all_news (dynamic_queries=%d cats)",
                len(dynamic_queries or {}))
    raw_news = collect_all_news(dynamic_queries=dynamic_queries or None)
    raw_total = sum(len(v) for v in raw_news.values())
    if raw_total == 0:
        logger.critical("[Research Crew] No articles collected — returning empty.")
        return {}, {"raw_collected": 0, "deduped": 0, "selected": 0}

    logger.info("[Research Crew] Step 2: deduplicate (raw=%d)", raw_total)
    deduped = deduplicate(raw_news)

    logger.info("[Research Crew] Step 2b: filter_stale_articles (max_age_days=3)")
    deduped = filter_stale_articles(deduped, max_age_days=3)

    logger.info("[Research Crew] Step 2c: filter_sports_by_trusted_sources")
    deduped = filter_sports_by_trusted_sources(deduped)
    deduped_total = sum(len(v) for v in deduped.values())

    logger.info("[Research Crew] Step 3: cluster_articles")
    clustered = cluster_articles(deduped)

    logger.info("[Research Crew] Step 4: score_and_rank_articles")
    scored = score_and_rank_articles(clustered, trending_context=trending_context)

    logger.info("[Research Crew] Step 5: select_top_articles (target=%d)", total_target)
    selected = select_top_articles(scored, total=total_target)
    total = sum(len(v) for v in selected.values())
    logger.info("[Research Crew] Selected %d articles across %d categories",
                total, len(selected))

    logger.info("[Research Crew] Step 6: enrich_top_stories (top_n=%d)", enrich_top_n)
    enriched = enrich_top_stories(selected, top_n=enrich_top_n)

    stats = {"raw_collected": raw_total, "deduped": deduped_total, "selected": total}
    return enriched, stats
