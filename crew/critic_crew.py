"""
Step 8b — Critic Crew.

Scorer (GPT-4o) reads the produced HTML briefing and grades each category
section 1-10. Sections with score < 7 are flagged as weak. A Patcher
(Claude) rewrites only the weak sections, except NY Sports — which is
NEVER patched (matches the legacy NEVER_PATCH_NORMALIZED guard in
mc_critic.py). If the NY Sports section were ever patched in a future
iteration, the Fact-Checker (validate_claim_against_articles) would gate
the patch — for now, the policy of "leave NY Sports alone" is preserved
verbatim from the legacy path.

After any HTML patch, both podcast scripts are regenerated against the
(unchanged) article set — same semantics as legacy.

max_iter = 1 (no critic loop).
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from crewai import Crew, Process, Task

from crew.agents import make_section_patcher, make_section_scorer
from crew.writing_crew import (
    _conversational_task,
    _kickoff_single_task,
    _single_voice_task,
)
from crew.agents import (
    make_conversational_writer,
    make_single_voice_writer,
)
from mc_critic import _extract_html_summary  # reuse the legacy HTML summariser

logger = logging.getLogger("mikecast.crew.critic")

# NY Sports section is never patched — matches legacy invariant.
_NEVER_PATCH_NORMALIZED = {"ny sports"}
_WEAK_THRESHOLD = 7


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

def _run_scorer(html: str, categorised: dict[str, list[dict]]) -> dict:
    summary = _extract_html_summary(html, categorised)
    scorer = make_section_scorer()
    task = Task(
        description=(
            "Below is a compact summary of today's MikeCast HTML briefing. Score each "
            "category section 1-10 on:\n"
            "  - depth: does it have 3+ substantive stories?\n"
            "  - analysis: does it go beyond mere headlines?\n"
            "  - substance: does it include specific facts, numbers, or implications?\n\n"
            "A score of 7+ means acceptable. Below 7 means the section needs improvement.\n\n"
            f"BRIEFING SUMMARY:\n{summary}\n\n"
            "Return ONLY valid JSON (no markdown, no commentary):\n"
            '{"category_scores": {"AI & Tech": 8, ...}, '
            '"issues": {"Business & Markets": "Only 1 story, lacks analysis"}, '
            '"overall_passed": true}'
        ),
        expected_output='JSON object with category_scores, issues, overall_passed.',
        agent=scorer,
    )
    crew = Crew(agents=[scorer], tasks=[task], process=Process.sequential, verbose=False)
    try:
        result = crew.kickoff()
        raw = (getattr(result, "raw", None) or str(result)).strip()
        if raw.startswith("```"):
            raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("```")).strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning("[Critic Crew] scorer failed (non-fatal): %s", exc)
        return {"category_scores": {}, "issues": {}, "overall_passed": True}


# ---------------------------------------------------------------------------
# Patcher
# ---------------------------------------------------------------------------

def _run_patcher(cat: str, articles: list[dict], issue: str) -> str:
    """Ask Claude to rewrite the HTML body for a single weak section."""
    patcher = make_section_patcher()

    article_lines: list[str] = []
    for i, art in enumerate(articles[:8], 1):
        title = art.get("title", "")
        desc = (art.get("description") or "")[:200]
        why = art.get("why_it_matters", "")
        block = f"{i}. {title}\n   Description: {desc}"
        if why:
            block += f"\n   Why it matters: {why}"
        article_lines.append(block)

    desc = (
        f"You are rewriting the '{cat}' section of an HTML briefing.\n\n"
        f"Quality issue identified: {issue}\n\n"
        f"Available articles ({len(articles)} total — use ONLY these):\n"
        + "\n".join(article_lines)
        + "\n\nWrite an improved HTML section that:\n"
        "  - Has 3-4 sentences of analysis per story.\n"
        "  - Includes specific facts (numbers, names, decisions) from the articles above.\n"
        "  - Uses <h3> for story headlines and <p> for analysis.\n"
        "  - Does NOT include an <h2> header — it will be added separately.\n\n"
        "Return ONLY the HTML fragment — no markdown, no explanation, no <html> wrapper. "
        "Do NOT add stories, facts, or events not explicitly stated in those articles."
    )
    return _kickoff_single_task(patcher, desc, "HTML fragment (<h3>+<p>) for this category.")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_critic_pass(
    html: str,
    single_voice_script: str,
    conversational_script: str,
    top_articles: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """
    Run the critic + (optional) patch pass.

    Returns (html, single_voice_script, conversational_script) — possibly
    unchanged if all sections passed.
    """
    try:
        critique = _run_scorer(html, top_articles)
    except Exception as exc:
        logger.warning("[Critic Crew] scorer raised: %s — keeping originals", exc)
        return html, single_voice_script, conversational_script

    scores = critique.get("category_scores", {})
    issues = critique.get("issues", {})
    overall_passed = critique.get("overall_passed", True)

    weak = [
        cat for cat, score in scores.items()
        if isinstance(score, (int, float)) and score < _WEAK_THRESHOLD
    ]
    logger.info("[Critic Crew] scores=%s | weak=%s | overall_passed=%s",
                scores, weak, overall_passed)

    if overall_passed and not weak:
        logger.info("[Critic Crew] briefing passed — no patches needed.")
        return html, single_voice_script, conversational_script

    patchable = [c for c in weak if c.lower() not in _NEVER_PATCH_NORMALIZED]
    skipped = [c for c in weak if c.lower() in _NEVER_PATCH_NORMALIZED]
    if skipped:
        logger.warning(
            "[Critic Crew] Skipping patch for %s — sports sections are never auto-patched "
            "(prevents hallucinated scores/players/trades).", skipped,
        )
    if not patchable:
        return html, single_voice_script, conversational_script

    improved_html = html
    for cat in patchable:
        articles = top_articles.get(cat, [])
        issue = issues.get(cat, "Section lacks depth and substance.")
        logger.info("[Critic Crew] patching weak section: %s — %s", cat, issue)
        try:
            new_section = _run_patcher(cat, articles, issue)
            if not new_section:
                continue
            header_pattern = (
                rf'(<h2[^>]*>[^<]*{re.escape(cat)}[^<]*</h2>)(.*?)'
                r'(?=<h2|<div\s[^>]*text-align\s*:\s*center)'
            )
            match = re.search(header_pattern, improved_html, re.DOTALL | re.IGNORECASE)
            if match:
                h2_tag = match.group(1)
                improved_html = re.sub(
                    header_pattern,
                    lambda _m: f"{h2_tag}\n{new_section}\n",
                    improved_html,
                    count=1,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                logger.info("[Critic Crew] patched section: %s", cat)
            else:
                logger.warning("[Critic Crew] could not locate HTML section for '%s' — skip", cat)
        except Exception as exc:
            logger.warning("[Critic Crew] patch failed for '%s': %s", cat, exc)

    # Regenerate both podcast scripts off the (unchanged) article set.
    logger.info("[Critic Crew] regenerating podcast scripts after patches…")
    try:
        single_agent = make_single_voice_writer()
        conv_agent = make_conversational_writer()
        s_desc, s_exp = _single_voice_task(top_articles, picks, trending, verified_sports_facts)
        c_desc, c_exp = _conversational_task(top_articles, picks, trending, verified_sports_facts)
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_single = ex.submit(_kickoff_single_task, single_agent, s_desc, s_exp)
            f_conv = ex.submit(_kickoff_single_task, conv_agent, c_desc, c_exp)
            new_single = f_single.result(timeout=600) or single_voice_script
            new_conv = f_conv.result(timeout=600) or conversational_script
        if new_conv:
            new_conv = re.sub(r'(\[(?:MIKE|ELIZABETH|JESSE)\])', r'\n\1\n', new_conv)
            new_conv = re.sub(r'\n{3,}', '\n\n', new_conv).strip()
        return improved_html, new_single, new_conv
    except Exception as exc:
        logger.warning("[Critic Crew] script regeneration failed (keeping originals): %s", exc)
        return improved_html, single_voice_script, conversational_script
