#!/usr/bin/env python3
"""
MikeCast Daily Briefing — Enhanced Edition
============================================
Collects news from multiple sources (web search + NYT APIs), deduplicates
against a rolling 7-day history, processes Mike's Picks, generates an HTML
briefing and podcast script, synthesises audio via OpenAI TTS, and emails
the complete package.

All secrets are read from environment variables — nothing is hardcoded.
"""

import json
import logging
import os
import re
import smtplib
import sys
import time
import hashlib
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore
from difflib import SequenceMatcher
from email import encoders
from email.mime.audio import MIMEAudio
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("mikecast")

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

HISTORY_FILE = SCRIPT_DIR / "briefing_history.json"
PICKS_FILE = SCRIPT_DIR / "mikes_picks.json"

# ---------------------------------------------------------------------------
# Environment variables (secrets)
# ---------------------------------------------------------------------------
NYT_API_KEY = os.environ.get("NYTAPIKEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace("\\n", "").replace("\n", "").strip()
GMAIL_FROM = os.environ.get("GMAIL_FROM", "prometheusagent23@gmail.com")
GMAIL_TO = os.environ.get("GMAIL_TO", "Michael.schwimmer@gmail.com")

# Use Eastern Time so file names match what the browser (in EST/EDT) expects
_ET = ZoneInfo("America/New_York")
TODAY = datetime.now(_ET).strftime("%Y-%m-%d")
TODAY_DISPLAY = datetime.now(_ET).strftime("%B %d, %Y")

# ---------------------------------------------------------------------------
# Search categories
# ---------------------------------------------------------------------------
CATEGORIES = {
    "AI & Tech": [
        "OpenAI", "Anthropic Claude AI", "Google AI Gemini",
        "Microsoft AI Copilot", "AI startups funding",
        "artificial intelligence breakthroughs",
    ],
    "Business & Markets": [
        "stock market today", "Nasdaq S&P 500 today",
        "AI spending enterprise", "venture capital funding",
        "Federal Reserve economy",
    ],
    "Companies": [
        "Apple news today", "Meta Facebook news",
        "Amazon news today", "Nvidia news today",
        "Tesla news today", "Netflix news today",
        "Microsoft news today", "Google Alphabet news",
    ],
    "NY Sports": [
        "New York Yankees", "New York Knicks",
        "New York Giants NFL", "New Jersey Devils NHL",
    ],
}

# NYT sections to pull top stories from
NYT_SECTIONS = ["technology", "business", "sports", "home"]

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe_request(url: str, params: dict | None = None, timeout: int = 15) -> requests.Response | None:
    """GET with retries and error handling."""
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning("Rate-limited on %s — waiting %ds", url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.warning("Request failed (attempt %d): %s", attempt + 1, exc)
            time.sleep(1)
    return None


def resolve_google_news_url(url: str) -> str:
    """
    Google News RSS links are redirect URLs.  Follow the redirect chain
    (up to 5 hops) to get the real article URL.  Falls back to the
    original URL on any error.
    """
    if not url or "news.google.com" not in url:
        return url
    try:
        resp = requests.head(
            url,
            allow_redirects=True,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MikeCast/1.0)"},
        )
        final = resp.url
        # Sometimes Google returns a consent page — keep original in that case
        if "google.com" in final or "consent" in final:
            return url
        return final
    except Exception:
        return url


def title_similarity(a: str, b: str) -> float:
    """Return 0-1 similarity ratio between two titles."""
    a_clean = re.sub(r"[^a-z0-9 ]", "", a.lower().strip())
    b_clean = re.sub(r"[^a-z0-9 ]", "", b.lower().strip())
    return SequenceMatcher(None, a_clean, b_clean).ratio()


def url_fingerprint(url: str) -> str:
    """Normalise a URL into a stable fingerprint."""
    url = re.sub(r"https?://", "", url).rstrip("/").lower()
    url = re.sub(r"[?#].*", "", url)
    return hashlib.md5(url.encode()).hexdigest()

# ===================================================================
# 1. NEWS COLLECTION
# ===================================================================

def search_news_via_nyt_article_search(query: str, max_results: int = 5) -> list[dict]:
    """Search NYT Article Search API for recent articles matching *query*."""
    if not NYT_API_KEY:
        logger.warning("NYTAPIKEY not set — skipping NYT Article Search.")
        return []

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    today_fmt = datetime.now(timezone.utc).strftime("%Y%m%d")

    params = {
        "q": query,
        "begin_date": yesterday,
        "end_date": today_fmt,
        "sort": "newest",
        "api-key": NYT_API_KEY,
    }
    resp = _safe_request(
        "https://api.nytimes.com/svc/search/v2/articlesearch.json",
        params=params,
    )
    if resp is None:
        return []

    articles = []
    try:
        response_data = resp.json() or {}
        docs = (response_data.get("response") or {}).get("docs") or []
        for doc in docs[:max_results]:
            headline = doc.get("headline", {}).get("main", "")
            articles.append({
                "title": headline,
                "url": doc.get("web_url", ""),
                "description": doc.get("snippet", ""),
                "source": "The New York Times",
                "published": doc.get("pub_date", ""),
            })
    except (KeyError, ValueError) as exc:
        logger.warning("NYT Article Search parse error: %s", exc)
    return articles


def fetch_nyt_top_stories(section: str) -> list[dict]:
    """Fetch top stories for a given NYT section."""
    if not NYT_API_KEY:
        logger.warning("NYTAPIKEY not set — skipping NYT Top Stories.")
        return []

    url = f"https://api.nytimes.com/svc/topstories/v2/{section}.json"
    resp = _safe_request(url, params={"api-key": NYT_API_KEY})
    if resp is None:
        return []

    articles = []
    try:
        results = resp.json().get("results") or []
        for item in results[:8]:
            articles.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("abstract", ""),
                "source": "The New York Times",
                "published": item.get("published_date", ""),
            })
    except (KeyError, ValueError) as exc:
        logger.warning("NYT Top Stories parse error for %s: %s", section, exc)
    return articles


