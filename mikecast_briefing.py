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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# ElevenLabs — 3-voice podcast
ELEVENLABS_API_KEY       = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_MIKE    = os.environ.get("ELEVENLABS_VOICE_MIKE", "")
ELEVENLABS_VOICE_ELIZABETH = os.environ.get("ELEVENLABS_VOICE_ELIZABETH", "")
ELEVENLABS_VOICE_JESSE   = os.environ.get("ELEVENLABS_VOICE_JESSE", "")

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
        "Uber news today",
    ],
    "NY Sports": [
        "New York Yankees", "New York Knicks",
        "New York Giants NFL", "New Jersey Devils NHL",
        "NBA news today", "MLB news today", "NFL news today",
    ],
}

# NYT sections to pull top stories from
NYT_SECTIONS = ["technology", "business", "sports", "home"]

# ---------------------------------------------------------------------------
# Source credibility tiers (used by LLM scoring prompt)
# ---------------------------------------------------------------------------
SOURCE_TIERS: dict[str, int] = {
    # Tier 1 — highest credibility
    "The New York Times": 1, "Reuters": 1, "Associated Press": 1,
    "The Verge": 1, "Ars Technica": 1, "MIT Technology Review": 1, "Wired": 1,
    # Tier 2 — strong trade sources
    "TechCrunch": 2, "VentureBeat": 2, "CNBC": 2, "ESPN": 2, "Hacker News": 2,
    # Tier 3 — community aggregators
    "Reddit": 3, "Google News": 3,
}

TECH_RSS_FEEDS = [
    ("TechCrunch",            "https://techcrunch.com/feed/",                    "AI & Tech", 8),
    ("The Verge",             "https://www.theverge.com/rss/index.xml",          "AI & Tech", 8),
    ("Ars Technica",          "https://feeds.arstechnica.com/arstechnica/index", "AI & Tech", 6),
    ("VentureBeat",           "https://venturebeat.com/feed/",                   "AI & Tech", 6),
    ("Wired",                 "https://www.wired.com/feed/rss",                  "AI & Tech", 5),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/",          "AI & Tech", 5),
]

WIRE_RSS_FEEDS = [
    ("Reuters",          "https://feeds.reuters.com/reuters/topNews",          "Business & Markets", 6),
    ("Reuters",          "https://feeds.reuters.com/reuters/businessNews",     "Business & Markets", 6),
    ("Reuters",          "https://feeds.reuters.com/reuters/technologyNews",   "AI & Tech",          5),
    ("Associated Press", "https://feeds.apnews.com/rss/apf-topnews",          "Business & Markets", 5),
    ("Associated Press", "https://feeds.apnews.com/rss/apf-technology",       "AI & Tech",          5),
    ("Associated Press", "https://feeds.apnews.com/rss/apf-business",         "Business & Markets", 5),
]

CNBC_RSS_FEEDS = [
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "Business & Markets", 6),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",  "AI & Tech",          6),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",  "Business & Markets", 6),
]

ESPN_RSS_FEEDS = [
    ("https://www.espn.com/espn/rss/news",     "General"),
    ("https://www.espn.com/espn/rss/nba/news", "NBA"),
    ("https://www.espn.com/espn/rss/mlb/news", "MLB"),
    ("https://www.espn.com/espn/rss/nfl/news", "NFL"),
    ("https://www.espn.com/espn/rss/nhl/news", "NHL"),
]

REDDIT_FEEDS = [
    ("MachineLearning", "AI & Tech",          10),
    ("artificial",      "AI & Tech",          10),
    ("technology",      "AI & Tech",          10),
    ("investing",       "Business & Markets", 10),
    ("nba",             "NY Sports",           8),
    ("baseball",        "NY Sports",           8),
]

REDDIT_USER_AGENT = "MikeCast/2.0 (personal news briefing bot; contact: prometheusagent23@gmail.com)"
SCORE_BATCH_SIZE = 40

# Per-category specialized scoring prompts (used by parallel scoring agents)
CATEGORY_SCORER_PROMPTS: dict[str, str] = {
    "AI & Tech": (
        "You score AI and technology news for Mike, a tech-focused executive in New York. "
        "Prioritize: model releases, AI research breakthroughs, funding rounds >$50M, product launches "
        "from major AI companies (OpenAI, Anthropic, Google DeepMind, Meta AI, xAI, Nvidia), regulatory moves. "
        "Bonus: clear business/investment implications +10. "
        "Penalty: vague AI hype without substance -15, recycled benchmarks -10, clickbait -15."
    ),
    "Business & Markets": (
        "You score financial and business news for Mike, an investor tracking macro trends and the AI sector. "
        "Prioritize: Fed/monetary policy, market-moving macro data, earnings surprises from major tech companies, "
        "M&A activity, IPOs, large VC rounds, economic indicators. "
        "Bonus: direct investment implications +10, unusual market moves +10. "
        "Penalty: generic 'markets up/down' with no analysis -20, non-US markets with no US impact -10."
    ),
    "Companies": (
        "You score company-specific news for Mike, who closely follows Apple, Meta, Amazon, Nvidia, Tesla, "
        "Microsoft, Google, Netflix, Uber. Prioritize: product announcements, earnings, leadership changes, "
        "strategic pivots, regulatory actions, major partnerships. "
        "Bonus: stories about those specific companies +15. "
        "Penalty: minor product updates with no strategic significance -10, obscure companies -15."
    ),
    "NY Sports": (
        "You score sports news for Mike, a devoted New York sports fan. "
        "His top teams are Yankees, Knicks, Giants, and Devils — always prioritize those. "
        "Bonus: Yankees or Knicks story +20, Devils or Giants story +10. "
        "Also include major national sports news (e.g. blockbuster trades, championship results, "
        "star player injuries, landmark records) even if not NY-related — score these 50-70. "
        "Penalty: routine non-NY game recaps with no broader significance -20, "
        "generic sports commentary with no specific news -15."
    ),
}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _safe_request(url: str, params: dict | None = None, timeout: int = 15, headers: dict | None = None) -> requests.Response | None:
    """GET with retries and error handling."""
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers=headers)
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


