"""
Model-compatibility helpers shared across the CrewAI pipeline and the eval
harness (eval/run_eval.py, crew/critic_crew.py, crew/tools.py).

Newer-generation models reject parameters older models accept without
complaint — confirmed 2026-09-13 (see MODEL_EVAL_PLAN.md):
  - claude-sonnet-5 / gpt-5.6-* reject a non-default `temperature`.
  - gpt-5.6-* rejects `max_tokens` on the raw OpenAI SDK (use
    `max_completion_tokens`, or route through LiteLLM, which normalizes this
    per-provider automatically).

Centralized here so these rules don't drift across the three call sites that
need them.
"""

from __future__ import annotations

from mc_config import ANTHROPIC_API_KEY, OPENAI_API_KEY

_NO_CUSTOM_TEMPERATURE_MODELS = {
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-5",
    "anthropic/claude-fable-5",
    "anthropic/claude-fable-5-1",
}
_NO_CUSTOM_TEMPERATURE_PREFIXES = ("openai/gpt-5.6-", "openai/gpt-6-")


def supports_custom_temperature(model: str) -> bool:
    if model in _NO_CUSTOM_TEMPERATURE_MODELS:
        return False
    return not model.startswith(_NO_CUSTOM_TEMPERATURE_PREFIXES)


def api_key_for_model(model: str) -> str | None:
    """Derive the right API key from a LiteLLM-style "provider/model" string,
    rather than assuming a fixed provider — needed anywhere a model string is
    swappable across providers (env var override, eval harness candidates)."""
    if model.startswith("anthropic/"):
        return ANTHROPIC_API_KEY or None
    if model.startswith("openai/"):
        return OPENAI_API_KEY or None
    return None
