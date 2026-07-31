"""
MikeCast — custom business metrics, submitted directly to Datadog's Metrics
API (no dogstatsd — that needs a local Agent to relay to, which the Datadog
trial plan rules out for this ephemeral-Fargate pipeline; see
DATADOG_PLAN.md Phase 4).

One batched POST per run, at the very end of mikecast_briefing.py, rather
than a call per event — the task is short-lived and there's no benefit to
streaming. No-ops cleanly when DD_API_KEY is unset.
"""

import logging
import time

import requests

from mc_config import DD_API_KEY, DD_SITE

logger = logging.getLogger("mikecast")

_GAUGE = 3  # Datadog Metrics API v2 MetricType enum; "gauge" as a string 400s.


def _point(value: float) -> dict:
    return {"timestamp": int(time.time()), "value": value}


def submit_run_metrics(
    pipeline_path: str,
    date: str,
    article_stats: dict,
    critic_metrics: dict,
    audio_duration_secs: int,
    social_results: dict,
) -> None:
    """Submit one batch of gauges for a completed daily run."""
    if not DD_API_KEY:
        logger.info("DD_API_KEY not set — metrics submission skipped.")
        return

    base_tags = [f"pipeline_path:{pipeline_path}", f"date:{date}"]
    series = []

    for name, value in (
        ("raw_collected", article_stats.get("raw_collected")),
        ("deduped", article_stats.get("deduped")),
        ("selected", article_stats.get("selected")),
    ):
        if value is not None:
            series.append({
                "metric": f"mikecast.articles.{name}",
                "type": _GAUGE,
                "points": [_point(value)],
                "tags": base_tags,
            })

    for category, score in critic_metrics.get("category_scores", {}).items():
        if isinstance(score, (int, float)):
            series.append({
                "metric": "mikecast.critic.category_score",
                "type": _GAUGE,
                "points": [_point(score)],
                "tags": base_tags + [f"category:{category.lower().replace(' ', '_')}"],
            })

    series.append({
        "metric": "mikecast.critic.patch_count",
        "type": _GAUGE,
        "points": [_point(len(critic_metrics.get("patched_categories", [])))],
        "tags": base_tags,
    })
    series.append({
        "metric": "mikecast.critic.ny_sports_skipped",
        "type": _GAUGE,
        "points": [_point(1 if critic_metrics.get("ny_sports_skipped") else 0)],
        "tags": base_tags,
    })

    if audio_duration_secs:
        series.append({
            "metric": "mikecast.audio.duration_secs",
            "type": _GAUGE,
            "points": [_point(audio_duration_secs)],
            "tags": base_tags,
        })

    for channel in ("x", "instagram"):
        result = str(social_results.get(channel, "skip"))
        success = 1 if result.startswith("posted:") or result == "already_sent" else 0
        series.append({
            "metric": f"mikecast.social.{channel}_success",
            "type": _GAUGE,
            "points": [_point(success)],
            "tags": base_tags,
        })

    try:
        resp = requests.post(
            f"https://api.{DD_SITE}/api/v2/series",
            headers={"DD-API-KEY": DD_API_KEY, "Content-Type": "application/json"},
            json={"series": series},
            timeout=10,
        )
        if resp.status_code >= 300:
            logger.warning("Datadog metrics submission failed (%d): %s", resp.status_code, resp.text[:300])
        else:
            logger.info("Datadog metrics submitted: %d series.", len(series))
    except Exception as exc:
        logger.warning("Datadog metrics submission raised (non-fatal): %s", exc)