def search_news_web(query: str, max_results: int = 5) -> list[dict]:
    """
    Lightweight web-search fallback using Google News RSS.
    Returns a list of article dicts.
    """
    rss_url = "https://news.google.com/rss/search"
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    resp = _safe_request(rss_url, params=params)
    if resp is None:
        return []

    articles = []
    try:
        soup = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")
        for item in items[:max_results]:
            title_tag = item.find("title")
            link_tag = item.find("link")
            desc_tag = item.find("description")
            source_tag = item.find("source")
            pub_tag = item.find("pubdate")
            # Use the publisher's domain URL from <source url='...'> as the article link.
            # Google News RSS redirect URLs cannot be resolved server-side (consent wall).
            source_url = source_tag.get("url", "") if source_tag else ""
            article_url = source_url if source_url else (link_tag.get_text(strip=True) if link_tag else "")
            articles.append({
                "title": title_tag.get_text(strip=True) if title_tag else "",
                "url": article_url,
                "description": BeautifulSoup(desc_tag.get_text(), "html.parser").get_text(strip=True) if desc_tag else "",
                "source": source_tag.get_text(strip=True) if source_tag else "Google News",
                "published": pub_tag.get_text(strip=True) if pub_tag else "",
            })
    except Exception as exc:
        logger.warning("Google News RSS parse error: %s", exc)
    return articles


