"""
MikeCast — configuration, constants, and environment variables.

All runtime secrets are read from environment variables; nothing is
hardcoded here.  Edit CATEGORIES / CATEGORY_SCORER_PROMPTS to change
what topics MikeCast covers and how each category is scored.
"""

import os
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

# ---------------------------------------------------------------------------
# Directories & file paths
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).resolve().parent
DATA_DIR     = SCRIPT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

HISTORY_FILE = SCRIPT_DIR / "briefing_history.json"
PICKS_FILE   = SCRIPT_DIR / "mikes_picks.json"

# ---------------------------------------------------------------------------
# Runtime secrets (loaded from environment — see ~/.profile)
# ---------------------------------------------------------------------------
NYT_API_KEY        = os.environ.get("NYTAPIKEY", "")
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY", "")
GMAIL_APP_PASSWORD = (
    os.environ.get("GMAIL_APP_PASSWORD", "")
    .replace("\\n", "").replace("\n", "").strip()
)
GMAIL_FROM = os.environ.get("GMAIL_FROM", "prometheusagent23@gmail.com")
GMAIL_TO   = os.environ.get("GMAIL_TO",   "michael.schwimmer@gmail.com")

# Resend — daily email newsletter broadcast to confirmed subscribers (optional;
# the broadcast is skipped gracefully when RESEND_API_KEY / RESEND_AUDIENCE_ID
# are unset, so the existing Gmail send to GMAIL_TO is never affected).
RESEND_API_KEY     = os.environ.get("RESEND_API_KEY", "")
RESEND_AUDIENCE_ID = os.environ.get("RESEND_AUDIENCE_ID", "")
RESEND_FROM        = os.environ.get("RESEND_FROM", "MikeCast <mike@mikecast.io>")
RESEND_REPLY_TO    = os.environ.get("RESEND_REPLY_TO", "michael.schwimmer@gmail.com")

# ElevenLabs — 3-voice podcast (Mike = host, Elizabeth = tech/biz, Jesse = sports)
ELEVENLABS_API_KEY         = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_MIKE      = os.environ.get("ELEVENLABS_VOICE_MIKE", "")
ELEVENLABS_VOICE_ELIZABETH = os.environ.get("ELEVENLABS_VOICE_ELIZABETH", "")
ELEVENLABS_VOICE_JESSE     = os.environ.get("ELEVENLABS_VOICE_JESSE", "")

# xAI — Grok-2 for adaptive search planning (optional; skip gracefully if unset)
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")

# Social distribution — daily auto-posts to X and Instagram (all optional; each
# channel is skipped gracefully when its credentials are unset, so an unconfigured
# or misconfigured social channel never blocks the daily pipeline).
#
# X (Twitter) API v2 — OAuth 1.0a user-context, "Read and write" app permission.
X_API_KEY             = os.environ.get("X_API_KEY", "")
X_API_SECRET          = os.environ.get("X_API_SECRET", "")
X_ACCESS_TOKEN        = os.environ.get("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

# Instagram Graph API — Business account linked to a Facebook Page. META_ACCESS_TOKEN
# is a system-user (non-expiring) or long-lived (60-day) token; IG_USER_ID is the
# Instagram Business account id fetched via GET /me/accounts → page → instagram_business_account.
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
IG_USER_ID        = os.environ.get("IG_USER_ID", "")

# Daily social media kind: "card" (static 1080×1080 image) or "reel" (9:16 vertical
# video with podcast audio + burned-in captions). Defaults to "card" so the proven
# path stays live; flip to "reel" via the ECS task-def env AFTER a live IG test —
# no code deploy needed. A reel-build failure always falls back to the card.
SOCIAL_MEDIA_KIND = os.environ.get("SOCIAL_MEDIA_KIND", "card").strip().lower()

# CloudFront distribution that fronts mikecast.io (used for cache invalidation
# after editing/republishing a past episode). Defaults to the live distribution.
CLOUDFRONT_DIST_ID = os.environ.get("CLOUDFRONT_DIST_ID", "EFNQM31KQHY56")

# Anthropic — Claude for the CrewAI writing crew (required when running --crew)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# CrewAI model selection (LiteLLM-style model strings)
# ---------------------------------------------------------------------------
CLAUDE_WRITER_MODEL  = os.environ.get("CLAUDE_WRITER_MODEL",  "anthropic/claude-sonnet-4-6")
OPENAI_SCORER_MODEL  = os.environ.get("OPENAI_SCORER_MODEL",  "openai/gpt-4o")
OPENAI_CRITIC_MODEL  = os.environ.get("OPENAI_CRITIC_MODEL",  "openai/gpt-4o")
OPENAI_HELPER_MODEL  = os.environ.get("OPENAI_HELPER_MODEL",  "openai/gpt-4o-mini")

# ---------------------------------------------------------------------------
# Date helpers (Eastern Time — matches browser / cron timezone)
# ---------------------------------------------------------------------------
_ET          = ZoneInfo("America/New_York")
# NOTE: TODAY is computed once at module import time. If an ECS task starts before
# midnight and runs past it, TODAY will reflect the wrong date for the entire run.
# The correct fix is to pass a RunContext with a frozen date, but that requires a
# larger refactor. For now, the cron schedule (6:45 AM ET) makes this very unlikely.
TODAY        = datetime.now(_ET).strftime("%Y-%m-%d")
TODAY_DISPLAY = datetime.now(_ET).strftime("%B %d, %Y")

# ---------------------------------------------------------------------------
# AWS
# ---------------------------------------------------------------------------
# Set S3_BUCKET to enable S3 mode. When set, all pipeline outputs are written
# to S3 instead of the local filesystem. Local execution is preserved when unset.
S3_BUCKET = os.environ.get("S3_BUCKET", "")

# ---------------------------------------------------------------------------
# Public site URL (used in RSS feed and email subscribe footer)
# ---------------------------------------------------------------------------
SITE_BASE_URL = "https://mikecast.io/"

# ---------------------------------------------------------------------------
# News categories and Google News search queries
# Extend or trim CATEGORIES to change what topics MikeCast covers.
# ---------------------------------------------------------------------------
CATEGORIES: dict[str, list[str]] = {
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
        "Uber news today", "Anthropic news today",
    ],
    "NY Sports": [
        "New York Yankees", "New York Knicks",
        "New York Giants NFL", "New Jersey Devils NHL",
    ],
}

