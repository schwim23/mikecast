"""
Step 8 — Writing Crew (Claude).

Three writers run in parallel and produce:
    1. HTML briefing body  (returned plain — wrapped in the styled template here)
    2. Single-voice podcast script
    3. 3-voice conversational podcast script ([MIKE] / [ELIZABETH] / [JESSE])

Each writer is a CrewAI Agent backed by Claude. All three writers share the
same article context, picks context, and (where verified) the NY-Sports
primary-source facts block produced by sports_research_crew.

Hallucination guardrails are enforced in the agent backstories
(crew/agents.py) and re-stated in each task description.

Falls back to empty strings on failure — the legacy critic / delivery code
already tolerates empty outputs gracefully.
"""

from __future__ import annotations

import html as _html
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from crewai import Crew, Process, Task

from crew.agents import (
    make_conversational_writer,
    make_html_writer,
    make_single_voice_writer,
)
from crew.context import (
    _build_articles_context,
    _build_trending_html,
    _build_trending_prompt_block,
    _filter_trending_to_articles,
    _safe_url,
)
from mc_config import TODAY_DISPLAY

logger = logging.getLogger("mikecast.crew.writing")


# ---------------------------------------------------------------------------
# Shared prompt assembly
# ---------------------------------------------------------------------------

def _picks_block(picks: list[dict]) -> str:
    if not picks:
        return ""
    out = "\n\n=== MIKE'S PICKS ==="
    for p in picks:
        out += f"\n- {p.get('title','')}: {p.get('summary','')[:300]}"
    return out


