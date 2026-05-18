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
# Legacy procedural pipeline (default — what cron runs today)
source ~/.profile && .venv/bin/python3 mikecast_briefing.py

# CrewAI pipeline (Steps 0–8b via crew/ agents; opt-in during shadow validation)
source ~/.profile && .venv/bin/python3 mikecast_briefing.py --crew

# Force regenerate today's briefing
source ~/.profile && .venv/bin/python3 mikecast_briefing.py --force
source ~/.profile && .venv/bin/python3 mikecast_briefing.py --crew --force

# Check the log
tail -f mikecast.log

# Local dashboard
.venv/bin/python3 server.py  # → http://localhost:8080
```

The cron wrapper `run_mikecast.sh` also auto-commits `data/` and `briefing_history.json` and pushes to GitHub after each run.

`--crew` and `--legacy` are mutually exclusive. Both paths produce the same on-disk output shape (data/`YYYY-MM-DD.json`, audio file, RSS feed) and use the same Steps 9–10 for audio + delivery. Default is `--legacy` while we shadow-validate the crew path.

## Environment Variables (all in `~/.profile`)

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Yes | GPT-4o for scoring + critic (legacy + crew) |
| `NYTAPIKEY` | Yes | NYT Top Stories + Article Search |
| `GMAIL_APP_PASSWORD` | Yes | Gmail SMTP (16-digit app password) |
| `GMAIL_FROM` | Yes | Sender address |
| `GMAIL_TO` | Yes | Recipient address |
| `ANTHROPIC_API_KEY` | Crew only | Claude for the writing crew (HTML + both podcast scripts) |
| `ELEVENLABS_API_KEY` | No | 3-voice audio (Mike/Elizabeth/Jesse) |
| `ELEVENLABS_VOICE_MIKE` | No | ElevenLabs voice ID |
| `ELEVENLABS_VOICE_ELIZABETH` | No | ElevenLabs voice ID |
| `ELEVENLABS_VOICE_JESSE` | No | ElevenLabs voice ID |
| `XAI_API_KEY` | No | Grok adaptive search planning (Step 0) |
| `CLAUDE_WRITER_MODEL` | No | LiteLLM model string for writers (default `anthropic/claude-sonnet-4-6`) |
| `OPENAI_SCORER_MODEL` | No | LiteLLM model string for scorers (default `openai/gpt-4o`) |
| `OPENAI_CRITIC_MODEL` | No | LiteLLM model string for critic (default `openai/gpt-4o`) |
| `OPENAI_HELPER_MODEL` | No | LiteLLM model string for helpers (default `openai/gpt-4o-mini`) |

## Module Map

| File | Owns |
|---|---|
| `mikecast_briefing.py` | Entry point — orchestrates all 10 steps; dispatches to legacy or crew path |
| `mc_config.py` | All constants, env vars, categories, RSS feeds, scoring prompts, CrewAI model strings |
| `mc_plan.py` | Step 0 (legacy): xAI Grok generates dynamic search queries |
| `mc_collect.py` | Steps 1–6 (legacy): collect, dedupe, cluster, score, select, enrich |
| `mc_generate.py` | Step 8 (legacy): HTML briefing + single-voice + 3-voice scripts |
| `mc_critic.py` | Step 8b (legacy): GPT-4o quality critic; patches weak sections |
| `mc_audio.py` | Step 9: ElevenLabs 3-voice + OpenAI TTS fallback (shared) |
| `mc_deliver.py` | Step 10: JSON, manifest.json, feed.xml, email (shared) |
| `mc_utils.py` | Shared helpers: HTTP (browser UA default), JSON I/O, text similarity, URL fingerprinting |
| `mikes_picks_ingest.py` | CLI to add URLs/PDFs/text to the picks queue |
| `server.py` | Flask server for local dashboard |
| `crew/tools.py` | Pydantic-typed CrewAI tools — wrap mc_collect/mc_plan + ESPN site-api + fact-checker |
| `crew/llm.py` | CrewAI `LLM` factory (Claude writers + GPT-4o scorers/critic + GPT-4o-mini helpers) |
| `crew/agents.py` | Agent personas; backstories pull verbatim from `mc_config` + `mc_generate` guardrails |
| `crew/planning_crew.py` | Step 0 (crew): wraps `xai_grok_search` tool |
| `crew/research_crew.py` | Steps 1–6 (crew, non-sports): drives the legacy collect→dedup→cluster→score→enrich pipeline |
| `crew/sports_research_crew.py` | Steps 1–6 (crew, NY Sports): Gatekeeper + Researcher with ESPN tools, Fact-Checker reserved for critic stage |
| `crew/picks_crew.py` | Step 7 (crew): thin pass-through to `mc_collect.process_picks` |
| `crew/writing_crew.py` | Step 8 (crew): three Claude writers in parallel, HTML template wrapping |
| `crew/critic_crew.py` | Step 8b (crew): GPT-4o scorer + Claude patcher; max_iter=1; NY Sports never auto-patched |
| `crew/context.py` | Re-exports legacy prompt helpers so writers/critic share `_build_articles_context`, `_filter_trending_to_articles`, etc. |

## Key Constraints — Do Not Violate

- **NY Sports is NEVER auto-patched by the critic** (`NEVER_PATCH_NORMALIZED = {"ny sports"}` in both `mc_critic.py` and `crew/critic_crew.py`). Patching sports sections causes GPT to hallucinate scores, players, and trades.
- **Critic threshold is 7/10** (`_WEAK_THRESHOLD = 7`). Sections scoring below 7 get regenerated once — no retry loop.
- **Hallucination guards are everywhere**: every GPT prompt and every agent backstory explicitly tells the model to only discuss articles in the input. Do not weaken these.
- **Sports sources are allowlisted**: `SPORTS_TRUSTED_SOURCES` in `mc_config.py`. Articles from untrusted publishers (e.g. AOL) are dropped before generation.
- **7-day dedup**: `briefing_history.json` tracks seen URLs/titles. Don't delete or corrupt this file.
- **feed.xml must be uploaded with `Cache-Control: no-cache`**: `s3_upload_text` in `mc_utils.py` accepts a `cache_control` kwarg; `mc_deliver.py` passes `cache_control="no-cache"` for `feed.xml`. Without this, CloudFront caches the feed for 24 hours and Apple Podcasts / Spotify receive stale content. Do not remove this header.
- **Sports Researcher has `max_iter=15` and `max_execution_time=180`**: prevents the ESPN-tool-using agent from looping on rate-limit errors and burning >10 minutes per run. Tune if you add more ESPN tools.
- **`_safe_request` defaults to a browser UA**: ESPN, Google News, and a few other hosts 403 the bare `python-requests` UA. Callers passing their own `headers` kwarg override the default. Don't remove this default.

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

## AWS Deployment & Docker Image Updates

The pipeline runs on **AWS ECS Fargate** (ephemeral task, ~20 min/day). The Docker image is in **ECR** (`602039469166.dkr.ecr.us-east-1.amazonaws.com/mikecast`). The daily schedule is managed by **EventBridge Scheduler** (`mikecast-daily`, 6:30 AM ET).

### How code changes reach production

Every `git push` to `main` triggers the GitHub Actions workflow (`.github/workflows/deploy.yml`), which:
1. Builds a new Docker image from the current `main` branch
2. Tags it with the commit SHA + `latest` and pushes to ECR
3. Registers a new ECS task definition revision pointing to that image
4. Updates the EventBridge Scheduler to use the new task definition ARN

The next scheduled run (6:30 AM ET) will use the updated image automatically. No manual ECS steps needed.

### Required GitHub Actions secrets

These must be set at https://github.com/schwim23/mikecast/settings/secrets/actions:
- `AWS_ACCESS_KEY_ID` — IAM user with ECR push + ECS task registration + EventBridge update permissions
- `AWS_SECRET_ACCESS_KEY` — corresponding secret key

### To deploy a change manually (without waiting for cron)

```bash
# Trigger an immediate ECS run with the latest task definition (uses --legacy default)
aws ecs run-task \
  --cluster mikecast \
  --task-definition mikecast \
  --launch-type FARGATE \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-086fe88cca1a9de84],securityGroups=[sg-0b075c1eea308976b],assignPublicIp=ENABLED}' \
  --region us-east-1

