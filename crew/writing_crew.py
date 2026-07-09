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

def _ny_sports_block(
    verified_sports_facts: dict[str, str] | None,
    ny_team_updates: list[dict] | None = None,
) -> str:
    from crew.sports_research_crew import (
        format_ny_team_updates_block,
        format_verified_facts_block,
    )
    updates = format_ny_team_updates_block(ny_team_updates or [])
    facts = format_verified_facts_block(verified_sports_facts or {})
    parts = [b for b in (updates, facts) if b]
    return "\n\n".join(parts)


# Shared anti-skip rule. The articles context already labels each category with
# its article count ("=== COMPANIES — 4 ARTICLES (discuss ONLY these) ==="). The
# writer has historically still output "No Companies News Available" anyway. This
# rule blocks that exact failure mode.
_ANTI_SKIP_RULE = (
    "MANDATORY: every category whose header above says '1 ARTICLE' or more MUST "
    "appear as a written section in your output. NEVER emit the phrases 'No X News "
    "Available', 'no items today', '(empty)', or any equivalent for a category that "
    "has articles. If a category has only 1-2 articles, write a shorter section "
    "(one well-developed story) — do not skip it. The only category you may omit "
    "entirely is one whose header explicitly says '0 ARTICLES'."
)

# Per-task brief reminder (full versions of TONE + TTS rules live in the agent
# backstories — this is a short repeat so the rules don't get diluted by
# everything else in the task description).
_TONE_TTS_REMINDER = (
    "NO STALE COLOR: state only the facts you were given — never add standings, "
    "records, seedings, rankings, streaks, or award/championship history from memory "
    "(e.g. do NOT call a team 'third in the East' unless that exact standing is in the "
    "input); it goes stale and reads as confidently wrong.\n"
    "TONE + TTS REMINDER (full version in your backstory): NO judgmental hyperbole "
    "at the listener (\"what are you doing with your life\", \"you HAVE to\", etc.) — "
    "talk ABOUT the news, not AT the audience. Spell out times (7:05 PM ET → "
    "\"seven-oh-five PM Eastern\"), scores (122-113 → \"one twenty-two to one "
    "thirteen\"), and dollar amounts (\"ten point nine billion dollars\"). No bare "
    "colons in times, no bare hyphens between numbers, no markdown, no URLs, no stage "
    "directions."
)


def _html_task(
    categorised: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None,
    ny_team_updates: list[dict] | None = None,
) -> tuple[str, str]:
    articles_context = _build_articles_context(categorised)
    picks_ctx = _picks_block(picks)
    sports_facts = _ny_sports_block(verified_sports_facts, ny_team_updates)
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
        "   - NY SPORTS: cover the Knicks, Devils, Yankees, and NY Giants ONLY, leading "
        "     with each team's news from the LAST 24 HOURS (a game result, trade, signing, "
        "     coaching/roster move, or injury). Any team marked [MANDATORY-INCLUDE] in the "
        "     RESULTS & UPCOMING GAMES block above is IN SEASON and MUST get at least one "
        "     sentence stating the score of its last game and when its NEXT game is — even "
        "     if NO article today mentions that team. Add a second sentence if a real story "
        "     exists. Copy the relative timing words (TODAY / TONIGHT / LAST NIGHT / etc.) "
        "     verbatim. A team that is out of season (not in that block) gets NO game line — "
        "     skip it unless a real story exists.\n"
        "     Non-NY sports content is OFF by default. Include a major national sports story "
        "     ONLY if it is seismic — a championship clinch, an MVP / Cy Young / Heisman / "
        "     Coach of the Year result, a Doncic-to-Lakers-tier trade, or a career-altering "
        "     injury to an all-time-great. Routine non-NY recaps, opinion segments, coaching "
        "     hires below superstar caliber, and 'fun bar conversation' items DO NOT belong "
        "     here.\n"
        "3. KEY TRENDS & INSIGHTS — 3-5 bullets.\n"
        "4. WHAT TO WATCH — 3-4 forward-looking items.\n\n"
        "Only use facts from the article inputs. Do NOT add details from training knowledge. "
        "Never tease — always tell the listener what actually happened.\n\n"
        f"{_ANTI_SKIP_RULE}\n\n"
        f"{_TONE_TTS_REMINDER}"
    )
    expected = "Plain-text briefing in the 4-section format described."
    return desc, expected


