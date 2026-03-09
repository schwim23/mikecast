# MikeCast — Claude Context

## What This Is

MikeCast is an automated daily news briefing system. Every morning at 6:45 AM ET, it:
1. Collects news from NYT, RSS feeds, Reddit, Hacker News, Google News
2. Deduplicates, clusters, scores, and ranks articles
3. Generates an HTML briefing + 2 podcast scripts via GPT-4o
4. Runs a quality critic pass (regenerates weak sections)
5. Generates audio via ElevenLabs (3-voice) or OpenAI TTS (fallback)
6. Saves JSON/RSS, sends an email, commits + pushes to GitHub Pages

## How to Run

```bash
# Normal run (skips if today already generated)
source ~/.profile && .venv/bin/python3 mikecast_briefing.py

# Force regenerate today's briefing
source ~/.profile && .venv/bin/python3 mikecast_briefing.py --force

# Check the log
tail -f mikecast.log

# Local dashboard
.venv/bin/python3 server.py  # → http://localhost:8080
```

The cron wrapper `run_mikecast.sh` also auto-commits `data/` and `briefing_history.json` and pushes to GitHub after each run.

## Environment Variables (all in `~/.profile`)

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Yes | GPT-4o for all generation + critic |
| `NYTAPIKEY` | Yes | NYT Top Stories + Article Search |
| `GMAIL_APP_PASSWORD` | Yes | Gmail SMTP (16-digit app password) |
| `GMAIL_FROM` | Yes | Sender address |
| `GMAIL_TO` | Yes | Recipient address |
| `ELEVENLABS_API_KEY` | No | 3-voice audio (Mike/Elizabeth/Jesse) |
| `ELEVENLABS_VOICE_MIKE` | No | ElevenLabs voice ID |
| `ELEVENLABS_VOICE_ELIZABETH` | No | ElevenLabs voice ID |
| `ELEVENLABS_VOICE_JESSE` | No | ElevenLabs voice ID |
| `XAI_API_KEY` | No | Grok adaptive search planning (Step 0) |

## Module Map

| File | Owns |
|---|---|
| `mikecast_briefing.py` | Entry point — orchestrates all 10 steps |
| `mc_config.py` | All constants, env vars, categories, RSS feeds, scoring prompts |
| `mc_plan.py` | Step 0: xAI Grok generates dynamic search queries |
| `mc_collect.py` | Steps 1–6: collect, dedupe, cluster, score, select, enrich |
| `mc_generate.py` | Step 8: HTML briefing + single-voice + 3-voice scripts |
| `mc_critic.py` | Step 8b: GPT-4o quality critic; patches weak sections |
| `mc_audio.py` | Step 9: ElevenLabs 3-voice + OpenAI TTS fallback |
| `mc_deliver.py` | Step 10: JSON, manifest.json, feed.xml, email |
| `mc_utils.py` | Shared helpers: HTTP, JSON I/O, text similarity, URL fingerprinting |
| `mikes_picks_ingest.py` | CLI to add URLs/PDFs/text to the picks queue |
| `server.py` | Flask server for local dashboard |

## Key Constraints — Do Not Violate

- **NY Sports is NEVER auto-patched by the critic** (`NEVER_PATCH_NORMALIZED = {"ny sports"}`). Patching sports sections causes GPT to hallucinate scores, players, and trades.
- **Critic threshold is 7/10** (`_WEAK_THRESHOLD = 7`). Sections scoring below 7 get regenerated once — no retry loop.
- **Hallucination guards are everywhere**: every GPT prompt explicitly tells the model to only discuss articles in the input. Do not weaken these.
- **Sports sources are allowlisted**: `SPORTS_TRUSTED_SOURCES` in `mc_config.py`. Articles from untrusted publishers (e.g. AOL) are dropped before generation.
- **7-day dedup**: `briefing_history.json` tracks seen URLs/titles. Don't delete or corrupt this file.

## Output Files

```
data/
  YYYY-MM-DD.json          — full episode data
  MikeCast_YYYY-MM-DD.mp3        — OpenAI TTS audio (fallback)
  MikeCast_3voice_YYYY-MM-DD.mp3 — ElevenLabs 3-voice audio (preferred)
  manifest.json            — list of all available dates
  feed.xml                 — RSS 2.0 podcast feed
briefing_history.json      — rolling 7-day dedup history
mikes_picks.json           — pending picks queue
```

## Adding/Changing Content

- **New category**: add to `CATEGORIES`, `NYT_SECTION_TO_CATEGORY`, `NYT_SEARCH_QUERIES`, `CATEGORY_SCORER_PROMPTS` in `mc_config.py`
- **New RSS feed**: add to the relevant `*_RSS_FEEDS` list in `mc_config.py`
- **Change scoring behavior**: edit `CATEGORY_SCORER_PROMPTS` in `mc_config.py`
- **Add a Mike's Pick**: use `mikes_picks_ingest.py` (see `/mc-picks` skill)
