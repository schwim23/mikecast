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
- Sports Researcher returns `{}` honestly when no article implies a verifiable game/standings/injury. That's correct — but it means verified-facts coverage varies day-to-day. ✅ **Resolved by SPO-4** — `fetch_all_ny_team_updates()` now queries box scores for all 4 NY teams unconditionally, so the writers always have ground-truth (last score + next game) even when the article batch is weak.

---

## Sports Hallucination Fix

Maintain a persistent `ny_sports_state.json` with ground-truth per-team facts (last result, next game, notes). Update each run from trusted articles; inject into all three generation prompts as a "KNOWN FACTS" block the LLM must not contradict.

| # | Item | Branch | Status |
|---|------|--------|--------|
| SPO-4 | Deterministic per-team results + next-game: fixed `FetchSportsBoxScoreTool` next_game (only future-dated, non-completed events — was returning stale postponed past games as "next"); replaced `fetch_all_ny_upcoming_games()` with `fetch_all_ny_team_updates()` (last score **and** next game for all 4 NY teams, mandatory-include); writers must now state score + next game for any team with a game | `sports/deterministic-scores-next-game` | done |
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

## Email Newsletter (Resend)

Turn the single-recipient Gmail briefing into a real newsletter anyone can subscribe to. Double opt-in via a Function-URL Lambda + Resend; daily broadcast added after the existing Gmail send. Full plan: `~/.claude/plans/id-like-to-add-velvety-cupcake.md`.

| # | Item | Branch | Status |
|---|------|--------|--------|
| NL-1 | `mc_config.py` Resend env reads; `send_newsletter_broadcast()` in `mc_deliver.py` (reuses subscribe row + CAN-SPAM footer); wired into `mikecast_briefing.py` after `send_email()` | `newsletter-resend` | code done |
| NL-2 | Frontend: inline signup form on `index.html` + `style.css`; `subscribe.html`, `confirmed.html`; shared `signup.js` | `newsletter-resend` | code done |
| NL-3 | `lambda/newsletter_signup/` — `handler.py` (POST /signup, GET /confirm, HMAC double opt-in), `requirements.txt`, `deploy.sh`, `README.md` | `newsletter-resend` | code done |
| NL-4 | `resend` added to `requirements.txt` | `newsletter-resend` | code done |
| NL-5 | **Manual:** Resend account + `mikecast.io` domain DNS verify; create "MikeCast Daily" audience + full-access key | — | pending (Mike) |
| NL-6 | **Manual:** SSM params `/mikecast/RESEND_API_KEY`, `/mikecast/RESEND_AUDIENCE_ID`, `/mikecast/SIGNUP_HMAC_SECRET`; add to ECS task def secrets/env | — | pending (Mike) |
| NL-7 | **Manual:** create Lambda + IAM role + Function URL (CORS mikecast.io); set `CONFIRM_BASE_URL` + `MIKECAST_SIGNUP_ENDPOINT` in `signup.js`; fill CAN-SPAM postal address in `_NEWSLETTER_FOOTER` | — | pending (Mike) |
| NL-8 | Email signup UI was temporarily disabled (forms commented out) until delivery finalized. **RE-ENABLED (NL-9)** now that the broadcast ships. | `newsletter-resend` | superseded |
| NL-9 | **Signup UI RE-ENABLED** — restored the pre-NL-8 `index.html`/`subscribe.html`/`confirmed.html` (forms + `signup.js` includes) as part of the distribution work; broadcast is now gated + wired. Deploy static site to S3 + invalidate CloudFront. | `newsletter-resend` | code done |

---

## Social Distribution (X + Instagram)

Auto-post the daily briefing to X and Instagram with a deep link to that day's episode; gate all sends to first-run-of-day; add editing/reposting tooling. Full plan: `~/.claude/plans/staged-petting-hellman.md`.

| # | Item | Branch | Status |
|---|------|--------|--------|
| SOC-1 | `mc_dist_state.py` — per-date distribution state (`data/dist/YYYY-MM-DD.json`), S3-authoritative; `channel_sent`/`record_send`/`record_social_copy`/`append_repost` | `newsletter-resend` | code done |
| SOC-2 | First-run-of-day gating in `mikecast_briefing.py` — personal email + newsletter + social gated; `--resend` bypasses (`--force` regenerates content only) | `newsletter-resend` | code done |
| SOC-3 | `crew/distribution_crew.py` + `make_social_copywriter()` — Claude writes X post + IG caption (strict JSON, hallucination-guarded) | `newsletter-resend` | code done |
| SOC-4 | `mc_social.py` — PIL 1080×1080 card, S3 card upload, X post/delete (OAuth1), IG Graph two-step publish, `run_social_distribution` orchestrator + `_fit_x`/`_fit_ig` + deterministic fallback; CLI | `newsletter-resend` | code done |
| SOC-5 | `mc_config.py` X/IG/CloudFront env reads; `Pillow` + `requests-oauthlib` in `requirements.txt` | `newsletter-resend` | code done |
| SOC-6 | `app.js` `?date=YYYY-MM-DD` deep links (init block + `history.replaceState` on render) | `newsletter-resend` | code done |
| SOC-7 | `mc_edit.py` — show/set-html/regen/publish/repost-social/delete-social; shared `invalidate_cloudfront()` in `mc_utils.py` | `newsletter-resend` | code done |
| SOC-8 | **Manual (Mike):** X developer account + OAuth1 read/write keys; IG Business account + FB Page + Meta app + system-user (or 60-day) token; SSM params `/mikecast/X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `META_ACCESS_TOKEN`, `IG_USER_ID` + ECS task-def secrets; add to `~/.profile`. See `SOCIAL_SETUP.md`. | — | pending (Mike) |

**Deferred (future work):**

| # | Item |
|---|------|
| SOC-Reels | Daily Instagram Reel — reuse `mc_ad.py`'s 1080×1920 video via the Graph API Reels flow (`media_type=REELS`, upload + poll + publish) once image-card posting is stable. Requires adding `mc_ad.py` deps (numpy/moviepy or ffmpeg-only) to the Docker image. |
| SOC-YT | Daily YouTube upload — wire the existing `mc_youtube.py` into pipeline Step 11 with its own `channel_sent` gate (`"youtube": {"video_id": ...}`), OAuth refresh-token in SSM. |

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
