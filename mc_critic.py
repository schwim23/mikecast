"""
MikeCast — post-generation quality critic pass.

After the HTML briefing and podcast scripts are generated, a GPT-4o critic
evaluates each category section for depth, substance, and story count.
Weak sections (score < 7) are regenerated with targeted prompts.

Entry point: run_critic_pass(html, single_voice_script, conversational_script,
                              categorised, picks)
Returns: (improved_html, improved_single_voice, improved_conversational)

Gracefully skips (returns inputs unchanged) if OPENAI_API_KEY is not set
or if any unhandled exception occurs.
"""

import json
import logging
import re

from mc_config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

# Categories considered weak if critic score is below this threshold
_WEAK_THRESHOLD = 7


# ---------------------------------------------------------------------------
# HTML section extraction helpers
# ---------------------------------------------------------------------------

def _extract_html_summary(html: str, categorised: dict[str, list[dict]]) -> str:
    """
    Build a compact text summary of the HTML briefing for the critic:
    h2 headers + article count per category + first sentence of each section.
    """
    lines: list[str] = []

    # Extract h2 headers
    headers = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL | re.IGNORECASE)
    for header in headers:
        clean_header = re.sub(r"<[^>]+>", "", header).strip()
        # Skip non-category headers (e.g. "Mike's Picks", "Today's Briefing")
        for cat, articles in categorised.items():
            if cat.lower() in clean_header.lower():
                lines.append(f"\n## {clean_header} ({len(articles)} articles)")
                # Extract first paragraph after this h2
                pattern = rf"<h2[^>]*>{re.escape(header)}</h2>(.*?)(?=<h2|$)"
                match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
                if match:
                    section_html = match.group(1)
                    # Get first <p> text
                    p_match = re.search(r"<p[^>]*>(.*?)</p>", section_html, re.DOTALL)
                    if p_match:
                        first_p = re.sub(r"<[^>]+>", "", p_match.group(1)).strip()
                        lines.append(f"  First paragraph: {first_p[:300]}")
                    # Count article bullets/items
                    article_count = len(articles)
                    lines.append(f"  Articles available: {article_count}")
                break

    return "\n".join(lines) if lines else html[:2000]


# ---------------------------------------------------------------------------
# Critique
# ---------------------------------------------------------------------------

def critique_briefing(html: str, categorised: dict[str, list[dict]]) -> dict:
    """
    Use GPT-4o to score each category section 1-10 on depth and substance.

    Returns:
        {
            "passed": bool,
            "weak_categories": [...],
            "issues": {"Category": "description of issue", ...},
            "category_scores": {"Category": score, ...},
        }
    On any failure or if OPENAI_API_KEY is not set, returns passed=True
    so the pipeline continues unmodified.
    """
    if not OPENAI_API_KEY:
        logger.info("OPENAI_API_KEY not set — skipping critic pass.")
        return {"passed": True, "weak_categories": [], "issues": {}, "category_scores": {}}

    try:
        from openai import OpenAI
        client = OpenAI()

        summary = _extract_html_summary(html, categorised)

        system_prompt = (
            "You are a quality editor for a daily news briefing. "
            "Score each category section 1-10 on:\n"
            "  - depth: does it have 3+ substantive stories?\n"
            "  - analysis: does it go beyond mere headlines?\n"
            "  - substance: does it include specific facts, numbers, or implications?\n\n"
            "A score of 7+ means acceptable quality. Below 7 means the section needs improvement.\n\n"
            "Return ONLY valid JSON in this exact structure (no markdown, no explanation):\n"
            "{\n"
            '  "category_scores": {"AI & Tech": 8, "Business & Markets": 5, ...},\n'
            '  "issues": {"Business & Markets": "Only 1 story, lacks analysis"},\n'
            '  "overall_passed": true\n'
            "}"
        )

        user_prompt = (
            "Here is a compact summary of today's news briefing. "
            "Score each category section:\n\n"
            f"{summary}"
        )

        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=600,
            temperature=0.2,
        )

        raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(
                line for line in lines if not line.strip().startswith("```")
            ).strip()

        result = json.loads(raw)
        category_scores: dict[str, int] = result.get("category_scores", {})
        issues: dict[str, str] = result.get("issues", {})
        overall_passed: bool = result.get("overall_passed", True)

        weak_categories = [
            cat for cat, score in category_scores.items()
            if isinstance(score, (int, float)) and score < _WEAK_THRESHOLD
        ]

        passed = overall_passed and len(weak_categories) == 0

        logger.info(
            "Critic pass: passed=%s | scores=%s | weak=%s",
            passed, category_scores, weak_categories,
        )

        return {
            "passed": passed,
            "weak_categories": weak_categories,
            "issues": issues,
            "category_scores": category_scores,
        }

    except Exception as exc:
        logger.warning("Critic briefing evaluation failed (non-fatal): %s", exc)
        return {"passed": True, "weak_categories": [], "issues": {}, "category_scores": {}}


# ---------------------------------------------------------------------------
# Patch weak sections
# ---------------------------------------------------------------------------

