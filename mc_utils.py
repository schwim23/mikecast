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
from typing import Any

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
            if resp.status_code >= 500:
                wait = 2 ** attempt
                logger.warning("Server error %d on %s — waiting %ds", resp.status_code, url, wait)
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


# ---------------------------------------------------------------------------
# S3 helpers (used when S3_BUCKET env var is set)
# ---------------------------------------------------------------------------

def _get_s3():
    import boto3
    return boto3.client("s3")


def s3_load_json(bucket: str, key: str) -> Any | None:
    """Download and parse a JSON object from S3. Returns None if the key doesn't exist."""
    from botocore.exceptions import ClientError
    try:
        resp = _get_s3().get_object(Bucket=bucket, Key=key)
        return json.loads(resp["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def s3_save_json(bucket: str, key: str, data: Any, **json_kwargs) -> None:
    """Serialize data as JSON and upload to S3."""
    body = json.dumps(data, **json_kwargs).encode("utf-8")
    _get_s3().put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    logger.debug("s3://%s/%s written (%d bytes)", bucket, key, len(body))


def s3_upload_file(bucket: str, key: str, local_path: Path, content_type: str = "application/octet-stream") -> int:
    """Upload a local file to S3. Returns the file size in bytes."""
    size = local_path.stat().st_size
    with open(local_path, "rb") as fh:
        _get_s3().put_object(Bucket=bucket, Key=key, Body=fh, ContentType=content_type)
    logger.info("Uploaded s3://%s/%s (%d bytes)", bucket, key, size)
    return size


def s3_upload_text(bucket: str, key: str, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
    """Upload a string as a text object to S3."""
    body = text.encode("utf-8")
    _get_s3().put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    logger.debug("s3://%s/%s written (%d bytes)", bucket, key, len(body))


def s3_object_size(bucket: str, key: str) -> int:
    """Return the content-length of an S3 object, or 0 if it doesn't exist."""
    from botocore.exceptions import ClientError
    try:
        resp = _get_s3().head_object(Bucket=bucket, Key=key)
        return resp["ContentLength"]
    except ClientError:
        return 0


def s3_list_keys(bucket: str, prefix: str) -> list[str]:
    """Return all object keys under a given prefix."""
    paginator = _get_s3().get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys
