# MikeCast — Datadog Free Trial Integration Plan

**Status:** IN PROGRESS — Phases 0–3 done, Phases 4–6 not built · **Author:** planning session
2026-07-29 · **Owner:** Mike

**Decisions locked in (2026-07-29):** no containers added to the scheduled task definition, full
stop — Mike doesn't want any risk to the `mikecast-daily` EventBridge-triggered run. Concretely:
logs go via the **Forwarder Lambda** (not FireLens), and APM tracing goes via **agentless
OpenTelemetry** (not the Datadog Agent sidecar). Both keep `.github/workflows/deploy.yml` and the
ECS task definition completely untouched — everything here is either a code-level SDK call or an
entirely separate AWS resource (a Lambda subscribed to the existing log group).

**Goal:** integrate Datadog into MikeCast's existing AWS stack (ECS Fargate, EventBridge Scheduler,
S3/CloudFront, GitHub Actions CI/CD) using the 14-day free trial, purely to get real hands-on
Datadog experience — APM, Fargate log/metric collection, CI Visibility, dashboards, monitors — ahead
of interviews. Not a request from the product; scope everything to what's genuinely useful to have
touched, not what's "complete."

---

## 0. Why MikeCast is actually a good trial subject

MikeCast is **not** a long-running web service — it's a single ECS Fargate task that boots once a
day (`mikecast-daily` EventBridge schedule, ~20 min, then exits) and runs a 10+ step pipeline
(`mikecast_briefing.py`: collect → dedupe → score → generate → critic → audio → deliver → distribute).
That's a meaningfully different Datadog story than "add `ddtrace-run` to a Flask app":

- **Batch pipeline tracing**, not request tracing — custom spans per pipeline step, one trace per
  daily run.
- **Ephemeral Fargate**, not an always-on host — no host-level Agent is possible, and Mike has ruled
  out sidecar containers on the scheduled task entirely, so everything here goes through agentless
  code-level submission (OTLP for traces, HTTP API for metrics, Forwarder Lambda for logs) — itself a
  real pattern worth being able to discuss.
- A real **GitHub Actions → ECR → ECS task-def → EventBridge** deploy pipeline (`.github/workflows/deploy.yml`)
  to wire up CI Visibility against.

---

## 1. Phase 0 — Trial account + secrets

1. Sign up for the Datadog free trial (14 days, full Pro/Enterprise feature set: APM, Log
   Management, CI Visibility all included).
2. Generate an API key and an APP key.
3. Store alongside existing secrets, same pattern as `ANTHROPIC_API_KEY` etc (see `CLAUDE.md`
   env var table and `aws_migration.txt` SSM list):
   ```bash
   aws ssm put-parameter --region us-east-1 --type SecureString --overwrite \
     --name /mikecast/DD_API_KEY --value '<key>'
   aws ssm put-parameter --region us-east-1 --type SecureString --overwrite \
     --name /mikecast/DD_APP_KEY --value '<key>'
   ```
4. Add both to `~/.profile` for local runs and to the ECS task definition's `containerDefinitions[0].secrets`
   (new revision, same mechanism used for `X_*`/`META_ACCESS_TOKEN` in the social-posting rollout).

---

## 2. Phase 1 — APM on the pipeline itself, agentless via OpenTelemetry (do this first — highest interview value)

Instrument `mikecast_briefing.py` with the **OpenTelemetry Python SDK**, exporting via OTLP
**directly to Datadog's intake** (`https://otlp.<DD_SITE>/v1/traces` or `otlp.datadoghq.com`,
depending on trial site) with `DD_API_KEY` as a header. No Datadog Agent, no sidecar container, no
task-definition change of any kind — this is a pure application-code addition, same risk profile as
adding any other library to `requirements.txt`.

Since there's no inbound HTTP request to anchor a trace, create one **root span per daily run**,
with **child spans per pipeline step**:

- `collect` (`mc_collect.py`)
- `plan` (`mc_plan.py`, legacy path only)
- `score` / `select` / `enrich`
- `generate` (`mc_generate.py` or `crew/writing_crew.py`)
- `critic` (`mc_critic.py` / `crew/critic_crew.py`)
- `audio` (`mc_audio.py`)
- `deliver` (`mc_deliver.py`)
- `distribute` (`mc_social.py`, `mc_dist_state.py`)

Add step-level span attributes: crew vs. legacy path, whether NY Sports was (correctly) skipped by
the patcher. (Article counts, critic scores, and audio duration are covered as **metrics**, not span
attributes — see Phase 5.) This demonstrates *custom instrumentation of an async/batch workflow* via
OTel — a distinguishing talking point vs. framework auto-instrumentation, and increasingly the
pattern companies actually run in production rather than vendor-locked tracer libraries.

**Files touched:** `mikecast_briefing.py` (root span + step spans, OTLP exporter config),
`requirements.txt` (+`opentelemetry-sdk`, `opentelemetry-exporter-otlp`). `DD_API_KEY` read from the
env var already staged in Phase 0 — no new SSM/task-def wiring beyond what Phase 0 already does.

---

## 3. Phase 2 — Log forwarding via the Datadog Forwarder Lambda

Deploy Datadog's official Forwarder Lambda (their CloudFormation/SAM template), subscribe it to the
existing `/ecs/mikecast` log group (`CLAUDE.md` confirms this is the current log group). Entirely a
new, separate AWS resource — **zero changes to the ECS task definition or `deploy.yml`**, so nothing
about the scheduled run is touched.

Standard log parsing should pick up the existing `mikecast.log` format used by `tail -f mikecast.log`
locally without extra configuration.

(FireLens/Fluent-Bit-sidecar was the alternative — ruled out specifically because it requires adding
a container to the task definition, which is the exact risk Mike wants to avoid.)

---

