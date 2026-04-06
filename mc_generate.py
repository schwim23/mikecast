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
    """
    Flatten all articles into a structured text block for GPT prompts.
    Each category header explicitly states the article count so the model
    knows exactly how many articles it is allowed to reference.
    """
    lines = []
    for cat, arts in categorised.items():
        count = len(arts)
        if count == 0:
            lines.append(f"\n=== {cat.upper()} — 0 ARTICLES (do not discuss this category) ===")
            continue
        lines.append(f"\n=== {cat.upper()} — {count} ARTICLE{'S' if count != 1 else ''} (discuss ONLY these) ===")
        for i, art in enumerate(arts, 1):
            title = art.get("title", "").replace("[Updated] ", "")
            desc = art.get("description", "")
            url = art.get("url", "")
            source = art.get("source", "")
            updated = "[Updated] " in art.get("title", "")
            prefix = "[UPDATE] " if updated else ""
            lines.append(f"{i}. {prefix}{title}")
            if desc:
                lines.append(f"   Summary: {desc[:500]}")
            if source:
                lines.append(f"   Source: {source}")
            if url:
                lines.append(f"   URL: {url}")
    return "\n".join(lines)


def _safe_url(url: str) -> str:
    """Return url if it starts with http:// or https://, otherwise return '#'."""
    if url and (url.startswith("http://") or url.startswith("https://")):
        return url
    return "#"


def _gpt_call(system_prompt: str, user_prompt: str, max_tokens: int = 2500) -> str:
    """Call GPT-4o and return the response text. Raises on failure or empty response."""
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set — cannot make GPT call.")
        raise RuntimeError("OPENAI_API_KEY not set")
    from openai import OpenAI
    client = OpenAI()
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.4,
    )
    text = resp.choices[0].message.content.strip()
    if not text:
        logger.error("GPT call returned empty response.")
        raise RuntimeError("GPT call returned empty response")
    return text


# ---------------------------------------------------------------------------
# HTML briefing
# ---------------------------------------------------------------------------

def _filter_trending_to_articles(
    trending: list[dict],
    categorised: dict[str, list[dict]],
) -> list[dict]:
    """
    Drop any trending topic that is not supported by the collected articles.

    Two-stage filter:
    1. Keyword gate: at least 1 significant word (≥5 chars) from the topic must
       appear in any article title or description. Topics with zero keyword hits
       are dropped immediately without an API call.
    2. Semantic validation: for topics that pass stage 1, ask GPT-4o whether the
       collected articles actually support the specific claim in the topic phrase.
       Topics the model judges as unsupported or contradicted are dropped.
    """
    # Build corpus and per-article snippet list for semantic check
    corpus_parts: list[str] = []
    article_snippets: list[str] = []
    for arts in categorised.values():
        for art in arts:
            title = art.get("title", "")
            desc = art.get("description", "")
            corpus_parts.append(title.lower())
            corpus_parts.append(desc.lower())
            if title:
                snippet = title if not desc else f"{title} — {desc[:120]}"
                article_snippets.append(snippet)
    corpus = " ".join(corpus_parts)
    articles_block = "\n".join(f"- {s}" for s in article_snippets[:80])

    # Stage 1: keyword gate
    keyword_passed: list[dict] = []
    for item in trending:
        topic = item.get("topic", "")
        keywords = [w.lower() for w in topic.split() if len(w) >= 5]
        if any(kw in corpus for kw in keywords):
            keyword_passed.append(item)
        else:
            logger.info("Trending topic dropped (no keyword match): %r", topic)

    if not keyword_passed or not OPENAI_API_KEY:
        return keyword_passed

    # Stage 2: semantic validation via GPT-4o
    try:
        from openai import OpenAI
        client = OpenAI()

        topics_block = "\n".join(
            f"{i}. {t['topic']}" for i, t in enumerate(keyword_passed, 1)
        )
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict fact-checker. Given a list of trending topic claims "
                        "and a set of collected news article headlines, determine whether each "
                        "topic claim is directly supported by the articles. A topic is SUPPORTED "
                        "only if the articles contain evidence for the specific claim made — "
                        "not just a related subject. A topic is UNSUPPORTED if the articles "
                        "contradict it, describe a different outcome, or merely mention the same "
                        "entity in a different context (e.g. 'OpenAI faces lawsuit' does NOT "
                        "support 'OpenAI launches new model'; 'Fed expects one cut this year' "
                        "does NOT support 'Fed announces rate cut'). "
                        "Return ONLY a JSON array of integers — the 1-based indices of SUPPORTED "
                        "topics. Example: [1, 3]. If none are supported, return []."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"TRENDING TOPIC CLAIMS:\n{topics_block}\n\n"
                        f"COLLECTED ARTICLE HEADLINES:\n{articles_block}"
                    ),
                },
            ],
            max_tokens=50,
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        # Parse JSON array of supported indices
        supported_indices: set[int] = set(json.loads(raw))
    except Exception as exc:
        logger.warning("Trending semantic filter failed (%s) — keeping keyword-passed topics", exc)
        return keyword_passed

    matched: list[dict] = []
    for i, item in enumerate(keyword_passed, 1):
        if i in supported_indices:
            matched.append(item)
        else:
            logger.info("Trending topic dropped (semantic check): %r", item.get("topic", ""))
    return matched


