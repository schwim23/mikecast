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

run_id() {
    # run_eval.py's last stdout line is "Full output: eval/out/<run_id>" — pull
    # the run_id back out rather than re-deriving/guessing the timestamp.
    grep -oP 'Full output: \K.*' | xargs -I{} basename {}
}

echo "--- writer ---"
WRITER_RUN=$(.venv/bin/python3 eval/run_eval.py --role writer --date "$TODAY" \
    --baseline anthropic/claude-sonnet-4-6 \
    --candidate anthropic/claude-sonnet-5 --candidate openai/gpt-5.6-sol --k 2 | tee /dev/stderr | run_id)

echo "--- critic ---"
CRITIC_RUN=$(.venv/bin/python3 eval/run_eval.py --role critic --date "$TODAY" \
    --baseline openai/gpt-4o \
    --candidate openai/gpt-5.6-terra --candidate anthropic/claude-sonnet-5 | tee /dev/stderr | run_id)

echo "--- helper ---"
HELPER_RUN=$(.venv/bin/python3 eval/run_eval.py --role helper --date "$TODAY" \
    --baseline openai/gpt-4o-mini \
    --candidate openai/gpt-5.6-luna --candidate anthropic/claude-haiku-4-5 | tee /dev/stderr | run_id)

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