def _single_voice_task(
    categorised: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None,
    ny_team_updates: list[dict] | None = None,
) -> tuple[str, str]:
    articles_context = _build_articles_context(categorised)
    picks_ctx = _picks_block(picks)
    sports_facts = _ny_sports_block(verified_sports_facts, ny_team_updates)
    trending_block = _build_trending_prompt_block(trending)

    desc = (
        f"Today is {TODAY_DISPLAY}. Write a tight MikeCast single-host podcast script.\n"
        f"{trending_block}\n"
        f"{sports_facts}\n\n"
        f"Here are today's articles:\n{articles_context}{picks_ctx}\n\n"
        "TARGET LENGTH: 6–7 minutes of spoken audio at a natural ~150 wpm pace. "
        "That means a TOTAL of 900–1000 words — not a word longer. Brevity matters.\n\n"
        "Segments with approximate word targets (sum ≈ 950):\n"
        "  1. INTRO (~60w) — warm welcome, tease top 2-3 stories.\n"
        "  2. AI & TECH (~300w) — top 3 stories. Substance over completeness.\n"
        "  3. BUSINESS & MARKETS (~180w) — top 2 stories with the why-it-matters.\n"
        "  4. COMPANIES (~220w) — top 2-3 stories with personality.\n"
        "  5. NY SPORTS (~100w) — Knicks / Devils / Yankees / NY Giants ONLY, leading with "
        "     each team's news from the LAST 24 HOURS. "
        "     MANDATORY: every team marked [MANDATORY-INCLUDE] in the RESULTS & UPCOMING "
        "     GAMES block is in season and MUST get at least one sentence with the score of "
        "     its last game and when its next game is, even if no article covers them. Copy "
        "     the relative timing words (TONIGHT / LAST NIGHT / etc.) verbatim. Add a second "
        "     sentence only if something noteworthy. A team out of season (not in that "
        "     block) gets no game line — skip it unless a real story exists. "
        "     Non-NY sports is OFF by default — include a major national story ONLY if "
        "     seismic (championship clinch, MVP / Cy Young / Heisman / Coach of the Year "
        "     result, Doncic-to-Lakers-tier trade, career-altering injury to an all-time-"
        "     great). Routine non-NY recaps and 'fun bar conversation' items DO NOT belong "
        "     here.\n"
        "  6. MIKE'S PICKS (~100w) — only if picks exist; \"Big Mike's hand-picked reads\".\n"
        "  7. OUTRO (~40w) — wrap-up, sign-off.\n\n"
        "Cut anything that isn't moving the listener forward. Prefer one specific "
        "fact over two vague ones. Natural spoken language — contractions, transitions, "
        "rhetorical questions. No stage directions. No URLs.\n\n"
        f"{_ANTI_SKIP_RULE}\n\n"
        f"{_TONE_TTS_REMINDER}"
    )
    expected = "Spoken-form podcast script, 900–1000 words total."
    return desc, expected


def _conversational_task(
    categorised: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None,
    ny_team_updates: list[dict] | None = None,
) -> tuple[str, str]:
    articles_context = _build_articles_context(categorised)
    picks_ctx = _picks_block(picks)
    sports_facts = _ny_sports_block(verified_sports_facts, ny_team_updates)
    trending_block = _build_trending_prompt_block(trending)

    desc = (
        f"Today is {TODAY_DISPLAY}. Write a tight MikeCast 3-host conversational script.\n"
        f"{trending_block}\n"
        f"{sports_facts}\n\n"
        f"Here are today's articles:\n{articles_context}{picks_ctx}\n\n"
        "Tag every line with [MIKE], [ELIZABETH], or [JESSE] on its OWN LINE, e.g.:\n"
        "[MIKE]\nHey everyone, welcome to MikeCast...\n\n"
        "TARGET LENGTH: 6–7 minutes of spoken audio at ~150 wpm. "
        "TOTAL 900–1000 words across all hosts — not a word longer. Brevity matters.\n\n"
        "Structure with approximate word targets (sum ≈ 950):\n"
        "1. [MIKE] INTRO (~60w) — welcome, tease top 2-3 stories.\n"
        "2. [ELIZABETH] AI & TECH (~300w) — top 3 stories with substance.\n"
        "3. [ELIZABETH] BUSINESS & MARKETS (~180w) — top 2 stories in depth.\n"
        "4. [ELIZABETH] COMPANIES (~210w) — top 2-3 with personality. "
        "End with: \"Alright Jesse, take it away with sports...\"\n"
        "5. [JESSE] NY SPORTS (~100w) — Knicks / Devils / Yankees / NY Giants ONLY, leading "
        "with each team's news from the LAST 24 HOURS. "
        "MANDATORY: every team marked [MANDATORY-INCLUDE] in the RESULTS & UPCOMING GAMES "
        "block is in season and MUST get at least one sentence with the score of its last "
        "game and when its next game is, even if no article covers them. Copy the relative "
        "timing words (TONIGHT / LAST NIGHT / etc.) verbatim. Add a second sentence only if "
        "noteworthy. A team out of season (not in that block) gets no game line — skip it "
        "unless a real story exists. "
        "Non-NY sports is OFF by "
        "default — include a major national story ONLY if seismic (championship clinch, "
        "MVP / Cy Young / Heisman / Coach of the Year, Doncic-to-Lakers-tier trade, "
        "career-altering injury to an all-time-great). Routine non-NY recaps and 'fun bar "
        "conversation' items DO NOT belong here. End with: \"Back to you, Mike.\"\n"
        "6. [MIKE] SIGN-OFF (~40w).\n\n"
        "Cut anything that isn't moving the listener forward. Prefer one specific "
        "fact over two vague ones. No URLs. No stage directions. Only spoken words.\n\n"
        f"{_ANTI_SKIP_RULE}\n\n"
        f"{_TONE_TTS_REMINDER}"
    )
    expected = "Tagged 3-host script with [MIKE]/[ELIZABETH]/[JESSE] on own lines, 900–1000 words total."
    return desc, expected


# ---------------------------------------------------------------------------
# Public entry point — three writers in parallel
# ---------------------------------------------------------------------------

def run_writing(
    top_articles: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None = None,
    ny_team_updates: list[dict] | None = None,
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

    html_desc, html_exp = _html_task(top_articles, picks, filtered_trending, verified_sports_facts, ny_team_updates)
    single_desc, single_exp = _single_voice_task(top_articles, picks, filtered_trending, verified_sports_facts, ny_team_updates)
    conv_desc, conv_exp = _conversational_task(top_articles, picks, filtered_trending, verified_sports_facts, ny_team_updates)

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