def _atomic_write_json(path: Path, data, **json_kwargs) -> None:
    """Write JSON atomically via a temp file + rename to avoid corruption on crash."""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, **json_kwargs)
        tmp.rename(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


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


def fetch_hacker_news_top(max_results: int = 20) -> list[dict]:
    """Fetch top stories from Hacker News via Algolia API."""
    resp = _safe_request(
        "https://hn.algolia.com/api/v1/search",
        params={"tags": "front_page", "hitsPerPage": max_results},
    )
    if resp is None:
        return []

    articles = []
    try:
        hits = resp.json().get("hits") or []
        for hit in hits[:max_results]:
            title = hit.get("title", "").strip()
            url = hit.get("url", "").strip()
            if not title or not url:
                continue
            articles.append({
                "title": title,
                "url": url,
                "description": f"HN score: {hit.get('points', 0)} | Comments: {hit.get('num_comments', 0)}",
                "source": "Hacker News",
                "published": hit.get("created_at", ""),
                "hn_score": hit.get("points", 0),
                "hn_comments": hit.get("num_comments", 0),
            })
    except (KeyError, ValueError) as exc:
        logger.warning("HN parse error: %s", exc)
    return articles


def _parse_rss_feed(source_name: str, url: str, category: str, max_results: int) -> list[dict]:
    """Generic RSS 2.0 feed parser (uses <item> + <link> text)."""
    resp = _safe_request(url, timeout=15)
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
            pub_tag = item.find("pubDate") or item.find("pubdate")

            title = title_tag.get_text(strip=True) if title_tag else ""
            # lxml's xml parser puts <link> text as next sibling NavigableString
            if link_tag:
                link = link_tag.get_text(strip=True) or link_tag.get("href", "")
            else:
                link = ""
            desc = BeautifulSoup(desc_tag.get_text(), "html.parser").get_text(strip=True)[:300] if desc_tag else ""
            pub = pub_tag.get_text(strip=True) if pub_tag else ""

            if title and link:
                articles.append({
                    "title": title,
                    "url": link,
                    "description": desc,
                    "source": source_name,
                    "published": pub,
                })
    except Exception as exc:
        logger.warning("%s RSS parse error: %s", source_name, exc)
    return articles


def fetch_tech_rss_feeds() -> dict[str, list[dict]]:
    """Fetch from all tech publication RSS feeds. Returns {category: [articles]}."""
    results: dict[str, list[dict]] = {}
    for source_name, feed_url, category, max_results in TECH_RSS_FEEDS:
        arts = _parse_rss_feed(source_name, feed_url, category, max_results)
        results.setdefault(category, []).extend(arts)
        logger.info("Tech RSS [%s]: %d articles", source_name, len(arts))
        time.sleep(0.3)
    return results


def fetch_wire_rss_feeds() -> dict[str, list[dict]]:
    """Fetch Reuters and AP wire service RSS feeds. Returns {category: [articles]}."""
    results: dict[str, list[dict]] = {}
    for source_name, feed_url, category, max_results in WIRE_RSS_FEEDS:
        arts = _parse_rss_feed(source_name, feed_url, category, max_results)
        results.setdefault(category, []).extend(arts)
        logger.info("Wire RSS [%s %s]: %d articles", source_name, category, len(arts))
        time.sleep(0.3)
    return results


def fetch_cnbc_rss_feeds() -> dict[str, list[dict]]:
    """Fetch CNBC RSS feeds. Returns {category: [articles]}."""
    results: dict[str, list[dict]] = {}
    for source_name, feed_url, category, max_results in CNBC_RSS_FEEDS:
        arts = _parse_rss_feed(source_name, feed_url, category, max_results)
        results.setdefault(category, []).extend(arts)
        logger.info("CNBC RSS [%s]: %d articles", source_name, len(arts))
        time.sleep(0.3)
    return results


def fetch_reddit_rss() -> dict[str, list[dict]]:
    """
    Fetch Reddit subreddit Atom feeds.
    Reddit uses Atom XML (<entry> + <link rel='alternate' href='...'>) not RSS 2.0.
    Requires a custom User-Agent or Reddit returns 429.
    Returns {category: [articles]}.
    """
    results: dict[str, list[dict]] = {}
    headers = {"User-Agent": REDDIT_USER_AGENT}

    for subreddit, category, max_results in REDDIT_FEEDS:
        feed_url = f"https://www.reddit.com/r/{subreddit}/.rss"
        resp = _safe_request(feed_url, timeout=15, headers=headers)
        if resp is None:
            logger.warning("Reddit r/%s: no response", subreddit)
            continue

        try:
            soup = BeautifulSoup(resp.content, "xml")
            entries = soup.find_all("entry")
            articles = []
            for entry in entries[:max_results]:
                title_tag = entry.find("title")
                # Atom: <link rel="alternate" href="..."/>
                link_tag = entry.find("link", attrs={"rel": "alternate"}) or entry.find("link")
                content_tag = entry.find("content") or entry.find("summary")
                pub_tag = entry.find("published") or entry.find("updated")

                title = title_tag.get_text(strip=True) if title_tag else ""
                link = link_tag.get("href", "") if link_tag else ""
                # Reddit content is HTML; strip tags for description
                if content_tag:
                    raw_html = content_tag.get_text()
                    desc = BeautifulSoup(raw_html, "html.parser").get_text(strip=True)[:300]
                else:
                    desc = ""
                pub = pub_tag.get_text(strip=True) if pub_tag else ""

                if title and link:
                    articles.append({
                        "title": title,
                        "url": link,
                        "description": desc,
                        "source": "Reddit",
                        "published": pub,
                    })
            results.setdefault(category, []).extend(articles)
            logger.info("Reddit r/%s: %d articles", subreddit, len(articles))
        except Exception as exc:
            logger.warning("Reddit r/%s parse error: %s", subreddit, exc)

        time.sleep(0.5)

    return results


def fetch_espn_rss_feeds() -> list[dict]:
    """Fetch ESPN RSS feeds for general sports, NBA, MLB, NFL, NHL. Returns flat list."""
    articles: list[dict] = []
    for feed_url, sport_label in ESPN_RSS_FEEDS:
        resp = _safe_request(feed_url, timeout=15)
        if resp is None:
            logger.warning("ESPN [%s]: no response", sport_label)
            continue
        try:
            soup = BeautifulSoup(resp.content, "xml")
            items = soup.find_all("item")
            count = 0
            for item in items[:8]:
                title_tag = item.find("title")
                link_tag = item.find("link")
                desc_tag = item.find("description")
                pub_tag = item.find("pubDate") or item.find("pubdate")

                title = title_tag.get_text(strip=True) if title_tag else ""
                if link_tag:
                    link = link_tag.get_text(strip=True) or link_tag.get("href", "")
                else:
                    link = ""
                desc = BeautifulSoup(desc_tag.get_text(), "html.parser").get_text(strip=True)[:300] if desc_tag else ""
                pub = pub_tag.get_text(strip=True) if pub_tag else ""

                if title and link:
                    articles.append({
                        "title": title,
                        "url": link,
                        "description": desc,
                        "source": "ESPN",
                        "published": pub,
                    })
                    count += 1
            logger.info("ESPN [%s]: %d articles", sport_label, count)
        except Exception as exc:
            logger.warning("ESPN [%s] parse error: %s", sport_label, exc)
        time.sleep(0.3)
    return articles


def collect_all_news() -> dict[str, list[dict]]:
    """
    Collect articles from all sources. RSS/API sources run in parallel;
    NYT calls remain serial to respect their rate limits.
    Returns {category: [article_dicts]}.
    """
    categorised: dict[str, list[dict]] = {cat: [] for cat in CATEGORIES}

    # --- Parallel: RSS feeds + HN (all I/O-bound, fully independent) ---
    def _fetch_hn():
        arts = fetch_hacker_news_top()
        logger.info("Hacker News: %d articles", len(arts))
        return ("hn", arts)

    def _fetch_tech():
        r = fetch_tech_rss_feeds()
        logger.info("Tech RSS: %d articles", sum(len(v) for v in r.values()))
        return ("cat_dict", r)

    def _fetch_wire():
        r = fetch_wire_rss_feeds()
        logger.info("Wire RSS: %d articles", sum(len(v) for v in r.values()))
        return ("cat_dict", r)

    def _fetch_cnbc():
        r = fetch_cnbc_rss_feeds()
        logger.info("CNBC RSS: %d articles", sum(len(v) for v in r.values()))
        return ("cat_dict", r)

    def _fetch_reddit():
        r = fetch_reddit_rss()
        logger.info("Reddit: %d articles", sum(len(v) for v in r.values()))
        return ("cat_dict", r)

    def _fetch_espn():
        arts = fetch_espn_rss_feeds()
        logger.info("ESPN: %d articles", len(arts))
        return ("espn", arts)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(fn) for fn in
                   (_fetch_hn, _fetch_tech, _fetch_wire, _fetch_cnbc, _fetch_reddit, _fetch_espn)]
        for fut in as_completed(futures):
            try:
                kind, result = fut.result()
                if kind == "hn":
                    categorised["AI & Tech"].extend(result)
                elif kind == "espn":
                    categorised["NY Sports"].extend(result)
                else:  # cat_dict
                    for cat, arts in result.items():
                        if cat in categorised:
                            categorised[cat].extend(arts)
            except Exception as exc:
                logger.warning("Parallel source fetch error: %s", exc)

    # --- Serial: NYT (rate-limited to 0.5s between calls) ---
    section_to_category = {
        "technology": "AI & Tech",
        "business": "Business & Markets",
        "sports": "NY Sports",
        "home": "AI & Tech",
    }
    for section in NYT_SECTIONS:
        cat = section_to_category.get(section, "AI & Tech")
        stories = fetch_nyt_top_stories(section)
        categorised[cat].extend(stories)
        logger.info("NYT Top Stories [%s]: %d articles", section, len(stories))
        time.sleep(0.5)

    nyt_search_queries = {
        "AI & Tech": ["artificial intelligence", "OpenAI Anthropic"],
        "Business & Markets": ["stock market economy", "venture capital AI"],
        "Companies": ["Apple Meta Amazon Nvidia Tesla", "Uber Netflix Microsoft Google"],
        "NY Sports": ["Yankees Knicks Giants Devils", "NBA MLB NFL sports"],
    }
    for cat, queries in nyt_search_queries.items():
        for q in queries:
            results = search_news_via_nyt_article_search(q, max_results=3)
            categorised[cat].extend(results)
            logger.info("NYT Search [%s] '%s': %d articles", cat, q, len(results))
            time.sleep(0.5)

    # --- Parallel: Google News RSS per category ---
    gnews_tasks = [(cat, q) for cat, queries in CATEGORIES.items() for q in queries]

    def _fetch_gnews(cat_q: tuple[str, str]) -> tuple[str, list[dict]]:
        cat, q = cat_q
        return cat, search_news_web(q, max_results=3)

    with ThreadPoolExecutor(max_workers=4) as ex:
        for cat, results in ex.map(_fetch_gnews, gnews_tasks):
            categorised[cat].extend(results)

    total_raw = sum(len(v) for v in categorised.values())
    logger.info("Collection complete: %d raw articles across %d categories.", total_raw, len(categorised))
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
    _atomic_write_json(HISTORY_FILE, pruned, indent=2, ensure_ascii=False)


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
    """
    Trim each category proportionally to keep roughly *total* articles.
    Articles must already be sorted by score (highest first) before calling this.
    """
    counts = {cat: len(arts) for cat, arts in categorised.items()}
    total_available = sum(counts.values())
    if total_available == 0:
        return categorised

    selected: dict[str, list[dict]] = {}
    for cat, arts in categorised.items():
        share = max(3, int(total * len(arts) / total_available))
        selected[cat] = arts[:share]
    return selected