def _regenerate_html_section(cat: str, articles: list[dict], issue: str) -> str:
    """
    Ask GPT-4o to write an improved HTML section for a weak category.
    Returns an HTML fragment (h2 + article paragraphs).
    """
    from openai import OpenAI
    client = OpenAI()

    # Build compact article list for the prompt
    article_lines: list[str] = []
    for i, art in enumerate(articles[:8], 1):
        title = art.get("title", "")
        desc  = art.get("description", "")
        why   = art.get("why_it_matters", "")
        article_lines.append(
            f"{i}. {title}\n"
            f"   Description: {desc[:200]}\n"
            + (f"   Why it matters: {why}\n" if why else "")
        )

    articles_text = "\n".join(article_lines)

    prompt = (
        f"You are writing the '{cat}' section of a daily news briefing HTML page.\n\n"
        f"Quality issue identified: {issue}\n\n"
        f"Available articles ({len(articles)} total — use ONLY these, nothing else):\n{articles_text}\n\n"
        "CRITICAL: Only write about the articles listed above. Do NOT add stories, facts, "
        "player names, scores, trades, or events that are not explicitly stated in those articles. "
        "If fewer than 4 articles are available, cover only those that exist — do not invent more.\n\n"
        "Write an improved HTML section that:\n"
        "  - Covers the available stories with deeper analysis\n"
        "  - Has 3-4 sentences of analysis per story\n"
        "  - Includes specific facts from the articles above\n"
        "  - Uses <h3> for story headlines and <p> for analysis\n"
        "  - Does NOT include an <h2> header (it will be added separately)\n"
        "Return ONLY the HTML fragment — no markdown, no explanation."
    )

    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1200,
        temperature=0.4,
    )
    return resp.choices[0].message.content.strip()


def patch_weak_sections(
    html: str,
    single_voice_script: str,
    conversational_script: str,
    categorised: dict[str, list[dict]],
    picks: list[dict],
    weak_categories: list[str],
    issues: dict[str, str],
) -> tuple[str, str, str]:
    """
    Regenerate HTML sections for weak categories, then regenerate podcast scripts.

    Returns (improved_html, improved_single_voice, improved_conversational).
    """
    from mc_generate import generate_conversational_script, generate_podcast_script

    # NY Sports is never patched — thin sports coverage is acceptable and honest.
    # Patching sports invites GPT to hallucinate games, scores, and players.
    # Use case-insensitive match since GPT may return "NY SPORTS" or "NY Sports".
    NEVER_PATCH_NORMALIZED = {"ny sports"}
    patchable = [c for c in weak_categories if c.lower() not in NEVER_PATCH_NORMALIZED]
    if len(patchable) < len(weak_categories):
        skipped = [c for c in weak_categories if c.lower() in NEVER_PATCH_NORMALIZED]
        logger.info("Skipping critic patch for fact-sensitive categories: %s", skipped)

    improved_html = html

    for cat in patchable:
        articles = categorised.get(cat, [])
        issue = issues.get(cat, "Section lacks depth and substance.")
        logger.info("Patching weak section: %s — %s", cat, issue)

        try:
            new_section_html = _regenerate_html_section(cat, articles, issue)

            # Find the h2 header for this category in the HTML
            # Match pattern: <h2 ...>...category...</h2> followed by section content
            header_pattern = rf'(<h2[^>]*>[^<]*{re.escape(cat)}[^<]*</h2>)(.*?)(?=<h2|<div\s[^>]*text-align\s*:\s*center)'
            match = re.search(header_pattern, improved_html, re.DOTALL | re.IGNORECASE)

            if match:
                h2_tag = match.group(1)
                replacement = f"{h2_tag}\n{new_section_html}\n"
                improved_html = re.sub(
                    header_pattern,
                    lambda m: replacement,
                    improved_html,
                    count=1,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                logger.info("Patched HTML section for: %s", cat)
            else:
                logger.warning("Could not locate HTML section for '%s' — skipping patch.", cat)

        except Exception as exc:
            logger.warning("Failed to patch section '%s': %s", cat, exc)

    # Only regenerate podcast scripts if at least one HTML section was actually patched.
    # If all weak categories were skipped (e.g. all in NEVER_PATCH), the originals are fine.
    if patchable:
        logger.info("Regenerating podcast scripts after critic patches…")
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_single = ex.submit(generate_podcast_script, categorised, picks)
                f_conv   = ex.submit(generate_conversational_script, categorised, picks)
                improved_single = f_single.result()
                improved_conv   = f_conv.result()
            logger.info("Podcast scripts regenerated successfully.")
        except Exception as exc:
            logger.warning("Script regeneration failed (keeping originals): %s", exc)
            improved_single = single_voice_script
            improved_conv   = conversational_script
    else:
        logger.info("No HTML sections patched — keeping original podcast scripts.")
        improved_single = single_voice_script
        improved_conv   = conversational_script

    return improved_html, improved_single, improved_conv


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_critic_pass(
    html: str,
    single_voice_script: str,
    conversational_script: str,
    categorised: dict[str, list[dict]],
    picks: list[dict],
) -> tuple[str, str, str]:
    """
    Orchestrate the full critic pass: evaluate → optionally patch weak sections.

    Max 1 pass (no loop). Returns inputs unchanged if passed=True or on exception.
    """
    try:
        critique = critique_briefing(html, categorised)

        logger.info(
            "Critic results — passed: %s | weak: %s",
            critique["passed"],
            critique.get("weak_categories", []),
        )

        if critique["passed"]:
            logger.info("Briefing passed quality check — no patches needed.")
            return html, single_voice_script, conversational_script

        weak = critique.get("weak_categories", [])
        issues = critique.get("issues", {})

        if not weak:
            return html, single_voice_script, conversational_script

        return patch_weak_sections(
            html,
            single_voice_script,
            conversational_script,
            categorised,
            picks,
            weak,
            issues,
        )

    except Exception as exc:
        logger.warning("Critic pass failed entirely (returning originals): %s", exc)
        return html, single_voice_script, conversational_script
