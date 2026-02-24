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
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_FROM = os.environ.get("GMAIL_FROM", "prometheusagent23@gmail.com")
GMAIL_TO = os.environ.get("GMAIL_TO", "Michael.schwimmer@gmail.com")

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
TODAY_DISPLAY = datetime.now(timezone.utc).strftime("%B %d, %Y")

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
            articles.append({
                "title": title_tag.get_text(strip=True) if title_tag else "",
                "url": link_tag.get_text(strip=True) if link_tag else "",
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

def generate_html_briefing(
    categorised: dict[str, list[dict]],
    picks: list[dict],
) -> str:
    """Build a professional HTML briefing email."""

    total_articles = sum(len(v) for v in categorised.values())

    # --- Executive Summary ---
    exec_lines = []
    for cat, arts in categorised.items():
        if arts:
            top = arts[0]
            exec_lines.append(f"<strong>{cat}:</strong> {top['title']}")

    exec_summary = " | ".join(exec_lines) if exec_lines else "No major stories today."

    # --- Build category sections ---
    category_html = ""
    for cat, arts in categorised.items():
        if not arts:
            continue
        category_html += f'<h2 style="color:#4fc3f7;border-bottom:1px solid #444;padding-bottom:6px;">{cat}</h2>\n<ul>\n'
        for art in arts:
            title = art.get("title", "Untitled")
            url = art.get("url", "#")
            desc = art.get("description", "")
            source = art.get("source", "")
            updated_tag = ""
            if title.startswith("[Updated]"):
                updated_tag = '<span style="background:#ff9800;color:#000;padding:1px 6px;border-radius:3px;font-size:0.8em;margin-right:4px;">Updated</span>'
                title = title.replace("[Updated] ", "")
            source_badge = f' <span style="color:#888;font-size:0.85em;">— {source}</span>' if source else ""
            category_html += f'<li style="margin-bottom:10px;">{updated_tag}<a href="{url}" style="color:#81d4fa;text-decoration:none;font-weight:600;">{title}</a>{source_badge}'
            if desc:
                category_html += f'<br><span style="color:#bbb;font-size:0.9em;">{desc[:200]}</span>'
            category_html += "</li>\n"
        category_html += "</ul>\n"

    # --- Mike's Picks ---
    picks_html = ""
    if picks:
        picks_html = '<h2 style="color:#ffb74d;border-bottom:1px solid #444;padding-bottom:6px;">🎯 Mike\'s Picks</h2>\n<ul>\n'
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

    # --- Trends & What to Watch ---
    trends_html = '<h2 style="color:#4fc3f7;border-bottom:1px solid #444;padding-bottom:6px;">Key Trends &amp; Insights</h2>\n'
    trends_html += '<p style="color:#ccc;">Today\'s briefing covers <strong>{}</strong> stories across {} categories. '.format(total_articles, len([c for c in categorised if categorised[c]]))

    ai_count = len(categorised.get("AI & Tech", []))
    biz_count = len(categorised.get("Business & Markets", []))
    if ai_count > 5:
        trends_html += "AI and technology continue to dominate headlines. "
    if biz_count > 3:
        trends_html += "Markets and business activity remain active. "
    trends_html += "</p>\n"

    watch_html = '<h2 style="color:#4fc3f7;border-bottom:1px solid #444;padding-bottom:6px;">What to Watch</h2>\n'
    watch_items = []
    if categorised.get("AI & Tech"):
        watch_items.append("AI sector developments and regulatory moves")
    if categorised.get("Business & Markets"):
        watch_items.append("Market reactions and earnings reports")
    if categorised.get("Companies"):
        watch_items.append("Big Tech product launches and strategic shifts")
    if categorised.get("NY Sports"):
        watch_items.append("Upcoming NY sports matchups and trade rumours")
    if watch_items:
        watch_html += "<ul>" + "".join(f'<li style="color:#ccc;">{w}</li>' for w in watch_items) + "</ul>\n"

    # --- Full HTML ---
    html = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="background:#1a1a2e;color:#e0e0e0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:700px;margin:auto;padding:20px;">
<div style="text-align:center;padding:20px 0;border-bottom:2px solid #4fc3f7;">
  <h1 style="color:#4fc3f7;margin:0;font-size:2em;">🎙️ MikeCast</h1>
  <p style="color:#888;margin:4px 0 0;">Daily Briefing — {TODAY_DISPLAY}</p>
</div>

<h2 style="color:#4fc3f7;border-bottom:1px solid #444;padding-bottom:6px;">Executive Summary</h2>
<p style="color:#ccc;line-height:1.6;">{exec_summary}</p>

{category_html}

{picks_html}

{trends_html}

{watch_html}

<div style="text-align:center;padding:20px 0;border-top:1px solid #444;margin-top:30px;">
  <p style="color:#666;font-size:0.85em;">MikeCast Daily Briefing • Generated {TODAY_DISPLAY}<br>
  Powered by NYT API, Google News &amp; OpenAI</p>
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
    """Create a conversational 5-10 minute podcast script."""
    lines: list[str] = []
    lines.append(f"[INTRO MUSIC FADES IN]\n")
    lines.append(f"Hey everyone, welcome to MikeCast — your daily news briefing. It's {TODAY_DISPLAY}, and I've got a packed show for you today. Let's dive right in.\n")

    for cat, arts in categorised.items():
        if not arts:
            continue
        lines.append(f"\n--- {cat.upper()} ---\n")
        lines.append(f"Starting with {cat}.\n")
        for i, art in enumerate(arts[:4]):
            title = art["title"].replace("[Updated] ", "")
            desc = art.get("description", "")
            updated = "[Updated] " in art.get("title", "")
            if updated:
                lines.append(f"We have an update on a story we've been tracking: {title}. {desc}\n")
            elif i == 0:
                lines.append(f"The big story here is: {title}. {desc}\n")
            else:
                lines.append(f"Also worth noting: {title}. {desc}\n")

    if picks:
        lines.append(f"\n--- MIKE'S PICKS ---\n")
        lines.append("Now for Mike's Picks — stories that Big Mike flagged as must-reads.\n")
        for p in picks:
            lines.append(f"First up: {p['title']}. {p.get('summary', '')[:200]}\n")

    lines.append(f"\n--- WRAP-UP ---\n")
    lines.append("That's your MikeCast for today. Stay sharp, stay informed, and I'll catch you tomorrow. Peace.\n")
    lines.append("[OUTRO MUSIC]\n")

    return "\n".join(lines)


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
    send_email(html, script, audio_path if audio_ok else None)

    logger.info("MikeCast briefing complete.")


if __name__ == "__main__":
    main()