def cluster_articles(categorised: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """
    Use gpt-4o-mini to cluster articles by story within each category, keeping
    one representative per cluster. Runs categories in parallel. Cheap and fast —
    reduces article count before the expensive scoring step.
    """
    if not OPENAI_API_KEY:
        return categorised

    from openai import OpenAI
    client = OpenAI()

    def cluster_category(cat: str, articles: list[dict]) -> list[dict]:
        if len(articles) <= 5:
            return articles  # not worth clustering tiny sets
        titles_text = "\n".join(
            f"{i}: {a.get('title', '')[:120]}" for i, a in enumerate(articles)
        )
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        f"These {len(articles)} headlines are from the '{cat}' news category. "
                        "Group articles that cover the same story or event. "
                        "Return ONLY a JSON array of integers — one index per distinct story, "
                        "picking the most informative headline from each cluster. "
                        f"Headlines:\n{titles_text}\n\n"
                        "Return format: [0, 3, 7, ...] — one integer per distinct story. No explanation."
                    ),
                }],
                max_tokens=300,
                temperature=0.1,
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            keep_indices = json.loads(raw)
            if isinstance(keep_indices, list) and all(isinstance(i, int) for i in keep_indices):
                kept = [articles[i] for i in keep_indices if 0 <= i < len(articles)]
                logger.info("Clustering [%s]: %d → %d articles", cat, len(articles), len(kept))
                return kept
        except Exception as exc:
            logger.warning("Clustering failed for [%s]: %s — keeping all", cat, exc)
        return articles

    clustered: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(cluster_category, cat, arts): cat
                   for cat, arts in categorised.items()}
        for fut in as_completed(futures):
            cat = futures[fut]
            try:
                clustered[cat] = fut.result()
            except Exception as exc:
                logger.warning("Cluster future failed [%s]: %s", cat, exc)
                clustered[cat] = categorised[cat]

    before = sum(len(v) for v in categorised.values())
    after  = sum(len(v) for v in clustered.values())
    logger.info("Clustering complete: %d → %d articles total", before, after)
    return clustered


