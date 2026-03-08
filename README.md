# MikeCast: Daily AI-Powered News Briefing

MikeCast is an automated daily news briefing system for "Big Mike." It runs a 10-step pipeline each morning to collect, score, and deliver a personalized news package covering AI/Tech, Business & Markets, key Companies, and NY Sports — as an HTML email, a podcast, and a static dashboard website.

## Features

1. **Adaptive Search Planning (xAI Grok)**: Before collecting news, Grok-3 searches the live web to identify today's breaking stories and generate targeted queries for each category. This ensures the briefing captures fresh, time-sensitive news rather than relying solely on static search terms.

2. **Multi-Source News Aggregation**: Pulls articles in parallel from:
   - **NYT Top Stories & Article Search APIs**: Authoritative headlines from Technology, Business, Sports, and Home sections.
   - **RSS Feeds**: TechCrunch, The Verge, Ars Technica, VentureBeat, Wired, MIT Technology Review, Reuters, Associated Press, CNBC, ESPN (General/NBA/MLB/NFL/NHL).
   - **Reddit Atom Feeds**: r/MachineLearning, r/artificial, r/technology, r/investing, r/nba, r/baseball.
   - **Google News**: Fallback keyword search for broad coverage.

3. **Content Deduplication & Clustering**: Maintains a 7-day rolling history (`briefing_history.json`) to skip repeated stories. A `gpt-4o-mini` clustering pass then groups near-duplicate articles before scoring.

4. **AI Scoring & Ranking**: Per-category GPT-4o agents score and rank articles using tailored prompts (e.g., bonus for Yankees/Knicks stories in Sports, penalty for vague AI hype in Tech). Grok's trending context is passed to scoring agents to weight breaking news higher.

5. **Story Enrichment**: The top 8 articles have their full body text fetched and receive a `gpt-4o-mini` "why it matters" annotation.

6. **"Mike's Picks"**: User-submitted content (URLs, local PDFs, or raw text) queued via `mikes_picks_ingest.py` and included as a dedicated section in every briefing.

7. **Multi-Format Generation** (parallel GPT-4o calls):
   - **HTML Briefing**: Professional email with executive summary, categorized stories, and clickable links.
   - **Single-voice podcast script**: For OpenAI TTS (fallback/email attachment).
   - **3-voice conversational script**: For ElevenLabs with `[MIKE]`, `[ELIZABETH]`, and `[JESSE]` speaker tags.

8. **Quality Critic Pass**: A GPT-4o critic scores each category section (1–10) on depth, substance, and story count. Sections scoring below 7 are automatically regenerated with targeted prompts, then podcast scripts are regenerated to match.

9. **Dual-TTS Audio Generation**:
   - **ElevenLabs 3-voice** (preferred): Mike = host, Elizabeth = tech/biz, Jesse = sports.
   - **OpenAI TTS** (single voice, "alloy"): Always generated as backup and email attachment.
   - The ElevenLabs version is used for the RSS podcast feed when available.

10. **Delivery & Publishing**:
    - **Email**: HTML briefing in the body, podcast script and audio as attachments, sent via Gmail SMTP.
    - **Daily JSON**: All content saved to `data/YYYY-MM-DD.json` for the dashboard.
    - **Manifest**: `data/manifest.json` updated for dashboard date-picker navigation.
    - **RSS Feed**: `data/feed.xml` updated as a standard podcast RSS 2.0 feed.

11. **Static Dashboard Website**: A responsive dark-themed SPA (`dashboard/`) for browsing briefings by date, with an embedded audio player and collapsible script viewer.

## Pipeline (10 Steps)