# NYT Top Stories sections → category mapping
NYT_SECTIONS: list[str] = ["technology", "business", "sports", "home"]
NYT_SECTION_TO_CATEGORY: dict[str, str] = {
    "technology": "AI & Tech",
    "business":   "Business & Markets",
    "sports":     "NY Sports",
    "home":       "AI & Tech",
}

# Additional NYT Article Search queries per category
NYT_SEARCH_QUERIES: dict[str, list[str]] = {
    "AI & Tech":          ["artificial intelligence", "OpenAI Anthropic"],
    "Business & Markets": ["stock market economy", "venture capital AI"],
    "Companies":          ["Apple Meta Amazon Nvidia Tesla Anthropic", "Uber Netflix Microsoft Google"],
    "NY Sports":          ["Yankees Knicks Giants Devils", "NBA MLB NFL sports"],
}

# ---------------------------------------------------------------------------
# Source credibility tiers (passed to scoring prompts)
# Tier 1 = highest credibility, Tier 3 = community aggregators.
# ---------------------------------------------------------------------------
SOURCE_TIERS: dict[str, int] = {
    # Tier 1
    "The New York Times": 1, "Reuters": 1, "Associated Press": 1,
    "The Verge": 1, "Ars Technica": 1, "MIT Technology Review": 1, "Wired": 1,
    # Tier 2
    "TechCrunch": 2, "VentureBeat": 2, "CNBC": 2, "ESPN": 2, "Hacker News": 2,
    # Tier 3
    "Reddit": 3, "Google News": 3,
}

# ---------------------------------------------------------------------------
# RSS feed source lists
# Each entry: (source_name, feed_url, category, max_articles)
# ---------------------------------------------------------------------------
TECH_RSS_FEEDS: list[tuple[str, str, str, int]] = [
    ("TechCrunch",            "https://techcrunch.com/feed/",                    "AI & Tech", 8),
    ("The Verge",             "https://www.theverge.com/rss/index.xml",          "AI & Tech", 8),
    ("Ars Technica",          "https://feeds.arstechnica.com/arstechnica/index", "AI & Tech", 6),
    ("VentureBeat",           "https://venturebeat.com/feed/",                   "AI & Tech", 6),
    ("Wired",                 "https://www.wired.com/feed/rss",                  "AI & Tech", 5),
    ("MIT Technology Review", "https://www.technologyreview.com/feed/",          "AI & Tech", 5),
]