# Same task, but force the CrewAI path. Required after a fresh deploy to shake out
# Fargate-only issues (network egress to anthropic.com, IAM, etc.) before flipping
# the default.
aws ecs run-task \
  --cluster mikecast \
  --task-definition mikecast \
  --launch-type FARGATE \
  --overrides '{"containerOverrides":[{"name":"mikecast","command":["python","mikecast_briefing.py","--crew","--force"]}]}' \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-086fe88cca1a9de84],securityGroups=[sg-0b075c1eea308976b],assignPublicIp=ENABLED}' \
  --region us-east-1
```

### CrewAI rollout state

- Default path is `--legacy`. The cron-scheduled 6:30 AM ET run uses legacy.
- `--crew` is invoked manually for validation. Outputs go to the same `data/YYYY-MM-DD.json` shape.
- To flip the production default to `--crew`, change the default branch in `main()` in `mikecast_briefing.py` and merge to `main`. The next scheduled run will use crew automatically.
- `ANTHROPIC_API_KEY` must be in SSM Parameter Store (`/mikecast/ANTHROPIC_API_KEY`, SecureString) and referenced from the ECS task definition's `containerDefinitions[0].secrets`.

### To check the status of the ECS task

```bash
# List recent task runs
aws ecs list-tasks --cluster mikecast --region us-east-1

# View CloudWatch logs
aws logs tail /ecs/mikecast --follow --region us-east-1
```

### Key AWS resources

| Resource | Name/ID |
|---|---|
| ECR repository | `mikecast` (account: 602039469166) |
| ECS cluster | `mikecast` |
| ECS task family | `mikecast` |
| EventBridge Scheduler | `mikecast-daily` |
| S3 bucket | `mikecast-io-data` |
| CloudFront distribution | serves `mikecast.io` |
| Scheduler IAM role | `mikecast-scheduler-role` |
| VPC subnet | `subnet-086fe88cca1a9de84` |
| Security group | `sg-0b075c1eea308976b` |