```
Step 0   Plan searches       xAI Grok-3 identifies breaking stories → dynamic queries
Step 1   Collect news        Parallel fetch from all sources (NYT, RSS, Reddit, Google News)
Step 2   Deduplicate         Skip articles seen in the past 7 days
Step 3   Cluster             GPT-4o-mini groups near-duplicate articles
Step 4   Score & rank        Per-category GPT-4o agents score articles (with trending context)
Step 5   Select top 25       Proportional across categories
Step 6   Enrich top 8        Fetch full body + "why it matters" via GPT-4o-mini
Step 7   Mike's Picks        Process user-submitted URLs, PDFs, and text
Step 8   Generate content    Parallel: HTML briefing + single-voice script + 3-voice script
Step 8b  Critic pass         GPT-4o scores sections; regenerates weak ones (score < 7)
Step 9   Generate audio      ElevenLabs 3-voice + OpenAI TTS single-voice backup
Step 10  Save & deliver      JSON → manifest → RSS feed → email
```

## Project Structure

```
mikecast/
├── mikecast_briefing.py      # Main entry point — orchestrates the full pipeline
├── mc_config.py              # Configuration, constants, env vars, category definitions
├── mc_plan.py                # xAI Grok adaptive search planning (Step 0)
├── mc_collect.py             # News collection, dedup, clustering, scoring, enrichment (Steps 1-6)
├── mc_generate.py            # GPT-4o content generation: HTML + podcast scripts (Step 8)
├── mc_critic.py              # Post-generation quality critic pass (Step 8b)
├── mc_audio.py               # TTS audio: OpenAI + ElevenLabs (Step 9)
├── mc_deliver.py             # Save JSON, manifest, RSS feed, send email (Step 10)
├── mc_utils.py               # Shared utility helpers (HTTP, JSON, text similarity)
├── mikes_picks_ingest.py     # Utility to queue content in Mike's Picks
├── server.py                 # Flask server for local dashboard with /api/manifest
├── mikes_picks.json          # Queue for user-submitted content
├── briefing_history.json     # Rolling 7-day history of processed articles
├── requirements.txt          # Python dependencies
├── README.md                 # This file
├── task_prompt.md            # Original scheduled task prompt
├── .venv/                    # Python virtual environment (not committed)
├── data/                     # Daily JSON files + audio + manifest + RSS
│   ├── YYYY-MM-DD.json
│   ├── MikeCast_YYYY-MM-DD.mp3
│   ├── MikeCast_3voice_YYYY-MM-DD.mp3
│   ├── manifest.json
│   └── feed.xml
└── dashboard/                # Static website files
    ├── index.html
    ├── style.css
    └── app.js
```

## Setup and Installation

This project runs locally on Linux (Ubuntu/Debian) with Python 3.12+.

### 1. Clone the Repository

```bash
git clone https://github.com/schwim23/mikecast.git
cd mikecast
```

### 2. Install System Dependencies

```bash
sudo apt install -y python3.12-venv python3-pip poppler-utils
```

`poppler-utils` provides `pdftotext`, required for PDF ingestion in Mike's Picks.

### 3. Create a Virtual Environment and Install Python Dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install flask  # for local dashboard server
```

### 4. Set Environment Variables

Add the following to both `~/.bashrc` (interactive terminals) and `~/.profile` (login shells and cron):

```bash
# Required
export NYTAPIKEY="your_nyt_api_key"
export OPENAI_API_KEY="your_openai_api_key"
export GMAIL_APP_PASSWORD="your_16_digit_gmail_app_password"
export GMAIL_FROM="sender@gmail.com"
export GMAIL_TO="recipient@example.com"

# Optional — enables ElevenLabs 3-voice podcast (preferred for RSS)
export ELEVENLABS_API_KEY="your_elevenlabs_api_key"
export ELEVENLABS_VOICE_MIKE="voice_id_for_mike"
export ELEVENLABS_VOICE_ELIZABETH="voice_id_for_elizabeth"
export ELEVENLABS_VOICE_JESSE="voice_id_for_jesse"