def _build_trending_prompt_block(trending: list[dict]) -> str:
    """
    Format trending items for podcast prompt injection.
    Returns a labeled block listing each topic phrase, or "" if trending is empty.
    Only call this AFTER filtering with _filter_trending_to_articles.
    """
    if not trending:
        return ""
    lines = [f"  {i}. {t.get('topic', '')}" for i, t in enumerate(trending, 1)]
    return (
        "\nTODAY'S SUGGESTED TRENDING TOPICS (verify against the articles above before covering):\n"
        + "\n".join(lines)
        + "\nIMPORTANT: Only reference a trending topic if the articles above directly support it. "
        "If you cannot find an article covering a listed topic, skip it entirely — do NOT invent details.\n"
    )


def _build_trending_html(trending: list[dict]) -> str:
    """Build the amber 'TOP TRENDING NOW' block from Grok topic+x_url dicts."""
    if not trending:
        return ""
    items = ""
    for i, item in enumerate(trending, 1):
        topic = _html.escape(item.get("topic", ""))
        x_url = _safe_url(item.get("x_url", "").strip())
        if x_url != "#":
            x_url_esc = _html.escape(x_url)
            content = (
                f'<a href="{x_url_esc}" style="color:#ffcc80;font-weight:600;'
                f'text-decoration:none;font-size:.95em;">{topic}</a>'
                f'<span style="color:#888;font-size:.75em;margin-left:6px;">↗ X</span>'
            )
        else:
            content = f'<span style="color:#ffcc80;font-weight:600;font-size:.95em;">{topic}</span>'
        items += (
            f'<div style="display:flex;gap:12px;padding:10px 0;border-bottom:1px solid #3a2a10;">'
            f'<span style="color:#ff8c00;font-weight:700;font-size:1.1em;min-width:22px;'
            f'text-align:center;flex-shrink:0;">{i}</span>'
            f'<div>{content}</div>'
            f'</div>'
        )
    return (
        '<div style="background:#1e1408;border:1px solid #8B4513;border-radius:8px;'
        'padding:16px 20px;margin-bottom:24px;">'
        '<h2 style="color:#ff8c00;margin:0 0 10px;font-size:1.05em;letter-spacing:.4px;">'
        '🔥 TOP TRENDING NOW</h2>'
        f'{items}</div>'
    )


