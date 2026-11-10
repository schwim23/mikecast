# Docs Sync Plan — keeping README.md and CLAUDE.md true after every deploy

Status: DESIGN (2026-11-10). Nothing below is built yet.

## Problem

The 2026-11-10 audit found ~25 inaccuracies in README.md / CLAUDE.md. Almost all were
one of three kinds:

| Kind of drift | Examples from the audit | Best fix |
|---|---|---|
| **A. A value that lives in code** | scorer default `gpt-4o` (really `gpt-5.6-terra`), `grok-2` (really `grok-3`), "top 25" (really 30), 900–1000 words (really 1150–1250), schedule 6:45 (really 6:30) | Generate it from code |
| **B. A name that no longer exists, or a new file nobody listed** | `WIRE_RSS_FEEDS`, root `test_trending_filter.py`, `mc_metrics.py`/`mc_tracing.py`/`mc_social.py` missing from the module map | Lint it in CI |
| **C. Prose that describes behaviour** | "CrewAI is opt-in", "local cron is the fallback", "cron pushes to GitHub which syncs S3", missing November hallucination guards | LLM review after deploy, as a PR |

CLAUDE.md's "Key Constraints" section had **zero** errors, because every fix that added
code also added its constraint in the same commit. The goal is to make every section of
both files behave like that one.

## Design: three layers, cheapest first

### Layer 1: generated blocks (kind A), enforced by the existing `test` job

- Add `scripts/gen_docs.py`. It imports `mc_config` (with `MIKECAST_FACTS_OFFLINE=1`, no
  network) and parses a few other sources, then rewrites only the text between marker
  comments:
  ```
  <!-- BEGIN GENERATED: models (scripts/gen_docs.py — do not edit by hand) -->
  ...
  <!-- END GENERATED: models -->
  ```
- Blocks and their sources of truth:
  | Block | Source |
  |---|---|
  | `models`: model-role table (README) + model rows of the env table (CLAUDE.md) | `mc_config.*_MODEL` defaults; `mc_plan.py` Grok model |
  | `env`: env-var table | every `os.environ.get("X", default)` in `mc_config.py` (AST walk) plus a small hand-kept dict of one-line purposes, keyed by var name. A var with no purpose fails generation, so a new var can't ship undocumented |
  | `numbers`: article budget, word targets, stale window, critic threshold | `TOP_ARTICLES_TOTAL`, `eval/score.py` `_PODCAST_WORD_MIN/MAX`, `_WEAK_THRESHOLD` |
  | `flags`: CLI flags | string literals `"--xxx"` checked in `mikecast_briefing.py` |
  | `schedule`: schedule + deploy steps | `.github/workflows/deploy.yml` (`schedule-expression`, job names, `needs:`) |
  | `tree`: project-structure tree | `git ls-files` filtered to top level + `crew/` + `eval/`; per-file comment taken from each module's docstring first line |
- New test `crew/tests/test_docs_in_sync.py` runs `gen_docs.py --check`, which renders in
  memory and diffs. If anything is stale, the test fails with
  `run: python scripts/gen_docs.py` in the message. The `test` job already gates `deploy`,
  so **stale generated docs block a deploy exactly like a failing test**. The fix is one
  local command, committed with the code change.
- Why not have CI regenerate and commit? A bot commit to `main` re-triggers the deploy
  workflow (loop risk), races the concurrency group, and lands docs that nobody read.
  Failing the check is simpler and keeps the doc change in the same commit as the code.

### Layer 2: drift lint (kind B), same test file

Deterministic, no LLM:
1. Every backticked token in README.md/CLAUDE.md that looks like a repo path
   (`*.py`, `*.sh`, `*.yml`, `*/`) must exist (`git ls-files`).
2. Every backticked `UPPER_SNAKE` or `_private_name` that looks like a code identifier must
   appear somewhere in tracked `*.py`/`*.yml`/`Dockerfile`. A short allowlist covers
   historical names that are mentioned on purpose (e.g. "raised from 900–1000").
3. Every tracked `mc_*.py` and `crew/*.py` must appear in CLAUDE.md's module map.
4. CLAUDE.md "Key Constraints" bullets that name a test file (`Pinned by crew/tests/...`)
   must point at a file that exists.

Taken together, layers 1 and 2 would have caught roughly 70% of today's findings before
they merged.

### Layer 3: post-deploy LLM review (kind C), opens a PR and never pushes

A new job in `deploy.yml`:

```yaml
docs-review:
  needs: deploy                      # only after a successful prod deploy
  if: "!startsWith(github.event.head_commit.message, 'docs:')"
  runs-on: ubuntu-latest
  permissions: { contents: write, pull-requests: write }
  steps:
    - uses: actions/checkout@v4
      with: { fetch-depth: 0 }
    - name: Live infra snapshot      # deploy job's AWS creds; secrets NAMES only, never values
      run: |
        aws ecs describe-task-definition --task-definition mikecast \
          --query 'taskDefinition.containerDefinitions[0].{env:environment,secrets:secrets[].name}' > infra.json
        aws scheduler get-schedule --name mikecast-daily --query ScheduleExpression >> infra.json
    - uses: anthropics/claude-code-action@v1
      with:
        anthropic_api_key: ${{ secrets.DOCS_ANTHROPIC_API_KEY }}
        prompt: |
          Diff since tag docs-synced: `git diff docs-synced..HEAD -- . ':!*.md'`.
          Live infra: infra.json. Update README.md and CLAUDE.md prose ONLY where this
          diff or infra makes them wrong or incomplete. Do not touch GENERATED blocks.
          New guards/incidents go in CLAUDE.md Key Constraints in the existing style.
          If nothing is stale, change nothing.
    - name: Open PR if changed
      run: |  # branch docs/sync-<sha>, title "docs: sync with <sha>", body = summary;
              # then move tag docs-synced to HEAD
```

Rules:
- **Output is always a PR, never a push to `main`.** CLAUDE.md steers every future
  Claude Code session, so a wrong sentence there is a real risk; a human merges it.
- **No loop.** Add `paths-ignore: ['**/*.md']` to the workflow's `push` trigger. A merged
  docs-only PR then doesn't rebuild the image or rerun this job. The `docs:` commit-message
  guard is a second safeguard. (Side benefit: docs-only pushes stop doing pointless deploys.)
- **The `docs-synced` git tag** marks the last commit the docs were reviewed against, so a
  skipped or failed run is covered by the next one's larger diff.
- **Separate, spend-capped Anthropic key** as a GitHub secret (`DOCS_ANTHROPIC_API_KEY`),
  not the prod pipeline key. Expected cost is under $1 per deploy.
- **Non-blocking.** `deploy` has already finished; a docs-review failure never affects prod.

## Rollout

1. Layer 2 (lint) first: about an hour of work, no markers needed, and it immediately
   guards against the B-class drift fixed in the 2026-11-10 branch.
2. Layer 1: add markers + `gen_docs.py` one block at a time (`models` → `env` →
   `numbers` → `flags` → `schedule` → `tree`); each block replaces hand text with identical
   generated text, so the diffs are reviewable.
3. Add `paths-ignore` to `deploy.yml`.
4. Layer 3: add the `DOCS_ANTHROPIC_API_KEY` secret, the job, and the initial `docs-synced`
   tag on the commit that merges the 2026-11-10 docs fix.

## Out of scope

- Long-form plan/log files (`*_PLAN.md`, `DATADOG_BUILD_LOG.md`, `BACKLOG.md`). They are
  point-in-time records, not current-state docs; layer 3's prompt excludes them.
- `.claude/commands/*.md`: covered by the layer 2 path lint only.
