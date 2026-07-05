"""
Step 11 — Distribution Crew (social copy only).

Produces the short-form copy for the daily X post and Instagram caption from an
episode's data. Posting itself is deterministic Python in `mc_social.py`; this
module only writes words.

Mirrors the single-task pattern in `crew/writing_crew.py`: one Claude agent, one
Task, strict-JSON output that we defensively parse (stripping ``` fences the way
`run_writing` does). On any failure the caller (`mc_social.run_social_distribution`)
falls back to a deterministic template, so an LLM outage never blocks posting.
"""

from __future__ import annotations

import json
import logging
import re

from crewai import Crew, Process, Task

from crew.agents import make_social_copywriter
from mc_config import TODAY_DISPLAY

logger = logging.getLogger("mikecast.crew.distribution")


def _headlines_block(episode_data: dict, per_cat: int = 2, cap: int = 8) -> str:
    """Top ~8 headlines (2 per category) — same extraction as mc_ad.generate_ad_script."""
    articles = episode_data.get("articles", {}) or {}
    headlines: list[str] = []
    for cat, arts in articles.items():
        for a in (arts or [])[:per_cat]:
            title = (a.get("title") or "").strip()
            if title:
                headlines.append(f"[{cat}] {title}")
    return "\n".join(headlines[:cap])


def _strip_json(raw: str) -> dict:
    """Parse the agent's JSON output, tolerating ``` fences and surrounding prose."""
    text = re.sub(r"```\w*\n?", "", raw or "").strip()
    # Grab the outermost {...} if the model wrapped it in prose.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


def run_distribution(episode_data: dict, link: str) -> dict:
    """
    Generate social copy for one episode.

    Returns {"x_text": str, "ig_caption": str}. Raises on total failure — the
    caller catches and falls back to a deterministic template.
    """
    episode_num = episode_data.get("episode_num", "?")
    date_display = episode_data.get("date_display", TODAY_DISPLAY)
    description = (episode_data.get("episode_description") or "").strip()
    headlines = _headlines_block(episode_data)

    description_block = f"Episode description: {description}\n\n" if description else ""

    desc = (
        f"Write social copy for MikeCast episode #{episode_num} ({date_display}).\n\n"
        f"{description_block}"
        f"Today's top headlines (use ONLY these — do not invent others):\n{headlines}\n\n"
        f"The episode link (do NOT put it in the copy — the system appends it): {link}\n\n"
        "Produce one X (Twitter) post and one Instagram caption per your constraints. "
        "Return STRICT JSON only: {\"x_text\": \"...\", \"ig_caption\": \"...\"}"
    )
    expected = 'Strict JSON with keys "x_text" and "ig_caption".'

    agent = make_social_copywriter()
    task = Task(description=desc, expected_output=expected, agent=agent)
    crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)

    result = crew.kickoff()
    raw = getattr(result, "raw", None) or str(result)
    parsed = _strip_json(raw)

    x_text = (parsed.get("x_text") or "").strip()
    ig_caption = (parsed.get("ig_caption") or "").strip()
    if not x_text and not ig_caption:
        raise ValueError(f"Copywriter returned empty copy: {raw!r}")

    logger.info(
        "[Distribution Crew] x_text=%d chars, ig_caption=%d chars",
        len(x_text), len(ig_caption),
    )
    return {"x_text": x_text, "ig_caption": ig_caption}
