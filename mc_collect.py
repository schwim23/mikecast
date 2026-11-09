"""
MikeCast — news collection, deduplication, clustering, scoring, enrichment,
and Mike's Picks processing.

Pipeline order:
  collect_all_news()       → raw articles from all sources
  deduplicate()            → remove articles seen in the last 7 days
  cluster_articles()       → merge same-story duplicates (gpt-4o-mini)
  score_and_rank_articles()→ per-category relevance scores (OPENAI_SCORER_MODEL)
  enrich_top_stories()     → add "why it matters" to the top-N stories
  select_top_articles()    → trim to a target total across categories
  process_picks()          → load and summarise Mike's hand-picked items
"""

import copy
import json
import logging
import os
import re
import time
import concurrent.futures
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from mc_config import (
    CATEGORIES, CATEGORY_SCORER_PROMPTS, CNBC_RSS_FEEDS, ESPN_RSS_FEEDS, PAYMENTS_RSS_FEEDS,
    DATA_DIR, HISTORY_FILE, NYT_API_KEY, NYT_SEARCH_QUERIES, NYT_SECTION_TO_CATEGORY,
    NYT_SECTIONS, OPENAI_API_KEY, OPENAI_HELPER_MODEL, OPENAI_SCORER_MODEL, PICKS_FILE, REDDIT_FEEDS, REDDIT_USER_AGENT, S3_BUCKET,
    SCORE_BATCH_SIZE, SOURCE_TIERS, TECH_RSS_FEEDS, TODAY, WIRE_GNEWS_QUERIES,
)
from mc_utils import (
    _atomic_write_json, _safe_request, format_published_et, parse_published,
    s3_load_json, title_similarity, url_fingerprint,
)
from eval.dump import dump_fixture, enabled as _dump_enabled

logger = logging.getLogger("mikecast")


class _LLMClient:
    """Drop-in for `OpenAI()` in this module's `client.chat.completions.create(...)`
    calls, routed through LiteLLM so OPENAI_SCORER_MODEL / OPENAI_HELPER_MODEL can
    name any provider (MODEL_EVAL_PLAN.md §1a prerequisite). Omits `temperature`
    for models that reject a non-default value; gpt-4o/-mini behave as before."""

    class _Completions:
        @staticmethod
        def create(model, **kwargs):
            import litellm
            from crew.model_compat import api_key_for_model, supports_custom_temperature
            if not supports_custom_temperature(model):
                kwargs.pop("temperature", None)
            return litellm.completion(model=model, api_key=api_key_for_model(model), **kwargs)

    class _Chat:
        completions = None

    def __init__(self):
        self.chat = self._Chat()
        self.chat.completions = self._Completions()



# ============================================================
# RSS / API source fetchers
# ============================================================

def _parse_rss_feed(source_name: str, url: str, category: str, max_results: int) -> list[dict]:
    """
    Generic RSS 2.0 feed parser.
    Handles the lxml quirk where <link> text appears as a NavigableString
    sibling rather than tag content, so we try both .get_text() and .get('href').
    """
    resp = _safe_request(url, timeout=15)
    if resp is None:
        return []

    if len(resp.content) > 10 * 1024 * 1024:
        logger.warning("%s RSS feed too large (%d bytes) — skipping", source_name, len(resp.content))
        return []

    articles = []
    try:
        soup = BeautifulSoup(resp.content, "xml")
        for item in soup.find_all("item")[:max_results]:
            title_tag = item.find("title")
            link_tag  = item.find("link")
            desc_tag  = item.find("description")
            pub_tag   = item.find("pubDate") or item.find("pubdate")

            title = title_tag.get_text(strip=True) if title_tag else ""
            link  = (link_tag.get_text(strip=True) or link_tag.get("href", "")) if link_tag else ""
            desc  = (
                BeautifulSoup(desc_tag.get_text(), "html.parser").get_text(strip=True)[:300]
                if desc_tag else ""
            )
            pub = pub_tag.get_text(strip=True) if pub_tag else ""

            if title and link:
                articles.append({
                    "title": title, "url": link, "description": desc,
                    "source": source_name, "published": pub,
                })
    except Exception as exc:
        logger.warning("%s RSS parse error: %s", source_name, exc)
    return articles


def _fetch_rss_source_list(
    feeds: list[tuple[str, str, str, int]],
    label: str,
) -> dict[str, list[dict]]:
    """
    Fetch a list of (source_name, url, category, max_results) RSS feeds
    sequentially (polite 0.3 s delay) and return {category: [articles]}.
    Used by all three tech/wire/CNBC feed groups.
    """
    results: dict[str, list[dict]] = {}
    for source_name, feed_url, category, max_results in feeds:
        arts = _parse_rss_feed(source_name, feed_url, category, max_results)
        results.setdefault(category, []).extend(arts)
        logger.info("%s [%s]: %d articles", label, source_name, len(arts))
        time.sleep(0.3)
    return results


def fetch_hacker_news_top(max_results: int = 20) -> list[dict]:
    """Fetch top stories from Hacker News via the Algolia front-page API."""
    resp = _safe_request(
        "https://hn.algolia.com/api/v1/search",
        params={"tags": "front_page", "hitsPerPage": max_results},
    )
    if resp is None:
        return []

    articles = []
    try:
        for hit in (resp.json().get("hits") or [])[:max_results]:
            title = hit.get("title", "").strip()
            url   = hit.get("url",   "").strip()
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


