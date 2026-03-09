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

# ElevenLabs — 3-voice podcast (Mike = host, Elizabeth = tech/biz, Jesse = sports)
ELEVENLABS_API_KEY         = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_MIKE      = os.environ.get("ELEVENLABS_VOICE_MIKE", "")
ELEVENLABS_VOICE_ELIZABETH = os.environ.get("ELEVENLABS_VOICE_ELIZABETH", "")
ELEVENLABS_VOICE_JESSE     = os.environ.get("ELEVENLABS_VOICE_JESSE", "")

# xAI — Grok-2 for adaptive search planning (optional; skip gracefully if unset)
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")

# ---------------------------------------------------------------------------
# Date helpers (Eastern Time — matches browser / cron timezone)
# ---------------------------------------------------------------------------
_ET          = ZoneInfo("America/New_York")
TODAY        = datetime.now(_ET).strftime("%Y-%m-%d")
TODAY_DISPLAY = datetime.now(_ET).strftime("%B %d, %Y")

# ---------------------------------------------------------------------------
# Public site URL (used in RSS feed and email subscribe footer)
# ---------------------------------------------------------------------------
SITE_BASE_URL = "https://schwim23.github.io/mikecast/"

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
        "Uber news today",
    ],
    # NY Sports: Google News RSS removed — sports articles come from
    # Grok live search (fetch_grok_articles) + NYT only.
    "NY Sports": [],
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
    "Companies":          ["Apple Meta Amazon Nvidia Tesla", "Uber Netflix Microsoft Google"],
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

# ESPN RSS removed — national feeds pulled stale/non-NY content.
# Sports now sourced exclusively from Grok live search + NYT.
ESPN_RSS_FEEDS: list[tuple[str, str]] = []

# Reddit Atom feeds: (subreddit, category, max_articles)
# Sports subreddits removed — fan speculation and old content caused hallucination.
REDDIT_FEEDS: list[tuple[str, str, int]] = [
    ("MachineLearning", "AI & Tech",          10),
    ("artificial",      "AI & Tech",          10),
    ("technology",      "AI & Tech",          10),
    ("investing",       "Business & Markets", 10),
]

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
