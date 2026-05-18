"""
Shared prompt-context helpers for the writing + critic crews.

These functions are re-exports of the legacy helpers in mc_generate so we
keep one source of truth: every escape, every "discuss ONLY these" caption,
every trending-topic filter behaves exactly as it does in the legacy path.
The crews layer judgment on top of these — they never re-implement them.
"""

from __future__ import annotations

# These helpers are stateless and well-tested in the legacy path. Re-exporting
# avoids divergence.
from mc_generate import (  # noqa: F401
    _build_articles_context,
    _build_trending_html,
    _build_trending_prompt_block,
    _filter_trending_to_articles,
    _safe_url,
)
