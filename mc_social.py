#!/usr/bin/env python3
"""
mc_social.py — Daily social distribution for MikeCast (X + Instagram).

Posts a short summary of the day's briefing to X (Twitter) and a branded
1080×1080 image card to Instagram, each with a deep link back to that day's
episode (https://mikecast.io/?date=YYYY-MM-DD).

Design principles (match the rest of the pipeline):
  • Nothing here ever raises out to the daily pipeline — every channel is wrapped
    in its own try/except and missing credentials degrade to a logged skip.
  • First-run-of-day gating lives in mc_dist_state: `--force` regenerates copy /
    card without re-posting; only an explicit re-post path re-posts.
  • Copy comes from crew/distribution_crew (Claude); posting is deterministic here.
    A crew outage falls back to a deterministic template so posting still works.

Usage:
    python3 mc_social.py                          # today's episode
    python3 mc_social.py --date 2026-07-05        # a specific episode
    python3 mc_social.py --dry-run               # generate copy + card, post nothing
    python3 mc_social.py --only x                # X only (or --only ig)
    python3 mc_social.py --force                 # re-post even if already sent today
    python3 mc_social.py --media reel            # post a 9:16 video reel (default: card)
    python3 mc_social.py --delete-tweet 123456   # delete a tweet by id

The daily asset kind is "card" (static 1080×1080 image) or "reel" (9:16 video with
podcast audio + burned-in captions). A reel that can't be built falls back to the
card (Instagram) and a plain text+link tweet (X), so the daily run never breaks.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import textwrap
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from mc_config import (
    CLOUDFRONT_DIST_ID,
    DATA_DIR,
    IG_USER_ID,
    META_ACCESS_TOKEN,
    S3_BUCKET,
    SCRIPT_DIR,
    SITE_BASE_URL,
    SOCIAL_MEDIA_KIND,
    TODAY,
    X_ACCESS_TOKEN,
    X_ACCESS_TOKEN_SECRET,
    X_API_KEY,
    X_API_SECRET,
)
from mc_dist_state import (
    channel_sent,
    load_dist_state,
    record_send,
    record_social_copy,
)
from mc_utils import _safe_request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("mikecast.social")

# ---------------------------------------------------------------------------
# Brand + layout constants (card is 1080×1080; palette matches mc_ad.py)
# ---------------------------------------------------------------------------
CARD_W = CARD_H = 1080
ASSETS_DIR   = SCRIPT_DIR / "assets"
FONT_BOLD    = ASSETS_DIR / "fonts" / "InterDisplay-Bold.ttf"
FONT_REGULAR = ASSETS_DIR / "fonts" / "Inter-Medium.ttf"
COVER_PATH   = DATA_DIR / "cover.png"
COVER_URL    = f"{SITE_BASE_URL}data/cover.png"

COLOR_BG    = (10, 14, 32)
COLOR_CYAN  = (0, 191, 255)
COLOR_WHITE = (255, 255, 255)
COLOR_MUTED = (150, 165, 195)

# X counts every link as 23 characters (t.co wrapping), regardless of real length.
X_LINK_WEIGHT = 23
X_MAX = 280
IG_MAX = 2200  # hard IG cap; we target well under this
IG_MAX_HASHTAGS = 8

# A hashtag is '#' followed by word chars containing AT LEAST ONE letter or
# underscore — the letter may appear anywhere, not just first. Requiring a letter
# *somewhere* is what stops an inline episode reference like "Episode #134" from
# being scooped up as a hashtag (which used to strip the number out of the
# sentence, prepend "#134" to the tag block, and push a real hashtag off the end
# of the 8-tag cap). Requiring it in *first* position was too strict: it left
# digit-leading tags like #76ers / #49ers stranded mid-sentence with the
# whitespace of the tags removed around them, and absent from the tag block.
_HASHTAG_RE = re.compile(r"#(?=\w*[A-Za-z_])\w+")


def _is_hashtag(word: str) -> bool:
    """True if a whitespace-delimited token is a real hashtag (# + word chars
    including at least one letter/underscore). Trailing punctuation is fine —
    '#134.' is still not a hashtag because '.' isn't a word char."""
    return bool(_HASHTAG_RE.match(word))