def _build_scoring_prompt(batch: list[dict]) -> str:
    """Build the user prompt for GPT-4o article scoring."""
    lines = []
    for item in batch:
        score_id = item["_score_id"]
        title = item.get("title", "").replace("[Updated] ", "")
        source = item.get("source", "Unknown")
        tier = SOURCE_TIERS.get(source, 3)
        desc = item.get("description", "")[:200]
        hn_info = ""
        if item.get("hn_score"):
            hn_info = f" | HN score={item['hn_score']} comments={item['hn_comments']}"
        lines.append(
            f"[{score_id}] {title}\n"
            f"  Source: {source} (tier {tier}){hn_info}\n"
            f"  Desc: {desc}"
        )
    return "\n\n".join(lines)


def score_and_rank_articles(categorised: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """
    Score articles per category in parallel, each with a domain-specific prompt.
    Runs N category scoring agents simultaneously via ThreadPoolExecutor.
    Falls back gracefully — never raises.
    """
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set — skipping LLM scoring.")
        return categorised

    from openai import OpenAI
    client = OpenAI()

    scorer_suffix = (
        "\n\nReturn ONLY a JSON array with no markdown, no explanation outside JSON:\n"
        '[{"id": 1, "score": 87, "reason": "One sentence."}]'
    )

    def score_category(cat: str, articles: list[dict]) -> tuple[str, list[dict]]:
        if not articles:
            return cat, articles
        # Assign per-category temp IDs (1-based, scoped to this category)
        for i, art in enumerate(articles):
            art["_score_id"] = i + 1
            art["score"] = 50
            art["score_reason"] = ""

        system_prompt = CATEGORY_SCORER_PROMPTS.get(cat, (
            "You are a news relevance scorer. Score each article 1-100 based on "
            "newsworthiness, credibility, and relevance to a tech executive."
        )) + scorer_suffix

        for batch_start in range(0, len(articles), SCORE_BATCH_SIZE):
            batch = articles[batch_start: batch_start + SCORE_BATCH_SIZE]
            user_prompt = _build_scoring_prompt(batch)
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_prompt},
                    ],
                    max_tokens=1500,
                    temperature=0.2,
                )
                raw = resp.choices[0].message.content.strip()
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
                scores = json.loads(raw)
                id_map = {item["id"]: item for item in scores}
                for art in batch:
                    if art["_score_id"] in id_map:
                        art["score"] = int(id_map[art["_score_id"]].get("score", 50))
                        art["score_reason"] = id_map[art["_score_id"]].get("reason", "")
                logger.info("Scored [%s] %d articles", cat, len(batch))
            except Exception as exc:
                logger.warning("Scoring failed [%s] batch %d: %s", cat, batch_start, exc)

        for art in articles:
            art.pop("_score_id", None)
        articles.sort(key=lambda a: a.get("score", 50), reverse=True)
        return cat, articles

    result: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(score_category, cat, list(arts)): cat
                   for cat, arts in categorised.items()}
        for fut in as_completed(futures):
            cat = futures[fut]
            try:
                cat, scored = fut.result()
                result[cat] = scored
            except Exception as exc:
                logger.warning("Category scoring future failed [%s]: %s", cat, exc)
                result[cat] = categorised[cat]

    all_arts = [a for arts in result.values() for a in arts]
    avg = sum(a.get("score", 50) for a in all_arts) / len(all_arts) if all_arts else 0
    logger.info("Scoring complete: %d articles across %d categories, avg score=%.1f",
                len(all_arts), len(result), avg)
    return result


