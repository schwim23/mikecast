#!/bin/bash
# eval/run_daily_article_eval.sh — cron wrapper: article scorer + enricher evals.
# Replays today's auto-captured article_scorer.json / article_enricher.json fixtures
# (mc_collect.py, captured by the ECS run) against baseline + candidates, scores them,
# rebuilds the dashboard. Self-expires after STOP_DATE. Costs ~$0.15/day.
#
# Installed via crontab:
#   20 8 * * * /home/mike-schwimmer/mikecast/eval/run_daily_article_eval.sh >> /home/mike-schwimmer/mikecast/eval/daily_article_eval.log 2>&1

set -uo pipefail

STOP_DATE="2026-10-05"
TODAY=$(date +%F)

cd /home/mike-schwimmer/mikecast || exit 1
source /home/mike-schwimmer/.profile
export S3_BUCKET=mikecast-io-data

echo "===== $(date) — article scorer/enricher eval for $TODAY ====="

if [[ "$TODAY" > "$STOP_DATE" ]]; then
    echo "Past STOP_DATE ($STOP_DATE) — done. Remove the crontab entry."
    exit 0
fi

run_role() {
    local role="$1" baseline="$2"; shift 2
    if ! aws s3 ls "s3://mikecast-io-data/eval/fixtures/live/${TODAY}/${role}.json" > /dev/null 2>&1; then
        echo "No ${role}.json fixture for $TODAY — skipping. Has the capture code been deployed / did today's ECS run happen?"
        return
    fi
    local tmp; tmp=$(mktemp)
    .venv/bin/python3 eval/run_eval.py --role "$role" --date "$TODAY" --baseline "$baseline" "$@" > "$tmp" 2>&1
    cat "$tmp"
    local run; run=$(grep -oP 'Full output: \K.*' "$tmp" | xargs -I{} basename {})
    rm -f "$tmp"
    if [[ -n "$run" ]]; then
        echo "--- scoring $role ---"
        .venv/bin/python3 eval/score.py --run "$run"
    else
        echo "run_id not captured for $role — skipping scoring"
    fi
}

run_role article_scorer openai/gpt-4o \
    --candidate openai/gpt-5.6-terra --candidate openai/gpt-5.6-sol --candidate anthropic/claude-sonnet-5
run_role article_enricher openai/gpt-4o-mini \
    --candidate openai/gpt-5.6-luna --candidate openai/gpt-5.6-terra --candidate anthropic/claude-sonnet-5

echo "--- dashboard ---"
.venv/bin/python3 eval/build_dashboard.py
echo "===== $(date) — done ====="
