# MikeCast Backlog

Tracks planned work. Each item lists the branch to create when starting implementation.

---

## AWS Migration

Move pipeline off local machine. Serve site at mikecast.io via CloudFront + S3. Retire GitHub Pages and local cron. Full architecture in `aws_migration.txt`.

| # | Item | Branch | Status |
|---|------|--------|--------|
| AWS-1 | Create `Dockerfile`, build + test locally with `docker run` | `aws/step-1-dockerfile` | done |
| AWS-2 | Create AWS resources: S3 bucket `mikecast-io-data`, ECR repo, IAM roles | `aws/step-2-aws-resources` | partial — S3 done; ECR + IAM pending |
| AWS-3 | Adapt `mc_deliver.py` — S3 file writes only (Gmail SMTP unchanged; move `GMAIL_APP_PASSWORD`, `GMAIL_FROM`, `GMAIL_TO` to SSM) | `aws/step-3-deliver-s3` | done |
| AWS-4 | Adapt `mc_utils.py` — S3-backed history/picks persistence | `aws/step-4-utils-s3` | done |
| AWS-5 | Adapt `mikes_picks_ingest.py` — read/write picks from S3 | `aws/step-5-picks-s3` | done |
| AWS-6 | Seed S3 with existing `data/`, `briefing_history.json`, `mikes_picks.json`, `dashboard/` | `aws/step-6-seed-s3` | backlog |
| AWS-7 | Push Docker image to ECR; create ECS cluster + task definition | `aws/step-7-ecs` | backlog |
| AWS-8 | Create EventBridge Scheduler rule (6:30 AM ET, DST-aware); load secrets into SSM Parameter Store | `aws/step-8-scheduler-ssm` | done |
| AWS-9 | Test manual ECS task run; verify output in S3 + email received | `aws/step-9-test` | backlog |
| AWS-10 | Set up CloudFront + ACM certificate; create Route53 hosted zone for mikecast.io | `aws/step-10-cloudfront-r53` | done |
| AWS-11 | Update registrar NS records → Route53; create A alias → CloudFront; update `SITE_BASE_URL` in `mc_config.py` | `aws/step-11-dns-cutover` | done |
| AWS-12 | Monitor first automated run via CloudWatch Logs; decommission GitHub Pages + local cron | `aws/step-12-decommission` | backlog |

**Estimated cost:** ~$2.25/month (see `aws_migration.txt` for full breakdown)

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
| VOL-1 | Volume normalization — added `_normalize_loudness()` to `mc_audio.py`; ffmpeg two-pass loudnorm to −16 LUFS; called in both `generate_podcast_audio()` and `generate_elevenlabs_audio()` |