## 4. Phase 3 — CI/CD visibility — DONE (2026-07-30)

Originally planned as a code change (`datadog/ci-cd-visibility` GitHub Action or `datadog-ci` CLI
wrapping build/deploy steps in `.github/workflows/deploy.yml`). Turned out Datadog's current
recommended path for GitHub Actions is a **zero-code GitHub App integration** instead — checked
docs before building anything, since the CLI-wrapper approach would have been solving an already-
solved problem the harder way:

1. Created a Datadog-managed GitHub App (`Actions: Read Only` permission only) via the
   [GitHub integration tile](https://app.datadoghq.com/integrations/github/), installed on the
   `mikecast` repo.
2. Enabled CI Visibility for the account via **Software Delivery → CI Visibility → Add a Pipeline
   Provider → GitHub**.

No changes to `deploy.yml`, no new GitHub Actions secrets. Mike completed both steps
(2026-07-30) — GitHub App authorization has to happen from his own GitHub account, not something
Claude can do on his behalf. Only **future** workflow runs get tracked (no backfill), so the
"Build and Deploy" pipeline won't show up under CI Visibility → Pipelines until the next `git push`
to `main`. Job logs collection (a separate, separately-billed toggle on the same setup page) was
left off.

This still closes the full loop for the interview story: **code push → GH Actions build/deploy →
ECS Fargate runtime trace**, all visible in one tool — just via account-level integration rather
than in-repo code.

---

## 5. Phase 4 — Custom business metrics, direct to the HTTP API

No `dogstatsd` (that requires a local Agent to relay to, which Mike has ruled out) — submit metrics
directly via Datadog's **Metrics API** (`POST /api/v2/series`), same `DD_API_KEY` as Phases 1 and 2.
Batch one payload per run at the end of `mikecast_briefing.py` rather than a call per event, since
the task is short-lived and there's no benefit to streaming.

Scope, per Mike's ask:
- **Article counts** — collected / deduped / selected per run (`mc_collect.py`)
- **Critic scores** — per category, plus patch count and NY Sports skip confirmation (ties into the
  `NEVER_PATCH_NORMALIZED` guard documented in `CLAUDE.md`)
- **Audio duration** — `audio_duration_secs`, already computed in `mc_audio.py`
- **Social post success** — X / Instagram post success-or-fail (`mc_social.py`, `mc_dist_state.py`)

This is the part that shows the difference between infra monitoring and product/business
observability — a step past what most take-home Datadog demos cover.

**Files touched:** `mikecast_briefing.py` (metrics payload assembly + one API call at the end of the
run), `mc_critic.py` / `mc_audio.py` / `mc_social.py` (expose the values needed, no behavior change).

---

## 6. Phase 5 — Dashboard + Monitors + Synthetics

**Dashboard:** one "MikeCast Ops" view — trace waterfall of the daily run, per-step timing, critic
scores over time, daily success/fail, social post status, S3/CloudFront request volume (via Phase 6
AWS integration).

**Monitors:**
- Task didn't run today (no trace/log by, say, 7:15 AM ET — schedule is 6:30 AM ET)
- Critic score below threshold (`_WEAK_THRESHOLD = 7` per `CLAUDE.md`) on any category more than N
  days running
- Log-pattern monitor on `ERROR`/`CRITICAL` in forwarded logs

**Synthetics — API test (not a browser test):** a scheduled HTTP check (every 15–30 min) hitting
`https://mikecast.io/data/manifest.json`, asserting the latest date entry matches today's date once
past the ~7:00 AM ET post-run window. This catches "task ran but `mc_deliver.py` failed to publish" —
a real gap the log/trace monitors above wouldn't necessarily surface, since it checks the *site*
rather than the *pipeline*. A browser test (full page render + click-through) was considered but adds
cost without adding real coverage for a static CloudFront site — skipped for now, worth revisiting if
Mike wants the click-through hands-on rep specifically.

---

## 7. Phase 6 — AWS integration (near-zero effort, good breadth)

Connect Datadog's AWS integration via a read-only IAM role (standard Datadog-provided CloudFormation
template) to auto-pull CloudWatch metrics for S3 (`mikecast-io-data`), CloudFront (`EFNQM31KQHY56`),
ECS, and EventBridge — no application code. Rounds out "cloud integration configuration" as a
resume-able skill with almost no build time.

---

## 8. Sequencing

Do **Phases 1–2 first** — agentless OTel tracing and the Forwarder Lambda are the parts unique to
this architecture and the most defensible in an interview conversation, and both are safe to build
and test without going near the scheduled task definition. Phases 3–6 are lower-effort polish, worth
doing if the trial clock allows but not the priority.

---

## 9. Cost / trial management

The 14-day trial covers all phases above for free (full Pro/Enterprise feature set). After it
expires:

- Datadog's free-forever tier only covers basic infra metrics (5 hosts, 1-day retention) —
  **APM, Log Management, and CI Visibility drop to paid** the moment the trial ends.
- Given the task runs ~20 min/day, actual paid usage would be trivial in absolute terms, but there's
  no need to keep paying for a personal project's observability once the hands-on exercise is done.
- **Action before day 14:** either downgrade to what's actually free (Phase 6's AWS integration +
  basic infra metrics only), or strip the OTel export call / Metrics API call back out of
  `mikecast_briefing.py`, delete the Forwarder Lambda stack, and cancel the subscription — no ECS or
  scheduler changes needed either way since none of this plan touches them.

Everything that was open is resolved as of 2026-07-29 (see "Decisions locked in" above): Forwarder
Lambda for logs, agentless OpenTelemetry for APM, Metrics API (not `dogstatsd`) for custom metrics
scoped to article counts / critic scores / audio duration / social post success, and a Synthetics API
test (not a browser test) on `manifest.json`.
