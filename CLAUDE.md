# MikeCast — Claude Context

## What This Is

MikeCast is an automated daily news briefing system. Every morning at 6:30 AM ET (EventBridge Scheduler → ECS Fargate), it:
1. Collects news from NYT, RSS feeds, Reddit, Hacker News, Google News (incl. Reuters/AP via `site:` searches)
2. Deduplicates, drops stale/undated/resurfaced articles, clusters, scores, merges same-event duplicates, drops repeat events, selects and enriches the top articles
3. Generates an HTML briefing + 2 podcast scripts (Claude writers on the default crew path)
4. Runs a quality critic pass (regenerates weak sections), a log-only grounding check, and a stale-title fix
5. Generates audio via ElevenLabs (3-voice) or OpenAI TTS (fallback)
6. Saves JSON/manifest/RSS to S3 (served by CloudFront at mikecast.io), sends the Gmail email + Resend newsletter
7. Auto-posts to X + Instagram (Step 11)

## How to Run

```bash
# CrewAI pipeline (default after cutover)
source ~/.profile && .venv/bin/python3 mikecast_briefing.py

# Legacy procedural pipeline — reachable for fast rollback / debugging
source ~/.profile && .venv/bin/python3 mikecast_briefing.py --legacy

# Force regenerate today's briefing
source ~/.profile && .venv/bin/python3 mikecast_briefing.py --force
source ~/.profile && .venv/bin/python3 mikecast_briefing.py --crew --force

# Check the log
tail -f mikecast.log

# Local dashboard
.venv/bin/python3 server.py  # → http://localhost:8080
```

