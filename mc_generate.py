"""
MikeCast — content generation.

GPT-4o calls for:
  - HTML briefing email
  - Single-voice podcast script (OpenAI TTS fallback)
  - 3-voice conversational script (ElevenLabs)
  - Short episode description for RSS/dashboard
"""

import html as _html
import json
import logging
import re

from mc_config import OPENAI_API_KEY, TODAY, TODAY_DISPLAY

logger = logging.getLogger("mikecast")


# ---------------------------------------------------------------------------
# Shared GPT helpers
# ---------------------------------------------------------------------------

def _build_articles_context(categorised: dict[str, list[dict]]) -> str:
    """Flatten all articles into a structured text block for GPT prompts."""
    lines = []
    for cat, arts in categorised.items():
        if not arts:
            continue
        lines.append(f"\n=== {cat.upper()} ===")
        for i, art in enumerate(arts, 1):
            title = art.get("title", "").replace("[Updated] ", "")
            desc = art.get("description", "")
            url = art.get("url", "")
            source = art.get("source", "")
            updated = "[Updated] " in art.get("title", "")
            prefix = "[UPDATE] " if updated else ""
            lines.append(f"{i}. {prefix}{title}")
            if desc:
                lines.append(f"   Summary: {desc[:300]}")
            if source:
                lines.append(f"   Source: {source}")
            if url:
                lines.append(f"   URL: {url}")
    return "\n".join(lines)


def _gpt_call(system_prompt: str, user_prompt: str, max_tokens: int = 2500) -> str:
    """Call GPT-4o and return the response text. Returns '' on any failure."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set — returning empty GPT response.")
        return ""
    try:
        from openai import OpenAI
        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("GPT call failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# HTML briefing
# ---------------------------------------------------------------------------

def generate_html_briefing(
    categorised: dict[str, list[dict]],
    picks: list[dict],
) -> str:
    """Build a professional HTML briefing email using GPT for rich prose."""

    articles_context = _build_articles_context(categorised)
    total_articles = sum(len(v) for v in categorised.values())

    picks_context = ""
    if picks:
        picks_context = "\n\n=== MIKE'S PICKS ==="
        for p in picks:
            picks_context += f"\n- {p.get('title','')}: {p.get('summary','')[:300]}"

    system_prompt = (
        "You are MikeCast, a sharp, well-informed daily briefing writer. "
        "Write in a professional yet engaging tone — like a smart friend who reads everything so you don't have to. "
        "Be concise but substantive. Use active voice. Avoid filler phrases."
    )

    user_prompt = f"""Today is {TODAY_DISPLAY}. You have collected {total_articles} news articles across 4 categories.

Here are today's articles:
{articles_context}
{picks_context}

Write a professional daily briefing (800-1200 words) with these exact sections:

1. EXECUTIVE SUMMARY (2-3 sentences capturing the most important themes of the day)

2. TOP STORIES — organized by category (AI & Tech, Business & Markets, Companies, NY Sports). For each story:
   - Write 2-4 sentences of analysis/context, not just a restatement of the headline
   - Include the clickable source URL at the end of each story item
   - Cover at least 3-4 stories per category that has content