def enrich_top_stories(articles: dict[str, list[dict]], top_n: int = 8) -> dict[str, list[dict]]:
    """
    For the top N articles (by score) across all categories, fetch the article
    body and use gpt-4o-mini to add a 'why_it_matters' one-sentence insight.
    Runs fetch + enrichment in parallel. Mutates articles in-place.
    """
    if not OPENAI_API_KEY:
        return articles

    from openai import OpenAI
    client = OpenAI()

    # Collect the top_n articles by score across all categories
    all_arts = [(cat, art) for cat, arts in articles.items() for art in arts]
    all_arts.sort(key=lambda x: x[1].get("score", 50), reverse=True)
    to_enrich = all_arts[:top_n]

    def fetch_body(url: str) -> str:
        """Fetch article body text (first ~1500 chars)."""
        try:
            resp = requests.get(
                url, timeout=10,
                headers={"User-Agent": "MikeCast/2.0 (enrichment)"},
            )
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)[:1500]
        except Exception:
            return ""

    def enrich_article(_cat: str, art: dict) -> None:
        url   = art.get("url", "")
        title = art.get("title", "")
        desc  = art.get("description", "")
        body  = fetch_body(url) if url else ""
        context = f"Title: {title}\nDescription: {desc}\nBody: {body[:800]}"
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{
                    "role": "user",
                    "content": (
                        "In one crisp sentence (max 30 words), explain why this story matters "
                        "to a tech-executive investor in New York today.\n\n"
                        + context
                    ),
                }],
                max_tokens=80,
                temperature=0.3,
            )
            art["why_it_matters"] = resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.debug("Enrichment failed for '%s': %s", title[:50], exc)

    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(enrich_article, cat, art) for cat, art in to_enrich]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                logger.debug("Enrichment future error: %s", exc)

    enriched_count = sum(1 for _, art in to_enrich if art.get("why_it_matters"))
    logger.info("Enrichment: %d/%d top articles enriched.", enriched_count, len(to_enrich))
    return articles


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
        _atomic_write_json(PICKS_FILE, data, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, IOError) as exc:
        logger.error("Could not mark picks processed: %s — picks may repeat next run", exc)


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
        except Exception as exc:
            logger.warning("Could not fetch URL %s: %s", content, exc)
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
        except Exception as exc:
            logger.warning("pdftotext failed for %s: %s", content, exc)
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
  Sources: NYT, Hacker News, TechCrunch, Ars Technica, CNBC, Reddit &amp; more &bull; Powered by OpenAI GPT-4o</p>
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

def _split_text_for_tts(text: str, max_chunk: int = 4000) -> list[str]:
    """Split *text* on sentence boundaries for TTS API calls (≤ max_chunk chars each)."""
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chunk:
            chunks.append(remaining)
            break
        split_at = remaining[:max_chunk].rfind(". ")
        if split_at == -1:
            split_at = max_chunk
        else:
            split_at += 2
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return chunks


def generate_podcast_audio(script: str, output_path: Path) -> bool:
    """Generate MP3 audio from the podcast script using OpenAI TTS."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set — skipping audio generation.")
        return False

    try:
        from openai import OpenAI
        client = OpenAI()  # uses OPENAI_API_KEY env var automatically

        chunks = _split_text_for_tts(script)

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


def generate_conversational_script(
    categorised: dict[str, list[dict]],
    picks: list[dict],
) -> str:
    """
    Generate a 3-voice conversational podcast script tagged with:
      [MIKE]      — host: intro and sign-off
      [ELIZABETH] — AI & Tech, Business & Markets, Companies
      [JESSE]     — NY Sports

    Elizabeth hands off to Jesse after the Companies segment.
    Returns raw tagged script as a string.
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

Total length: 5-8 minutes of spoken audio (approx 800-1200 words).
Write the COMPLETE script with all tags. No outline, no placeholders."""

    script = _gpt_call(system_prompt, user_prompt, max_tokens=2500)

    if not script:
        logger.warning("Conversational script generation failed — empty response.")
        return ""

    # Ensure every [SPEAKER] tag is on its own line
    script = re.sub(r'(\[(?:MIKE|ELIZABETH|JESSE)\])', r'\n\1\n', script)
    script = re.sub(r'\n{3,}', '\n\n', script).strip()
    return script


def parse_conversational_script(script: str) -> list[tuple[str, str]]:
    """
    Parse a tagged conversational script into (speaker, text) tuples.
    Speaker tags look like: [MIKE], [ELIZABETH], [JESSE]
    Returns list of (speaker_name, spoken_text) pairs.
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


