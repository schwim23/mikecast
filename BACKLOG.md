# MikeCast Backlog

Tracks planned work. Each item lists the branch to create when starting implementation.

---

## AWS Migration

Move pipeline off local machine. Serve site at mikecast.io via CloudFront + S3. Retire GitHub Pages and local cron. Full architecture in `aws_migration.txt`.

| # | Item | Branch | Status |
|---|------|--------|--------|
| AWS-1 | Create `Dockerfile`, build + test locally with `docker run` | `aws/step-1-dockerfile` | done |
| AWS-2 | Create AWS resources: S3 bucket `mikecast-io-data`, ECR repo, IAM roles | `aws/step-2-aws-resources` | done |
| AWS-3 | Adapt `mc_deliver.py` — S3 file writes only (Gmail SMTP unchanged; move `GMAIL_APP_PASSWORD`, `GMAIL_FROM`, `GMAIL_TO` to SSM) | `aws/step-3-deliver-s3` | done |
| AWS-4 | Adapt `mc_utils.py` — S3-backed history/picks persistence | `aws/step-4-utils-s3` | done |
| AWS-5 | Adapt `mikes_picks_ingest.py` — read/write picks from S3 | `aws/step-5-picks-s3` | done |
| AWS-6 | Seed S3 with existing `data/`, `briefing_history.json`, `mikes_picks.json`, `dashboard/` | `aws/step-6-seed-s3` | done |
| AWS-7 | Push Docker image to ECR; create ECS cluster + task definition | `aws/step-7-ecs` | done |
| AWS-8 | Create EventBridge Scheduler rule (6:30 AM ET, DST-aware); load secrets into SSM Parameter Store | `aws/step-8-scheduler-ssm` | done |
| AWS-9 | Test manual ECS task run; verify output in S3 + email received | `aws/step-9-test` | done |
| AWS-10 | Set up CloudFront + ACM certificate; create Route53 hosted zone for mikecast.io | `aws/step-10-cloudfront-r53` | done |
| AWS-11 | Update registrar NS records → Route53; create A alias → CloudFront; update `SITE_BASE_URL` in `mc_config.py` | `aws/step-11-dns-cutover` | done |
| AWS-12 | Monitor first automated run via CloudWatch Logs; decommission GitHub Pages + local cron | `aws/step-12-decommission` | done — local cron disabled; GitHub Pages retained as fallback |

**Estimated cost:** ~$1.00/month (Route53 $0.50, ECS Fargate ~$0.29, S3 ~$0.10, public IPv4 ~$0.05, ECR/CloudFront/EventBridge/VPC/CloudWatch all ~$0 at this scale — CloudWatch logs ~60MB/month well under 5GB free tier; no NAT Gateway)

---

## CrewAI Migration (Steps 0–8b)

Replace the procedural pipeline (xAI planner → mc_collect → mc_generate → mc_critic) with a CrewAI agent architecture: per-category Researchers, a dedicated NY Sports specialist crew with ESPN primary-source tools, three Claude writers in parallel, and a GPT-4o critic + Claude patcher. Full plan: `~/.claude/plans/review-the-code-in-immutable-cherny.md`.