def fetch_reddit_rss() -> dict[str, list[dict]]:
    """
    Fetch Reddit subreddit Atom feeds.
    Reddit uses Atom XML (<entry> + <link rel='alternate' href='...'>) not RSS 2.0,
    and requires a descriptive User-Agent or it returns 429.
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
            soup    = BeautifulSoup(resp.content, "xml")
            entries = soup.find_all("entry")
            articles = []
            for entry in entries[:max_results]:
                title_tag   = entry.find("title")
                link_tag    = entry.find("link", attrs={"rel": "alternate"}) or entry.find("link")
                content_tag = entry.find("content") or entry.find("summary")
                pub_tag     = entry.find("published") or entry.find("updated")

                title = title_tag.get_text(strip=True) if title_tag else ""
                link  = link_tag.get("href", "") if link_tag else ""
                # Reddit content is HTML-in-XML — strip all tags for the description
                desc = (
                    BeautifulSoup(content_tag.get_text(), "html.parser").get_text(strip=True)[:300]
                    if content_tag else ""
                )
                pub = pub_tag.get_text(strip=True) if pub_tag else ""

                if title and link:
                    articles.append({
                        "title": title, "url": link, "description": desc,
                        "source": "Reddit", "published": pub,
                    })
            results.setdefault(category, []).extend(articles)
            logger.info("Reddit r/%s: %d articles", subreddit, len(articles))
        except Exception as exc:
            logger.warning("Reddit r/%s parse error: %s", subreddit, exc)

        time.sleep(0.5)

    return results


def fetch_espn_rss_feeds() -> list[dict]:
    """
    Fetch ESPN league news (NBA, MLB, NFL, NHL) from site.api.espn.com.

    Despite the function/config name still saying "RSS" (kept stable for
    callers), the actual transport is a JSON API — the legacy RSS host
    (www.espn.com/espn/rss/*) was bot-blocked. Returns a flat list of
    article dicts with the same shape every other source produces.
    """
    articles: list[dict] = []
    for api_url, sport_label in ESPN_RSS_FEEDS:
        resp = _safe_request(api_url, timeout=15)
        if resp is None:
            logger.warning("ESPN [%s]: no response", sport_label)
            continue
        try:
            payload = resp.json()
            raw_items = payload.get("articles") or []
            count = 0
            for item in raw_items[:8]:
                title = (item.get("headline") or "").strip()
                desc = (item.get("description") or "").strip()[:300]
                pub = (item.get("published") or item.get("lastModified") or "").strip()
                link = (
                    item.get("links", {})
                        .get("web", {})
                        .get("href", "")
                        .strip()
                )
                if title and link:
                    articles.append({
                        "title": title, "url": link, "description": desc,
                        "source": "ESPN", "published": pub,
                    })
                    count += 1
            logger.info("ESPN [%s]: %d articles", sport_label, count)
        except Exception as exc:
            logger.warning("ESPN [%s] parse error: %s", sport_label, exc)
        time.sleep(0.3)
    return articles


def search_news_via_nyt_article_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the NYT Article Search API for articles published in the last 24 hours."""
    if not NYT_API_KEY:
        logger.warning("NYTAPIKEY not set — skipping NYT Article Search.")
        return []

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    today_fmt = datetime.now(timezone.utc).strftime("%Y%m%d")

    resp = _safe_request(
        "https://api.nytimes.com/svc/search/v2/articlesearch.json",
        params={"q": query, "begin_date": yesterday, "end_date": today_fmt,
                "sort": "newest", "api-key": NYT_API_KEY},
    )
    if resp is None:
        return []

    articles = []
    try:
        docs = (resp.json().get("response") or {}).get("docs") or []
        for doc in docs[:max_results]:
            articles.append({
                "title":       doc.get("headline", {}).get("main", ""),
                "url":         doc.get("web_url", ""),
                "description": doc.get("snippet", ""),
                "source":      "The New York Times",
                "published":   doc.get("pub_date", ""),
            })
    except (KeyError, ValueError) as exc:
        logger.warning("NYT Article Search parse error: %s", exc)
    return articles


def fetch_nyt_top_stories(section: str) -> list[dict]:
    """Fetch the NYT Top Stories API for a given section (technology, business, etc.)."""
    if not NYT_API_KEY:
        logger.warning("NYTAPIKEY not set — skipping NYT Top Stories.")
        return []

    resp = _safe_request(
        f"https://api.nytimes.com/svc/topstories/v2/{section}.json",
        params={"api-key": NYT_API_KEY},
    )
    if resp is None:
        return []

    articles = []
    try:
        for item in (resp.json().get("results") or [])[:8]:
            articles.append({
                "title":       item.get("title", ""),
                "url":         item.get("url", ""),
                "description": item.get("abstract", ""),
                "source":      "The New York Times",
                "published":   item.get("published_date", ""),
            })
    except (KeyError, ValueError) as exc:
        logger.warning("NYT Top Stories parse error for %s: %s", section, exc)
    return articles


def search_news_web(query: str, max_results: int = 5) -> list[dict]:
    """
    Fetch Google News RSS for a query and return article dicts.

    The article URL is Google's per-article link (news.google.com/rss/articles/...),
    which redirects to the story in a reader's browser. It used to be the publisher's
    HOMEPAGE (<source url>), which gave every story from one outlet the same URL:
    dedup then treated a new ABC story as an "[Updated]" copy of an old one, and
    readers clicking the link landed on abcnews.com. The homepage is kept as
    "source_url". Enrichment skips news.google.com links (they hit a consent wall
    server-side).

    'when:1d' asks Google for the past 24 hours, but it is not reliable on its own;
    filter_stale_articles enforces the age limit on the parsed pubDate.
    """
    resp = _safe_request(
        "https://news.google.com/rss/search",
        params={"q": f"{query} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"},
    )
    if resp is None:
        return []

    articles = []
    try:
        soup = BeautifulSoup(resp.content, "xml")
        for item in soup.find_all("item")[:max_results]:
            title_tag  = item.find("title")
            link_tag   = item.find("link")
            desc_tag   = item.find("description")
            source_tag = item.find("source")
            # The "xml" parser is case-sensitive: the tag is <pubDate>. Looking for
            # "pubdate" (pre-2026-11-05) never matched, so every Google News article
            # was undated and filter_stale_articles let all of them through.
            pub_tag    = item.find("pubDate")

            source_url  = source_tag.get("url", "") if source_tag else ""
            link_url    = link_tag.get_text(strip=True) if link_tag else ""
            article_url = link_url or source_url

            articles.append({
                "title":       title_tag.get_text(strip=True) if title_tag else "",
                "url":         article_url,
                "description": (
                    BeautifulSoup(desc_tag.get_text(), "html.parser").get_text(strip=True)
                    if desc_tag else ""
                ),
                "source":    source_tag.get_text(strip=True) if source_tag else "Google News",
                "source_url": source_url,
                "published": pub_tag.get_text(strip=True) if pub_tag else "",
            })
    except Exception as exc:
        logger.warning("Google News RSS parse error: %s", exc)
    return articles


def fetch_wire_via_google_news() -> dict[str, list[dict]]:
    """Reuters + AP via Google News site: searches (their direct feeds are dead)."""
    results: dict[str, list[dict]] = {}
    for label, query, category, max_results in WIRE_GNEWS_QUERIES:
        arts = search_news_web(query, max_results=max_results)
        results.setdefault(category, []).extend(arts)
        logger.info("Wire [%s] '%s': %d articles", label, query, len(arts))
        time.sleep(0.3)
    return results


def collect_all_news(dynamic_queries: dict[str, list[str]] | None = None) -> dict[str, list[dict]]:
    """
    Collect articles from all configured sources.

    Strategy:
    - RSS feeds + HN run in parallel (all I/O-bound, fully independent).
    - NYT calls are serial because the API rate-limits aggressively.
    - Google News searches run in a second parallel batch.

    Args:
        dynamic_queries: Optional {category: [query, ...]} from mc_plan.plan_daily_searches().
                         Appended to the static CATEGORIES queries for Google News.

    Returns {category: [article_dicts]}.
    """
    categorised: dict[str, list[dict]] = {cat: [] for cat in CATEGORIES}

    # --- Phase 1: parallel RSS + HN ---
    parallel_sources = {
        "hn":   lambda: ("hn",      fetch_hacker_news_top()),
        "tech": lambda: ("cat_dict", _fetch_rss_source_list(TECH_RSS_FEEDS, "Tech RSS")),
        "wire": lambda: ("cat_dict", fetch_wire_via_google_news()),
        "cnbc": lambda: ("cat_dict", _fetch_rss_source_list(CNBC_RSS_FEEDS, "CNBC RSS")),
        "payments": lambda: ("cat_dict", _fetch_rss_source_list(PAYMENTS_RSS_FEEDS, "Payments RSS")),
        "reddit": lambda: ("cat_dict", fetch_reddit_rss()),
        "espn":  lambda: ("espn",   fetch_espn_rss_feeds()),
    }

    with ThreadPoolExecutor(max_workers=7) as ex:
        futures = {ex.submit(fn): name for name, fn in parallel_sources.items()}
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
                logger.warning("Parallel source fetch error [%s]: %s", futures[fut], exc)

    # --- Phase 2: serial NYT (0.5 s between calls to avoid rate limits) ---
    for section in NYT_SECTIONS:
        cat = NYT_SECTION_TO_CATEGORY.get(section, "AI & Tech")
        stories = fetch_nyt_top_stories(section)
        categorised[cat].extend(stories)
        logger.info("NYT Top Stories [%s]: %d articles", section, len(stories))
        time.sleep(0.5)

    for cat, queries in NYT_SEARCH_QUERIES.items():
        for q in queries:
            results = search_news_via_nyt_article_search(q, max_results=3)
            categorised[cat].extend(results)
            logger.info("NYT Search [%s] '%s': %d articles", cat, q, len(results))
            time.sleep(0.5)

    # --- Phase 3: parallel Google News per query ---
    # Merge static queries with any dynamic queries from the planning step
    all_queries: dict[str, list[str]] = {cat: list(qs) for cat, qs in CATEGORIES.items()}
    if dynamic_queries:
        for cat, queries in dynamic_queries.items():
            if cat in all_queries:
                all_queries[cat].extend(queries)
        dyn_total = sum(len(v) for v in dynamic_queries.values())
        logger.info("Merged %d dynamic queries from planning step.", dyn_total)

    gnews_tasks = [(cat, q) for cat, queries in all_queries.items() for q in queries]

    with ThreadPoolExecutor(max_workers=4) as ex:
        for cat, results in ex.map(lambda t: (t[0], search_news_web(t[1], max_results=3)), gnews_tasks):
            categorised[cat].extend(results)

    total_raw = sum(len(v) for v in categorised.values())
    logger.info("Collection complete: %d raw articles across %d categories.", total_raw, len(categorised))
    return categorised


# ============================================================
# Deduplication
# ============================================================

def load_history() -> list[dict]:
    """Load the rolling 7-day article history (S3 or local disk)."""
    from mc_config import S3_BUCKET
    if S3_BUCKET:
        from mc_utils import s3_load_json
        data = s3_load_json(S3_BUCKET, "briefing_history.json")
        return data if isinstance(data, list) else []
    if not HISTORY_FILE.exists():
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_history(history: list[dict]) -> None:
    """Persist history, pruning entries older than 7 days (S3 or local disk)."""
    from mc_config import S3_BUCKET
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    pruned = [h for h in history if h.get("date", "") >= cutoff]
    if S3_BUCKET:
        from mc_utils import s3_save_json
        s3_save_json(S3_BUCKET, "briefing_history.json", pruned, indent=2, ensure_ascii=False)
    else:
        _atomic_write_json(HISTORY_FILE, pruned, indent=2, ensure_ascii=False)


def deduplicate(categorised: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """
    Remove duplicate articles within the current batch and against the 7-day history.

    Duplicate detection uses two thresholds:
    - URL fingerprint match → exact same article URL.
    - Title similarity > 0.85 → same story, different URLs (e.g. syndicated content).

    If a previously-seen article's description changed by > 30% (similarity < 0.70),
    it is treated as an update and re-included with an '[Updated]' prefix.

    Returns the deduplicated dict and updates the history file.
    """
    history     = load_history()
    hist_urls   = {h.get("url_fp") for h in history}
    hist_titles = {h.get("title", ""): h for h in history}

    seen_fps: set[str] = set()
    deduped:  dict[str, list[dict]] = {cat: [] for cat in categorised}
    new_history_entries: list[dict] = []

    for cat, articles in categorised.items():
        for art in articles:
            title = art.get("title", "").strip()
            url   = art.get("url",   "").strip()
            if not title or not url:
                continue

            fp = url_fingerprint(url)

            # Skip articles already seen in this batch
            if fp in seen_fps:
                continue
            seen_fps.add(fp)

            is_duplicate = False
            is_updated   = False

            if fp in hist_urls:
                is_duplicate = True
                # Check whether the description changed significantly (= update)
                for h in history:
                    if h.get("url_fp") == fp:
                        if (h.get("description") and art.get("description") and
                                title_similarity(h["description"], art["description"]) < 0.70):
                            is_updated = True
                        break
            else:
                # Title similarity check — catches syndicated / reposted articles
                for hist_title in hist_titles:
                    if title_similarity(title, hist_title) > 0.85:
                        is_duplicate = True
                        h = hist_titles[hist_title]
                        if (h.get("description") and art.get("description") and
                                title_similarity(h["description"], art["description"]) < 0.70):
                            is_updated = True
                        break

            if is_duplicate and not is_updated:
                logger.debug("Skipping duplicate: %s", title[:60])
                continue

            if is_updated:
                art["title"] = f"[Updated] {title}"

            deduped[cat].append(art)
            new_history_entries.append({
                "title":       title,
                "url":         url,
                "url_fp":      fp,
                "description": art.get("description", ""),
                "date":        datetime.now(timezone.utc).isoformat(),
            })

    history.extend(new_history_entries)
    save_history(history)

    # Second pass: cross-category dedup — keep first occurrence by category order
    cross_seen_fps: set[str] = set()
    cross_seen_titles: list[str] = []
    for cat in deduped:
        kept_cross: list[dict] = []
        for art in deduped[cat]:
            fp = url_fingerprint(art.get("url", ""))
            if fp in cross_seen_fps:
                logger.debug("Cross-category duplicate (url): %s", art.get("title", "")[:60])
                continue
            if any(title_similarity(art.get("title", ""), t) > 0.85 for t in cross_seen_titles):
                logger.debug("Cross-category duplicate (title): %s", art.get("title", "")[:60])
                continue
            cross_seen_fps.add(fp)
            cross_seen_titles.append(art.get("title", ""))
            kept_cross.append(art)
        deduped[cat] = kept_cross

    return deduped


# Far-future dates are bad feed data; a few hours can be a mislabeled timezone.
MAX_FUTURE_SKEW = timedelta(hours=24)


def filter_stale_articles(
    categorised: dict[str, list[dict]],
    max_age_days: int = 3,
) -> dict[str, list[dict]]:
    """
    Drop articles whose parsed publication date is older than *max_age_days*
    or more than MAX_FUTURE_SKEW ahead of now, and articles whose date is
    missing or unparseable. A small skew is allowed because some feeds mislabel
    their timezone.

    Undated articles used to be kept (benefit of the doubt). That let the April
    "Tim Cook stepping down" story into the 2026-11-05 episode: Google News dates
    were never parsed, so every Google News article was undated and nothing was
    age-checked. A source that stops sending dates now loses its articles
    (logged per source as a warning) instead of letting old news through.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    latest_allowed = now + MAX_FUTURE_SKEW
    filtered: dict[str, list[dict]] = {}
    total_dropped = 0
    future_dropped = 0
    undated_by_source: dict[str, int] = {}

    for cat, arts in categorised.items():
        kept: list[dict] = []
        for art in arts:
            pub = (art.get("published") or "").strip()
            parsed_dt = parse_published(pub)

            if parsed_dt is None:
                src = art.get("source") or "unknown"
                undated_by_source[src] = undated_by_source.get(src, 0) + 1
                continue
            if parsed_dt < cutoff:
                logger.debug("Dropping stale article (%s): %s", pub[:10], art.get("title", "")[:60])
                total_dropped += 1
                continue
            if parsed_dt > latest_allowed:
                logger.debug("Dropping future-dated article (%s): %s", pub[:30], art.get("title", "")[:60])
                future_dropped += 1
                continue
            kept.append(art)
        filtered[cat] = kept

    if total_dropped:
        logger.info("Stale article filter: dropped %d articles older than %d days.", total_dropped, max_age_days)
    if future_dropped:
        logger.warning("Stale article filter: dropped %d future-dated articles.", future_dropped)
    if undated_by_source:
        logger.warning(
            "Stale article filter: dropped %d undated/unparseable articles by source: %s",
            sum(undated_by_source.values()),
            ", ".join(f"{k}={v}" for k, v in sorted(undated_by_source.items(), key=lambda kv: -kv[1])),
        )
    return filtered


def filter_sports_by_trusted_sources(
    categorised: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """
    For the NY Sports category only, drop articles from publishers not in
    SPORTS_TRUSTED_SOURCES. This blocks low-quality aggregators (AOL.com,
    random blogs) that recirculate old content.

    Articles from other categories are passed through unchanged.
    Sports articles with no source field are dropped (fail-closed).
    """
    from mc_config import SPORTS_TRUSTED_SOURCES

    result = dict(categorised)
    sports = categorised.get("NY Sports", [])
    if not sports:
        return result

    kept = []
    dropped = 0
    for art in sports:
        source = art.get("source", "").strip()
        if any(trusted.lower() in source.lower() for trusted in SPORTS_TRUSTED_SOURCES):
            kept.append(art)
        else:
            logger.debug("Dropping untrusted sports source '%s': %s", source, art.get("title", "")[:60])
            dropped += 1

    if dropped:
        logger.info("Sports source filter: dropped %d articles from untrusted publishers.", dropped)
    result["NY Sports"] = kept
    return result


def select_top_articles(categorised: dict[str, list[dict]], total: int = 25) -> dict[str, list[dict]]:
    """
    Trim each category proportionally to keep roughly *total* articles overall.
    Each category gets at least 3 slots to prevent a dominant category from
    crowding out others.  Articles must already be sorted by score (desc).
    """
    total_available = sum(len(arts) for arts in categorised.values())
    if total_available == 0:
        return categorised

    selected: dict[str, list[dict]] = {}
    for cat, arts in categorised.items():
        share = max(3, int(total * len(arts) / total_available))
        selected[cat] = arts[:share]
    return selected


# Categories excluded from cross-story merging. NY Sports has its own
# ESPN-verified facts flow, and recap + preview of the same team are intentional.
_MERGE_EXCLUDED_CATEGORIES = {"NY Sports"}
# Extra candidates per category beyond its selection share, so a dropped
# duplicate is replaced by the next-best story when select_top_articles runs.
_MERGE_BUFFER = 4


def _merge_model() -> str:
    """One call per run, so it uses the scorer model, not the helper: on the
    2026-11-05 articles gpt-4o-mini missed same-event pairs that gpt-5.6-terra
    caught 3/3 (two Stripe stablecoin write-ups, two Google school lawsuits).
    Override with MIKECAST_MERGE_MODEL."""
    return os.environ.get("MIKECAST_MERGE_MODEL") or OPENAI_SCORER_MODEL


def _selection_candidates(
    scored: dict[str, list[dict]], total: int, buffer: int,
) -> list[tuple[str, int, dict]]:
    """Articles select_top_articles could take, plus *buffer* per category so a
    dropped story is replaced by one that was also checked. NY Sports excluded."""
    total_available = sum(len(a) for a in scored.values())
    candidates: list[tuple[str, int, dict]] = []
    for cat, arts in scored.items():
        if cat in _MERGE_EXCLUDED_CATEGORIES:
            continue
        share = max(3, int(total * len(arts) / total_available))
        for idx, art in enumerate(arts[: share + buffer]):
            candidates.append((cat, idx, art))
    return candidates


def merge_duplicate_stories(
    scored: dict[str, list[dict]], total: int = 25,
) -> dict[str, list[dict]]:
    """
    Second-pass duplicate merge, run after scoring and before select_top_articles.

    cluster_articles works on titles alone, one category at a time, and misses
    the same story from two outlets when the headlines differ ("Block reaches
    $964M DOJ pact" / "Block faces $765m DoJ settlement" on 2026-11-05), and it
    can't see a story that landed in two categories (Visa earnings in both
    Business & Markets and Payments & Commerce). This pass sends the selection
    candidates from every category (title, source, description) to the scorer
    model in one call and asks for groups covering the same specific event.
    Uses the scorer model (see _merge_model).
    In each group the highest-scored article is kept; the others are removed.
    Fails open: any error returns the input unchanged.
    """
    if not OPENAI_API_KEY:
        return scored

    total_available = sum(len(a) for a in scored.values())
    if total_available == 0:
        return scored

    candidates = _selection_candidates(scored, total, _MERGE_BUFFER)
    if len(candidates) < 2:
        return scored

    lines = []
    for i, (cat, _, art) in enumerate(candidates):
        desc = re.sub(r"\s+", " ", art.get("description", "") or "")[:200]
        lines.append(
            f"{i}: [{cat}] {art.get('title', '')[:140]} — {art.get('source', '')} — {desc}"
        )

    try:
        resp = _LLMClient().chat.completions.create(
            model=_merge_model(),
            messages=[{
                "role": "user",
                "content": (
                    "Below are news articles (id: [category] title — source — description). "
                    "Find groups of articles that report the SAME specific event or "
                    "announcement, even when the headlines are worded differently or the "
                    "figures differ (e.g. two outlets on one settlement, one earnings "
                    "report, one product launch). Different events about the same company "
                    "or topic are NOT duplicates.\n\n"
                    + "\n".join(lines)
                    + "\n\nReturn ONLY a JSON array of groups, each an array of 2+ ids, "
                    "e.g. [[3, 17], [5, 6, 22]]. Return [] if there are no duplicates. "
                    "No explanation."
                ),
            }],
            max_tokens=400,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        groups = json.loads(raw)
        if not isinstance(groups, list):
            raise ValueError(f"expected a list, got {type(groups).__name__}")
    except Exception as exc:
        logger.warning("Duplicate-story merge failed: %s — keeping all", exc)
        return scored

    drop: set[int] = set()  # id() of articles to remove
    merged_groups = 0
    for group in groups:
        if not isinstance(group, list):
            continue
        ids = sorted({i for i in group if isinstance(i, int) and 0 <= i < len(candidates)})
        if len(ids) < 2:
            continue
        # Keep the highest score; ties go to the earlier (higher-ranked) candidate.
        keep = max(ids, key=lambda i: (candidates[i][2].get("score", 0), -i))
        kept_art = candidates[keep][2]
        for i in ids:
            if i == keep:
                continue
            cat, _, art = candidates[i]
            drop.add(id(art))
            logger.info(
                "Duplicate-story merge: dropped [%s] %r (%s, score %s) — same story as [%s] %r (%s, score %s)",
                cat, art.get("title", "")[:70], art.get("source", ""), art.get("score"),
                candidates[keep][0], kept_art.get("title", "")[:70], kept_art.get("source", ""),
                kept_art.get("score"),
            )
        merged_groups += 1

    if not drop:
        logger.info("Duplicate-story merge: no duplicates among %d candidates", len(candidates))
        return scored

    logger.info("Duplicate-story merge: %d groups, %d articles dropped", merged_groups, len(drop))
    return {cat: [a for a in arts if id(a) not in drop] for cat, arts in scored.items()}


# How many previous days of episodes a new story is compared against. Matches
# the 3-day age limit in filter_stale_articles.
REPEAT_LOOKBACK_DAYS = 3
# More than merge's buffer: a bad news day can drop several stories per category,
# and every article that can backfill a slot must have been checked.
_REPEAT_BUFFER = 8
# The scorer model doesn't accept temperature=0 and flips on borderline stories
# from run to run (2026-11-08 replay: the Nasdaq repeat caught in 2 of 3 single
# calls, and "SpaceX, Tesla extend losses" wrongly dropped in 1 of 2). Drop a
# story only when a majority of independent calls agree.
_REPEAT_VOTES = 3
_REPEAT_VOTES_NEEDED = 2


def _load_recent_episode_articles(days: int = REPEAT_LOOKBACK_DAYS) -> list[dict]:
    """Non-sports articles from the previous *days* episodes (never today's, so a
    --force rerun doesn't match itself). A missing or unreadable day is skipped."""
    today = datetime.strptime(TODAY, "%Y-%m-%d").date()
    prior: list[dict] = []
    for back in range(1, days + 1):
        day = (today - timedelta(days=back)).isoformat()
        try:
            if S3_BUCKET:
                episode = s3_load_json(S3_BUCKET, f"data/{day}.json")
            else:
                path = DATA_DIR / f"{day}.json"
                episode = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except Exception as exc:
            logger.warning("Repeat-event filter: could not load %s episode: %s", day, exc)
            continue
        for cat, arts in ((episode or {}).get("articles") or {}).items():
            if cat in _MERGE_EXCLUDED_CATEGORIES:
                continue
            prior.extend(dict(a, _episode_date=day) for a in arts)
    return prior


def filter_repeat_events(
    scored: dict[str, list[dict]], total: int = 25,
) -> dict[str, list[dict]]:
    """
    Drop stories that repeat an event an earlier episode already covered and add
    nothing new. Runs after merge_duplicate_stories, before select_top_articles.

    The stale filter sees publication time and deduplicate() needs a URL or a
    near-identical title, so a recap with a fresh timestamp and a new headline
    passes both. On 2026-11-08 (a Sunday) TradingView's "Nasdaq ends -5.5%, S&P
    500 sees worst day since April 2025", dated that morning, ran as the market
    news; the session was Thursday 11/05, covered on 11/06, and Friday's rebound
    went unmentioned. Real follow-ups are kept. Three calls vote and a story is
    dropped only on a majority. Fails open on any error.
    Disable with MIKECAST_REPEAT_FILTER=0.
    """
    if os.environ.get("MIKECAST_REPEAT_FILTER") == "0" or not OPENAI_API_KEY:
        return scored
    if sum(len(a) for a in scored.values()) == 0:
        return scored

    candidates = _selection_candidates(scored, total, _REPEAT_BUFFER)
    prior = _load_recent_episode_articles()
    if not candidates or not prior:
        return scored

    def describe(art: dict) -> str:
        desc = re.sub(r"\s+", " ", art.get("description", "") or "")[:200]
        return f"{art.get('title', '')[:140]} — {art.get('source', '')} — {desc}"

    prior_lines = [f"P{i} ({a['_episode_date']}): {describe(a)}" for i, a in enumerate(prior)]
    candidate_lines = [
        f"{i}: [{cat}] (posted {format_published_et(art.get('published')) or 'unknown'}) {describe(art)}"
        for i, (cat, _, art) in enumerate(candidates)
    ]

    from mc_facts import market_calendar_line

    prompt = (
        "List A is stories a daily news briefing already covered in recent "
        "episodes. List B is candidates for today's episode.\n\n"
        + market_calendar_line() + "\n\n"
        "Mark a candidate in B as a REPEAT only if it reports the same specific "
        "event as a story in A AND adds no material new development: a recap or "
        "re-report of the same market session, earnings report, deal, ruling or "
        "announcement, even when the headline is worded differently or the "
        "posting time is today. Posting time does not show when an event "
        "happened. A market-close headline that names no day and reports a "
        "plunge or rally of the scale already in A is a repeat unless it shows "
        "it is about a later session.\n\n"
        "NOT a repeat: a follow-up with something new since (the next market "
        "session, a stock extending its move on a later day, a new decision, new "
        "figures, a response, a resignation); a new opinion or forecast from a "
        "named person; a different event about the same company or topic. If "
        "unsure, it is not a repeat.\n\n"
        "LIST A:\n" + "\n".join(prior_lines)
        + "\n\nLIST B:\n" + "\n".join(candidate_lines)
        + '\n\nReturn ONLY a JSON array of objects, one per repeat, like '
        '[{"id": 3, "prior": "P12"}]. Return [] if there are none. No explanation.'
    )

    def ask() -> dict[int, int] | None:
        """One model opinion as {candidate index: prior index}; None if it failed."""
        try:
            resp = _LLMClient().chat.completions.create(
                model=_merge_model(),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
                temperature=0.0,
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            flagged = json.loads(raw)
            if not isinstance(flagged, list):
                raise ValueError(f"expected a list, got {type(flagged).__name__}")
        except Exception as exc:
            logger.warning("Repeat-event filter: vote failed: %s", exc)
            return None
        vote: dict[int, int] = {}
        for item in flagged:
            if not isinstance(item, dict):
                continue
            cand_id, prior_ref = item.get("id"), str(item.get("prior", ""))
            if not isinstance(cand_id, int) or not 0 <= cand_id < len(candidates):
                continue
            if re.fullmatch(r"P\d+", prior_ref) and int(prior_ref[1:]) < len(prior):
                vote[cand_id] = int(prior_ref[1:])  # a repeat has to name a real earlier story
        return vote

    with ThreadPoolExecutor(_REPEAT_VOTES) as pool:
        votes = [v for v in pool.map(lambda _: ask(), range(_REPEAT_VOTES)) if v is not None]
    if len(votes) < _REPEAT_VOTES_NEEDED:
        logger.warning("Repeat-event filter: only %d votes returned — keeping all", len(votes))
        return scored

    tally = Counter(cand_id for vote in votes for cand_id in vote)
    # The first vote that flagged the candidate names the prior story for the log.
    drop = {
        cand_id: (cand_id, next(v[cand_id] for v in votes if cand_id in v))
        for cand_id, count in tally.items() if count >= _REPEAT_VOTES_NEEDED
    }
    for cand_id, count in tally.items():
        if count < _REPEAT_VOTES_NEEDED:
            logger.info(
                "Repeat-event filter: kept [%s] %r — only %d of %d votes",
                candidates[cand_id][0], candidates[cand_id][2].get("title", "")[:70], count, len(votes),
            )

    if len(drop) > len(candidates) / 2:
        logger.warning(
            "Repeat-event filter: flagged %d of %d candidates, which is implausible — keeping all",
            len(drop), len(candidates),
        )
        return scored
    if not drop:
        logger.info("Repeat-event filter: no repeats among %d candidates", len(candidates))
        return scored

    drop_ids: set[int] = set()
    for cand_id, prior_id in drop.values():
        cat, _, art = candidates[cand_id]
        drop_ids.add(id(art))
        logger.info(
            "Repeat-event filter: dropped [%s] %r (%s) — already covered %s: %r (%s)",
            cat, art.get("title", "")[:70], art.get("source", ""),
            prior[prior_id]["_episode_date"], prior[prior_id].get("title", "")[:70],
            prior[prior_id].get("source", ""),
        )
    logger.info("Repeat-event filter: dropped %d of %d candidates", len(drop_ids), len(candidates))
    return {cat: [a for a in arts if id(a) not in drop_ids] for cat, arts in scored.items()}


# ============================================================
# Clustering (gpt-4o-mini — runs before expensive scoring)
# ============================================================

def cluster_articles(categorised: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """
    Group articles that cover the same story within each category and keep only
    the most informative headline per cluster.

    Uses gpt-4o-mini (cheap + fast) to identify clusters before the more
    expensive per-category gpt-4o scoring step.  All categories run in parallel.
    Falls back to the original list if clustering fails for a category.
    """
    if not OPENAI_API_KEY:
        return categorised

    client = _LLMClient()

    def cluster_category(cat: str, articles: list[dict]) -> list[dict]:
        if len(articles) <= 5:
            return articles  # not worth the API call for tiny sets

        titles_text = "\n".join(
            f"{i}: {a.get('title', '')[:120]}" for i, a in enumerate(articles)
        )
        try:
            resp = client.chat.completions.create(
                model=OPENAI_HELPER_MODEL,
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
            # Strip markdown code fences if the model wraps its output
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


# ============================================================
# Scoring (OPENAI_SCORER_MODEL — per-category parallel agents)
# ============================================================

def _build_scoring_prompt(batch: list[dict]) -> str:
    """
    Format a batch of articles as a numbered list for the GPT-4o scoring prompt.
    Each article gets a temporary integer ID (scoped to the current batch)
    so the model can reference it in its JSON response.
    HN articles include their upvote/comment counts as extra signal.
    """
    lines = []
    for item in batch:
        score_id = item["_score_id"]
        title    = item.get("title", "").replace("[Updated] ", "")
        source   = item.get("source", "Unknown")
        tier     = SOURCE_TIERS.get(source, 3)
        desc     = item.get("description", "")[:200]
        hn_info  = (
            f" | HN score={item['hn_score']} comments={item['hn_comments']}"
            if item.get("hn_score") else ""
        )
        published = format_published_et(item.get("published")) or "unknown"
        lines.append(
            f"[{score_id}] {title}\n"
            f"  Source: {source} (tier {tier}){hn_info}\n"
            f"  Published: {published}\n"
            f"  Desc: {desc}"
        )
    return "\n\n".join(lines)


def score_and_rank_articles(
    categorised: dict[str, list[dict]],
    trending_context: str = "",
) -> dict[str, list[dict]]:
    """
    Score each article 1–100 using a category-specific GPT-4o agent.
    All categories run in parallel via ThreadPoolExecutor(4).
    Returns each category's articles sorted by score descending.
    Falls back gracefully — never raises.

    Args:
        trending_context: Optional breaking-news context from mc_plan (prepended to
                          each category's system prompt so Grok's live intel influences
                          article scoring).
    """
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set — skipping LLM scoring.")
        return categorised

    client = _LLMClient()

    # Eval fixture capture (MODEL_EVAL_PLAN.md): snapshot the pool BEFORE scoring mutates it.
    scorer_input = ({"categorised": copy.deepcopy(categorised), "trending_context": trending_context}
                    if _dump_enabled() else None)

    # Who holds office / runs the tracked companies TODAY, so the scorer can sink
    # resurfaced old news (2026-11-05: ABC's April "Tim Cook is stepping down" scored
    # 100 and led the Companies section, two months after Ternus became CEO).
    from mc_facts import facts_lines, market_calendar_line
    old_news_block = (
        "\n\nCURRENT FACTS (override your training data):\n" + facts_lines() + "\n"
        "OLD NEWS: if an article reports as new a change these facts show already happened "
        "(e.g. a CEO 'is stepping down' / 'will be replaced' but the successor is already the "
        "current CEO above), it is a resurfaced old story — score it 5 or lower and say so.\n\n"
        + market_calendar_line() + "\n"
        "Published is when an article was posted, not when the event happened. A recap of a "
        "market session earlier than the most recent one above is old news — score it 10 or lower."
    )

    # Appended to every category's system prompt to enforce JSON output format
    scorer_suffix = (
        "\n\nReturn ONLY a JSON array with no markdown, no explanation outside JSON:\n"
        '[{"id": 1, "score": 87, "reason": "One sentence."}]'
    )

    def score_category(cat: str, articles: list[dict]) -> tuple[str, list[dict]]:
        if not articles:
            return cat, articles

        # Assign temporary per-category IDs (1-based) for the prompt
        for i, art in enumerate(articles):
            art["_score_id"] = i + 1
            art["score"]        = 50   # default if scoring fails
            art["score_reason"] = ""

        context_prefix = (
            f"Today's breaking context (prioritize these stories):\n{trending_context}\n\n"
            if trending_context else ""
        )
        system_prompt = context_prefix + CATEGORY_SCORER_PROMPTS.get(cat, (
            "You are a news relevance scorer. Score each article 1-100 based on "
            "newsworthiness, credibility, and relevance to a tech executive."
        )) + old_news_block + scorer_suffix

        def score_batch(batch: list[dict]) -> None:
            resp = client.chat.completions.create(
                model=OPENAI_SCORER_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": _build_scoring_prompt(batch)},
                ],
                max_tokens=1500,
                temperature=0.2,
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            scores  = json.loads(raw)
            id_map  = {item["id"]: item for item in scores}
            for art in batch:
                if art["_score_id"] in id_map:
                    art["score"]        = int(id_map[art["_score_id"]].get("score", 50))
                    art["score_reason"] = id_map[art["_score_id"]].get("reason", "")

        for batch_start in range(0, len(articles), SCORE_BATCH_SIZE):
            batch = articles[batch_start: batch_start + SCORE_BATCH_SIZE]
            try:
                score_batch(batch)
                # Models can return valid JSON that silently omits articles (gpt-4o skipped
                # ~1/3 of each run, 2026-09-22..24) — retry the omitted ones once.
                missing = [a for a in batch if not a["score_reason"]]
                if missing:
                    logger.warning("Scorer omitted %d/%d articles [%s] — retrying them",
                                   len(missing), len(batch), cat)
                    score_batch(missing)
                    still = sum(1 for a in missing if not a["score_reason"])
                    if still:
                        logger.warning("Scorer still omitted %d articles [%s] — left at default 50",
                                       still, cat)
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
    logger.info(
        "Scoring complete: %d articles across %d categories, avg score=%.1f",
        len(all_arts), len(result), avg,
    )
    if scorer_input is not None:
        dump_fixture("article_scorer", TODAY, {
            "input": scorer_input,
            "output": {"scored": {cat: [{"url": a.get("url"), "title": a.get("title"),
                                         "score": a.get("score"), "score_reason": a.get("score_reason")}
                                        for a in arts] for cat, arts in result.items()}},
        })
    return result


# ============================================================
# Enrichment (gpt-4o-mini — top-N articles only)
# ============================================================

def enrich_top_stories(articles: dict[str, list[dict]], top_n: int = 8) -> dict[str, list[dict]]:
    """
    For the top *top_n* articles by score, fetch the article body and ask
    gpt-4o-mini to add a 'why_it_matters' one-sentence insight.

    Body fetching + enrichment run in parallel.  Mutates articles in-place.
    Enrichment failures are logged at DEBUG level and skipped silently —
    missing 'why_it_matters' keys are handled downstream.
    """
    if not OPENAI_API_KEY:
        return articles

    client = _LLMClient()

    all_arts = [(cat, art) for cat, arts in articles.items() for art in arts]
    all_arts.sort(key=lambda x: x[1].get("score", 50), reverse=True)
    to_enrich = all_arts[:top_n]

    def fetch_body(url: str) -> str:
        """Fetch and strip the article body (first ~1500 chars of visible text)."""
        if not url or "news.google.com/" in url:
            return ""   # consent wall server-side — the body would be Google's interstitial
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "MikeCast/2.0 (enrichment)"})
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)[:1500]
        except Exception:
            return ""

    def fetch_all_bodies() -> list[str]:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures_b = [ex.submit(fetch_body, art.get("url", "")) for _, art in to_enrich]
            bodies: list[str] = [""] * len(futures_b)
            done_futures = {fut: idx for idx, fut in enumerate(futures_b)}
            try:
                for fut in as_completed(futures_b, timeout=120):
                    idx = done_futures[fut]
                    try:
                        bodies[idx] = fut.result()
                    except Exception:
                        bodies[idx] = ""
            except FuturesTimeoutError:
                logger.warning("Enrichment body fetching timed out after 120s")
        return bodies

    def enrich_article_individual(_cat: str, art: dict, body: str) -> None:
        title   = art.get("title", "")
        desc    = art.get("description", "")
        context = f"Title: {title}\nDescription: {desc}\nBody: {body[:800]}"
        try:
            resp = client.chat.completions.create(
                model=OPENAI_HELPER_MODEL,
                messages=[{
                    "role": "user",
                    "content": (
                        "In one crisp sentence (max 30 words), explain why this story matters "
                        "to a tech-executive investor in New York today.\n\n" + context
                    ),
                }],
                max_tokens=80,
                temperature=0.3,
            )
            art["why_it_matters"] = resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.debug("Enrichment failed for '%s': %s", title[:50], exc)

    bodies = fetch_all_bodies()
    # Bodies are live-fetched, so the fixture must record them for a deterministic replay.
    enrich_input = ([{"category": cat, "title": art.get("title", ""), "description": art.get("description", ""),
                      "url": art.get("url", ""), "body": body} for (cat, art), body in zip(to_enrich, bodies)]
                    if _dump_enabled() else None)

    # Batch all articles into a single gpt-4o-mini call
    try:
        batch_lines = []
        for idx, ((_cat, art), body) in enumerate(zip(to_enrich, bodies), 1):
            title = art.get("title", "")
            desc  = art.get("description", "")
            batch_lines.append(
                f"{idx}. Title: {title}\n   Description: {desc}\n   Body: {body[:400]}"
            )
        batch_prompt = (
            "For each numbered article below, write ONE crisp sentence (max 30 words) explaining "
            "why it matters to a tech-executive investor in New York today.\n"
            "Return ONLY a JSON object mapping number (as string) to sentence, e.g. "
            '{"1": "...", "2": "..."}.\n\n'
            + "\n\n".join(batch_lines)
        )
        batch_resp = client.chat.completions.create(
            model=OPENAI_HELPER_MODEL,
            messages=[{"role": "user", "content": batch_prompt}],
            max_tokens=80 * len(to_enrich),
            temperature=0.3,
        )
        raw = batch_resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        results_map: dict = json.loads(raw)
        for idx, (_cat, art) in enumerate(to_enrich, 1):
            sentence = results_map.get(str(idx), "")
            if sentence:
                art["why_it_matters"] = sentence
        logger.info("Batch enrichment: enriched %d articles in one call", len(to_enrich))
    except Exception as exc:
        logger.warning("Batch enrichment failed (%s) — falling back to individual calls", exc)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(enrich_article_individual, cat, art, body)
                       for (cat, art), body in zip(to_enrich, bodies)]
            try:
                for fut in as_completed(futures, timeout=120):
                    try:
                        fut.result()
                    except Exception as exc2:
                        logger.debug("Enrichment future error: %s", exc2)
            except FuturesTimeoutError:
                logger.warning("Individual enrichment timed out after 120s")

    enriched_count = sum(1 for _, art in to_enrich if art.get("why_it_matters"))
    logger.info("Enrichment: %d/%d top articles enriched.", enriched_count, len(to_enrich))
    if enrich_input is not None:
        dump_fixture("article_enricher", TODAY, {
            "input": {"articles": enrich_input},
            "output": {"why_it_matters": [art.get("why_it_matters", "") for _, art in to_enrich]},
        })
    return articles


# ============================================================
# Mike's Picks
# ============================================================

def load_picks() -> list[dict]:
    """Load unprocessed picks from mikes_picks.json (S3 or local disk)."""
    from mc_config import S3_BUCKET
    if S3_BUCKET:
        from mc_utils import s3_load_json
        data = s3_load_json(S3_BUCKET, "mikes_picks.json")
        if not isinstance(data, list):
            return []
        return [p for p in data if not p.get("processed")]
    if not PICKS_FILE.exists():
        return []
    try:
        with open(PICKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return [p for p in data if not p.get("processed")]
    except (json.JSONDecodeError, IOError):
        return []


def mark_picks_processed() -> None:
    """Set processed=True on all picks so they don't appear in the next run (S3 or local disk)."""
    from mc_config import S3_BUCKET
    if S3_BUCKET:
        from mc_utils import s3_load_json, s3_save_json
        data = s3_load_json(S3_BUCKET, "mikes_picks.json")
        if not isinstance(data, list):
            return
        for item in data:
            item["processed"] = True
        s3_save_json(S3_BUCKET, "mikes_picks.json", data, indent=2, ensure_ascii=False)
        return
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
    Supports three types:
    - 'url'  — fetches the page and extracts up to 500 chars of body text.
    - 'pdf'  — attempts pdftotext extraction; falls back to filename.
    - 'text' — uses the content field directly.
    """
    ptype   = pick.get("type", "text")
    content = pick.get("content", "")
    title   = pick.get("title", "")

    if ptype == "url":
        try:
            resp = requests.get(content, timeout=10, headers={"User-Agent": "MikeCast/1.0"})
            soup = BeautifulSoup(resp.text, "html.parser")
            if not title:
                title = soup.title.get_text(strip=True) if soup.title else content
            body_text = soup.get_text(separator=" ", strip=True)[:1500]
            summary   = body_text[:500] + ("..." if len(body_text) > 500 else "")
        except Exception as exc:
            logger.warning("Could not fetch URL %s: %s", content, exc)
            summary = f"Submitted URL: {content}"
            if not title:
                title = content
        return {"title": title, "summary": summary, "url": content, "type": "url"}

    elif ptype == "pdf":
        summary = f"PDF document submitted: {os.path.basename(content)}"
        try:
            import subprocess
            result = subprocess.run(
                ["pdftotext", content, "-", "-l", "3"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                extracted = result.stdout.strip()[:1500]
                summary   = extracted[:500] + ("..." if len(extracted) > 500 else "")
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
    """Load, summarise, and mark-as-processed all pending Mike's Picks."""
    raw_picks = load_picks()
    if not raw_picks:
        return []
    summaries = [summarise_pick(p) for p in raw_picks]
    mark_picks_processed()
    logger.info("Processed %d Mike's Pick(s).", len(summaries))
    return summaries