def generate_elevenlabs_audio(
    conversational_script: str,
    output_path: Path,
) -> bool:
    """
    Generate a 3-voice MP3 using ElevenLabs TTS.
    Parses [MIKE]/[ELIZABETH]/[JESSE] tags, calls the ElevenLabs API for each
    segment with the correct voice ID, then concatenates the raw MP3 bytes.
    Falls back gracefully and returns False on failure.
    """
    if not ELEVENLABS_API_KEY:
        logger.warning("ELEVENLABS_API_KEY not set — skipping ElevenLabs audio.")
        return False

    voice_map = {
        "MIKE":      ELEVENLABS_VOICE_MIKE,
        "ELIZABETH": ELEVENLABS_VOICE_ELIZABETH,
        "JESSE":     ELEVENLABS_VOICE_JESSE,
    }
    missing = [name for name, vid in voice_map.items() if not vid]
    if missing:
        logger.warning("ElevenLabs voice IDs missing for: %s — skipping.", missing)
        return False

    segments = parse_conversational_script(conversational_script)
    if not segments:
        logger.warning("No segments parsed from conversational script.")
        return False

    def tts_segment(speaker: str, text: str) -> bytes:
        voice_id = voice_map[speaker]
        # Split long segments to stay under ElevenLabs' practical limit (~5000 chars)
        chunks = _split_text_for_tts(text, max_chunk=4500)
        audio_parts: list[bytes] = []
        for chunk in chunks:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            headers = {
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            }
            payload = {
                "text": chunk,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.80},
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=90)
            resp.raise_for_status()
            audio_parts.append(resp.content)
        return b"".join(audio_parts)

    audio_segments: list[bytes] = []
    for i, (speaker, text) in enumerate(segments):
        logger.info(
            "ElevenLabs TTS segment %d/%d [%s] (%d chars)…",
            i + 1, len(segments), speaker, len(text),
        )
        try:
            audio_bytes = tts_segment(speaker, text)
            audio_segments.append(audio_bytes)
            time.sleep(0.3)  # gentle rate-limit buffer
        except Exception as exc:
            logger.error("ElevenLabs segment %d [%s] failed: %s", i + 1, speaker, exc)
            return False

    try:
        with open(output_path, "wb") as fh:
            for seg in audio_segments:
                fh.write(seg)
        size_mb = output_path.stat().st_size / 1e6
        logger.info(
            "ElevenLabs audio saved: %s (%.1f MB, %d segments)",
            output_path, size_mb, len(audio_segments),
        )
        return True
    except Exception as exc:
        logger.error("Failed to write ElevenLabs audio: %s", exc)
        return False