| # | Item | Branch | Status |
|---|------|--------|--------|
| CREW-1 | Build crew/ package: tools.py (Pydantic), llm.py, agents.py, 6 crew files | `crewai-migration` | done |
| CREW-2 | Rewire mikecast_briefing.py with --crew / --legacy flags (default legacy) | `crewai-migration` | done |
| CREW-3 | Add ANTHROPIC_API_KEY + model env vars to mc_config + requirements (setuptools<81, crewai==0.86.0, pydantic>=2) | `crewai-migration` | done |
| CREW-4 | Fix `_safe_request` default UA — ESPN/Google News had been silently 403ing for the full repo history | `crewai-migration` | done |
| CREW-5 | Replace bot-blocked www.espn.com/espn/rss with site.api.espn.com JSON; fix score-as-dict shape; switch standings to site.web.api; filter league-wide injuries by team displayName | `crewai-migration` | done |
| CREW-6 | Harden mc_plan: JSON-mode response_format, raise max_tokens, log raw response on empty result | `crewai-migration` | done |
| CREW-7 | Cap Sports Researcher max_iter=15, max_execution_time=180 (prevents 12-min retry loops on rate-limit) | `crewai-migration` | done |
| CREW-8 | Local end-to-end verification: 4 full --crew --force runs, runtime 6–7m, hallucination guards held | `crewai-migration` | done |
| CREW-9 | Docker build verification — image content size 590MB (under plan's <600MB target) | `crewai-migration` | done |
| CREW-10 | Open PR; merge to main → GH Actions deploys; one-shot ECS run with --crew override; verify | `crewai-migration` | pending |
| CREW-11 | Cutover: flip mikecast_briefing.py default from --legacy to --crew after 7 consecutive clean ECS runs | `crew/cutover-default` | backlog |
| CREW-12 | Remove --legacy and delete mc_generate / mc_critic / mc_plan modules 14 days after cutover | `crew/legacy-cleanup` | backlog |

**Follow-ups (not blocking the migration):**

- `legacy ESPN RSS` (`www.espn.com/espn/rss/*`) still bot-blocked. We now use `site.api.espn.com/.../news` instead. Keep an eye on that endpoint — if ESPN ever shuts it down, fallback options are very limited.
- xAI Grok occasionally returns trending_stories: [] even when categories populate. The hardened mc_plan now logs the raw response in that case so we can diagnose.
- Sports Researcher returns `{}` honestly when no article implies a verifiable game/standings/injury. That's correct — but it means verified-facts coverage varies day-to-day. Future enhancement: have the agent query box scores for all 4 NY teams unconditionally so the writers always have ground-truth even when articles are weak.

---

## Sports Hallucination Fix

Maintain a persistent `ny_sports_state.json` with ground-truth per-team facts (last result, next game, notes). Update each run from trusted articles; inject into all three generation prompts as a "KNOWN FACTS" block the LLM must not contradict.

| # | Item | Branch | Status |
|---|------|--------|--------|
| SPO-1 | Add `mc_sports.py` with `extract_team_state()` + `ny_sports_state.json` schema; call after Step 6 in `mc_collect.py`; update `mc_config.py` with `NY_SPORTS_STATE_FILE` path constant | `sports/team-state-extraction` | backlog |
| SPO-2 | In `mc_generate.py`, load `ny_sports_state.json` and prepend "KNOWN FACTS" block to NY Sports section in all three generation functions | `sports/inject-team-state` | backlog |
| SPO-3 | Staleness guard: if state > 2 days old and no new sports articles, omit NY Sports section rather than risk fabrication; add note in HTML briefing | `sports/staleness-guard` | backlog |

**Schema for `ny_sports_state.json`:**
```json
{
  "last_updated": "YYYY-MM-DD",
  "teams": {
    "New York Yankees":  { "last_game": "...", "next_game": "...", "recent_notes": "..." },
    "New York Knicks":   { "last_game": "...", "next_game": "...", "recent_notes": "..." },
    "New York Giants":   { "last_game": "...", "next_game": "...", "recent_notes": "..." },
    "New Jersey Devils": { "last_game": "...", "next_game": "...", "recent_notes": "..." }
  }
}
```

**Files to modify:** `mc_collect.py`, `mc_generate.py`, `mc_config.py`, new `mc_sports.py`

---

## Done

| Item | Description |
|------|-------------|
| AUD-2 | Audio stitching hardened — replaced raw MP3 byte-concatenation + `_strip_id3_header`/`_strip_vbr_header` surgery with `_concat_mp3_segments()` (ffmpeg concat-demuxer re-encode → one valid `Xing`/`LAME` header, accurate duration). Fixes Apple Podcasts/Spotify replaying the tail of an episode |
| CRIT-1 | Critic Companies-wipe fixed — scorer emits ALL-CAPS category names but `top_articles`/`categorised` are title-cased, so `.get(cat)` returned `[]` and the patcher wrote "No Companies News Available" over a section with real articles. Both `crew/critic_crew.py` and `mc_critic.py` now resolve case-insensitively and skip patching to empty |
| VOL-1 | Volume normalization — added `_normalize_loudness()` to `mc_audio.py`; ffmpeg two-pass loudnorm to −16 LUFS; called in both `generate_podcast_audio()` and `generate_elevenlabs_audio()` |
| SEC-1 | Security + efficiency pass — XSS fix (`html.escape`, `_safe_url`) in `mc_generate.py`; batch enrichment (N+1 → 1 GPT call) in `mc_collect.py`; cross-category dedup; thread timeout; RSS 10MB size limit; 5xx retry in `mc_utils.py`; sports critic warning upgrade |
| CI-1 | GitHub Actions deploy workflow (`.github/workflows/deploy.yml`) — builds Docker image on push to `main`, pushes to ECR, registers new ECS task definition, updates EventBridge Scheduler. Dedicated IAM user `mikecast-github-actions` with least-privilege policy |
| UI-1 | Official Spotify (`blk-grn`) and Apple Podcasts (`mono-white`) badge images replacing text buttons on mikecast.io; favicon added (microphone, brand colors); CloudFront `CachingDisabled` behavior for `*.html`; fixed wrong CloudFront distribution ID in all invalidation calls |
