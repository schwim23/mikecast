"""
MikeCast — shared utility helpers.

Small, stateless functions used across multiple modules.
"""

import hashlib
import json
import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import requests

logger = logging.getLogger("mikecast")


def _safe_request(
    url: str,
    params: dict | None = None,
    timeout: int = 15,
    headers: dict | None = None,
) -> requests.Response | None:
    """
    GET a URL with up to 3 attempts and exponential back-off on 429s.
    Returns the Response on success, None if all attempts fail.
    """
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers=headers)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning("Rate-limited on %s — waiting %ds", url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            logger.warning("Request failed (attempt %d): %s", attempt + 1, exc)
            time.sleep(1)
    return None


def _atomic_write_json(path: Path, data, **json_kwargs) -> None:
    """
    Write JSON atomically: write to a .tmp file first, then rename.
    Prevents a partial/corrupt file if the process crashes mid-write.
    """
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, **json_kwargs)
        tmp.rename(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def title_similarity(a: str, b: str) -> float:
    """
    Return a 0–1 similarity ratio between two title strings.
    Strips punctuation and case-folds before comparing so minor
    formatting differences don't prevent a match.
    """
    a_clean = re.sub(r"[^a-z0-9 ]", "", a.lower().strip())
    b_clean = re.sub(r"[^a-z0-9 ]", "", b.lower().strip())
    return SequenceMatcher(None, a_clean, b_clean).ratio()


def url_fingerprint(url: str) -> str:
    """
    Normalise a URL to a stable MD5 fingerprint for deduplication.
    Strips scheme, trailing slash, and query/fragment components.
    """
    url = re.sub(r"https?://", "", url).rstrip("/").lower()
    url = re.sub(r"[?#].*", "", url)
    return hashlib.md5(url.encode()).hexdigest()
