"""
Step 7 — Mike's Picks Crew.

The legacy implementation (mc_collect.process_picks) already does all the
work — load picks from S3 or disk, summarise URLs / PDFs / raw text, and
mark them processed. There is no per-pick judgment for an LLM to add, so
this crew is a thin pass-through that preserves the legacy on-disk format.
"""

from __future__ import annotations

import logging

from mc_collect import process_picks

logger = logging.getLogger("mikecast.crew.picks")


def run_picks() -> list[dict]:
    """Load, summarise, and mark-processed all pending Mike's Picks."""
    picks = process_picks()
    logger.info("[Picks Crew] processed %d pick(s)", len(picks))
    return picks
