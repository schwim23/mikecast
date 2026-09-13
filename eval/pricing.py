"""
Model pricing table for the eval harness — $ per 1M tokens (input, output).

This is a point-in-time snapshot, not a live lookup. Refresh it whenever pricing
changes or a new candidate model is added (see MODEL_EVAL_PLAN.md §5 — "Cost
pricing goes stale"). Sourced 2026-09-13: Anthropic via the claude-api skill's
model table, OpenAI via developers.openai.com/api/docs/pricing and
developers.openai.com/api/docs/models.
"""

from __future__ import annotations

# LiteLLM-style model string -> (input $/1M tokens, output $/1M tokens)
PRICING: dict[str, tuple[float, float]] = {
    # Writer role (baseline + candidates)
    "anthropic/claude-sonnet-4-6": (3.00, 15.00),
    "anthropic/claude-sonnet-5": (2.00, 10.00),
    "openai/gpt-5.6-sol": (4.00, 20.00),
    # Critic role (baseline + candidates)
    "openai/gpt-4o": (2.50, 10.00),
    "openai/gpt-5.6-terra": (2.00, 12.00),
    # (anthropic/claude-sonnet-5 candidate for critic reuses the writer entry above)
    # Helper role (baseline + candidate) — OpenAI-only, see MODEL_EVAL_PLAN.md §1b
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-5.6-luna": (0.20, 1.20),
    # Available but not in the current eval matrix
    "anthropic/claude-haiku-4-5": (1.00, 5.00),
    "openai/gpt-6-astra": (10.00, 50.00),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    if model not in PRICING:
        raise KeyError(f"No pricing entry for model {model!r} — add it to eval/pricing.py")
    in_rate, out_rate = PRICING[model]
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
