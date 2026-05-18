"""
LLM factory for the CrewAI crews.

Returns CrewAI `LLM` instances pointing at the right provider for each role:

  - Writers (HTML, single-voice, conversational)  → Claude
  - Scorers, critic, planner                     → GPT-4o
  - Helpers (gatekeeper, fact-checker, enrich)   → GPT-4o-mini

Model strings are LiteLLM-style ("anthropic/claude-sonnet-4-6", "openai/gpt-4o")
and pinned via env vars in mc_config.py so we can swap models without code edits.
"""

from __future__ import annotations

from crewai import LLM

from mc_config import (
    ANTHROPIC_API_KEY,
    CLAUDE_WRITER_MODEL,
    OPENAI_API_KEY,
    OPENAI_CRITIC_MODEL,
    OPENAI_HELPER_MODEL,
    OPENAI_SCORER_MODEL,
)


def claude_writer_llm(temperature: float = 0.4, max_tokens: int = 6000) -> LLM:
    """
    Claude Sonnet for the three writer agents (long-form prose).

    max_tokens defaults to 6000 — comfortable headroom for the HTML briefing
    (~1500 words ≈ 2400 tokens) and the long-form podcast script even after
    the agent framing overhead. The previous 4000 cap risked truncating
    mid-script with the critic unable to detect it.
    """
    return LLM(
        model=CLAUDE_WRITER_MODEL,
        api_key=ANTHROPIC_API_KEY or None,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def openai_scorer_llm(temperature: float = 0.2) -> LLM:
    """GPT-4o for per-category scorers (structured JSON judgments)."""
    return LLM(
        model=OPENAI_SCORER_MODEL,
        api_key=OPENAI_API_KEY or None,
        temperature=temperature,
        max_tokens=2000,
    )


def openai_critic_llm(temperature: float = 0.2) -> LLM:
    """GPT-4o for the section critic / scorer."""
    return LLM(
        model=OPENAI_CRITIC_MODEL,
        api_key=OPENAI_API_KEY or None,
        temperature=temperature,
        max_tokens=1500,
    )


def openai_helper_llm(temperature: float = 0.2) -> LLM:
    """GPT-4o-mini for cheap helpers (gatekeeper, fact-checker, picks summariser)."""
    return LLM(
        model=OPENAI_HELPER_MODEL,
        api_key=OPENAI_API_KEY or None,
        temperature=temperature,
        max_tokens=800,
    )