def generate_html_briefing(
    categorised: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict] | None = None,
) -> str:
    """Build a professional HTML briefing email using GPT for rich prose."""

    articles_context = _build_articles_context(categorised)
    total_articles = sum(len(v) for v in categorised.values())

    picks_context = ""
    if picks:
        picks_context = "\n\n=== MIKE'S PICKS ==="
        for p in picks:
            picks_context += f"\n- {p.get('title','')}: {p.get('summary','')[:300]}"

    trending = _filter_trending_to_articles(trending or [], categorised)

    system_prompt = (
        "You are MikeCast, a sharp, well-informed daily briefing writer. "
        "Write in a professional yet engaging tone — like a smart friend who reads everything so you don't have to. "
        "Use active voice. Avoid filler phrases.\n\n"
        "STORYTELLING RULE: For every story, tell the reader exactly what happened — the full substance. "
        "Do NOT tease, trail off, or leave things unresolved. Do not write cliffhangers like "
        "'what this means remains to be seen' or 'the fallout could be significant.' "
        "Tell the reader the outcome, the numbers, the decision, the result — whatever the article contains.\n\n"
        "CRITICAL RULE: Only report facts explicitly stated in the provided articles. "
        "Do NOT add details, claims, trades, events, statistics, or context from your training knowledge. "
        "If a category has few or no articles, write only what the articles say — do not fill gaps with invented news. "
        "Every specific claim (names, numbers, events) must trace directly to an article in the input.\n\n"
        "SPORTS TEAM RULE: If an article mentions a player's name but does NOT explicitly state "
        "which team they play for, do NOT name their team. Do not use training knowledge to infer "
        "team affiliations, positions, or stats. Say only what the article says."
    )

    user_prompt = f"""Today is {TODAY_DISPLAY}. You have collected {total_articles} news articles across 4 categories.

Here are today's articles:
{articles_context}
{picks_context}

Write a professional daily briefing (1200-1800 words) with these exact sections:

1. EXECUTIVE SUMMARY (2-3 sentences capturing the most important themes of the day)

2. TOP STORIES — organized by category (AI & Tech, Business & Markets, Companies, NY Sports). For each story:
   - Write a substantive paragraph of AT LEAST 80 words per story. Explain what happened, who was involved,
     what the key details are (numbers, decisions, outcomes), and why it matters.
   - Do NOT summarize in one line. Do NOT use a vague teaser. Tell the full story as the article presents it.
   - Include the clickable source URL at the end of each story item
   - Cover at least 3-4 stories per category that has content

   NY SPORTS rules (apply these exactly):
   - For each NY team (Yankees, Knicks, Giants, Devils): if they played a game in the last 24 hours,
     state the final score and opponent, and mention when their next game is. If there is a major
     story or highlight beyond game results (trade, injury, milestone), include that too. Keep it to
     1-2 sentences per team. Skip a team entirely if nothing substantive happened.
   - For major national sports stories unrelated to NY teams: summarize in 1-2 sentences.
   - Do NOT write generic filler about a team if there is nothing in the articles to report.

3. KEY TRENDS & INSIGHTS (3-5 bullet points identifying patterns, themes, or connections across today's stories)

4. WHAT TO WATCH (3-4 forward-looking items — what developments to monitor in the coming days)

Format rules:
- Return ONLY the briefing text sections (no HTML, no markdown headers with #)
- Use plain section headers like: EXECUTIVE SUMMARY, AI & TECH, BUSINESS & MARKETS, COMPANIES, NY SPORTS, KEY TRENDS & INSIGHTS, WHAT TO WATCH
- Each story should be on its own paragraph
- IMPORTANT: End every story paragraph with a clickable source link in this exact format: [Source Name](URL)
  Use the exact URL provided in the article data above — do not make up or omit URLs"""

    try:
        briefing_text = _gpt_call(system_prompt, user_prompt, max_tokens=3500)
    except Exception as exc:
        logger.error("HTML briefing GPT call failed: %s", exc)
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
    trending_html = _build_trending_html(trending)

    picks_html = ""
    if picks:
        picks_html = (
            '<h2 style="color:#ffb74d;border-bottom:1px solid #444;'
            'padding-bottom:6px;margin-top:28px;">🎯 Mike\'s Picks</h2>\n<ul>\n'
        )
        for p in picks:
            title = _html.escape(p.get("title", "Untitled"))
            summary = _html.escape(p.get("summary", ""))
            url = _safe_url(p.get("url", ""))
            if url != "#":
                picks_html += (
                    f'<li style="margin-bottom:10px;">'
                    f'<a href="{_html.escape(url)}" style="color:#ffcc80;text-decoration:none;font-weight:600;">{title}</a>'
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

{trending_html}
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
    trending: list[dict] | None = None,
) -> str:
    """Create a conversational 5-10 minute single-voice podcast script using GPT."""

    articles_context = _build_articles_context(categorised)
    picks_context = ""
    if picks:
        picks_context = "\n\n=== MIKE'S PICKS ==="
        for p in picks:
            picks_context += f"\n- {p.get('title','')}: {p.get('summary','')[:300]}"

    trending_block = _build_trending_prompt_block(
        _filter_trending_to_articles(trending or [], categorised)
    )

    system_prompt = (
        "You are the host of MikeCast, a daily news podcast. "
        "Your style is smart, conversational, and energetic — like a knowledgeable friend catching you up over coffee. "
        "You speak directly to the listener. You add context and insight — not just headlines. "
        "You're substantive but never dry. You use natural spoken language, not written prose.\n\n"
        "STORYTELLING RULE: For every story, fully explain what happened — the outcome, the numbers, the key "
        "people, the decision. Do NOT tease or leave the listener hanging with lines like 'we'll have to wait "
        "and see' or 'the implications could be huge.' Tell them what the article actually says happened.\n\n"
        "CRITICAL RULE: Only discuss stories explicitly present in the provided articles. "
        "Do NOT mention trades, events, statistics, or facts from your training knowledge that aren't in the input. "
        "If a category has few articles, keep that segment short — never invent news to fill time.\n\n"
        "SPORTS TEAM RULE: If an article mentions a player's name but does NOT explicitly state "
        "which team they play for, do NOT name their team. Do not use training knowledge to infer "
        "team affiliations, positions, or stats. Say only what the article says."
    )

    user_prompt = f"""Today is {TODAY_DISPLAY}. Write a full podcast script for today's MikeCast episode.
{trending_block}
Here are today's news articles:
{articles_context}
{picks_context}

Script requirements:
- Total length: 10-14 minutes of spoken audio. At a natural podcast pace of 140 words per minute,
  that means a MINIMUM of 1400 words and a TARGET of 1800-2000 words. Do not stop short.
- Structure with approximate word targets per segment:
  1. Warm, engaging INTRO — welcome listeners, tease the top 2-3 stories (~100 words).
     If trending topics are listed above, briefly name 1-2 of them to hook listeners.
  2. AI & TECH segment — cover the top 3-4 stories with full context, details, and why it matters (~500 words).
     If trending topics are listed above, open by framing them as today's most-watched stories,
     then cover the full substance of each from the articles.
  3. BUSINESS & MARKETS segment — cover top 2-3 stories in depth, explain what it means for listeners (~400 words)
  4. COMPANIES segment — cover top 3-4 company stories with personality and substance (~400 words)
  5. NY SPORTS segment (~100-150 words):
     - For each NY team (Yankees, Knicks, Giants, Devils): if they played a game in the last 24 hours,
       say the final score and opponent, and mention when their next game is. If there is a notable
       story or highlight beyond the game result, include it briefly. Keep it to 1-2 sentences per team.
       Skip a team entirely if nothing noteworthy happened — do not pad with filler.
     - For major national sports stories unrelated to NY teams: 1-2 sentences.
     - Do NOT mention any team, player, or event not explicitly named in the articles above.
  6. MIKE'S PICKS segment (only if picks exist) — introduce as "Big Mike's hand-picked reads" (~150 words)
  7. OUTRO — brief wrap-up, call to action, sign-off (~50 words)

- Write in natural spoken language — use contractions, rhetorical questions, transitions
- For each major story, include the actual facts: numbers, names, outcomes, decisions — not vague commentary
- Use natural transitions between segments (e.g., "Alright, switching gears...", "Now let's talk money...")
- Do NOT include stage directions like [MUSIC] or [PAUSE] — write only the spoken words
- Do NOT include URLs in the script — this is audio only
- Write the full script, not an outline"""

    try:
        script = _gpt_call(system_prompt, user_prompt, max_tokens=4000)
    except Exception as exc:
        logger.error("Podcast script GPT call failed: %s", exc)
        script = ""

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
    trending: list[dict] | None = None,
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

    trending_block = _build_trending_prompt_block(
        _filter_trending_to_articles(trending or [], categorised)
    )

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
        "- Each host segment should feel like a real broadcast, not a list.\n"
        "- For every story, ELIZABETH should convey the actual substance: the numbers, the outcome, "
        "the key people, the decision. Do NOT use vague teaser language like 'we'll see what happens' "
        "or 'this could be significant.' Tell the listener what the article says happened.\n\n"
        "CRITICAL RULE: Only discuss stories explicitly present in the provided articles. "
        "Do NOT mention trades, signings, game scores, injuries, or any sports/business facts "
        "from your training knowledge that aren't in the input articles. "
        "If a category (especially NY Sports) has few or no articles, Jesse should say there's "
        "not much happening today and keep it brief — never fabricate news.\n\n"
        "SPORTS TEAM RULE: If an article mentions a player's name but does NOT explicitly state "
        "which team they play for, Jesse must NOT name their team. Do not use training knowledge "
        "to infer team affiliations, positions, or stats. Say only what the article says."
    )

    user_prompt = f"""Today is {TODAY_DISPLAY}. Write the full MikeCast 3-host podcast script.
{trending_block}
Here are today's articles:
{articles_context}
{picks_context}

Script structure:
1. [MIKE] INTRO — Welcome listeners, briefly tease the top 2-3 stories (~30 seconds).
   If trending topics are listed above, name 1-2 of them to hook listeners.
2. [ELIZABETH] AI & TECH — Cover top 3-4 stories with full context, key details, and why it matters.
   If trending topics are listed above, open with a line like "Today's most-watched stories are..."
   then work through each one with full substance from the articles.
   For each story: explain what happened, who's involved, what the specific numbers/outcomes are.
3. [ELIZABETH] BUSINESS & MARKETS — Cover top 2-3 stories in depth, explain what it means for listeners.
4. [ELIZABETH] COMPANIES — Cover top 3-4 company stories with personality and substance.
   End with a handoff: "Alright Jesse, take it away with sports..."
5. [JESSE] NY SPORTS (~100-150 words total):
   - For each NY team (Yankees, Knicks, Giants, Devils): if they played a game in the last 24 hours,
     say the final score and opponent, and mention when their next game is. If there's a notable
     story or highlight, include it too. Keep it to 1-2 sentences per team.
     Skip a team entirely if nothing noteworthy happened — do not pad with filler.
   - For major national sports stories unrelated to NY teams: 1-2 sentences.
   - Do NOT mention any team, player, coach, or event not explicitly named in the articles above.
   End with: "Back to you, Mike."
6. [MIKE] SIGN-OFF — Brief wrap-up, thank listeners, sign off (~20 seconds).

Total length: 10-14 minutes of spoken audio. At a natural podcast pace of 140 words per minute,
that means a MINIMUM of 1400 words and a TARGET of 1800-2000 words. Do not stop short.
Write the COMPLETE script with all tags. No outline, no placeholders."""

    try:
        script = _gpt_call(system_prompt, user_prompt, max_tokens=3500)
    except Exception as exc:
        logger.error("Conversational script GPT call failed: %s", exc)
        script = ""

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
                        "Do not start with 'Episode', a number, or the word 'Today'. "
                        "CRITICAL: Only mention stories and facts that are explicitly stated "
                        "in the podcast script below. Do not add details, events, or claims "
                        "from your training knowledge. If a topic is only vaguely mentioned, "
                        "omit it rather than embellish it."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Podcast script:\n\n{podcast_script[:6000]}",
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