# Optional — enables xAI Grok adaptive search planning (Step 0)
export XAI_API_KEY="your_xai_api_key"
```

**Note:** Cron jobs do not source `~/.bashrc`. The `run_mikecast.sh` wrapper explicitly sources `~/.profile`.

Where to get API keys:
- `NYTAPIKEY`: [NYT Developer Portal](https://developer.nytimes.com/)
- `OPENAI_API_KEY`: [OpenAI Platform](https://platform.openai.com/api-keys)
- `GMAIL_APP_PASSWORD`: A 16-digit App Password from Google Account (not your regular password)
- `ELEVENLABS_API_KEY`: [ElevenLabs](https://elevenlabs.io/)
- `XAI_API_KEY`: [xAI](https://x.ai/)

### 5. Configure Git Credentials (for GitHub Pages auto-push)

```bash
git -C ~/mikecast config credential.helper store
```

Then do one manual push with your GitHub username and a Personal Access Token as the password. Credentials are stored in `~/.git-credentials` for all future automated pushes.

### 6. Test a Manual Run

```bash
source ~/.profile
cd ~/mikecast
.venv/bin/python3 mikecast_briefing.py
```

To force-regenerate today's briefing if one already exists:

```bash
.venv/bin/python3 mikecast_briefing.py --force
```

### 7. Schedule the Daily Cron Job

```bash
crontab -e
```

Current entry (runs at **6:45 AM ET** daily):

```
45 6 * * * /home/mike-schwimmer/mikecast/run_mikecast.sh >> /home/mike-schwimmer/mikecast/mikecast.log 2>&1
```

The `run_mikecast.sh` wrapper:
- Sources `~/.profile` to load environment variables
- Runs `mikecast_briefing.py` with the virtual environment
- Commits and pushes updated data to GitHub (which updates the GitHub Pages dashboard)

## Usage

### Generating the Daily Briefing

Runs automatically via cron. To run manually:

```bash
cd ~/mikecast && .venv/bin/python3 mikecast_briefing.py
```

### Adding to "Mike's Picks"

```bash
# Add a URL
.venv/bin/python3 mikes_picks_ingest.py --url "https://example.com/article"

# Add a local PDF
.venv/bin/python3 mikes_picks_ingest.py --pdf "/path/to/paper.pdf"

# Add raw text
.venv/bin/python3 mikes_picks_ingest.py --text "Some interesting analysis..."

# Add with a custom title
.venv/bin/python3 mikes_picks_ingest.py --url "https://example.com" --title "My Custom Title"
```

### Viewing the Dashboard

#### Option A: GitHub Pages (Remote, Auto-Updated)

After each run, the cron script pushes data to GitHub, automatically updating the public site.

**URL:** `https://schwim23.github.io/mikecast/`

> **Limitation:** The archive date-picker dropdown requires the `/api/manifest` endpoint, which GitHub Pages (static host) cannot serve. All other features — today's briefing, audio player, article links — work fine.

#### Option B: Local Server (Full Functionality)

```bash
cd ~/mikecast
.venv/bin/python3 server.py
```

Then open `http://localhost:8080/dashboard/` in your browser. The local Flask server exposes `/api/manifest`, enabling full archive date-picker navigation. Run in `screen` or `tmux` for persistence.

### Subscribing to the Podcast RSS Feed

The RSS feed is published at:

```
https://schwim23.github.io/mikecast/data/feed.xml
```

Add this URL to any podcast app (Overcast, Pocket Casts, Castro, etc.) to receive new episodes automatically.

### Monitoring

```bash
cat ~/mikecast/mikecast.log
```

## Configuration

Edit `mc_config.py` to customize:
- **`CATEGORIES`**: The search topics and Google News queries per category.
- **`CATEGORY_SCORER_PROMPTS`**: The LLM scoring criteria for each category.
- **`NYT_SECTIONS` / `NYT_SEARCH_QUERIES`**: NYT API sections and search terms.
- **`TECH_RSS_FEEDS` / `WIRE_RSS_FEEDS` / `CNBC_RSS_FEEDS` / `ESPN_RSS_FEEDS` / `REDDIT_FEEDS`**: RSS and Reddit sources.
- **`SOURCE_TIERS`**: Source credibility rankings (1 = highest) passed to scoring agents.