WIRE_RSS_FEEDS: list[tuple[str, str, str, int]] = [
    ("Reuters",          "https://feeds.reuters.com/reuters/topNews",        "Business & Markets", 6),
    ("Reuters",          "https://feeds.reuters.com/reuters/businessNews",   "Business & Markets", 6),
    ("Reuters",          "https://feeds.reuters.com/reuters/technologyNews", "AI & Tech",          5),
    ("Associated Press", "https://feeds.apnews.com/rss/apf-topnews",        "Business & Markets", 5),
    ("Associated Press", "https://feeds.apnews.com/rss/apf-technology",     "AI & Tech",          5),
    ("Associated Press", "https://feeds.apnews.com/rss/apf-business",       "Business & Markets", 5),
]

CNBC_RSS_FEEDS: list[tuple[str, str, str, int]] = [
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "Business & Markets", 6),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",  "AI & Tech",          6),
    ("CNBC", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",  "Business & Markets", 6),
]

# ESPN news feeds. We switched from www.espn.com/espn/rss/* (RSS XML, blocked
# by bot detection — every call returned HTTP 202 with an empty body) to
# site.api.espn.com/.../{sport}/{league}/news (JSON, no challenge). Constant
# name kept as ESPN_RSS_FEEDS so callers don't churn; format is unchanged
# (url, sport_label) and the parser handles JSON now.
ESPN_RSS_FEEDS: list[tuple[str, str]] = [
    ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news", "NBA"),
    ("https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/news",   "MLB"),
    ("https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",   "NFL"),
    ("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/news",     "NHL"),
]

# Reddit Atom feeds: (subreddit, category, max_articles)
# Sports subreddits excluded — fan speculation caused hallucination in generation.
REDDIT_FEEDS: list[tuple[str, str, int]] = [
    ("MachineLearning", "AI & Tech",          10),
    ("artificial",      "AI & Tech",          10),
    ("technology",      "AI & Tech",          10),
    ("investing",       "Business & Markets", 10),
]

# Publishers trusted for sports articles. Articles from other sources in the
# NY Sports category are dropped before generation to prevent stale aggregator
# content (e.g. AOL.com) from entering the pipeline.
SPORTS_TRUSTED_SOURCES: set[str] = {
    "ESPN", "The New York Times", "Associated Press", "Reuters",
    "CBS Sports", "NBC Sports", "Sports Illustrated", "The Athletic",
    "Bleacher Report", "MLB.com", "NBA.com", "NFL.com", "NHL.com",
    "New York Post", "New York Daily News", "NJ.com",
}

REDDIT_USER_AGENT = (
    "MikeCast/2.0 (personal news briefing bot; contact: prometheusagent23@gmail.com)"
)

# Max articles per scoring batch sent to a single GPT-4o call
SCORE_BATCH_SIZE = 40

# ---------------------------------------------------------------------------
# Per-category LLM scoring prompts
# Each prompt is appended with a JSON response-format instruction at runtime.
# ---------------------------------------------------------------------------
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
        "Microsoft, Google, Netflix, Uber, Anthropic. Prioritize: product announcements, earnings, leadership changes, "
        "strategic pivots, regulatory actions, major partnerships. "
        "Bonus: stories about those specific companies +15. "
        "Penalty: minor product updates with no strategic significance -10, obscure companies -15."
    ),
    "NY Sports": (
        "You score sports news for Mike, a devoted New York sports fan. "
        "His teams are the Knicks, Devils, Yankees, and NY Giants — these four are the "
        "ONLY teams that reliably belong in the briefing, and they are weighted equally. "
        "Bonus: a story about any of the four (Knicks / Devils / Yankees / Giants) +20. "
        "Prefer fresh news — a development from the last 24 hours (game result, trade, "
        "signing, injury, roster or coaching move) +10 on top; stale recaps of games more "
        "than a couple days old score low. "
        "NON-NY content has a high bar. Score 50+ ONLY when the story is a major national "
        "sports story that is seismic and culture-defining: a championship being clinched, "
        "an MVP / Cy Young / Heisman / Coach of the Year announcement, a generational trade "
        "(Doncic-to-Lakers tier), or a career-altering injury to an all-time-great player. "
        "Routine non-NY content scores 25 or lower, even if it would make for fun bar "
        "conversation. "
        "Penalty: routine non-NY game recaps -25, non-NY coaching hires (unless the hire "
        "is a sitting HOFer or league-altering) -20, non-NY player news that doesn't "
        "affect NY teams -20, generic sports commentary with no specific news -15."
    ),
}