def _kickoff_single_task(agent, description: str, expected_output: str) -> str:
    """Run a single-agent Crew + Task and return the raw output string (or "")."""
    task = Task(description=description, expected_output=expected_output, agent=agent)
    crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
    try:
        result = crew.kickoff()
        raw = getattr(result, "raw", None) or str(result)
        return (raw or "").strip()
    except Exception as exc:
        logger.warning("Writer task failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# HTML output template wrapper (mirrors mc_generate.generate_html_briefing)
# ---------------------------------------------------------------------------

_SECTION_HEADERS = [
    "EXECUTIVE SUMMARY", "AI & TECH", "BUSINESS & MARKETS",
    "COMPANIES", "NY SPORTS", "KEY TRENDS & INSIGHTS", "WHAT TO WATCH",
]


def _text_to_html_sections(text: str) -> str:
    """
    Convert the writer's plain-text section output into styled HTML.
    Mirrors the nested helper inside mc_generate.generate_html_briefing.
    """
    html_parts: list[str] = []
    current_section: str | None = None
    buffer: list[str] = []

    def flush(section: str, buf: list[str]) -> str:
        if not buf:
            return ""
        color = "#ffb74d" if section in ("KEY TRENDS & INSIGHTS", "WHAT TO WATCH") else "#4fc3f7"
        out = (
            f'<h2 style="color:{color};border-bottom:1px solid #444;'
            f'padding-bottom:6px;margin-top:28px;">{_html.escape(section)}</h2>\n'
        )
        combined = " ".join(buf).strip()
        combined = re.sub(
            r'\[([^\]]+)\]\((https?://[^)]+)\)',
            r'<a href="\2" style="color:#81d4fa;text-decoration:none;">\1</a>',
            combined,
        )
        for para in re.split(r'\n{2,}', combined):
            para = para.strip()
            if not para:
                continue
            if para.startswith("- ") or para.startswith("• "):
                items = re.split(r'\n[-•] ', para)
                out += '<ul style="color:#ccc;line-height:1.7;">'
                for item in items:
                    item = item.lstrip("- •").strip()
                    if item:
                        out += f'<li style="margin-bottom:8px;">{item}</li>'
                out += '</ul>\n'
            else:
                out += f'<p style="color:#ccc;line-height:1.7;margin-bottom:12px;">{para}</p>\n'
        return out

    for line in text.splitlines():
        stripped = line.strip()
        matched: str | None = None
        for h in _SECTION_HEADERS:
            if stripped.upper().startswith(h):
                matched = h
                break
        if matched:
            if current_section is not None:
                html_parts.append(flush(current_section, buffer))
            current_section = matched
            buffer = []
            remainder = stripped[len(matched):].lstrip(":- ").strip()
            if remainder:
                buffer.append(remainder)
        else:
            buffer.append(stripped if stripped else "\n\n")

    if current_section is not None:
        html_parts.append(flush(current_section, buffer))
    return "\n".join(html_parts)


def _picks_html_block(picks: list[dict]) -> str:
    if not picks:
        return ""
    out = (
        '<h2 style="color:#ffb74d;border-bottom:1px solid #444;'
        'padding-bottom:6px;margin-top:28px;">🎯 Mike\'s Picks</h2>\n<ul>\n'
    )
    for p in picks:
        title = _html.escape(p.get("title", "Untitled"))
        summary = _html.escape(p.get("summary", ""))
        url = _safe_url(p.get("url", ""))
        if url != "#":
            out += (
                f'<li style="margin-bottom:10px;">'
                f'<a href="{_html.escape(url)}" style="color:#ffcc80;text-decoration:none;font-weight:600;">{title}</a>'
            )
        else:
            out += f'<li style="margin-bottom:10px;"><strong style="color:#ffcc80;">{title}</strong>'
        if summary:
            out += f'<br><span style="color:#bbb;font-size:0.9em;">{summary[:300]}</span>'
        out += "</li>\n"
    out += "</ul>\n"
    return out


def _wrap_html_template(briefing_html_sections: str, trending_html: str, picks_html: str) -> str:
    return (
        f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#1a1a2e;color:#e0e0e0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:720px;margin:auto;padding:24px;">
<div style="text-align:center;padding:24px 0;border-bottom:2px solid #4fc3f7;margin-bottom:24px;">
  <h1 style="color:#4fc3f7;margin:0;font-size:2.2em;letter-spacing:1px;">🎙️ MikeCast</h1>
  <p style="color:#888;margin:6px 0 0;font-size:1.05em;">Daily Briefing — {TODAY_DISPLAY}</p>
</div>

{trending_html}
{briefing_html_sections}

{picks_html}

<div style="text-align:center;padding:20px 0;border-top:1px solid #444;margin-top:36px;">
  <p style="color:#666;font-size:0.85em;">MikeCast Daily Briefing • Generated {TODAY_DISPLAY}<br>
  Sources: NYT, Hacker News, TechCrunch, Ars Technica, CNBC, Reddit &amp; more &bull; Powered by CrewAI + Claude + GPT-4o</p>
</div>
</body>
</html>"""
    )


# ---------------------------------------------------------------------------
# Per-writer task prompts
# ---------------------------------------------------------------------------

def _ny_sports_block(verified_sports_facts: dict[str, str] | None) -> str:
    from crew.sports_research_crew import format_verified_facts_block
    return format_verified_facts_block(verified_sports_facts or {})


def _html_task(
    categorised: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None,
) -> tuple[str, str]:
    articles_context = _build_articles_context(categorised)
    picks_ctx = _picks_block(picks)
    sports_facts = _ny_sports_block(verified_sports_facts)
    total = sum(len(v) for v in categorised.values())

    desc = (
        f"Today is {TODAY_DISPLAY}. You have {total} news articles across 4 categories.\n\n"
        f"{sports_facts}\n\n"
        f"Here are today's articles:\n{articles_context}{picks_ctx}\n\n"
        "Write a 1200-1800 word briefing with these EXACT plain-text sections "
        "(no HTML, no markdown headers — just the ALL CAPS section name on its own line):\n\n"
        "1. EXECUTIVE SUMMARY — 2-3 sentences on the day's themes.\n"
        "2. TOP STORIES — organized by AI & TECH, BUSINESS & MARKETS, COMPANIES, NY SPORTS.\n"
        "   - At least 80 words per story (full substance — what happened, who, numbers, why it matters).\n"
        "   - Cover 3-4 stories per category that has content.\n"
        "   - End EVERY story paragraph with a clickable link: [Source Name](URL)\n"
        "   - NY SPORTS: per team, 1-2 sentences IF a real story exists. Skip teams with nothing to say.\n"
        "3. KEY TRENDS & INSIGHTS — 3-5 bullets.\n"
        "4. WHAT TO WATCH — 3-4 forward-looking items.\n\n"
        "Only use facts from the article inputs. Do NOT add details from training knowledge. "
        "Never tease — always tell the listener what actually happened."
    )
    expected = "Plain-text briefing in the 4-section format described."
    return desc, expected


def _single_voice_task(
    categorised: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None,
) -> tuple[str, str]:
    articles_context = _build_articles_context(categorised)
    picks_ctx = _picks_block(picks)
    sports_facts = _ny_sports_block(verified_sports_facts)
    trending_block = _build_trending_prompt_block(trending)

    desc = (
        f"Today is {TODAY_DISPLAY}. Write a full MikeCast single-host podcast script.\n"
        f"{trending_block}\n"
        f"{sports_facts}\n\n"
        f"Here are today's articles:\n{articles_context}{picks_ctx}\n\n"
        "Requirements:\n"
        "- Total: 10-14 minutes of audio (1800-2000 words MINIMUM).\n"
        "- Segments with approximate word targets:\n"
        "  1. INTRO (~100w) — warm welcome, tease top 2-3 stories.\n"
        "  2. AI & TECH (~500w) — top 3-4 stories with substance.\n"
        "  3. BUSINESS & MARKETS (~400w) — top 2-3 stories with depth.\n"
        "  4. COMPANIES (~400w) — top 3-4 stories with personality.\n"
        "  5. NY SPORTS (~100-150w) — per team, 1-2 sentences IF something noteworthy "
        "     happened. Skip teams with nothing. Then 1-2 sentences for major non-NY sports news.\n"
        "  6. MIKE'S PICKS (~150w) — only if picks exist; intro as \"Big Mike's hand-picked reads\".\n"
        "  7. OUTRO (~50w) — wrap-up, sign-off.\n\n"
        "Natural spoken language — contractions, transitions, rhetorical questions. "
        "No stage directions. No URLs. No outline — write the full script."
    )
    expected = "Spoken-form podcast script, 1800-2000+ words."
    return desc, expected


def _conversational_task(
    categorised: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None,
) -> tuple[str, str]:
    articles_context = _build_articles_context(categorised)
    picks_ctx = _picks_block(picks)
    sports_facts = _ny_sports_block(verified_sports_facts)
    trending_block = _build_trending_prompt_block(trending)

    desc = (
        f"Today is {TODAY_DISPLAY}. Write the full MikeCast 3-host conversational script.\n"
        f"{trending_block}\n"
        f"{sports_facts}\n\n"
        f"Here are today's articles:\n{articles_context}{picks_ctx}\n\n"
        "Tag every line with [MIKE], [ELIZABETH], or [JESSE] on its OWN LINE, e.g.:\n"
        "[MIKE]\nHey everyone, welcome to MikeCast...\n\n"
        "Structure:\n"
        "1. [MIKE] INTRO (~30s) — welcome, tease top 2-3 stories.\n"
        "2. [ELIZABETH] AI & TECH — top 3-4 stories with full substance.\n"
        "3. [ELIZABETH] BUSINESS & MARKETS — top 2-3 stories in depth.\n"
        "4. [ELIZABETH] COMPANIES — top 3-4 with personality. End with: \"Alright Jesse, take it away with sports...\"\n"
        "5. [JESSE] NY SPORTS (~100-150w) — per NY team, 1-2 sentences IF something noteworthy. "
        "   Then 1-2 sentences for major non-NY sports news. End with: \"Back to you, Mike.\"\n"
        "6. [MIKE] SIGN-OFF (~20s).\n\n"
        "Total: 10-14 minutes (1800-2000 words MINIMUM). "
        "No URLs. No stage directions. Only spoken words. Write the COMPLETE script."
    )
    expected = "Tagged 3-host script with [MIKE]/[ELIZABETH]/[JESSE] on their own lines, 1800+ words."
    return desc, expected


# ---------------------------------------------------------------------------
# Public entry point — three writers in parallel
# ---------------------------------------------------------------------------

def run_writing(
    top_articles: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """
    Run all three writers in parallel via ThreadPoolExecutor.

    Returns (html, single_voice_script, conversational_script).
    Empty strings on failure for any individual writer — downstream code tolerates this.
    """
    # Trending filter (uses GPT-4o under the hood; same as legacy)
    filtered_trending = _filter_trending_to_articles(trending or [], top_articles)

    html_agent = make_html_writer()
    single_agent = make_single_voice_writer()
    conv_agent = make_conversational_writer()

    html_desc, html_exp = _html_task(top_articles, picks, filtered_trending, verified_sports_facts)
    single_desc, single_exp = _single_voice_task(top_articles, picks, filtered_trending, verified_sports_facts)
    conv_desc, conv_exp = _conversational_task(top_articles, picks, filtered_trending, verified_sports_facts)

    logger.info("[Writing Crew] launching 3 parallel Claude writers…")
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_html = ex.submit(_kickoff_single_task, html_agent, html_desc, html_exp)
        f_single = ex.submit(_kickoff_single_task, single_agent, single_desc, single_exp)
        f_conv = ex.submit(_kickoff_single_task, conv_agent, conv_desc, conv_exp)

        try:
            html_body = f_html.result(timeout=600)
        except Exception as exc:
            logger.error("[Writing Crew] HTML writer failed: %s", exc)
            html_body = ""
        try:
            single_script = f_single.result(timeout=600)
        except Exception as exc:
            logger.error("[Writing Crew] Single-voice writer failed: %s", exc)
            single_script = ""
        try:
            conv_script = f_conv.result(timeout=600)
        except Exception as exc:
            logger.error("[Writing Crew] Conversational writer failed: %s", exc)
            conv_script = ""

    # Normalize conversational tags onto their own lines (defensive — matches legacy)
    if conv_script:
        conv_script = re.sub(r'(\[(?:MIKE|ELIZABETH|JESSE)\])', r'\n\1\n', conv_script)
        conv_script = re.sub(r'\n{3,}', '\n\n', conv_script).strip()

    # Wrap HTML body in the styled template
    if html_body:
        # Strip any markdown code fences the writer may have included
        html_body = re.sub(r"```\w*\n?", "", html_body)
        briefing_html_sections = _text_to_html_sections(html_body)
        trending_html = _build_trending_html(filtered_trending)
        picks_html = _picks_html_block(picks)
        html_out = _wrap_html_template(briefing_html_sections, trending_html, picks_html)
    else:
        html_out = "<p>Briefing generation failed.</p>"

    logger.info(
        "[Writing Crew] outputs: html=%d chars, single=%d chars, conv=%d chars",
        len(html_out), len(single_script), len(conv_script),
    )
    return html_out, single_script, conv_script
