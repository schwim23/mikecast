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
    python3 mc_social.py --delete-tweet 123456   # delete a tweet by id
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

# A hashtag is '#' followed by a LETTER or underscore, then word chars. The
# letter-leading requirement is deliberate: it stops an inline episode reference
# like "Episode #134" from being scooped up as a hashtag (which used to strip the
# number out of the sentence, prepend "#134" to the tag block, and push a real
# hashtag off the end of the 8-tag cap).
_HASHTAG_RE = re.compile(r"#[A-Za-z_]\w*")


def _is_hashtag(word: str) -> bool:
    """True if a whitespace-delimited token is a real hashtag (# + letter/underscore)."""
    return bool(re.match(r"#[A-Za-z_]", word))

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


def _build_ig_caption(hook: str, date_display: str | None = None) -> str:
    """
    Assemble the IG caption in the same shape as the tweet — a 'MikeCast Daily ·
    {date}' header, the news hook, then a CTA — with hashtags at the very end.

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
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    header = f"🎙️ MikeCast Daily · {date_display}" if date_display else "🎙️ MikeCast Daily"
    cta = "📰 Full briefing → mikecast.io\n🎧 Listen — link in bio"
    caption = "\n\n".join(p for p in (header, body, cta) if p)
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


def post_to_x(text: str) -> str | None:
    """Post a tweet. Returns the tweet id on success, None on failure."""
    if not _x_configured():
        logger.info("X not configured — skipping tweet.")
        return None
    try:
        resp = requests.post(
            "https://api.x.com/2/tweets",
            auth=_x_auth(),
            json={"text": text},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            logger.error("X post failed: %s %s", resp.status_code, resp.text[:400])
            return None
        tweet_id = resp.json().get("data", {}).get("id")
        logger.info("Tweet posted: id=%s", tweet_id)
        return tweet_id
    except Exception as exc:
        logger.error("X post raised: %s", exc)
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


def post_to_instagram(image_url: str, caption: str) -> tuple[str | None, str | None]:
    """
    Publish a single image to Instagram. Returns (media_id, container_id).
    Both are None on failure. Requires a publicly reachable image_url.
    """
    if not _ig_configured():
        logger.info("Instagram not configured — skipping post.")
        return None, None
    try:
        # 1. Create media container
        create = requests.post(
            f"{_GRAPH}/{IG_USER_ID}/media",
            data={"image_url": image_url, "caption": caption, "access_token": META_ACCESS_TOKEN},
            timeout=60,
        )
        if create.status_code != 200:
            logger.error("IG container create failed: %s %s", create.status_code, create.text[:400])
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

        # 3. Publish
        publish = requests.post(
            f"{_GRAPH}/{IG_USER_ID}/media_publish",
            data={"creation_id": container_id, "access_token": META_ACCESS_TOKEN},
            timeout=60,
        )
        if publish.status_code != 200:
            logger.error("IG publish failed: %s %s", publish.status_code, publish.text[:400])
            return None, container_id
        media_id = publish.json().get("id")
        logger.info("Instagram published: media_id=%s", media_id)
        return media_id, container_id
    except Exception as exc:
        logger.error("Instagram post raised: %s", exc)
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

def run_social_distribution(
    date: str,
    dry_run: bool = False,
    force: bool = False,
    only: str | None = None,
) -> dict:
    """
    Post the day's briefing to X and Instagram. Each channel is independent and
    self-contained: a failure or missing-credential skip in one never affects the
    other, and nothing here raises out to the caller.

    Gating: a channel already recorded as sent for `date` is skipped unless
    `force`. `dry_run` generates copy + card and prints them, posting nothing.

    Returns a per-channel result summary dict.
    """
    results: dict = {"date": date, "x": "skip", "instagram": "skip"}

    episode_data = load_episode(date)
    if not episode_data:
        logger.error("No episode data for %s — cannot distribute.", date)
        results["error"] = "no_episode"
        return results

    link = deep_link(date)

    # Copy (shared by both channels)
    copy = _resolve_copy(date, episode_data, link, force=force)
    x_text = _fit_x(copy["x_text"], link, date_display=episode_data.get("date_display"))
    ig_caption = _build_ig_caption(copy["ig_caption"], date_display=episode_data.get("date_display"))

    want_x = only in (None, "x")
    want_ig = only in (None, "ig")

    # --- X ---
    if want_x:
        try:
            if not dry_run and not force and channel_sent(load_dist_state(date), "x"):
                logger.info("X already sent for %s — skipping (use --force).", date)
                results["x"] = "already_sent"
            elif dry_run:
                logger.info("[dry-run] X text (%d chars):\n%s", len(x_text), x_text)
                results["x"] = "dry_run"
            else:
                tweet_id = post_to_x(x_text)
                if tweet_id:
                    record_send(date, "x", {"tweet_id": tweet_id, "text": x_text})
                    results["x"] = f"posted:{tweet_id}"
                else:
                    results["x"] = "not_configured_or_failed"
        except Exception as exc:
            logger.error("X channel raised (non-fatal): %s", exc)
            results["x"] = "error"

    # --- Instagram (needs a card image + public URL) ---
    if want_ig:
        try:
            if not dry_run and not force and channel_sent(load_dist_state(date), "instagram"):
                logger.info("Instagram already sent for %s — skipping (use --force).", date)
                results["instagram"] = "already_sent"
            else:
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
                                "media_id": media_id,
                                "container_id": container_id,
                                "image_url": image_url,
                                "caption": ig_caption,
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
    parser.add_argument("--delete-tweet", metavar="ID", help="Delete a tweet by id and exit")
    args = parser.parse_args()

    if args.delete_tweet:
        ok = delete_tweet(args.delete_tweet)
        sys.exit(0 if ok else 1)

    run_social_distribution(args.date, dry_run=args.dry_run, force=args.force, only=args.only)


if __name__ == "__main__":
    main()
