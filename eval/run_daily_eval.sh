#!/bin/bash
# eval/run_daily_eval.sh — cron wrapper for the model-eval harness.
#
# Runs the full baseline-vs-candidate matrix for writer/critic/helper against
# today's auto-captured fixture (see MODEL_EVAL_PLAN.md §0j — the daily ECS
# run writes eval fixtures to S3 automatically now), scores writer/critic,
# rebuilds the results dashboard, and writes today's run_ids to
# eval/out/latest.json so `eval/review.py --latest writer` (or critic) can
# find them without a run_id being copy-pasted by hand. Self-expires after
# STOP_DATE so a short trial period doesn't need to be remembered and
# manually removed.
#
# Costs real API money every time it runs (writer runs especially).
#
# Installed via crontab:
#   0 8 * * * /home/mike-schwimmer/mikecast/eval/run_daily_eval.sh >> /home/mike-schwimmer/mikecast/eval/daily_eval.log 2>&1

set -uo pipefail

STOP_DATE="2026-09-20"  # one week from setup (2026-09-13) — extend or remove this check to keep running longer
TODAY=$(date +%F)

cd /home/mike-schwimmer/mikecast || exit 1
source /home/mike-schwimmer/.profile
export S3_BUCKET=mikecast-io-data

echo "===== $(date) — daily eval run for $TODAY ====="

if [[ "$TODAY" > "$STOP_DATE" ]]; then
    echo "Past STOP_DATE ($STOP_DATE) — trial period over. Edit or remove this script's STOP_DATE, or remove the crontab entry, to keep going."
    exit 0
fi

# Don't try to eval a fixture that isn't there yet (daily ECS run failed, ran
# late, or --dump-eval-fixture got reverted) — fail loud in the log, not with
# a confusing downstream traceback.
if ! aws s3 ls "s3://mikecast-io-data/eval/fixtures/live/${TODAY}/writer.json" > /dev/null 2>&1; then
    echo "No fixture found for $TODAY at s3://mikecast-io-data/eval/fixtures/live/${TODAY}/ — skipping. Did today's ECS run happen?"
    exit 0
fi

# run_id() used to pipe through `tee /dev/stderr` so the log got the full output
# while a variable captured just the run_id — but that races against this
# script's own `echo` statements writing into the SAME log file (both are
# separate processes appending concurrently), so lines went missing or got cut
# mid-word (confirmed 2026-09-14, MODEL_EVAL_PLAN.md). A temp file has exactly
# one writer at a time — the racing writer and the mystery are both gone by
# construction, not by tuning buffering (stdbuf/PYTHONUNBUFFERED did NOT fix it).
run_step() {
    # $1 = step label, $2 = output variable name to set with the parsed run_id,
    # rest = the command. Redirects to a temp file, dumps it to the log (this
    # script's only writer at that moment), extracts the run_id, cleans up.
    local label="$1" outvar="$2"; shift 2
    local tmp; tmp=$(mktemp)
    echo "--- $label ---"
    "$@" > "$tmp" 2>&1
    cat "$tmp"
    printf -v "$outvar" '%s' "$(grep -oP 'Full output: \K.*' "$tmp" | xargs -I{} basename {})"
    rm -f "$tmp"
}

run_step writer WRITER_RUN \
    .venv/bin/python3 eval/run_eval.py --role writer --date "$TODAY" \
    --baseline anthropic/claude-sonnet-4-6 \
    --candidate anthropic/claude-sonnet-5 --candidate openai/gpt-5.6-sol --k 2

run_step critic CRITIC_RUN \
    .venv/bin/python3 eval/run_eval.py --role critic --date "$TODAY" \
    --baseline openai/gpt-4o \
    --candidate openai/gpt-5.6-terra --candidate anthropic/claude-sonnet-5

run_step helper HELPER_RUN \
    .venv/bin/python3 eval/run_eval.py --role helper --date "$TODAY" \
    --baseline openai/gpt-4o-mini \
    --candidate openai/gpt-5.6-luna --candidate anthropic/claude-haiku-4-5

echo "--- scoring ---"
if [[ -n "${WRITER_RUN:-}" ]]; then
    .venv/bin/python3 eval/score.py --run "$WRITER_RUN"
else
    echo "writer run_id not captured — skipping score.py for writer"
fi
if [[ -n "${CRITIC_RUN:-}" ]]; then
    .venv/bin/python3 eval/score.py --run "$CRITIC_RUN"
else
    echo "critic run_id not captured — skipping score.py for critic"
fi

echo "--- dashboard ---"
.venv/bin/python3 eval/build_dashboard.py

# So the human-review step doesn't need `ls -t eval/out/ | head` and a guess
# each morning — always today's exact run_ids, at a fixed, predictable path.
cat > eval/out/latest.json <<EOF
{
  "date": "$TODAY",
  "generated_at": "$(date -u +%FT%TZ)",
  "writer_run": "${WRITER_RUN:-}",
  "critic_run": "${CRITIC_RUN:-}",
  "helper_run": "${HELPER_RUN:-}"
}
EOF
echo "Wrote eval/out/latest.json"

echo "===== $(date) — done ====="