def collect_all_news() -> dict[str, list[dict]]:
    """
    Collect articles for every category from both NYT APIs and web search.
    Returns {category: [article_dicts]}.
    """
    categorised: dict[str, list[dict]] = {cat: [] for cat in CATEGORIES}

    # --- NYT Top Stories ---
    section_to_category = {
        "technology": "AI & Tech",
        "business": "Business & Markets",
        "sports": "NY Sports",
        "home": "AI & Tech",  # homepage may have cross-cutting stories
    }
    for section in NYT_SECTIONS:
        cat = section_to_category.get(section, "AI & Tech")
        stories = fetch_nyt_top_stories(section)
        categorised[cat].extend(stories)
        logger.info("NYT Top Stories [%s]: %d articles", section, len(stories))
        time.sleep(0.5)  # respect rate limits

    # --- NYT Article Search per category ---
    nyt_search_queries = {
        "AI & Tech": ["artificial intelligence", "OpenAI Anthropic"],
        "Business & Markets": ["stock market economy", "venture capital AI"],
        "Companies": ["Apple Meta Amazon Nvidia Tesla"],
        "NY Sports": ["Yankees Knicks Giants Devils"],
    }
    for cat, queries in nyt_search_queries.items():
        for q in queries:
            results = search_news_via_nyt_article_search(q, max_results=3)
            categorised[cat].extend(results)
            logger.info("NYT Search [%s] '%s': %d articles", cat, q, len(results))
            time.sleep(0.5)

    # --- Google News RSS per category ---
    for cat, queries in CATEGORIES.items():
        for q in queries:
            results = search_news_web(q, max_results=3)
            categorised[cat].extend(results)
            logger.info("Web [%s] '%s': %d articles", cat, q, len(results))
            time.sleep(0.3)

    return categorised


# ===================================================================
# 2. DEDUPLICATION
# ===================================================================