def generate_episode_description(podcast_script: str, episode_num: int) -> str:
    """Generate a ~50-word episode description using GPT-4o."""
    try:
        from openai import OpenAI
        openai_client = OpenAI()
        resp = openai_client.chat.completions.create(
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

    # Append subscribe footer to HTML body
    subscribe_html = """
<div style="margin:2rem auto;max-width:600px;text-align:center;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <p style="color:#8b949e;font-size:13px;margin:0 0 12px;">Subscribe to MikeCast on your favourite podcast app:</p>
  <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 auto;">
    <tr>
      <td style="padding:0 6px;">
        <a href="https://podcasts.apple.com/us/podcast/mikecast-daily-briefing/id1882539449" style="display:inline-block;text-decoration:none;border:0;">
          <img src="https://schwim23.github.io/mikecast/data/badge-apple.png" width="180" height="54" alt="Listen on Apple Podcasts" style="display:block;border:0;">
        </a>
      </td>
      <td style="padding:0 6px;">
        <a href="https://open.spotify.com/show/3SEexX9wC3nr4xStYK2jOv?si=Ia1BvyEGQLKqZ7TwByXOCQ" style="display:inline-block;text-decoration:none;border:0;">
          <img src="https://schwim23.github.io/mikecast/data/badge-spotify.png" width="180" height="54" alt="Listen on Spotify" style="display:block;border:0;">
        </a>
      </td>
      <td style="padding:0 6px;">
        <a href="https://schwim23.github.io/mikecast/data/feed.xml"
           style="display:inline-block;padding:8px 18px;border-radius:8px;background:#f97316;color:#ffffff;text-decoration:none;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;vertical-align:middle;">
          <div style="font-size:9px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:rgba(255,255,255,.75);line-height:1;margin-bottom:3px;">Subscribe via</div>
          <div style="font-size:14px;font-weight:700;color:#ffffff;line-height:1;">RSS Feed</div>
        </a>
      </td>
    </tr>
  </table>
</div>"""
    msg.attach(MIMEText(html_body + subscribe_html, "html", "utf-8"))

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
    _atomic_write_json(DATA_DIR / "manifest.json", manifest, indent=2)
    logger.info("Manifest updated: %d dates", len(dates))


SITE_BASE_URL = "https://schwim23.github.io/mikecast/"


def generate_rss_feed() -> None:
    """Generate a podcast-compatible RSS 2.0 feed at data/feed.xml."""
    from email.utils import formatdate
    import calendar

    # Build episode number map: chronological order → episode #1, #2, …
    all_episodes = sorted(DATA_DIR.glob("????-??-??.json"))
    episode_num_map = {p.stem: i + 1 for i, p in enumerate(all_episodes)}

    items = []
    for json_path in sorted(DATA_DIR.glob("????-??-??.json"), reverse=True):
        try:
            with open(json_path) as f:
                data = json.load(f)
        except Exception:
            continue

        date_str = data.get("date", json_path.stem)
        date_display = data.get("date_display", date_str)
        # Prefer ElevenLabs 3-voice audio when available, fall back to OpenAI TTS
        audio_file = data.get("elevenlabs_audio_file") or data.get("audio_file")
        if not audio_file:
            continue

        audio_url = f"{SITE_BASE_URL}data/{audio_file}"
        audio_path = DATA_DIR / audio_file
        file_size = audio_path.stat().st_size if audio_path.exists() else 0

        # Build pubDate in RFC 2822 format (6:45 AM ET → 11:45 UTC)
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=11, minute=45, tzinfo=timezone.utc
            )
            pub_date = formatdate(calendar.timegm(dt.timetuple()), usegmt=True)
        except ValueError:
            pub_date = formatdate(usegmt=True)

        episode_num = episode_num_map.get(json_path.stem, "?")

        # Use stored ~50-word description if available; fall back for older episodes
        if data.get("episode_description"):
            description = data["episode_description"]
        else:
            # Fallback for episodes generated before this feature: use exec summary
            exec_summary = ""
            try:
                soup = BeautifulSoup(data.get("html_briefing", ""), "html.parser")
                for h2 in soup.find_all("h2"):
                    if "EXECUTIVE SUMMARY" in h2.get_text().upper():
                        p = h2.find_next_sibling("p")
                        if p:
                            exec_summary = p.get_text().strip()
                        break
            except Exception:
                pass
            fallback = exec_summary or f"MikeCast daily news briefing for {date_display}."
            description = f"Episode #{episode_num} — {fallback}"
            if len(description) > 4000:
                description = description[:3997] + "..."

        subtitle = description[:252] + "..." if len(description) > 255 else description

        def _esc(s):
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        items.append(f"""  <item>
    <title>MikeCast #{episode_num} — {_esc(date_display)}</title>
    <description>{_esc(description)}</description>
    <pubDate>{pub_date}</pubDate>
    <guid isPermaLink="false">{audio_url}</guid>
    <enclosure url="{audio_url}" type="audio/mpeg" length="{file_size}"/>
    <itunes:title>MikeCast #{episode_num} — {_esc(date_display)}</itunes:title>
    <itunes:subtitle>{_esc(subtitle)}</itunes:subtitle>
    <itunes:summary>{_esc(description)}</itunes:summary>
    <itunes:duration>0</itunes:duration>
    <itunes:explicit>false</itunes:explicit>
  </item>""")

    feed_url = f"{SITE_BASE_URL}data/feed.xml"
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
  xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>MikeCast — Daily Briefing</title>
    <link>{SITE_BASE_URL}</link>
    <description>Your daily AI-powered news briefing. Personalized news across AI &amp; Tech, Business &amp; Markets, Companies, and NY Sports.</description>
    <language>en-us</language>
    <atom:link href="{feed_url}" rel="self" type="application/rss+xml"/>
    <itunes:author>MikeCast</itunes:author>
    <itunes:owner>
      <itunes:name>MikeCast</itunes:name>
      <itunes:email>michael.schwimmer@gmail.com</itunes:email>
    </itunes:owner>
    <itunes:image href="{SITE_BASE_URL}data/cover.png"/>
    <image>
      <url>{SITE_BASE_URL}data/cover.png</url>
      <title>MikeCast — Daily Briefing</title>
      <link>{SITE_BASE_URL}</link>
    </image>
    <itunes:summary>Your daily AI-powered news briefing.</itunes:summary>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="News"/>
{chr(10).join(items)}
  </channel>
