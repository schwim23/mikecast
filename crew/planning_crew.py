"""
Step 0 — Planning Crew.

In the legacy pipeline, mc_plan.plan_daily_searches() makes one Grok call
and returns (dynamic_queries, trending_context, trending). The CrewAI
version preserves that interface — the Planner agent exists primarily as
a named persona so the rest of the pipeline reads consistently, but the
actual Grok call still happens via the xai_grok_search tool.

We deliberately keep this lightweight: there is no judgment for an LLM to
add on top of Grok's structured JSON output. Wrapping the call in a full
Crew + Task would add tokens and latency for zero quality gain.
"""

from __future__ import annotations

import logging

from crew.tools import xai_grok_search_tool

logger = logging.getLogger("mikecast.crew.planning")


def run_planning() -> tuple[dict[str, list[str]], str, list[dict]]:
    """
    Execute the planning step. Returns the same 3-tuple shape as
    mc_plan.plan_daily_searches() so mikecast_briefing.py is interface-compatible.
    """
    logger.info("[Planning Crew] invoking xai_grok_search…")
    result = xai_grok_search_tool._run()  # direct invocation — same fallback semantics
    dynamic_queries = result.get("dynamic_queries", {}) or {}
    trending_context = result.get("trending_context", "") or ""
    trending = result.get("trending", []) or []
    logger.info(
        "[Planning Crew] result: %d dynamic queries, %d trending stories",
        sum(len(v) for v in dynamic_queries.values()),
        len(trending),
    )
    return dynamic_queries, trending_context, trending