3. KEY TRENDS & INSIGHTS (3-5 bullet points identifying patterns, themes, or connections across today's stories)

4. WHAT TO WATCH (3-4 forward-looking items — what developments to monitor in the coming days)

Format rules:
- Return ONLY the briefing text sections (no HTML, no markdown headers with #)
- Use plain section headers like: EXECUTIVE SUMMARY, AI & TECH, BUSINESS & MARKETS, COMPANIES, NY SPORTS, KEY TRENDS & INSIGHTS, WHAT TO WATCH
- Each story should be on its own paragraph
- IMPORTANT: End every story paragraph with a clickable source link in this exact format: [Source Name](URL)
  Use the exact URL provided in the article data above — do not make up or omit URLs
- Keep it tight and informative — this is a busy executive's morning read"""

    briefing_text = _gpt_call(system_prompt, user_prompt, max_tokens=2500)
    if not briefing_text:
        briefing_text = "Unable to generate GPT briefing. See articles below."
    # Strip any markdown code fences the LLM may have accidentally included
    briefing_text = re.sub(r"```\w*\n?", "", briefing_text)

    # Convert the GPT plain-text briefing into styled HTML
    def text_to_html_sections(text: str) -> str:
        section_headers = [
            "EXECUTIVE SUMMARY", "AI & TECH", "BUSINESS & MARKETS",
            "COMPANIES", "NY SPORTS", "KEY TRENDS & INSIGHTS", "WHAT TO WATCH",
        ]
        html_parts = []
        current_section = None
        buffer: list[str] = []

        def flush_buffer(section: str, buf: list[str]) -> str:
            if not buf:
                return ""
            color = "#ffb74d" if section in ("KEY TRENDS & INSIGHTS", "WHAT TO WATCH") else "#4fc3f7"
            out = (
                f'<h2 style="color:{color};border-bottom:1px solid #444;'
                f'padding-bottom:6px;margin-top:28px;">{_html.escape(section)}</h2>\n'
            )
            combined = " ".join(buf).strip()
            # Convert [Source](URL) markdown links to HTML
            combined = re.sub(
                r'\[([^\]]+)\]\((https?://[^)]+)\)',
                r'<a href="\2" style="color:#81d4fa;text-decoration:none;">\1</a>',
                combined,
            )
            paragraphs = re.split(r'\n{2,}', combined)
            for para in paragraphs:
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
            matched_header = None
            for h in section_headers:
                if stripped.upper().startswith(h):
                    matched_header = h
                    break
            if matched_header:
                if current_section is not None:
                    html_parts.append(flush_buffer(current_section, buffer))
                current_section = matched_header
                buffer = []
                remainder = stripped[len(matched_header):].lstrip(":- ").strip()
                if remainder:
                    buffer.append(remainder)
            else:
                if stripped:
                    buffer.append(stripped)
                else:
                    buffer.append("\n\n")

        if current_section is not None:
            html_parts.append(flush_buffer(current_section, buffer))

        return "\n".join(html_parts)

    briefing_html_sections = text_to_html_sections(briefing_text)

    picks_html = ""
    if picks:
        picks_html = (
            '<h2 style="color:#ffb74d;border-bottom:1px solid #444;'
            'padding-bottom:6px;margin-top:28px;">🎯 Mike\'s Picks</h2>\n<ul>\n'
        )
        for p in picks:
            title = p.get("title", "Untitled")
            summary = p.get("summary", "")
            url = p.get("url", "")
            if url:
                picks_html += (
                    f'<li style="margin-bottom:10px;">'
                    f'<a href="{url}" style="color:#ffcc80;text-decoration:none;font-weight:600;">{title}</a>'
                )
            else:
                picks_html += (
                    f'<li style="margin-bottom:10px;">'
                    f'<strong style="color:#ffcc80;">{title}</strong>'
                )
            if summary:
                picks_html += f'<br><span style="color:#bbb;font-size:0.9em;">{summary[:300]}</span>'
            picks_html += "</li>\n"
        picks_html += "</ul>\n"

    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#1a1a2e;color:#e0e0e0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:720px;margin:auto;padding:24px;">
<div style="text-align:center;padding:24px 0;border-bottom:2px solid #4fc3f7;margin-bottom:24px;">
  <h1 style="color:#4fc3f7;margin:0;font-size:2.2em;letter-spacing:1px;">🎙️ MikeCast</h1>
  <p style="color:#888;margin:6px 0 0;font-size:1.05em;">Daily Briefing — {TODAY_DISPLAY}</p>
</div>

{briefing_html_sections}

{picks_html}

<div style="text-align:center;padding:20px 0;border-top:1px solid #444;margin-top:36px;">
  <p style="color:#666;font-size:0.85em;">MikeCast Daily Briefing • Generated {TODAY_DISPLAY}<br>
  Sources: NYT, Hacker News, TechCrunch, Ars Technica, CNBC, Reddit &amp; more &bull; Powered by OpenAI GPT-4o</p>
</div>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Single-voice podcast script (used as email attachment + OpenAI TTS fallback)
# ---------------------------------------------------------------------------

def generate_podcast_script(
    categorised: dict[str, list[dict]],
    picks: list[dict],
) -> str:
    """Create a conversational 5-10 minute single-voice podcast script using GPT."""

    articles_context = _build_articles_context(categorised)
    picks_context = ""
    if picks:
        picks_context = "\n\n=== MIKE'S PICKS ==="
        for p in picks:
            picks_context += f"\n- {p.get('title','')}: {p.get('summary','')[:300]}"

    system_prompt = (
        "You are the host of MikeCast, a daily news podcast. "
        "Your style is smart, conversational, and energetic — like a knowledgeable friend catching you up over coffee. "
        "You speak directly to the listener. You add context, opinion, and insight — not just headlines. "
        "You're concise but never dry. You use natural spoken language, not written prose."
    )

    user_prompt = f"""Today is {TODAY_DISPLAY}. Write a full podcast script for today's MikeCast episode.

Here are today's news articles:
{articles_context}
{picks_context}

Script requirements:
- Total length: 5-10 minutes of spoken audio (approximately 1000-1500 words)
- Structure:
  1. Warm, engaging INTRO (welcome listeners, tease the top stories, ~30 seconds)
  2. AI & TECH segment — cover the top 3-4 stories with context and insight
  3. BUSINESS & MARKETS segment — cover top 2-3 stories, explain what it means for listeners
  4. COMPANIES segment — cover top 3-4 company stories with personality
  5. NY SPORTS segment — quick, energetic rundown of sports news
  6. MIKE'S PICKS segment (only if picks exist) — introduce as "Big Mike's hand-picked reads"
  7. OUTRO — brief wrap-up, call to action, sign-off (~20 seconds)

- Write in natural spoken language — use contractions, rhetorical questions, transitions
- Add brief commentary or "why this matters" for major stories
- Use natural transitions between segments (e.g., "Alright, switching gears...", "Now let's talk money...")
- Do NOT include stage directions like [MUSIC] or [PAUSE] — write only the spoken words
- Do NOT include URLs in the script — this is audio only
- Write the full script, not an outline"""

    script = _gpt_call(system_prompt, user_prompt, max_tokens=2000)

    if not script:
        logger.warning("GPT podcast script generation failed — using simple fallback.")
        lines: list[str] = [f"Hey everyone, welcome to MikeCast. It's {TODAY_DISPLAY}. Let's get into it."]
        for cat, arts in categorised.items():
            if not arts:
                continue
            lines.append(f"In {cat}:")
            for art in arts[:3]:
                title = art["title"].replace("[Updated] ", "")
                desc = art.get("description", "")
                lines.append(f"{title}. {desc}")
        lines.append("That's your MikeCast for today. Stay sharp, catch you tomorrow.")
        script = " ".join(lines)

    return script


# ---------------------------------------------------------------------------
# 3-voice conversational script (ElevenLabs)
# ---------------------------------------------------------------------------

def generate_conversational_script(
    categorised: dict[str, list[dict]],
    picks: list[dict],
) -> str:
    """
    Generate a 3-voice conversational podcast script tagged with:
      [MIKE]      — host: intro and sign-off only
      [ELIZABETH] — AI & Tech, Business & Markets, Companies
      [JESSE]     — NY Sports

    Returns the raw tagged script string.
    """
    articles_context = _build_articles_context(categorised)
    picks_context = ""
    if picks:
        picks_context = "\n\n=== MIKE'S PICKS ==="
        for p in picks:
            picks_context += f"\n- {p.get('title','')}: {p.get('summary','')[:300]}"

    system_prompt = (
        "You write scripts for a 3-host daily news podcast called MikeCast.\n"
        "The hosts are:\n"
        "  MIKE — the executive producer and host. Warm, authoritative. Does the intro and sign-off only.\n"
        "  ELIZABETH — the tech and business correspondent. Sharp, energetic, insightful. "
        "Covers AI & Tech, Business & Markets, and Companies stories.\n"
        "  JESSE — the sports guy. Enthusiastic, quick-witted, NY-sports-obsessed. "
        "Covers NY Sports only.\n\n"
        "Tag every line of dialogue with the speaker name in brackets on its own line, e.g.:\n"
        "[MIKE]\nHey everyone, welcome to MikeCast...\n\n"
        "[ELIZABETH]\nAlright, let's start with AI news...\n\n"
        "Rules:\n"
        "- MIKE speaks ONLY at the start (intro) and the very end (sign-off).\n"
        "- ELIZABETH covers everything until NY Sports, then explicitly hands off to Jesse.\n"
        "- JESSE covers NY Sports and hands back to Mike for the sign-off.\n"
        "- Write in natural spoken language — contractions, energy, personality.\n"
        "- NO URLs in the script. NO stage directions. Only spoken words.\n"
        "- Each host segment should feel like a real broadcast, not a list."
    )

    user_prompt = f"""Today is {TODAY_DISPLAY}. Write the full MikeCast 3-host podcast script.

Here are today's articles:
{articles_context}
{picks_context}

Script structure:
1. [MIKE] INTRO — Welcome listeners, briefly tease the top 2-3 stories (~30 seconds).
2. [ELIZABETH] AI & TECH — Cover top 3-4 stories with context and insight.
3. [ELIZABETH] BUSINESS & MARKETS — Cover top 2-3 stories, explain what it means.
4. [ELIZABETH] COMPANIES — Cover top 3-4 company stories with personality.
   End with a handoff: "Alright Jesse, take it away with sports..."
5. [JESSE] NY SPORTS — Energetic, team-focused rundown of Yankees, Knicks, Giants, Devils.
   End with: "Back to you, Mike."
6. [MIKE] SIGN-OFF — Brief wrap-up, thank listeners, sign off (~20 seconds).

Total length: 5-8 minutes of spoken audio (approx 1000-1500 words).
Write the COMPLETE script with all tags. No outline, no placeholders."""

    script = _gpt_call(system_prompt, user_prompt, max_tokens=2500)

    if not script:
        logger.warning("Conversational script generation failed — empty response.")
        return ""

    # Normalise: every [SPEAKER] tag on its own line, no excess blank lines
    script = re.sub(r'(\[(?:MIKE|ELIZABETH|JESSE)\])', r'\n\1\n', script)
    script = re.sub(r'\n{3,}', '\n\n', script).strip()
    return script


def parse_conversational_script(script: str) -> list[tuple[str, str]]:
    """
    Parse a tagged conversational script into (speaker, text) tuples.
    Speaker tags look like: [MIKE], [ELIZABETH], [JESSE]
    """
    segments: list[tuple[str, str]] = []
    current_speaker: str | None = None
    buffer: list[str] = []

    for line in script.splitlines():
        stripped = line.strip()
        m = re.fullmatch(r'\[(MIKE|ELIZABETH|JESSE)\]', stripped)
        if m:
            if current_speaker and buffer:
                text = " ".join(buffer).strip()
                if text:
                    segments.append((current_speaker, text))
            current_speaker = m.group(1)
            buffer = []
        else:
            if stripped:
                buffer.append(stripped)

    if current_speaker and buffer:
        text = " ".join(buffer).strip()
        if text:
            segments.append((current_speaker, text))

    return segments


# ---------------------------------------------------------------------------
# Episode description (RSS + dashboard)
# ---------------------------------------------------------------------------

def generate_episode_description(podcast_script: str, episode_num: int) -> str:
    """Generate a ~50-word episode description for the RSS feed using GPT-4o."""
    if not OPENAI_API_KEY:
        return f"Episode #{episode_num} — MikeCast daily news briefing."
    try:
        from openai import OpenAI
        client = OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write concise podcast episode descriptions. "
                        "Write a single sentence of approximately 50 words summarizing "
                        "the key topics covered in this episode. Be specific about the "
                        "actual stories — name the companies, people, or events discussed. "
                        "Do not start with 'Episode', a number, or the word 'Today'."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Podcast script:\n\n{podcast_script[:3000]}",
                },
            ],
            max_tokens=120,
            temperature=0.3,
        )
        summary = resp.choices[0].message.content.strip().rstrip(".")
        return f"Episode #{episode_num} — {summary}."
    except Exception as exc:
        logger.warning("Episode description generation failed: %s", exc)
        return f"Episode #{episode_num} — MikeCast daily news briefing."
