"""
MikeCast — per-date distribution state.

Tracks which delivery channels (personal email, newsletter broadcast, X, Instagram)
have already fired for a given briefing date, so that `--force` reruns of the daily
pipeline regenerate content WITHOUT re-emailing subscribers or re-posting to social.

State lives at `data/dist/YYYY-MM-DD.json`. In S3 mode (S3_BUCKET set) S3 is the
authoritative store — a Fargate task and a local run share the same state — and a
local mirror is also written under DATA_DIR/dist/ for offline inspection. When
S3_BUCKET is unset, only the local file is used.

Schema (all channel keys optional; absent = not yet sent):

    {
      "date": "2026-07-05",
      "episode_num": 123,
      "personal_email": {"sent_at": "..."},
      "newsletter":     {"broadcast_id": "...", "sent_at": "..."},
      "x":              {"tweet_id": "...", "text": "...", "sent_at": "..."},
      "instagram":      {"media_id": "...", "container_id": "...",
                         "image_url": "...", "caption": "...", "sent_at": "..."},
      "social_copy":    {"x_text": "...", "ig_caption": "...", "generated_at": "..."},
      "repost_history": [{"channel": "x", "old_id": "...", "new_id": "...", "deleted_at": "..."}]
    }

`social_copy` is persisted *before* posting so a crash mid-post never loses the
generated copy — mc_edit.py --reuse-copy and a rerun both reuse it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from mc_config import DATA_DIR, S3_BUCKET
from mc_utils import _atomic_write_json

logger = logging.getLogger("mikecast")

_DIST_DIR = DATA_DIR / "dist"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s3_key(date: str) -> str:
    # NOTE: this prefix (data/dist/) is intentionally invisible to the manifest /
    # RSS / episode-number regexes in mc_deliver.py, which match only
    # r"data/\d{4}-\d{2}-\d{2}\.json$". Keep it that way.
    return f"data/dist/{date}.json"


def _local_path(date: str) -> Path:
    return _DIST_DIR / f"{date}.json"


def load_dist_state(date: str) -> dict:
    """
    Load the distribution state for a date. Returns a fresh skeleton dict when no
    state exists yet. S3 is authoritative in S3 mode; falls back to the local file.
    """
    if S3_BUCKET:
        from mc_utils import s3_load_json
        try:
            data = s3_load_json(S3_BUCKET, _s3_key(date))
        except Exception as exc:
            logger.warning("Could not load dist state from S3 for %s (%s) — falling back to local.", date, exc)
            data = None
        if data:
            return data

    local = _local_path(date)
    if local.exists():
        try:
            import json
            return json.loads(local.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not parse local dist state %s: %s", local, exc)

    return {"date": date}


def save_dist_state(date: str, state: dict) -> None:
    """Persist the distribution state locally and (in S3 mode) to S3."""
    state.setdefault("date", date)

    _DIST_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_write_json(_local_path(date), state, indent=2, ensure_ascii=False)
    except Exception as exc:
        logger.warning("Could not write local dist state for %s: %s", date, exc)

    if S3_BUCKET:
        from mc_utils import s3_save_json
        try:
            s3_save_json(S3_BUCKET, _s3_key(date), state, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Could not write dist state to S3 for %s: %s", date, exc)


def channel_sent(state: dict, channel: str) -> bool:
    """True if the given channel has already been sent (has a sent_at timestamp)."""
    entry = state.get(channel)
    return bool(entry and entry.get("sent_at"))


def record_send(date: str, channel: str, payload: dict) -> dict:
    """
    Mark a channel as sent: merge `payload` into state[channel], stamp sent_at,
    persist, and return the updated state. Reloads first so concurrent channel
    writes in the same run don't clobber each other.
    """
    state = load_dist_state(date)
    entry = dict(payload or {})
    entry["sent_at"] = _now_iso()
    state[channel] = entry
    save_dist_state(date, state)
    return state


def record_social_copy(date: str, x_text: str, ig_caption: str) -> dict:
    """
    Persist generated social copy BEFORE posting (crash-safe / reusable). Returns
    the updated state.
    """
    state = load_dist_state(date)
    state["social_copy"] = {
        "x_text": x_text,
        "ig_caption": ig_caption,
        "generated_at": _now_iso(),
    }
    save_dist_state(date, state)
    return state


def append_repost(date: str, channel: str, old_id: str | None, new_id: str | None) -> dict:
    """Append a repost record to repost_history and persist. Returns updated state."""
    state = load_dist_state(date)
    history = state.setdefault("repost_history", [])
    history.append({
        "channel": channel,
        "old_id": old_id,
        "new_id": new_id,
        "deleted_at": _now_iso(),
    })
    save_dist_state(date, state)
    return state