def load_history() -> list[dict]:
    """Load the rolling 7-day history."""
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_history(history: list[dict]) -> None:
    """Persist history, pruning entries older than 7 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    pruned = [h for h in history if h.get("date", "") >= cutoff]
    with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
        json.dump(pruned, fh, indent=2, ensure_ascii=False)


def deduplicate(categorised: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """
    Remove duplicates within the current batch and against the 7-day history.
    Mark stories that reappear with significant updates as '[Updated]'.
    Returns the deduplicated dict and updates the history file.
    """
    history = load_history()
    hist_urls = {h.get("url_fp") for h in history}
    hist_titles = {h.get("title", ""): h for h in history}

    seen_fps: set[str] = set()
    deduped: dict[str, list[dict]] = {cat: [] for cat in categorised}
    new_history_entries: list[dict] = []

    for cat, articles in categorised.items():
        for art in articles:
            title = art.get("title", "").strip()
            url = art.get("url", "").strip()
            if not title or not url:
                continue

            fp = url_fingerprint(url)

            # Skip exact URL duplicates within this batch
            if fp in seen_fps:
                continue
            seen_fps.add(fp)

            # Check against history
            is_duplicate = False
            is_updated = False

            if fp in hist_urls:
                is_duplicate = True
                # Check if description changed significantly (= update)
                for h in history:
                    if h.get("url_fp") == fp:
                        old_desc = h.get("description", "")
                        new_desc = art.get("description", "")
                        if old_desc and new_desc and title_similarity(old_desc, new_desc) < 0.7:
                            is_updated = True
                        break
            else:
                # Check title similarity
                for hist_title in hist_titles:
                    if title_similarity(title, hist_title) > 0.85:
                        is_duplicate = True
                        old_desc = hist_titles[hist_title].get("description", "")
                        new_desc = art.get("description", "")
                        if old_desc and new_desc and title_similarity(old_desc, new_desc) < 0.7:
                            is_updated = True
                        break

            if is_duplicate and not is_updated:
                logger.debug("Skipping duplicate: %s", title[:60])
                continue

            if is_updated:
                art["title"] = f"[Updated] {title}"

            deduped[cat].append(art)
            new_history_entries.append({
                "title": title,
                "url": url,
                "url_fp": fp,
                "description": art.get("description", ""),
                "date": datetime.now(timezone.utc).isoformat(),
            })

    # Merge new entries into history and save
    history.extend(new_history_entries)
    save_history(history)

    return deduped


def select_top_articles(categorised: dict[str, list[dict]], total: int = 25) -> dict[str, list[dict]]:
    """Trim each category to keep roughly *total* articles across all categories."""
    counts = {cat: len(arts) for cat, arts in categorised.items()}
    total_available = sum(counts.values())
    if total_available == 0:
        return categorised

    selected: dict[str, list[dict]] = {}
    for cat, arts in categorised.items():
        share = max(3, int(total * len(arts) / total_available))
        selected[cat] = arts[:share]
    return selected


# ===================================================================
# 3. MIKE'S PICKS
# ===================================================================

def load_picks() -> list[dict]:
    if not PICKS_FILE.exists():
        return []
    try:
        with open(PICKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return [p for p in data if not p.get("processed")]
    except (json.JSONDecodeError, IOError):
        return []


def mark_picks_processed() -> None:
    """Mark all current picks as processed."""
    if not PICKS_FILE.exists():
        return
    try:
        with open(PICKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for item in data:
            item["processed"] = True
        with open(PICKS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, IOError) as exc:
        logger.warning("Could not mark picks processed: %s", exc)


def summarise_pick(pick: dict) -> dict:
    """
    Produce a summary dict for a single pick.
    For URLs we fetch the page and extract text; for PDFs we note the path;
    for raw text we use it directly.
    """
    ptype = pick.get("type", "text")
    content = pick.get("content", "")
    title = pick.get("title", "")

    if ptype == "url":
        # Fetch and summarise the URL
        try:
            resp = requests.get(content, timeout=10, headers={"User-Agent": "MikeCast/1.0"})
            soup = BeautifulSoup(resp.text, "html.parser")
            if not title:
                title = soup.title.get_text(strip=True) if soup.title else content
            # Extract first ~500 chars of body text
            body_text = soup.get_text(separator=" ", strip=True)[:1500]
            summary = body_text[:500] + ("..." if len(body_text) > 500 else "")
        except Exception:
            summary = f"Submitted URL: {content}"
            if not title:
                title = content
        return {"title": title, "summary": summary, "url": content, "type": "url"}

    elif ptype == "pdf":
        summary = f"PDF document submitted: {os.path.basename(content)}"
        # Attempt text extraction with pdftotext if available
        try:
            import subprocess
            result = subprocess.run(
                ["pdftotext", content, "-", "-l", "3"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                extracted = result.stdout.strip()[:1500]
                summary = extracted[:500] + ("..." if len(extracted) > 500 else "")
        except Exception:
            pass
        if not title:
            title = os.path.basename(content)
        return {"title": title, "summary": summary, "url": "", "type": "pdf"}

    else:  # raw text
        if not title:
            title = content[:80].strip() + ("..." if len(content) > 80 else "")
        summary = content[:500] + ("..." if len(content) > 500 else "")
        return {"title": title, "summary": summary, "url": "", "type": "text"}


def process_picks() -> list[dict]:
    """Load, summarise, and mark-as-processed all pending picks."""
    raw_picks = load_picks()
    if not raw_picks:
        return []
    summaries = [summarise_pick(p) for p in raw_picks]
    mark_picks_processed()
    logger.info("Processed %d Mike's Pick(s).", len(summaries))
    return summaries


# ===================================================================
# 4. HTML BRIEFING GENERATION
# ===================================================================

def _build_articles_context(categorised: dict[str, list[dict]]) -> str:
    """Flatten all articles into a structured text block for the GPT prompt."""
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
    """Call GPT and return the response text, with a plain-text fallback."""
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
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("GPT call failed: %s", exc)
        return ""


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
        # Fallback: simple concatenation
        briefing_text = "Unable to generate GPT briefing. See articles below."

    # --- Convert the GPT plain-text briefing into styled HTML ---
    def text_to_html_sections(text: str) -> str:
        """Convert the structured plain-text briefing into HTML."""
        section_headers = [
            "EXECUTIVE SUMMARY", "AI & TECH", "BUSINESS & MARKETS",
            "COMPANIES", "NY SPORTS", "KEY TRENDS & INSIGHTS", "WHAT TO WATCH",
        ]
        html_parts = []
        current_section = None
        buffer = []

        def flush_buffer(section, buf):
            if not buf:
                return ""
            color = "#ffb74d" if section in ("KEY TRENDS & INSIGHTS", "WHAT TO WATCH") else "#4fc3f7"
            out = f'<h2 style="color:{color};border-bottom:1px solid #444;padding-bottom:6px;margin-top:28px;">{section}</h2>\n'
            combined = " ".join(buf).strip()
            # Convert [Source](URL) markdown links to HTML
            combined = re.sub(
                r'\[([^\]]+)\]\((https?://[^)]+)\)',
                r'<a href="\2" style="color:#81d4fa;text-decoration:none;">\1</a>',
                combined,
            )
            # Split into paragraphs on double newline or sentence-ending period followed by capital
            paragraphs = re.split(r'\n{2,}', combined)
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                # Bullet points
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
                # Remainder of the line after the header
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

    # --- Mike's Picks ---
    picks_html = ""
    if picks:
        picks_html = '<h2 style="color:#ffb74d;border-bottom:1px solid #444;padding-bottom:6px;margin-top:28px;">🎯 Mike\'s Picks</h2>\n<ul>\n'
        for p in picks:
            title = p.get("title", "Untitled")
            summary = p.get("summary", "")
            url = p.get("url", "")
            if url:
                picks_html += f'<li style="margin-bottom:10px;"><a href="{url}" style="color:#ffcc80;text-decoration:none;font-weight:600;">{title}</a>'
            else:
                picks_html += f'<li style="margin-bottom:10px;"><strong style="color:#ffcc80;">{title}</strong>'
            if summary:
                picks_html += f'<br><span style="color:#bbb;font-size:0.9em;">{summary[:300]}</span>'
            picks_html += "</li>\n"
        picks_html += "</ul>\n"

    # --- Full HTML ---
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
  Powered by NYT API, Google News &amp; OpenAI GPT-4o</p>
</div>
</body>
</html>"""
    return html