Other flags: `--resend` (with `--force`, bypass the first-run-of-day send gate), `--dump-eval-fixture` (capture eval fixtures; the prod image's default CMD), `--eval-only` (capture fixtures without publishing/sending anything).

`run_mikecast.sh` (local cron wrapper that commits `data/` and pushes to GitHub) is **legacy and disabled** — its crontab entry is commented out. Production is ECS only.

`--crew` and `--legacy` are mutually exclusive. Both paths produce the same on-disk output shape (data/`YYYY-MM-DD.json`, audio file, RSS feed) and use the same Steps 9–10 for audio + delivery. Default is `--crew` after the cutover; `--legacy` remains available as a fast rollback.

## Environment Variables (locally in `~/.profile`, except `ANTHROPIC_API_KEY` — see below; in prod, secrets come from SSM via the ECS task def)

<!-- BEGIN GENERATED: env (scripts/gen_docs.py — do not edit by hand) -->
| Variable | Required | Default | Purpose |
|---|---|---|---|
| `NYTAPIKEY` | Yes | — | NYT Top Stories + Article Search |
| `OPENAI_API_KEY` | Yes | — | OpenAI scorer/helper models (scoring, clustering, enrichment, merge/repeat filters, grounding) + OpenAI TTS fallback |
| `GMAIL_APP_PASSWORD` | Yes | — | Gmail SMTP (16-digit app password) |
| `GMAIL_FROM` | Yes | (set in code) | Sender address |
| `GMAIL_TO` | Yes | (set in code) | Recipient address |
| `RESEND_API_KEY` | No | — | Resend key — enables the daily email newsletter broadcast |
| `RESEND_AUDIENCE_ID` | No | — | Resend "MikeCast Daily" audience id (required with the key) |
| `RESEND_FROM` | No | `MikeCast <mike@mikecast.io>` | Newsletter sender |
| `RESEND_REPLY_TO` | No | (set in code) | Newsletter reply-to |
| `ELEVENLABS_API_KEY` | No | — | 3-voice audio (Mike/Elizabeth/Jesse) |
| `ELEVENLABS_VOICE_MIKE` | No | — | ElevenLabs voice ID |
| `ELEVENLABS_VOICE_ELIZABETH` | No | — | ElevenLabs voice ID |
| `ELEVENLABS_VOICE_JESSE` | No | — | ElevenLabs voice ID |
| `XAI_API_KEY` | No | — | Grok adaptive search planning (Step 0) |
| `X_API_KEY` | No | — | X auto-post (OAuth 1.0a, Step 11) |
| `X_API_SECRET` | No | — | X auto-post |
| `X_ACCESS_TOKEN` | No | — | X auto-post |
| `X_ACCESS_TOKEN_SECRET` | No | — | X auto-post |
| `META_ACCESS_TOKEN` | No | — | Instagram auto-post (Graph API, Step 11) |
| `IG_USER_ID` | No | — | Instagram Business account id |
| `DD_API_KEY` | No | — | Datadog OTLP tracing (`mc_tracing.py`) + run metrics (`mc_metrics.py`); both no-op when unset |
| `DD_SITE` | No | `datadoghq.com` | Datadog site |
| `SOCIAL_MEDIA_KIND` | No | `card` | `card` or `reel` (the prod task def sets `reel`) |
| `CLOUDFRONT_DIST_ID` | No | (set in code) | CloudFront distribution invalidated by `mc_edit.py` after a republish |
| `ANTHROPIC_API_KEY` | Crew only | — | Claude for the writing crew, patcher and social copywriter. Locally lives in git-ignored `mikecast/.env` (loaded by `mc_config.py` via python-dotenv), NOT `~/.profile` — a globally exported key forces Claude Code onto API-key auth. Prod gets it from SSM via the task def. |
| `CLAUDE_WRITER_MODEL` | No | `anthropic/claude-sonnet-5` | LiteLLM model for writers, section patcher, social copywriter |
| `OPENAI_SCORER_MODEL` | No | `openai/gpt-5.6-terra` | LiteLLM model for article scorer, duplicate-merge/repeat-event filters, grounding check |
| `OPENAI_CRITIC_MODEL` | No | `anthropic/claude-sonnet-5` | LiteLLM model for the section-quality critic scorer (despite the name, defaults to Claude) |
| `OPENAI_HELPER_MODEL` | No | `openai/gpt-4o-mini` | LiteLLM model for helpers: clustering, enrichment, picks, NY Sports fact-checker |
| `S3_BUCKET` | Prod | — | Enables S3 output mode; set in the task def (`mikecast-io-data`), unset locally |
<!-- END GENERATED: env -->

## Module Map

| File | Owns |
|---|---|
| `mikecast_briefing.py` | Entry point — orchestrates all 11 steps; dispatches to legacy or crew path |
| `mc_config.py` | All constants, env vars, categories, RSS feeds, scoring prompts, CrewAI model strings |
| `mc_plan.py` | Step 0 (legacy): xAI Grok generates dynamic search queries |
| `mc_collect.py` | Steps 1–6 (legacy): collect, dedupe, cluster, score, merge duplicate stories, filter repeat events, select, enrich |
| `mc_generate.py` | Step 8 (legacy): HTML briefing + single-voice + 3-voice scripts |
| `mc_critic.py` | Step 8b (legacy): quality critic; patches weak sections |
| `mc_facts.py` | Live (Wikidata) current officeholders injected into every writer prompt + `fix_stale_titles` post-pass (both paths) |
| `mc_audio.py` | Step 9: ElevenLabs 3-voice + OpenAI TTS fallback; `_concat_mp3_segments` stitches segments into one clean MP3 via ffmpeg (shared) |
| `mc_deliver.py` | Step 10: JSON, manifest.json, feed.xml, Gmail email + Resend newsletter broadcast (shared) |
| `mc_dist_state.py` | Per-date send state (`data/dist/YYYY-MM-DD.json`) — first-run-of-day gating for email/newsletter/X/IG |
| `mc_social.py` | Step 11: X + Instagram posting (card or reel), weighted tweet length, standalone CLI |
| `mc_video.py` | 9:16 captioned reel renderer shared by `mc_social` and `mc_ad` |
| `mc_edit.py` | Edit/republish a past episode; delete/repost its social posts |
| `mc_metrics.py` / `mc_tracing.py` | Datadog run metrics (one batched POST) + OTLP tracing spans; no-ops without `DD_API_KEY` |
| `mc_ad.py` / `mc_youtube.py` | Standalone promo-video CLI / YouTube upload (not part of the daily run) |
| `lambda/newsletter_signup/` | Lambda for double-opt-in email signup (POST /signup, GET /confirm), reached via the API Gateway custom domain `https://api.mikecast.io` (the endpoint in `signup.js`) — bridges the static form and Resend. Public Lambda Function URLs are blocked in this AWS account |
| `mc_utils.py` | Shared helpers: HTTP (browser UA default), JSON I/O, S3 I/O, text similarity, URL fingerprinting, CloudFront invalidation |
| `mikes_picks_ingest.py` | CLI to add URLs/PDFs/text to the picks queue |
| `server.py` | Flask server for local dashboard |
| `crew/tools.py` | Pydantic-typed CrewAI tools — wrap mc_collect/mc_plan + ESPN site-api + fact-checker |
| `crew/llm.py` | CrewAI `LLM` factory (writer / scorer / critic / helper, models from the env vars above) |
| `crew/model_compat.py` | Cross-model rules: which models accept custom `temperature`, API key by provider |
| `crew/agents.py` | Agent personas; backstories pull verbatim from `mc_config` + `mc_generate` guardrails |
| `crew/planning_crew.py` | Step 0 (crew): wraps `xai_grok_search` tool |
| `crew/research_crew.py` | Steps 1–6 (crew, non-sports): drives the legacy collect→dedup→cluster→score→enrich pipeline |
| `crew/sports_research_crew.py` | Steps 1–6 (crew, NY Sports): Gatekeeper + Researcher with ESPN tools, Fact-Checker reserved for critic stage |
| `crew/picks_crew.py` | Step 7 (crew): thin pass-through to `mc_collect.process_picks` |
| `crew/writing_crew.py` | Step 8 (crew): three Claude writers in parallel, HTML template wrapping |
| `crew/critic_crew.py` | Step 8b (crew): critic scorer (`OPENAI_CRITIC_MODEL`, plain LiteLLM call) + Claude patcher; NY Sports never auto-patched |
| `crew/grounding_check.py` | Step 8c (crew): log-only fact-check of every non-sports HTML + 3-voice-script sentence against the articles + officeholder facts |
| `crew/context.py` | Re-exports legacy prompt helpers so writers/critic share `_build_articles_context`, `_filter_trending_to_articles`, etc. |
| `crew/distribution_crew.py` | Step 11 (crew): Claude social copywriter (X post + IG caption) |
| `crew/tests/` | pytest suite — CI gate for every deploy |
| `eval/` | Model-eval harness (fixture dump, replay, scoring, blind review, dashboard, grounding backfill) — see `MODEL_EVAL_PLAN.md` |

## Key Numbers

Generated from source — edit the code, then run `python scripts/gen_docs.py`. Generation fails if two files that must agree (e.g. both critics' thresholds, writer prompts vs. `eval/score.py` word targets) disagree.

<!-- BEGIN GENERATED: numbers (scripts/gen_docs.py — do not edit by hand) -->
| Setting | Value | Source of truth |
|---|---|---|
| Daily schedule (EventBridge) | `cron(30 6 * * ? *)` America/New_York | `.github/workflows/deploy.yml` (re-applied every deploy) |
| Article budget | 30 (each category ≥3) | `TOP_ARTICLES_TOTAL` in `mc_config.py` |
| Articles enriched with full text | top 15 | `run_research(enrich_top_n=)` / legacy `enrich_top_stories(top_n=)` |
| Stale-article cutoff | 3 days | `filter_stale_articles(max_age_days=)` calls (crew + legacy) |
| Max future-dated skew | 24h | `MAX_FUTURE_SKEW` in `mc_collect.py` |
| Repeat-event lookback | 3 episodes | `REPEAT_LOOKBACK_DAYS` in `mc_collect.py` |
| Podcast script target | 1150–1250 words (~8 min) | `_PODCAST_WORD_MIN/MAX` in `eval/score.py`; must match `crew/writing_crew.py` + `crew/agents.py` |
| Expansion pass | below 1100 words; kept if ≤1350 | `_EXPAND_BELOW_WORDS` / `_EXPAND_ACCEPT_MAX_WORDS` in `crew/writing_crew.py` |
| Critic patch threshold | below 7/10 | `_WEAK_THRESHOLD` in `mc_critic.py` + `crew/critic_crew.py` |
<!-- END GENERATED: numbers -->

## Key Constraints — Do Not Violate

- **NY Sports is NEVER auto-patched by the critic** (`NEVER_PATCH_NORMALIZED = {"ny sports"}` in both `mc_critic.py` and `crew/critic_crew.py`). Patching sports sections causes GPT to hallucinate scores, players, and trades.
- **Critic threshold is 7/10** (`_WEAK_THRESHOLD = 7`). Sections scoring below 7 get regenerated once — no retry loop.
- **Hallucination guards are everywhere**: every GPT prompt and every agent backstory explicitly tells the model to only discuss articles in the input. Do not weaken these.
- **Sports sources are allowlisted**: `SPORTS_TRUSTED_SOURCES` in `mc_config.py`. Articles from untrusted publishers (e.g. AOL) are dropped before generation.
- **7-day dedup**: `briefing_history.json` tracks seen URLs/titles. Don't delete or corrupt this file.
- **feed.xml must be uploaded with `Cache-Control: no-cache`**: `s3_upload_text` in `mc_utils.py` accepts a `cache_control` kwarg; `mc_deliver.py` passes `cache_control="no-cache"` for `feed.xml`. Without this, CloudFront caches the feed for 24 hours and Apple Podcasts / Spotify receive stale content. Do not remove this header.
- **Sports Researcher has `max_iter=15` and `max_execution_time=180`**: prevents the ESPN-tool-using agent from looping on rate-limit errors and burning >10 minutes per run. Tune if you add more ESPN tools.
- **`_safe_request` defaults to a browser UA**: ESPN, Google News, and a few other hosts 403 the bare `python-requests` UA. Callers passing their own `headers` kwarg override the default. Don't remove this default.
- **NY Sports Fact-Checker runs every critic pass as read-only observability**: `fact_check_ny_sports()` in `crew/critic_crew.py` extracts substantive sentences from the NY Sports HTML section and the `[JESSE]` block of the 3-voice script, then calls `validate_claim_against_articles` on each. Unsupported claims are logged at WARNING; the section is still NEVER auto-patched. Don't turn this off — it's the only signal that catches Claude drift on sports.
- **Podcast scripts target ~8 minutes (1150–1250 words)**: raised from 900–1000 on 2026-11-04 when the Payments & Commerce segment (~220w, voiced by MIKE in the 3-host script) was added without cutting other segments. Per-segment word budgets are in `crew/writing_crew.py` `_single_voice_task` and `_conversational_task`; the agent goals in `crew/agents.py` and `_PODCAST_WORD_MIN/MAX` in `eval/score.py` must match. If you change the target, update all of them plus the run_summary expectations.
- **Short podcast scripts get one expansion pass**: writers undershoot the floor (2026-11-06: 1004/1105 words → 5.5 min; still ~1000 with the reworded prompt). `run_writing` calls `_expand_if_short` when a script is under 1100 spoken words: same writer + same task (articles included) + the draft, NY SPORTS left untouched. The result is kept only if longer, ≤1350 words and (3-host) all three speakers present. Log: `script expanded N → M words`. Don't put "brevity" language back in the length prompts.
- **Audio segments are stitched by re-encoding, not raw byte concatenation, and shipped as CBR**: `mc_audio._concat_mp3_segments` writes each TTS MP3 to a temp file and re-encodes one stream with ffmpeg's concat demuxer; both it and `_normalize_loudness` encode **constant-bitrate 128 kbps @ 44.1 kHz** (`-b:a 128k -ar 44100`, NOT `-q:a`). Raw byte-joining (the old approach) left embedded mid-stream headers that made Apple Podcasts/Spotify replay the tail. Even a metadata-perfect VBR file (correct single `Xing` header + accurate `itunes:duration`) still made **Spotify** replay the last ~20s: Spotify ignores both and estimates length from size÷bitrate, overshoots on VBR, and loops the tail (Apple decodes real frames and is fine). CBR makes size÷bitrate equal the true duration exactly, so every player agrees. Don't revert to byte concatenation (raw-concat is the ffmpeg-missing fallback only) and don't switch the encode back to VBR.
- **`itunes:duration` is persisted in the episode JSON, not re-derived at feed time**: `save_daily_data` reads the audio duration while the file is still on local disk and stores it as `audio_duration_secs`; `generate_rss_feed` reads that field and only falls back to a local-file scan (`_audio_duration_secs`) for pre-existing episodes that lack it. On ephemeral Fargate, only the current run's audio is local, so a feed rebuilt from local files alone stamps every *past* episode `itunes:duration=0`. Spotify then estimates length from the VBR file's size/bitrate, overshoots, and replays the tail to fill the gap (Apple decodes real frames and is fine). Don't go back to reading duration only from the local file.
- **The critic matches section headers in escaped AND unescaped form**: the page renders `AI &amp; TECH` but the scorer echoes `AI & TECH`; both critics build `(?:cat|html.escape(cat))` into the patch regex. Before 2026-11-04 every `&` section (AI & Tech, Business & Markets) logged `could not locate HTML section` and was never patched. Pinned by `crew/tests/test_payments_section.py`.
- **Critic patches must keep source links**: both patchers get each article's Source + URL (`mc_critic.patch_article_lines`) and `PATCH_LINK_RULE`; `enforce_patch_links` unwraps any href not in the section's articles and returns None (keep the original section) if no valid link is left. Before 2026-11-06 patched sections shipped to the email with zero links. Pinned by `crew/tests/test_patch_links_and_length.py`.
- **The critic resolves category names case-insensitively and never patches a section to empty**: the GPT scorer echoes ALL-CAPS section headers (`COMPANIES`) while `top_articles`/`categorised` are title-cased (`Companies`). Both `crew/critic_crew.py` and `mc_critic.py` build a lowercased lookup and skip the patch when no articles resolve — otherwise the patcher is handed 0 articles and emits a "No Companies News Available" placeholder over a section that had real stories. Don't go back to a plain `.get(cat)`.
- **Docs ship in the same commit as the change.** Any change to behavior, config, models, env vars, schedule, CLI flags, pipeline steps, or files (added/moved/removed) must update README.md and CLAUDE.md in that commit; a new guard or incident gets a bullet in this Key Constraints section, in the existing style (what, why/incident date, how to apply, pinning test). Then run `python scripts/gen_docs.py` (rewrites the `<!-- BEGIN GENERATED -->` blocks — never hand-edit inside them), `python scripts/make_diagrams.py` (README diagrams `docs/diagrams/*.svg`; edit the layout in the script, never the SVG — if a pipeline step, AWS resource or guard changes, update the layout too) and `pytest crew/tests/`. Two tests back this up and block the deploy: `test_docs_in_sync.py` (generated blocks and diagrams current; every env var `mc_config.py` reads has an `ENV_DOCS` entry; files that must agree do) and `test_docs_drift.py` (every backticked path/identifier in both docs exists in tracked code; every `mc_*.py`/`crew/*.py` is in the Module Map and README). Docs-only pushes don't redeploy (`paths-ignore: **/*.md`); use the workflow's manual "Run workflow" button to redeploy without a code change. Design: `DOCS_SYNC_PLAN.md`.
- **GitHub Actions gates `deploy` on `test`**: `.github/workflows/deploy.yml` runs `pytest crew/tests/` in a `test` job before the `deploy` job (Docker build → ECR push → new ECS task-def revision → EventBridge Scheduler update) is allowed to run. A push to `main` that fails the test suite never reaches ECR/prod. Don't remove the `needs: test` dependency on the `deploy` job, and don't merge/push code you haven't run `pytest crew/tests/` against locally first.
- **Env-flag-gated code paths need a test that turns the flag on** — normal dev/test runs won't exercise them otherwise. Incident (2026-09-22, commit 0052e69): commit 097c30b added `dump_fixture()` calls in `mc_collect.py` referencing `TODAY` without importing it. `dump_fixture()` is a no-op unless `MIKECAST_DUMP_EVAL_FIXTURE=1` — but that's exactly what the prod Docker image's default `CMD ["--dump-eval-fixture"]` sets, so it crashed the real 6:30 AM run mid-scoring (no briefing generated, published, or emailed that day) while every local/dev run stayed silent. `crew/tests/test_eval_fixture_dump.py` now sets the flag and forces both `dump_fixture()` call sites (`score_and_rank_articles`, `enrich_top_stories`) to execute in CI; `crew/tests/test_smoke_imports.py` additionally import-checks every `mc_*.py`/`crew/*.py` module as a cheap catch-all for the separate class of bug (bad imports, module-level typos) an import-only check *can* catch. When you add a new `--flag`-gated or env-var-gated branch (eval dumps, `--legacy`, `--dump-eval-fixture`, future ones), add a test that sets the flag and executes that branch — don't rely on it only ever running for the first time in prod.
- **Writers' training data is stale on who holds office** (2026-10-06: podcast said "former President Trump"). `mc_facts.prompt_block()` is appended to every writer/patcher/copywriter prompt (crew + legacy), and `fix_stale_titles_in_outputs` rewrites stale titles in the HTML + both scripts after the critic in `mikecast_briefing.py` (logged as `Stale title in ...`). Officeholders come from a live Wikidata SPARQL lookup each run (filtered to real humans, non-deprecated, exactly one current holder), falling back to the last good result in `s3://mikecast-io-data/facts/officeholders.json`, then the hardcoded `FALLBACK`. Grep logs for `Officeholder facts:` — it warns on fallback use, Wikidata/FALLBACK disagreement, and a `FALLBACK_REVIEWED` older than 90 days. `crew/tests/conftest.py` sets `MIKECAST_FACTS_OFFLINE=1` so tests never hit the network. To track a new office, add an `Office` + `FALLBACK` entry.
- **Resurfaced old news has three guards** (incident 2026-11-05: ABC's April 20 "Tim Cook is stepping down, John Ternus set to replace him" led the Companies section two months after Ternus became CEO). (1) `search_news_web` reads `<pubDate>` (the "xml" parser is case-sensitive — the old `find("pubdate")` never matched, so every Google News article was undated and `filter_stale_articles` kept them all) and uses Google's per-article link as `url` (the publisher homepage, now `source_url`, made every story from one outlet share a URL fingerprint → bogus `[Updated]` re-includes and links to abcnews.com). (2) `mc_facts` now also lists the CEOs of the tracked companies (Wikidata P169, `COMPANY_FALLBACK`; prompt-only — `fix_stale_titles` never rewrites company titles) plus an `OLD_NEWS_RULE`, and the article scorer gets the same facts. (3) `mc_facts.filter_resurfaced_leadership_news` runs after the stale filter in both pipelines and drops articles naming a known predecessor AND successor with present/future transition wording when the handover is >14 days old (logs `Dropping resurfaced old news`). The prompt rule alone was NOT enough — on replay 2 of 3 writers still ran the story. Keep all three.
- **Undated articles are DROPPED, and same-event duplicates are merged across categories** (2026-11-05, after the payments rollout). `filter_stale_articles` now drops articles with a missing or unparseable `published` date (it used to keep them, which is how the undated Google News Tim Cook story got in); drops log as `dropped N undated/unparseable articles by source: ...` — if a source shows up there, its parser lost the date, fix the parser rather than re-allowing undated. `mc_collect.merge_duplicate_stories` runs after scoring and before `select_top_articles` in both pipelines: one call on `OPENAI_SCORER_MODEL` (override `MIKECAST_MERGE_MODEL`; gpt-4o-mini missed pairs) over each category's selection window + 4, with title/source/description, returns same-event groups; the highest score in each group is kept, so the next-best story backfills the slot. NY Sports is excluded. Fails open. Logs `Duplicate-story merge: dropped ...`. Title-only `cluster_articles` missed "Block reaches $964M DOJ pact" / "Block faces $765m DoJ settlement" and two Stripe stablecoin write-ups.
- **Publication time is not event time** (2026-11-08, a Sunday: TradingView's "Nasdaq ends -5.5%", posted that morning, ran as the market news; the crash was Thursday 11/05, covered 11/06, and Friday's rebound went unmentioned). `mc_collect.filter_repeat_events` runs after `merge_duplicate_stories` in both pipelines: three calls on the merge model vote (a story is dropped on 2 of 3, because single calls flip on borderline stories) comparing the selection candidates (+8 per category) with the non-sports articles of the previous `REPEAT_LOOKBACK_DAYS` (3) episodes and drops re-reports with nothing new; a repeat must name a real prior article, flagging over half the candidates keeps everything, it fails open, and `MIKECAST_REPEAT_FILTER=0` disables it. Logs `Repeat-event filter: dropped ...`. Writers and scorers also see each article's `Published` time plus `mc_facts.market_calendar_line()` (computed NYSE calendar; unscheduled closures aren't known) and `DATE_RULE`. `filter_stale_articles` also drops dates more than `MAX_FUTURE_SKEW` (24h) ahead. Tightening `max_age_days` would not have caught this: the article looked 2.5 hours old. `DATE_RULE` alone is NOT enough: on 11/08 replays with the recap left in, 5 of 8 single-host scripts still wrote "Friday was rough" (0 of 6 three-host scripts named a day, but none called it a recent session either), so the filter is the guard that matters.
- **ElevenLabs hallucinates on very short lines** (2026-11-05: "[MIKE] End of an era." → ~7s of babble in the published MP3). `generate_elevenlabs_audio` sends `previous_text`/`next_text` (neighbouring script text) with every request and length-checks each clip (`_tts_duration_suspicious`: >0.12 s/char + 2s): retry once, then DROP it if the text is ≤80 chars, else keep with a WARNING. Live test: without context 1/3 calls hallucinated; with context 3/3 clean. The 3-host writers also get `_MIN_TURN_RULE` (every turn ≥10 words). Grep logs for `likely hallucinated audio` / `DROPPING`.
- **X tweet length uses X's real weighting** (`mc_social._weighted_len`): characters outside twitter-text's weight-1 ranges (emoji, "…", CJK, most symbols) count 2, URLs 23. Until 2026-11-05 it counted everything as 1; the 🎙️/📰/🎧 template plus a trailing "…" put long-copy days at 281–283, and X answers an over-length tweet with a bare **403 "You are not permitted to perform this action"** (not a 400) — 4 of 8 days 10/28–11/05 failed that way. If X 403s again, check the weighted length before suspecting credentials or app permissions.
- **Reuters/AP come from Google News `site:` searches** (`WIRE_GNEWS_QUERIES`, `fetch_wire_via_google_news`). Their direct feeds (feeds.reuters.com / feeds.apnews.com) stopped resolving and logged 0 articles every run since at least 2026-10-01; reuters.com/apnews.com RSS and sitemaps 401/403 bots. Google labels AP as "AP News" (tier 1 in `SOURCE_TIERS`). Don't use `site:apnews.com technology` — it returns state election-results pages.
- **Grounding check is LOG-ONLY** (`crew/grounding_check.py`, Step 8c, crew path): every non-sports sentence of the HTML and the 3-voice script goes through `validate_claim_against_articles` against ALL of today's articles (+ ESPN blocks), with the officeholder facts as `background`, on `OPENAI_SCORER_MODEL` (override: `MIKECAST_GROUNDING_MODEL`) — gpt-4o-mini misread the articles on ~half its flags. Verdicts: `'n/a'` = not a factual claim (banter, topic teasers); `'analysis'` (opt-in via `allow_analysis`, only this caller) = the writer's own interpretation of supported facts, counted not flagged. Unsupported sentences log as `[Grounding] UNSUPPORTED`; nothing is rewritten. 9/30–10/09 backfill: ~22% of factual sentences unsupported, mostly invented investor reaction and causal links — hence `_NO_INVENTED_REACTION_RULE` in the writer prompts. Measure with `eval/grounding_backfill.py --last 7` before ever making it enforce (backfill lacks game times unless that day's writer fixture exists locally, so sports times in What to Watch can false-flag). Disable with `MIKECAST_GROUNDING_CHECK=0`.
- **Synthetic eval fixtures live in `eval/fixtures/synthetic/<name>/` (committed)** and are selected with `--date <name>` in `eval/run_eval.py`. `bare-names` names every officeholder without a title; `eval/score.py`'s `stale_titles` check (hard gate) catches any title the writer invents. Run it on every writer-model change.

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

- **New category**: add to `CATEGORIES`, `NYT_SEARCH_QUERIES`, `CATEGORY_SCORER_PROMPTS` (and `NYT_SECTION_TO_CATEGORY` if an NYT section maps to it) in `mc_config.py`; `_CATEGORIES` + the JSON schema in `mc_plan.py`; `_SECTION_HEADERS` in `crew/writing_crew.py` and `section_headers` in `mc_generate.py` (otherwise the header is merged into the previous section); the segment in all three writer tasks + `crew/agents.py` goals; `_CONDITIONAL_H2_SECTIONS` in `eval/score.py`. `crew/tests/test_payments_section.py::TestCategoryRegistration` fails if a registry is missed.
- **Payments & Commerce** (5th category, 2026-11-04): Stripe/Adyen company news, agentic commerce, payment methods/rails, networks. Sources: Google News queries + `PAYMENTS_RSS_FEEDS` (Payments Dive, Finextra, PYMNTS, Stripe blog; Adyen has no RSS). The Companies and Business scorer prompts push payments-company stories here so they aren't written twice. Synthetic fixture: `eval/fixtures/synthetic/payments/`.
- **Article budget**: `TOP_ARTICLES_TOTAL` in `mc_config.py` (30; each category gets ≥3) — used by both pipelines and `eval/score.py`.
- **New RSS feed**: add to the relevant `*_RSS_FEEDS` list in `mc_config.py`
- **Change scoring behavior**: edit `CATEGORY_SCORER_PROMPTS` in `mc_config.py`
- **Add a Mike's Pick**: use `mikes_picks_ingest.py` (see `/mc-picks` skill)

## AWS Deployment & Docker Image Updates

The pipeline runs on **AWS ECS Fargate** (ephemeral task, ~20 min/day). The Docker image is in **ECR** (`<ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/mikecast`). The daily schedule is managed by **EventBridge Scheduler** (`mikecast-daily`, 6:30 AM ET).

### How code changes reach production

Every `git push` to `main` triggers the GitHub Actions workflow (`.github/workflows/deploy.yml`), which:
1. Builds a new Docker image from the current `main` branch
2. Tags it with the commit SHA + `latest` and pushes to ECR
3. Registers a new ECS task definition revision pointing to that image
4. Updates the EventBridge Scheduler to use the new task definition ARN

The `deploy` job only runs after the `test` job (`pytest crew/tests/`) passes, and a `deploy-ecs` concurrency group serializes deploys in push order. Pushes that change only `*.md` files skip the workflow; `workflow_dispatch` (Actions → "Deploy to ECS" → Run workflow, on `main`) redeploys manually. The next scheduled run (6:30 AM ET) will use the updated image automatically. No manual ECS steps needed.

### Required GitHub Actions secrets

These must be set at https://github.com/schwim23/mikecast/settings/secrets/actions:
- `AWS_ACCESS_KEY_ID` — IAM user with ECR push + ECS task registration + EventBridge update permissions
- `AWS_SECRET_ACCESS_KEY` — corresponding secret key

### To deploy a change manually (without waiting for cron)

```bash
# Trigger an immediate ECS run with the latest task definition (default CMD: crew path +
# --dump-eval-fixture). The "already exists" guard only checks LOCAL disk, which is empty
# on Fargate — so this REGENERATES and overwrites today's episode JSON/MP3/feed in S3.
aws ecs run-task \
  --cluster mikecast \
  --task-definition mikecast \
  --launch-type FARGATE \
  --network-configuration 'awsvpcConfiguration={subnets=[<SUBNET_ID>],securityGroups=[<SECURITY_GROUP_ID>],assignPublicIp=ENABLED}' \
  --region us-east-1

# Force-regenerate today's episode on the crew path (sends stay deduped per date
# unless you also pass --resend).
aws ecs run-task \
  --cluster mikecast \
  --task-definition mikecast \
  --launch-type FARGATE \
  --overrides '{"containerOverrides":[{"name":"mikecast","command":["--crew","--force"]}]}' \
  --network-configuration 'awsvpcConfiguration={subnets=[<SUBNET_ID>],securityGroups=[<SECURITY_GROUP_ID>],assignPublicIp=ENABLED}' \
  --region us-east-1
```

> **WARNING — `command` overrides are ARGUMENTS to `mikecast_briefing.py`, not a new command.**
> The image has `ENTRYPOINT ["python", "mikecast_briefing.py"]` and `CMD ["--dump-eval-fixture"]`, so a
> `command` override *replaces only the CMD*. `["python","-c","..."]` does NOT run your snippet — the
> full daily pipeline runs (audio, S3 uploads, manifest/feed rewrite, and any un-deduped sends).
> Happened 2026-09-18: a "one-off ESPN check" regenerated and overwrote that day's episode in S3.
> To run something other than the briefing, override the entrypoint too:
> `--overrides '{"containerOverrides":[{"name":"mikecast","entryPoint":["python","-c"],"command":["print(1)"]}]}'`
> (test with a harmless snippet first). Email/newsletter/X/IG are dedup-guarded per date; the episode
> JSON, MP3, `manifest.json` and `feed.xml` are NOT — S3 versioning is on if you need to roll back.

### CrewAI rollout state

- **Default path is `--crew`** after cutover. The 6:30 AM ET scheduled run uses the CrewAI pipeline.
- `--legacy` is the rollback flag. Trigger a one-shot legacy run with `aws ecs run-task ... --overrides '{"containerOverrides":[{"name":"mikecast","command":["--legacy","--force"]}]}'` if today's crew run misbehaves.
- To revert the cutover entirely, change the default branch in `main()` in `mikecast_briefing.py` from `use_crew = True` back to `use_legacy = True` and merge. The next GH Actions deploy returns the scheduler to legacy automatically.
- `ANTHROPIC_API_KEY` must be in SSM Parameter Store (`/mikecast/ANTHROPIC_API_KEY`, SecureString) and referenced from the ECS task definition's `containerDefinitions[0].secrets`. The task def's `secrets` currently carry all API keys/creds (NYT, OpenAI, Gmail, ElevenLabs, xAI, Anthropic, Resend, X, Meta/IG, Datadog); `environment` carries `S3_BUCKET`, `SOCIAL_MEDIA_KIND=reel`, `RESEND_FROM`, `RESEND_REPLY_TO`. CI copies the current task def forward and only swaps the image, so env/secret changes are made on the task def directly.

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
| ECR repository | `mikecast` (account: <ACCOUNT_ID>) |
| ECS cluster | `mikecast` |
| ECS task family | `mikecast` |
| EventBridge Scheduler | `mikecast-daily` |
| S3 bucket | `mikecast-io-data` |
| CloudFront distribution | serves `mikecast.io` (`CLOUDFRONT_DIST_ID`) |
| API Gateway custom domain | `api.mikecast.io` → newsletter signup Lambda |
| Scheduler IAM role | `mikecast-scheduler-role` |
| VPC subnet | `<SUBNET_ID>` |
| Security group | `<SECURITY_GROUP_ID>` |