# Spotify show link (secondary "listen" CTA on each tweet). Kept clean (no ?si=).
SPOTIFY_SHOW_URL = "https://open.spotify.com/show/3SEexX9wC3nr4xStYK2jOv"


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    if path.exists():
        return ImageFont.truetype(str(path), size)
    for fb in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(fb).exists():
            return ImageFont.truetype(fb, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Card image
# ---------------------------------------------------------------------------

def _load_cover() -> Image.Image | None:
    """
    Load the MikeCast cover art. Local data/cover.png first; on Fargate (where
    data/ is .dockerignore'd) fall back to fetching the public site copy. Returns
    None if neither is available — the card then uses the solid brand background.
    """
    if COVER_PATH.exists():
        try:
            return Image.open(COVER_PATH).convert("RGB")
        except Exception as exc:
            logger.warning("Could not open local cover %s: %s", COVER_PATH, exc)
    resp = _safe_request(COVER_URL)
    if resp is not None:
        try:
            from io import BytesIO
            return Image.open(BytesIO(resp.content)).convert("RGB")
        except Exception as exc:
            logger.warning("Could not decode remote cover %s: %s", COVER_URL, exc)
    logger.warning("No cover art available — using solid background.")
    return None


def _card_background() -> Image.Image:
    """Solid brand background, subtly tinted by a blurred cover if available."""
    base = Image.new("RGB", (CARD_W, CARD_H), COLOR_BG)
    cover = _load_cover()
    if cover is None:
        return base
    scale = max(CARD_W / cover.width, CARD_H / cover.height)
    nw, nh = int(cover.width * scale), int(cover.height * scale)
    cover = cover.resize((nw, nh), Image.LANCZOS)
    cx, cy = (nw - CARD_W) // 2, (nh - CARD_H) // 2
    cover = cover.crop((cx, cy, cx + CARD_W, cy + CARD_H))
    cover = cover.filter(ImageFilter.GaussianBlur(radius=22))
    return Image.blend(cover, base, alpha=0.72)


def _clean_title(title: str) -> str:
    # Match app.js: the "[Updated] " prefix is an internal marker, not display text.
    return re.sub(r"^\[Updated\]\s*", "", (title or "").strip())


def _top_headlines(episode_data: dict, cap: int = 3) -> list[str]:
    """
    Fallback card bullets: the day's *highest-scored* stories across every
    category (importance-ranked), used when the copywriter crew didn't supply
    curated ``card_bullets``.

    Previously this took ``arts[0]`` from each category in dict order, which is
    breadth-first and reads as arbitrary ("random what shows up"). Ranking by
    the scorer's ``score`` surfaces the actual top stories of the day instead.
    """
    articles = episode_data.get("articles", {}) or {}
    ranked: list[tuple[float, str]] = []
    for arts in articles.values():
        for a in arts or []:
            title = _clean_title(a.get("title", ""))
            if not title:
                continue
            try:
                score = float(a.get("score") or 0)
            except (TypeError, ValueError):
                score = 0.0
            ranked.append((score, title))
    ranked.sort(key=lambda t: t[0], reverse=True)
    # de-dupe titles while preserving the score order
    seen: set[str] = set()
    out: list[str] = []
    for _, title in ranked:
        if title not in seen:
            seen.add(title)
            out.append(title)
        if len(out) >= cap:
            break
    return out[:cap]


def generate_card(episode_data: dict, out_path: Path,
                  bullets: list[str] | None = None) -> Path:
    """
    Render a 1080×1080 branded summary card for Instagram and save to out_path.

    ``bullets`` are the (up to 3) headline lines to show. Pass the copywriter
    crew's curated ``card_bullets`` — the day's top-3 stories from the briefing
    summary. When omitted, fall back to the highest-scored stories of the day
    (``_top_headlines``) rather than one-per-category breadth.
    """
    img = _card_background().convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    episode_num = episode_data.get("episode_num", "?")
    date_display = episode_data.get("date_display", TODAY)

    # --- Wordmark ---
    f_brand = _font(84, bold=True)
    draw.text((80, 96), "MikeCast", font=f_brand, fill=COLOR_CYAN)

    # --- Episode + date line ---
    f_meta = _font(40)
    draw.text((84, 210), f"Daily Briefing · #{episode_num}", font=f_meta, fill=COLOR_WHITE)

    # --- Date badge (solid cyan pill, dark text for contrast) ---
    f_date = _font(34, bold=True)
    db = draw.textbbox((0, 0), date_display, font=f_date)
    dw, dh = db[2] - db[0] + 48, db[3] - db[1] + 26
    draw.rounded_rectangle([(84, 272), (84 + dw, 272 + dh)], radius=dh // 2, fill=COLOR_CYAN)
    draw.text((84 + dw // 2, 272 + dh // 2), date_display, font=f_date, fill=COLOR_BG, anchor="mm")

    # --- Divider ---
    y = 272 + dh + 56
    draw.line([(84, y), (CARD_W - 84, y)], fill=(*COLOR_MUTED, 90), width=2)
    y += 48

    # --- Top headlines ---
    card_lines = [_clean_title(b) for b in (bullets or []) if b and b.strip()][:3]
    if not card_lines:
        card_lines = _top_headlines(episode_data, cap=3)
    f_head = _font(46, bold=True)
    line_h = 58
    for headline in card_lines:
        wrapped = textwrap.wrap(headline, width=34)[:2]
        # cyan bullet
        draw.ellipse([(88, y + 20), (108, y + 40)], fill=COLOR_CYAN)
        for i, line in enumerate(wrapped):
            draw.text((132, y + i * line_h), line, font=f_head, fill=COLOR_WHITE)
        y += len(wrapped) * line_h + 34
        if y > CARD_H - 200:
            break

    # --- CTA footer bar ---
    bar_top = CARD_H - 132
    draw.rectangle([(0, bar_top), (CARD_W, CARD_H)], fill=(0, 0, 0, 150))
    f_cta = _font(44, bold=True)
    draw.text((CARD_W // 2, bar_top + 66), "Full briefing → mikecast.io",
              font=f_cta, fill=COLOR_CYAN, anchor="mm")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    logger.info("Card written: %s", out_path)
    return out_path


def upload_card(local_path: Path, date: str, suffix: str = "") -> str | None:
    """
    Upload the card to the site bucket and return its public URL. Returns None if
    S3 isn't configured (Instagram needs a publicly reachable image URL).

    The data/social/ prefix is invisible to the manifest/RSS regexes. `suffix`
    lets reposts use a versioned filename (e.g. "_r1") so IG re-fetches a new URL.
    """
    if not S3_BUCKET:
        logger.warning("S3_BUCKET not set — cannot upload card for a public URL.")
        return None
    from mc_utils import s3_upload_file
    key = f"data/social/MikeCast_card_{date}{suffix}.png"
    try:
        s3_upload_file(S3_BUCKET, key, local_path, content_type="image/png")
        return f"{SITE_BASE_URL}{key}"
    except Exception as exc:
        logger.error("Card upload failed: %s", exc)
        return None


def upload_reel(local_path: Path, date: str, suffix: str = "") -> str | None:
    """
    Upload the reel MP4 to the site bucket and return its public URL. Returns None
    if S3 isn't configured (Instagram Reels needs a publicly reachable video_url).

    Same data/social/ prefix as the card (invisible to the manifest/RSS regexes);
    `suffix` lets reposts use a versioned filename so IG re-fetches a fresh URL.
    """
    if not S3_BUCKET:
        logger.warning("S3_BUCKET not set — cannot upload reel for a public URL.")
        return None
    from mc_utils import s3_upload_file
    key = f"data/social/MikeCast_reel_{date}{suffix}.mp4"
    try:
        s3_upload_file(S3_BUCKET, key, local_path, content_type="video/mp4")
        return f"{SITE_BASE_URL}{key}"
    except Exception as exc:
        logger.error("Reel upload failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Copy fitting
# ---------------------------------------------------------------------------

def _truncate_words(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip() + "…"


def _weighted_len(s: str) -> int:
    """Length as X counts it: every URL weighs 23 chars (t.co), everything else 1."""
    w = len(s)
    for u in re.findall(r"https?://\S+", s):
        w += X_LINK_WEIGHT - len(u)
    return w


def _fit_x(text: str, link: str, date_display: str | None = None,
           spotify_link: str | None = SPOTIFY_SHOW_URL) -> str:
    """
    Build the daily tweet and fit it into 280 weighted chars:

        🎙️ MikeCast Daily · {date}

        {news hook}

        📰 {deep link}
        🎧 {spotify link}

    The header makes it obvious every post is the daily briefing; the two links
    each weigh 23, and the news hook is truncated at a word boundary to fit.
    """
    header = f"🎙️ MikeCast Daily · {date_display}" if date_display else "🎙️ MikeCast Daily"
    footer_lines = [f"📰 {link}"]
    if spotify_link:
        footer_lines.append(f"🎧 {spotify_link}")
    footer = "\n".join(footer_lines)

    body = text.strip()
    while body and _weighted_len(f"{header}\n\n{body}\n\n{footer}") > X_MAX:
        body = body.rstrip("… ")
        body = body[:body.rfind(" ")] if " " in body else ""
        if body:
            body += "…"

    return f"{header}\n\n{body}\n\n{footer}" if body else f"{header}\n\n{footer}"


def _fit_ig(caption: str) -> str:
    """Cap the IG caption length and trim to at most 8 hashtags."""
    caption = caption.strip()
    words = caption.split()
    hashtags = [w for w in words if _is_hashtag(w)]
    if len(hashtags) > IG_MAX_HASHTAGS:
        # drop the excess hashtags (keep the first 8, in order)
        kept = 0
        rebuilt = []
        for w in words:
            if _is_hashtag(w):
                if kept < IG_MAX_HASHTAGS:
                    rebuilt.append(w)
                    kept += 1
                # else: skip
            else:
                rebuilt.append(w)
        caption = " ".join(rebuilt)
    if len(caption) > IG_MAX - 50:
        caption = _truncate_words(caption, IG_MAX - 50)
    return caption


def _build_ig_caption(
    hook: str, date_display: str | None = None, headlines: list[str] | None = None,
) -> str:
    """
    Assemble the IG caption in the same shape as the tweet — a 'MikeCast Daily ·
    {date}' header, the news hook, a top-stories bullet list, then a CTA — with
    hashtags at the very end.

    ``headlines`` (the day's card_bullets) are the caption's own text, separate
    from the reel video itself — the IG counterpart to what the tweet body
    already does on X, and a skimmable stand-in for the static card's headline
    list now that the reel's burned-in captions are just the spoken cold-open
    rather than a highlights summary. IG's 2200-char caption cap has plenty of
    room for this (unlike X's 280, where the hook alone already fights for space).

    Instagram does NOT hyperlink URLs in captions, so the CTA points to the short,
    typeable domain (mikecast.io) and the bio ('link in bio') rather than a raw,
    unclickable Spotify URL. Set the IG bio links to mikecast.io + Spotify so the
    'link in bio' actually resolves.
    """
    hook = (hook or "").strip()
    tags = list(dict.fromkeys(_HASHTAG_RE.findall(hook)))          # dedup, keep order
    body = _HASHTAG_RE.sub("", hook)
    # drop any CTA/link the model may have added so we don't duplicate ours
    body = re.sub(r"(?im)^\s*(📰|🎧|🔗)?\s*(full briefing|listen on spotify|link in bio).*$", "", body)
    body = re.sub(r"(?i)\bfull briefing at mikecast\.io\.?", "", body)
    # Lifting tags out of the copy leaves their surrounding spaces behind — a run
    # of blanks mid-sentence, or a trailing-whitespace-only line where the tag
    # block used to be. Collapse both so the body reads clean.
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    header = f"🎙️ MikeCast Daily · {date_display}" if date_display else "🎙️ MikeCast Daily"
    headline_lines = [_clean_title(h) for h in (headlines or []) if h and h.strip()][:3]
    stories = "\n".join(f"• {h}" for h in headline_lines)
    if stories:
        stories = f"Today's top stories:\n{stories}"
    cta = "📰 Full briefing → mikecast.io\n🎧 Listen — link in bio"
    caption = "\n\n".join(p for p in (header, body, stories, cta) if p)
    if tags:
        caption += "\n\n" + " ".join(tags[:IG_MAX_HASHTAGS])
    return _fit_ig(caption)


def _fallback_copy(episode_data: dict) -> dict:
    """
    Deterministic copy used when the copywriter crew is unavailable. The X header
    ('MikeCast Daily · date') and links are added by _fit_x, so the body here is
    just the clean news hook — strip any leading 'Episode #N —' from the stored
    description so it doesn't read redundantly.
    """
    desc = (episode_data.get("episode_description") or "").strip()
    desc = re.sub(r"^Episode\s*#?\d+\s*[—:-]\s*", "", desc).strip()
    if not desc:
        desc = "Today's top stories across AI, tech, business, and NY sports."
    # header + CTA are added by _build_ig_caption; here we just supply the hook + tags
    return {
        "x_text": desc,
        "ig_caption": f"{desc}\n\n#MikeCast #AInews #tech #news",
        # score-ranked top stories so the card isn't blank / arbitrary on fallback
        "card_bullets": _top_headlines(episode_data, cap=3),
    }


# ---------------------------------------------------------------------------
# X (Twitter) API v2 — OAuth 1.0a user context
# ---------------------------------------------------------------------------

def _x_configured() -> bool:
    return all((X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET))


def _x_auth():
    from requests_oauthlib import OAuth1
    return OAuth1(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)


def post_to_x(text: str, media_ids: list[str] | None = None) -> str | None:
    """Post a tweet, optionally with attached media. Returns the tweet id on
    success, None on failure."""
    if not _x_configured():
        logger.info("X not configured — skipping tweet.")
        return None
    body: dict = {"text": text}
    if media_ids:
        body["media"] = {"media_ids": media_ids}
    try:
        resp = requests.post(
            "https://api.x.com/2/tweets",
            auth=_x_auth(),
            json=body,
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            logger.error("X post failed: %s %s", resp.status_code, resp.text[:400])
            return None
        tweet_id = resp.json().get("data", {}).get("id")
        logger.info("Tweet posted: id=%s%s", tweet_id, " (with media)" if media_ids else "")
        return tweet_id
    except Exception as exc:
        logger.error("X post raised: %s", exc)
        return None


# X media upload (chunked). Free tier caps INIT/FINALIZE at ~17/24h, so treat X
# video as best-effort: any failure returns None and the caller posts a plain
# text+link tweet instead. Uses the v1.1 chunked upload endpoint, which remains
# the OAuth1 path for attaching video to a v2 tweet.
_X_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
_X_CHUNK_BYTES = 4 * 1024 * 1024  # 4MB per APPEND (under the 5MB limit)


def upload_video_to_x(path: Path) -> str | None:
    """
    Upload an MP4 to X via chunked INIT/APPEND/FINALIZE(/STATUS) as
    media_category=amplify_video and return the media_id, or None on any failure
    (unconfigured, rate-limited, processing error). Never raises.
    """
    if not _x_configured():
        logger.info("X not configured — skipping video upload.")
        return None
    try:
        total_bytes = path.stat().st_size
        auth = _x_auth()

        # INIT
        init = requests.post(
            _X_UPLOAD_URL, auth=auth,
            data={
                "command": "INIT",
                "total_bytes": total_bytes,
                "media_type": "video/mp4",
                "media_category": "amplify_video",
            },
            timeout=60,
        )
        if init.status_code not in (200, 201, 202):
            logger.error("X video INIT failed: %s %s", init.status_code, init.text[:400])
            return None
        media_id = init.json().get("media_id_string")
        if not media_id:
            logger.error("X video INIT returned no media_id: %s", init.text[:400])
            return None

        # APPEND (chunked)
        with open(path, "rb") as fh:
            seg = 0
            while True:
                chunk = fh.read(_X_CHUNK_BYTES)
                if not chunk:
                    break
                ap = requests.post(
                    _X_UPLOAD_URL, auth=auth,
                    data={"command": "APPEND", "media_id": media_id, "segment_index": seg},
                    files={"media": chunk},
                    timeout=120,
                )
                if ap.status_code not in (200, 201, 204):
                    logger.error("X video APPEND seg %d failed: %s %s", seg, ap.status_code, ap.text[:300])
                    return None
                seg += 1

        # FINALIZE
        fin = requests.post(
            _X_UPLOAD_URL, auth=auth,
            data={"command": "FINALIZE", "media_id": media_id},
            timeout=60,
        )
        if fin.status_code not in (200, 201):
            logger.error("X video FINALIZE failed: %s %s", fin.status_code, fin.text[:400])
            return None

        # STATUS poll (if async transcoding is required)
        info = fin.json().get("processing_info")
        while info and info.get("state") in ("pending", "in_progress"):
            wait = int(info.get("check_after_secs", 5))
            time.sleep(min(wait, 15))
            st = requests.get(
                _X_UPLOAD_URL, auth=auth,
                params={"command": "STATUS", "media_id": media_id},
                timeout=30,
            )
            if st.status_code != 200:
                logger.error("X video STATUS failed: %s %s", st.status_code, st.text[:300])
                return None
            info = st.json().get("processing_info")
        if info and info.get("state") == "failed":
            logger.error("X video processing failed: %s", info)
            return None

        logger.info("X video uploaded: media_id=%s", media_id)
        return media_id
    except Exception as exc:
        logger.error("X video upload raised: %s", exc)
        return None


def delete_tweet(tweet_id: str) -> bool:
    """Delete a tweet by id. Returns True on success."""
    if not _x_configured():
        logger.info("X not configured — cannot delete tweet.")
        return False
    try:
        resp = requests.delete(
            f"https://api.x.com/2/tweets/{tweet_id}",
            auth=_x_auth(),
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error("Tweet delete failed: %s %s", resp.status_code, resp.text[:400])
            return False
        deleted = resp.json().get("data", {}).get("deleted", False)
        logger.info("Tweet %s deleted=%s", tweet_id, deleted)
        return bool(deleted)
    except Exception as exc:
        logger.error("Tweet delete raised: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Instagram Graph API (v21.0) — two-step container → publish
# ---------------------------------------------------------------------------
_GRAPH = "https://graph.facebook.com/v21.0"


def _ig_configured() -> bool:
    return bool(META_ACCESS_TOKEN and IG_USER_ID)


# Meta Graph intermittently returns transient failures on otherwise-valid requests
# — a 5xx, or an OAuthException with is_transient=true (e.g. code 2, "An unexpected
# error has occurred. Please retry your request later."). A single one used to drop
# the entire daily IG post, so retry the create/publish calls with backoff.
_IG_RETRY_DELAYS = (3, 8, 20)  # seconds between the 3 retries after the first try


def _ig_transient(resp: "requests.Response") -> bool:
    """True if a Graph response is a transient failure worth retrying."""
    if resp.status_code >= 500:
        return True
    try:
        err = resp.json().get("error", {})
    except Exception:
        return False
    return bool(err.get("is_transient")) or err.get("code") == 2


def _graph_post(url: str, data: dict, timeout: int = 60, label: str = "IG call"):
    """
    POST to the Graph API, retrying transient failures with backoff. Returns the
    final Response (which may be non-200 on a permanent error or after exhausting
    retries), or None if every attempt raised an exception.
    """
    attempts = len(_IG_RETRY_DELAYS) + 1
    last = None
    for i in range(attempts):
        try:
            last = requests.post(url, data=data, timeout=timeout)
        except Exception as exc:
            logger.warning("%s raised (attempt %d/%d): %s", label, i + 1, attempts, exc)
            last = None
        if last is not None:
            if last.status_code == 200:
                return last
            if not _ig_transient(last):
                return last  # permanent error — let the caller log and bail
            logger.warning("%s transient %s (attempt %d/%d): %s",
                           label, last.status_code, i + 1, attempts, last.text[:200])
        if i < attempts - 1:
            time.sleep(_IG_RETRY_DELAYS[i])
    return last


def post_to_instagram(image_url: str, caption: str) -> tuple[str | None, str | None]:
    """
    Publish a single image to Instagram. Returns (media_id, container_id).
    Both are None on failure. Requires a publicly reachable image_url.
    """
    if not _ig_configured():
        logger.info("Instagram not configured — skipping post.")
        return None, None
    try:
        # 1. Create media container (retries transient Graph failures)
        create = _graph_post(
            f"{_GRAPH}/{IG_USER_ID}/media",
            {"image_url": image_url, "caption": caption, "access_token": META_ACCESS_TOKEN},
            timeout=60, label="IG container create",
        )
        if create is None or create.status_code != 200:
            logger.error("IG container create failed: %s %s",
                         getattr(create, "status_code", "no-response"),
                         getattr(create, "text", "")[:400])
            return None, None
        container_id = create.json().get("id")
        if not container_id:
            logger.error("IG container create returned no id: %s", create.text[:400])
            return None, None

        # 2. Poll until the container finishes processing
        for attempt in range(12):
            status = requests.get(
                f"{_GRAPH}/{container_id}",
                params={"fields": "status_code", "access_token": META_ACCESS_TOKEN},
                timeout=30,
            )
            code = status.json().get("status_code") if status.status_code == 200 else None
            if code == "FINISHED":
                break
            if code == "ERROR":
                logger.error("IG container processing errored: %s", status.text[:400])
                return None, container_id
            time.sleep(3)
        else:
            logger.error("IG container %s never reached FINISHED — aborting publish.", container_id)
            return None, container_id

        # 3. Publish (retries transient Graph failures)
        publish = _graph_post(
            f"{_GRAPH}/{IG_USER_ID}/media_publish",
            {"creation_id": container_id, "access_token": META_ACCESS_TOKEN},
            timeout=60, label="IG publish",
        )
        if publish is None or publish.status_code != 200:
            logger.error("IG publish failed: %s %s",
                         getattr(publish, "status_code", "no-response"),
                         getattr(publish, "text", "")[:400])
            return None, container_id
        media_id = publish.json().get("id")
        logger.info("Instagram published: media_id=%s", media_id)
        return media_id, container_id
    except Exception as exc:
        logger.error("Instagram post raised: %s", exc)
        return None, None


def post_reel_to_instagram(video_url: str, caption: str) -> tuple[str | None, str | None]:
    """
    Publish a Reel to Instagram. Returns (media_id, container_id); both None on
    failure. Requires a publicly reachable video_url (9:16, 5–90s, faststart MP4).

    Same 3-step container→poll→publish flow as the image post, but with
    media_type=REELS and a longer poll loop — video transcoding is much slower
    than image processing.
    """
    if not _ig_configured():
        logger.info("Instagram not configured — skipping reel.")
        return None, None
    try:
        # 1. Create the REELS media container (retries transient Graph failures)
        create = _graph_post(
            f"{_GRAPH}/{IG_USER_ID}/media",
            {
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "share_to_feed": "true",
                "access_token": META_ACCESS_TOKEN,
            },
            timeout=60, label="IG reel container create",
        )
        if create is None or create.status_code != 200:
            logger.error("IG reel container create failed: %s %s",
                         getattr(create, "status_code", "no-response"),
                         getattr(create, "text", "")[:400])
            return None, None
        container_id = create.json().get("id")
        if not container_id:
            logger.error("IG reel container returned no id: %s", create.text[:400])
            return None, None

        # 2. Poll until the container finishes transcoding (video is slow → ~2 min)
        for attempt in range(24):
            status = requests.get(
                f"{_GRAPH}/{container_id}",
                params={"fields": "status_code", "access_token": META_ACCESS_TOKEN},
                timeout=30,
            )
            code = status.json().get("status_code") if status.status_code == 200 else None
            if code == "FINISHED":
                break
            if code == "ERROR":
                logger.error("IG reel container errored: %s", status.text[:400])
                return None, container_id
            time.sleep(5)
        else:
            logger.error("IG reel container %s never reached FINISHED — aborting publish.", container_id)
            return None, container_id

        # 3. Publish (retries transient Graph failures)
        publish = _graph_post(
            f"{_GRAPH}/{IG_USER_ID}/media_publish",
            {"creation_id": container_id, "access_token": META_ACCESS_TOKEN},
            timeout=60, label="IG reel publish",
        )
        if publish is None or publish.status_code != 200:
            logger.error("IG reel publish failed: %s %s",
                         getattr(publish, "status_code", "no-response"),
                         getattr(publish, "text", "")[:400])
            return None, container_id
        media_id = publish.json().get("id")
        logger.info("Instagram Reel published: media_id=%s", media_id)
        return media_id, container_id
    except Exception as exc:
        logger.error("Instagram reel post raised: %s", exc)
        return None, None


# ---------------------------------------------------------------------------
# Episode loading + deep link
# ---------------------------------------------------------------------------

def deep_link(date: str) -> str:
    return f"{SITE_BASE_URL}?date={date}"


def load_episode(date: str) -> dict | None:
    """Load an episode's JSON — local file first, then S3."""
    local = DATA_DIR / f"{date}.json"
    if local.exists():
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not parse local episode %s: %s", local, exc)
    if S3_BUCKET:
        from mc_utils import s3_load_json
        try:
            return s3_load_json(S3_BUCKET, f"data/{date}.json")
        except Exception as exc:
            logger.warning("Could not load episode from S3 for %s: %s", date, exc)
    return None


def _resolve_copy(date: str, episode_data: dict, link: str, force: bool) -> dict:
    """
    Return {"x_text", "ig_caption"}. Reuses persisted social_copy unless --force,
    otherwise generates via the crew (persisting before posting) and falls back to
    a deterministic template on crew failure.
    """
    state = load_dist_state(date)
    existing = state.get("social_copy")
    if existing and not force and existing.get("x_text"):
        logger.info("Reusing persisted social copy for %s.", date)
        return {
            "x_text": existing["x_text"],
            "ig_caption": existing.get("ig_caption", ""),
            # older persisted copy predates card_bullets — fall back to ranked stories
            "card_bullets": existing.get("card_bullets") or _top_headlines(episode_data, cap=3),
        }

    try:
        from crew.distribution_crew import run_distribution
        copy = run_distribution(episode_data, link)
    except Exception as exc:
        logger.warning("Copywriter crew failed (%s) — using fallback template.", exc)
        copy = _fallback_copy(episode_data)

    copy.setdefault("card_bullets", _top_headlines(episode_data, cap=3))
    record_social_copy(date, copy["x_text"], copy["ig_caption"], copy.get("card_bullets"))
    return copy


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _maybe_build_reel(date: str, episode_data: dict, dry_run: bool, headlines: list[str] | None = None):
    """
    Build the day's reel once (shared by X + IG) and upload it for IG's public URL.
    Returns (reel_path, reel_url). Either may be None: a None reel_path means the
    reel couldn't be built (missing ElevenLabs creds, no script, render error) and
    every channel falls back to card/text. reel_url is None in dry-run (nothing is
    uploaded) — the local reel_path is still returned for visual inspection.

    ``headlines`` (the day's card_bullets) puts a "Today's Top Stories" title
    slate ahead of the cold-open, same data the static card used to show.
    """
    from mc_video import build_daily_reel
    reel_path = build_daily_reel(
        episode_data, DATA_DIR / f"MikeCast_reel_{date}.mp4", headlines=headlines,
    )
    if not reel_path:
        logger.warning("Reel build failed for %s — falling back to card/text.", date)
        return None, None
    reel_url = None if dry_run else upload_reel(reel_path, date)
    return reel_path, reel_url


def run_social_distribution(
    date: str,
    dry_run: bool = False,
    force: bool = False,
    only: str | None = None,
    media: str | None = None,
) -> dict:
    """
    Post the day's briefing to X and Instagram. Each channel is independent and
    self-contained: a failure or missing-credential skip in one never affects the
    other, and nothing here raises out to the caller.

    ``media`` selects the daily asset: "card" (static 1080×1080 image) or "reel"
    (9:16 video with podcast audio + burned-in captions). Defaults to
    SOCIAL_MEDIA_KIND. A reel that can't be built falls back to the card (IG) and
    a plain text+link tweet (X), so the daily run never breaks.

    Gating: a channel already recorded as sent for `date` is skipped unless
    `force`. `dry_run` generates copy + asset and prints them, posting nothing.

    Returns a per-channel result summary dict.
    """
    media = (media or SOCIAL_MEDIA_KIND or "card").strip().lower()
    if media not in ("card", "reel"):
        logger.warning("Unknown media kind %r — defaulting to card.", media)
        media = "card"
    results: dict = {"date": date, "media": media, "x": "skip", "instagram": "skip"}

    episode_data = load_episode(date)
    if not episode_data:
        logger.error("No episode data for %s — cannot distribute.", date)
        results["error"] = "no_episode"
        return results

    link = deep_link(date)

    # Copy (shared by both channels)
    copy = _resolve_copy(date, episode_data, link, force=force)
    x_text = _fit_x(copy["x_text"], link, date_display=episode_data.get("date_display"))
    ig_caption = _build_ig_caption(
        copy["ig_caption"],
        date_display=episode_data.get("date_display"),
        headlines=copy.get("card_bullets"),
    )

    want_x = only in (None, "x")
    want_ig = only in (None, "ig")

    # --- Reel asset (built once, shared by X + IG) ---
    reel_path = reel_url = None
    if media == "reel" and (want_x or want_ig):
        state = load_dist_state(date)
        # Only spend TTS/render if a channel actually still needs to post.
        need = dry_run or force or (
            (want_x and not channel_sent(state, "x")) or
            (want_ig and not channel_sent(state, "instagram"))
        )
        if need:
            try:
                reel_path, reel_url = _maybe_build_reel(
                    date, episode_data, dry_run, headlines=copy.get("card_bullets"),
                )
            except Exception as exc:
                logger.error("Reel prep raised (non-fatal): %s", exc)
        if reel_path is None:
            media = "card"  # hard fallback for the rest of this run
            results["media"] = "card (reel-fallback)"

    # --- X ---
    if want_x:
        try:
            if not dry_run and not force and channel_sent(load_dist_state(date), "x"):
                logger.info("X already sent for %s — skipping (use --force).", date)
                results["x"] = "already_sent"
            elif dry_run:
                logger.info("[dry-run] X text (%d chars):\n%s", len(x_text), x_text)
                if reel_path:
                    logger.info("[dry-run] reel saved for visual check: %s", reel_path)
                results["x"] = "dry_run"
            else:
                media_ids = None
                if reel_path:
                    vid = upload_video_to_x(reel_path)
                    if vid:
                        media_ids = [vid]
                    else:
                        logger.info("X video upload unavailable — posting text+link only.")
                tweet_id = post_to_x(x_text, media_ids=media_ids)
                if tweet_id:
                    record_send(date, "x", {
                        "tweet_id": tweet_id, "text": x_text,
                        "media": "reel" if media_ids else "text",
                    })
                    results["x"] = f"posted:{tweet_id}" + ("+video" if media_ids else "")
                else:
                    results["x"] = "not_configured_or_failed"
        except Exception as exc:
            logger.error("X channel raised (non-fatal): %s", exc)
            results["x"] = "error"

    # --- Instagram (Reel via public video URL, else card image) ---
    if want_ig:
        try:
            if not dry_run and not force and channel_sent(load_dist_state(date), "instagram"):
                logger.info("Instagram already sent for %s — skipping (use --force).", date)
                results["instagram"] = "already_sent"
            elif reel_path and (reel_url or dry_run):
                # Reel path
                if dry_run:
                    logger.info("[dry-run] IG caption (%d chars):\n%s", len(ig_caption), ig_caption)
                    logger.info("[dry-run] reel saved for visual check: %s", reel_path)
                    results["instagram"] = "dry_run"
                else:
                    media_id, container_id = post_reel_to_instagram(reel_url, ig_caption)
                    if media_id:
                        record_send(date, "instagram", {
                            "media_id": media_id, "container_id": container_id,
                            "video_url": reel_url, "caption": ig_caption, "media": "reel",
                        })
                        results["instagram"] = f"posted:{media_id}+reel"
                    else:
                        results["instagram"] = "not_configured_or_failed"
            else:
                # Card path (media=="card" or reel/url unavailable)
                card_path = DATA_DIR / f"MikeCast_card_{date}.png"
                generate_card(episode_data, card_path, bullets=copy.get("card_bullets"))
                if dry_run:
                    logger.info("[dry-run] IG caption (%d chars):\n%s", len(ig_caption), ig_caption)
                    logger.info("[dry-run] card saved for visual check: %s", card_path)
                    results["instagram"] = "dry_run"
                else:
                    image_url = upload_card(card_path, date)
                    if not image_url:
                        results["instagram"] = "no_public_url"
                    else:
                        media_id, container_id = post_to_instagram(image_url, ig_caption)
                        if media_id:
                            record_send(date, "instagram", {
                                "media_id": media_id, "container_id": container_id,
                                "image_url": image_url, "caption": ig_caption, "media": "card",
                            })
                            results["instagram"] = f"posted:{media_id}"
                        else:
                            results["instagram"] = "not_configured_or_failed"
        except Exception as exc:
            logger.error("Instagram channel raised (non-fatal): %s", exc)
            results["instagram"] = "error"

    logger.info("Social distribution result: %s", results)
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post the MikeCast daily briefing to X and Instagram",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--date", default=TODAY, help="Episode date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Generate copy + card; post nothing")
    parser.add_argument("--only", choices=["x", "ig"], help="Post to only one channel")
    parser.add_argument("--force", action="store_true", help="Re-post even if already sent today")
    parser.add_argument("--media", choices=["card", "reel"], default=None,
                        help="Daily asset kind (default: SOCIAL_MEDIA_KIND env, else card)")
    parser.add_argument("--delete-tweet", metavar="ID", help="Delete a tweet by id and exit")
    args = parser.parse_args()

    if args.delete_tweet:
        ok = delete_tweet(args.delete_tweet)
        sys.exit(0 if ok else 1)

    run_social_distribution(args.date, dry_run=args.dry_run, force=args.force,
                            only=args.only, media=args.media)


if __name__ == "__main__":
    main()