# ===================================================================
# 5. PODCAST SCRIPT
# ===================================================================

def generate_podcast_script(
    categorised: dict[str, list[dict]],
    picks: list[dict],
) -> str:
    """Create a conversational 5-10 minute podcast script using GPT."""

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
- Total length: 5-10 minutes of spoken audio (approximately 800-1500 words)
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
        # Fallback to simple script
        logger.warning("GPT podcast script generation failed — using simple fallback.")
        lines: list[str] = []
        lines.append(f"Hey everyone, welcome to MikeCast. It's {TODAY_DISPLAY}. Let's get into it.")
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


# ===================================================================
# 6. TTS AUDIO GENERATION
# ===================================================================

def generate_podcast_audio(script: str, output_path: Path) -> bool:
    """Generate MP3 audio from the podcast script using OpenAI TTS."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set — skipping audio generation.")
        return False

    try:
        from openai import OpenAI
        client = OpenAI()  # uses OPENAI_API_KEY env var automatically

        # TTS has a 4096-char limit per request — split if needed
        max_chunk = 4000
        chunks = []
        remaining = script
        while remaining:
            if len(remaining) <= max_chunk:
                chunks.append(remaining)
                break
            # Find a sentence boundary near the limit
            split_at = remaining[:max_chunk].rfind(". ")
            if split_at == -1:
                split_at = max_chunk
            else:
                split_at += 2
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]

        audio_segments: list[bytes] = []
        for i, chunk in enumerate(chunks):
            logger.info("Generating TTS chunk %d/%d (%d chars)…", i + 1, len(chunks), len(chunk))
            response = client.audio.speech.create(
                model="tts-1-hd",
                voice="alloy",
                input=chunk,
            )
            audio_segments.append(response.content)
            time.sleep(0.5)

        with open(output_path, "wb") as fh:
            for seg in audio_segments:
                fh.write(seg)

        logger.info("Podcast audio saved: %s (%.1f MB)", output_path, output_path.stat().st_size / 1e6)
        return True

    except Exception as exc:
        logger.error("TTS generation failed: %s", exc)
        return False


# ===================================================================
# 7. EMAIL DELIVERY
# ===================================================================

def send_email(
    html_body: str,
    podcast_script: str,
    audio_path: Path | None,
) -> bool:
    """Send the briefing via Gmail SMTP."""
    if not GMAIL_APP_PASSWORD:
        logger.warning("GMAIL_APP_PASSWORD not set — skipping email.")
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = GMAIL_FROM
    msg["To"] = GMAIL_TO
    msg["Subject"] = f"MikeCast Daily Briefing — {TODAY_DISPLAY}"

    # HTML body
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Podcast script attachment
    script_part = MIMEText(podcast_script, "plain", "utf-8")
    script_part.add_header("Content-Disposition", "attachment", filename=f"MikeCast_Script_{TODAY}.txt")
    msg.attach(script_part)

    # Audio attachment
    if audio_path and audio_path.exists():
        with open(audio_path, "rb") as fh:
            audio_part = MIMEAudio(fh.read(), _subtype="mpeg")
        audio_part.add_header("Content-Disposition", "attachment", filename=f"MikeCast_{TODAY}.mp3")
        msg.attach(audio_part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_FROM, [GMAIL_TO], msg.as_string())
        logger.info("Email sent to %s", GMAIL_TO)
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


# ===================================================================
# 8. DATA PERSISTENCE (for dashboard)
# ===================================================================

def generate_manifest() -> None:
    """Write a manifest.json listing all available briefing dates (newest first)."""
    dates = sorted(
        [p.stem for p in DATA_DIR.glob("????-??-??.json")],
        reverse=True,
    )
    manifest = {"dates": dates}
    manifest_path = DATA_DIR / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("Manifest updated: %d dates", len(dates))


def save_daily_data(
    html_briefing: str,
    categorised: dict[str, list[dict]],
    picks: list[dict],
    podcast_script: str,
    audio_filename: str | None,
) -> Path:
    """Save all briefing data as a JSON file for the dashboard."""
    data = {
        "date": TODAY,
        "date_display": TODAY_DISPLAY,
        "html_briefing": html_briefing,
        "articles": categorised,
        "mikes_picks": picks,
        "podcast_script": podcast_script,
        "audio_file": audio_filename,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path = DATA_DIR / f"{TODAY}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    logger.info("Daily data saved: %s", out_path)
    return out_path


# ===================================================================
# MAIN
# ===================================================================

def main() -> None:
    logger.info("=" * 60)
    logger.info("MikeCast Daily Briefing — %s", TODAY_DISPLAY)
    logger.info("=" * 60)

    # 1. Collect news
    logger.info("Step 1/7: Collecting news…")
    raw_news = collect_all_news()

    # 2. Deduplicate
    logger.info("Step 2/7: Deduplicating…")
    deduped = deduplicate(raw_news)

    # 3. Select top articles
    logger.info("Step 3/7: Selecting top articles…")
    top_articles = select_top_articles(deduped, total=25)
    total = sum(len(v) for v in top_articles.values())
    logger.info("Selected %d articles across %d categories.", total, len(top_articles))

    # 4. Process Mike's Picks
    logger.info("Step 4/7: Processing Mike's Picks…")
    picks = process_picks()

    # 5. Generate HTML briefing
    logger.info("Step 5/7: Generating HTML briefing…")
    html = generate_html_briefing(top_articles, picks)

    # 6. Generate podcast
    logger.info("Step 6/7: Generating podcast script & audio…")
    script = generate_podcast_script(top_articles, picks)
    audio_path = DATA_DIR / f"MikeCast_{TODAY}.mp3"
    audio_ok = generate_podcast_audio(script, audio_path)
    audio_filename = audio_path.name if audio_ok else None

    # 7. Save & send
    logger.info("Step 7/7: Saving data & sending email…")
    save_daily_data(html, top_articles, picks, script, audio_filename)
    generate_manifest()
    send_email(html, script, audio_path if audio_ok else None)

    logger.info("MikeCast briefing complete.")


if __name__ == "__main__":
    main()
