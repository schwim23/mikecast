"""
Fixture capture for the model-eval harness — Phase 0 (MODEL_EVAL_PLAN.md).

Writes each role's exact input/output for the day's run to
eval/fixtures/live/<date>/<role>.json when MIKECAST_DUMP_EVAL_FIXTURE=1 is set
(--dump-eval-fixture on mikecast_briefing.py). A no-op otherwise, so it's safe
to call unconditionally from production code paths.

Also uploads the same JSON to S3 (mikecast-io-data, same S3_BUCKET the rest of
mc_deliver.py uses) when S3_BUCKET is configured — the production pipeline runs
on ephemeral ECS Fargate with no persistent local disk, so a local-only write
there would silently vanish the moment the task exits. Local write is
best-effort too; neither failing raises out to the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("mikecast.eval.dump")

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "live"


def enabled() -> bool:
    return os.environ.get("MIKECAST_DUMP_EVAL_FIXTURE") == "1"


def dump_fixture(role: str, date: str, payload: dict) -> None:
    if not enabled():
        return

    try:
        out_dir = FIXTURES_DIR / date
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{role}.json"
        with open(out_path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        logger.info("[eval] dumped %s fixture -> %s", role, out_path)
    except Exception as exc:
        logger.warning("[eval] failed to write local %s fixture (non-fatal): %s", role, exc)

    try:
        from mc_config import S3_BUCKET
        if S3_BUCKET:
            from mc_utils import s3_save_json
            key = f"eval/fixtures/live/{date}/{role}.json"
            s3_save_json(S3_BUCKET, key, payload, default=str, indent=2)
            logger.info("[eval] uploaded %s fixture -> s3://%s/%s", role, S3_BUCKET, key)
    except Exception as exc:
        logger.warning("[eval] failed to upload %s fixture to S3 (non-fatal): %s", role, exc)
