#!/bin/bash
# eval/run_daily_sports_eval.sh — cron wrapper: Sports Researcher eval only.
# Replays today's auto-captured sports_researcher.json fixture against the
# baseline + candidates, scores tool fidelity, rebuilds the dashboard.
# Self-expires after STOP_DATE. Costs ~$0.05/run.
#
# Installed via crontab:
#   15 8 * * * /home/mike-schwimmer/mikecast/eval/run_daily_sports_eval.sh >> /home/mike-schwimmer/mikecast/eval/daily_sports_eval.log 2>&1

set -uo pipefail

STOP_DATE="2026-10-05"
TODAY=$(date +%F)

cd /home/mike-schwimmer/mikecast || exit 1
source /home/mike-schwimmer/.profile
export S3_BUCKET=mikecast-io-data

echo "===== $(date) — sports researcher eval for $TODAY ====="

if [[ "$TODAY" > "$STOP_DATE" ]]; then
    echo "Past STOP_DATE ($STOP_DATE) — done. Remove the crontab entry."
    exit 0
fi

if ! aws s3 ls "s3://mikecast-io-data/eval/fixtures/live/${TODAY}/sports_researcher.json" > /dev/null 2>&1; then
    echo "No sports_researcher.json fixture for $TODAY — skipping. Did today's ECS run happen?"
    exit 0
fi

tmp=$(mktemp)
.venv/bin/python3 eval/run_eval.py --role sports_researcher --date "$TODAY" \
    --baseline openai/gpt-4o \
    --candidate openai/gpt-5.6-terra --candidate openai/gpt-5.6-sol \
    --candidate anthropic/claude-sonnet-5 --k 2 > "$tmp" 2>&1
cat "$tmp"
RUN=$(grep -oP 'Full output: \K.*' "$tmp" | xargs -I{} basename {})
rm -f "$tmp"

if [[ -n "$RUN" ]]; then
    echo "--- scoring ---"
    .venv/bin/python3 eval/score.py --run "$RUN"
else
    echo "run_id not captured — skipping scoring"
fi

echo "--- dashboard ---"
.venv/bin/python3 eval/build_dashboard.py
echo "===== $(date) — done ====="