</rss>"""

    out_path = DATA_DIR / "feed.xml"
    out_path.write_text(rss, encoding="utf-8")
    logger.info("RSS feed written: %s (%d episodes)", out_path, len(items))


def save_daily_data(
    html_briefing: str,
    categorised: dict[str, list[dict]],
    picks: list[dict],
    podcast_script: str,
    audio_filename: str | None,
    conversational_script: str = "",
    elevenlabs_audio_filename: str | None = None,
) -> Path:
    """Save all briefing data as a JSON file for the dashboard."""
    # Episode number = chronological position (existing completed episodes + 1)
    existing = sorted(DATA_DIR.glob("????-??-??.json"))
    episode_num = len(existing) + 1

    # Use conversational script for episode description if available, else single-voice
    desc_source = conversational_script if conversational_script else podcast_script
    episode_description = generate_episode_description(desc_source, episode_num)
    logger.info("Episode description: %s", episode_description)

    data = {
        "date": TODAY,
        "date_display": TODAY_DISPLAY,
        "episode_num": episode_num,
        "episode_description": episode_description,
        "html_briefing": html_briefing,
        "articles": categorised,
        "mikes_picks": picks,
        "podcast_script": podcast_script,
        "conversational_script": conversational_script,
        "audio_file": audio_filename,
        "elevenlabs_audio_file": elevenlabs_audio_filename,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path = DATA_DIR / f"{TODAY}.json"
    _atomic_write_json(out_path, data, indent=2, ensure_ascii=False)
    logger.info("Daily data saved: %s", out_path)
    return out_path


# ===================================================================
# MAIN
# ===================================================================

def main() -> None:
    force = "--force" in sys.argv

    logger.info("=" * 60)
    logger.info("MikeCast Daily Briefing — %s", TODAY_DISPLAY)
    logger.info("=" * 60)

    # Idempotency guard: don't overwrite a completed briefing unless --force
    daily_path = DATA_DIR / f"{TODAY}.json"
    if daily_path.exists() and not force:
        logger.warning(
            "Today's briefing (%s) already exists. Re-run with --force to regenerate. Exiting.",
            TODAY,
        )
        sys.exit(0)

    # 1. Collect news (parallel I/O)
    logger.info("Step 1/10: Collecting news…")
    raw_news = collect_all_news()
    raw_total = sum(len(v) for v in raw_news.values())
    if raw_total == 0:
        logger.critical("No articles collected from any source — aborting.")
        sys.exit(1)
    if raw_total < 5:
        logger.warning("Very few articles collected (%d) — possible widespread API failure.", raw_total)

    # 2. Deduplicate
    logger.info("Step 2/10: Deduplicating…")
    deduped = deduplicate(raw_news)

    # 3. Cluster (cheap gpt-4o-mini — reduces article count before expensive scoring)
    logger.info("Step 3/10: Clustering duplicate stories…")
    clustered = cluster_articles(deduped)

    # 4. Score and rank articles (parallel per-category gpt-4o agents)
    logger.info("Step 4/10: Scoring and ranking articles…")
    scored = score_and_rank_articles(clustered)

    # 5. Select top articles
    logger.info("Step 5/10: Selecting top articles…")
    top_articles = select_top_articles(scored, total=25)
    total = sum(len(v) for v in top_articles.values())
    logger.info("Selected %d articles across %d categories.", total, len(top_articles))
    if total == 0:
        logger.warning("All articles were duplicates — briefing will have no new stories.")

    # 6. Enrich top stories (fetch body + gpt-4o-mini 'why it matters')
    logger.info("Step 6/10: Enriching top stories…")
    top_articles = enrich_top_stories(top_articles, top_n=8)

    # 7. Process Mike's Picks
    logger.info("Step 7/10: Processing Mike's Picks…")
    picks = process_picks()

    # 8. Generate HTML briefing + both scripts in parallel
    logger.info("Step 8/10: Generating HTML briefing and podcast scripts…")

    html: str = ""
    single_voice_script: str = ""
    conversational_script: str = ""

    def _gen_html() -> str:
        return generate_html_briefing(top_articles, picks)

    def _gen_single_script() -> str:
        return generate_podcast_script(top_articles, picks)

    def _gen_conv_script() -> str:
        return generate_conversational_script(top_articles, picks)

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_html   = ex.submit(_gen_html)
        f_single = ex.submit(_gen_single_script)
        f_conv   = ex.submit(_gen_conv_script)
        try:
            html = f_html.result()
        except Exception as exc:
            logger.error("HTML briefing generation failed: %s", exc)
            html = "<p>Briefing generation failed.</p>"
        try:
            single_voice_script = f_single.result()
        except Exception as exc:
            logger.error("Single-voice script generation failed: %s", exc)
        try:
            conversational_script = f_conv.result()
        except Exception as exc:
            logger.error("Conversational script generation failed: %s", exc)

    # 9. Generate audio
    logger.info("Step 9/10: Generating audio…")
    audio_path        = DATA_DIR / f"MikeCast_{TODAY}.mp3"
    el_audio_path     = DATA_DIR / f"MikeCast_3voice_{TODAY}.mp3"
    audio_ok          = False
    el_audio_ok       = False
    audio_filename    = None
    el_audio_filename = None

    # Try ElevenLabs 3-voice first (preferred)
    if conversational_script and ELEVENLABS_API_KEY:
        logger.info("Generating ElevenLabs 3-voice audio…")
        el_audio_ok = generate_elevenlabs_audio(conversational_script, el_audio_path)
        if not el_audio_ok and el_audio_path.exists():
            el_audio_path.unlink()
            logger.warning("Removed partial ElevenLabs audio: %s", el_audio_path)
        el_audio_filename = el_audio_path.name if el_audio_ok else None

    # Always generate single-voice OpenAI TTS as backup / email attachment
    script_for_tts = single_voice_script or conversational_script
    if script_for_tts:
        logger.info("Generating OpenAI TTS single-voice audio…")
        audio_ok = generate_podcast_audio(script_for_tts, audio_path)
        if not audio_ok and audio_path.exists():
            audio_path.unlink()
            logger.warning("Removed partial OpenAI TTS audio: %s", audio_path)
        audio_filename = audio_path.name if audio_ok else None

    # Primary podcast audio = ElevenLabs if available, else OpenAI TTS
    primary_audio_file = el_audio_filename or audio_filename
    primary_audio_path = el_audio_path if el_audio_ok else (audio_path if audio_ok else None)

    # 10. Save & send
    logger.info("Step 10/10: Saving data & sending email…")
    save_daily_data(
        html,
        top_articles,
        picks,
        single_voice_script,
        primary_audio_file,
        conversational_script=conversational_script,
        elevenlabs_audio_filename=el_audio_filename,
    )
    generate_manifest()
    generate_rss_feed()
    email_ok = send_email(html, single_voice_script or conversational_script, primary_audio_path)

    logger.info(
        "Run summary — articles: %d | picks: %d | "
        "elevenlabs: %s | openai_tts: %s | email: %s",
        total,
        len(picks),
        "ok" if el_audio_ok else ("skip" if not ELEVENLABS_API_KEY else "FAILED"),
        "ok" if audio_ok else "FAILED",
        "ok" if email_ok else "FAILED",
    )
    logger.info("MikeCast briefing complete.")


if __name__ == "__main__":
    main()
