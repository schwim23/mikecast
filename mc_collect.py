"""
MikeCast — news collection, deduplication, clustering, scoring, enrichment,
and Mike's Picks processing.

Pipeline order:
  collect_all_news()       → raw articles from all sources
  deduplicate()            → remove articles seen in the last 7 days
  cluster_articles()       → merge same-story duplicates (gpt-4o-mini)
  score_and_rank_articles()→ per-category relevance scores (gpt-4o)
  enrich_top_stories()     → add "why it matters" to the top-N stories
  select_top_articles()    → trim to a target total across categories
  process_picks()          → load and summarise Mike's hand-picked items
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from mc_config import (
    CATEGORIES, CATEGORY_SCORER_PROMPTS, CNBC_RSS_FEEDS, ESPN_RSS_FEEDS,
    HISTORY_FILE, NYT_API_KEY, NYT_SEARCH_QUERIES, NYT_SECTION_TO_CATEGORY,
    NYT_SECTIONS, OPENAI_API_KEY, PICKS_FILE, REDDIT_FEEDS, REDDIT_USER_AGENT,
    SCORE_BATCH_SIZE, SOURCE_TIERS, TECH_RSS_FEEDS, WIRE_RSS_FEEDS,
)
from mc_utils import _atomic_write_json, _safe_request, title_similarity, url_fingerprint

logger = logging.getLogger("mikecast")


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
    """Fetch ESPN RSS feeds (General, NBA, MLB, NFL, NHL). Returns a flat list."""
    articles: list[dict] = []
    for feed_url, sport_label in ESPN_RSS_FEEDS:
        resp = _safe_request(feed_url, timeout=15)
        if resp is None:
            logger.warning("ESPN [%s]: no response", sport_label)
            continue
        try:
            soup  = BeautifulSoup(resp.content, "xml")
            count = 0
            for item in soup.find_all("item")[:8]:
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
    Uses the publisher's domain URL (from <source url='...'>) as the article link
    because Google News redirect URLs hit a consent wall server-side.
    Restricts results to the past 24 hours via the 'when:1d' modifier.
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
            pub_tag    = item.find("pubdate")

            source_url  = source_tag.get("url", "") if source_tag else ""
            article_url = source_url or (link_tag.get_text(strip=True) if link_tag else "")

            articles.append({
                "title":       title_tag.get_text(strip=True) if title_tag else "",
                "url":         article_url,
                "description": (
                    BeautifulSoup(desc_tag.get_text(), "html.parser").get_text(strip=True)
                    if desc_tag else ""
                ),
                "source":    source_tag.get_text(strip=True) if source_tag else "Google News",
                "published": pub_tag.get_text(strip=True) if pub_tag else "",
            })
    except Exception as exc:
        logger.warning("Google News RSS parse error: %s", exc)
    return articles


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
        "wire": lambda: ("cat_dict", _fetch_rss_source_list(WIRE_RSS_FEEDS, "Wire RSS")),
        "cnbc": lambda: ("cat_dict", _fetch_rss_source_list(CNBC_RSS_FEEDS, "CNBC RSS")),
        "reddit": lambda: ("cat_dict", fetch_reddit_rss()),
        "espn":  lambda: ("espn",   fetch_espn_rss_feeds()),
    }

    with ThreadPoolExecutor(max_workers=6) as ex:
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
    """Load the rolling 7-day article history from disk."""
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
    return deduped


def filter_stale_articles(
    categorised: dict[str, list[dict]],
    max_age_days: int = 3,
) -> dict[str, list[dict]]:
    """
    Drop articles whose parsed publication date is older than *max_age_days*.
    Articles with unparseable or missing dates are kept (benefit of the doubt).
    This prevents old recirculated content (e.g. from AOL) from entering the pipeline.
    """
    from email.utils import parsedate_to_datetime

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    filtered: dict[str, list[dict]] = {}
    total_dropped = 0

    for cat, arts in categorised.items():
        kept: list[dict] = []
        for art in arts:
            pub = art.get("published", "").strip()
            if not pub:
                kept.append(art)
                continue
            parsed_dt = None
            for parse_fn in (
                lambda s: parsedate_to_datetime(s),            # RFC 2822 (RSS pubDate)
                lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),  # ISO 8601
            ):
                try:
                    parsed_dt = parse_fn(pub)
                    if parsed_dt.tzinfo is None:
                        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    continue

            if parsed_dt is not None and parsed_dt < cutoff:
                logger.debug("Dropping stale article (%s): %s", pub[:10], art.get("title", "")[:60])
                total_dropped += 1
                continue
            kept.append(art)
        filtered[cat] = kept

    if total_dropped:
        logger.info("Stale article filter: dropped %d articles older than %d days.", total_dropped, max_age_days)
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

    from openai import OpenAI
    client = OpenAI()

    def cluster_category(cat: str, articles: list[dict]) -> list[dict]:
        if len(articles) <= 5:
            return articles  # not worth the API call for tiny sets

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
# Scoring (gpt-4o — per-category parallel agents)
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
        lines.append(
            f"[{score_id}] {title}\n"
            f"  Source: {source} (tier {tier}){hn_info}\n"
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

    from openai import OpenAI
    client = OpenAI()

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
        )) + scorer_suffix

        for batch_start in range(0, len(articles), SCORE_BATCH_SIZE):
            batch = articles[batch_start: batch_start + SCORE_BATCH_SIZE]
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o",
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

    from openai import OpenAI
    client = OpenAI()

    all_arts = [(cat, art) for cat, arts in articles.items() for art in arts]
    all_arts.sort(key=lambda x: x[1].get("score", 50), reverse=True)
    to_enrich = all_arts[:top_n]

    def fetch_body(url: str) -> str:
        """Fetch and strip the article body (first ~1500 chars of visible text)."""
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "MikeCast/2.0 (enrichment)"})
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            return soup.get_text(separator=" ", strip=True)[:1500]
        except Exception:
            return ""

    def enrich_article(_cat: str, art: dict) -> None:
        url     = art.get("url", "")
        title   = art.get("title", "")
        desc    = art.get("description", "")
        body    = fetch_body(url) if url else ""
        context = f"Title: {title}\nDescription: {desc}\nBody: {body[:800]}"
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
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


# ============================================================
# Mike's Picks
# ============================================================

def load_picks() -> list[dict]:
    """Load unprocessed picks from mikes_picks.json."""
    if not PICKS_FILE.exists():
        return []
    try:
        with open(PICKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return [p for p in data if not p.get("processed")]
    except (json.JSONDecodeError, IOError):
        return []


def mark_picks_processed() -> None:
    """Set processed=True on all picks so they don't appear in the next run."""
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
