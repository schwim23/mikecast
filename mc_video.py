#!/usr/bin/env python3
"""
mc_video.py — Shared vertical-video renderer for MikeCast (Reels / ads).

This is the render engine behind two callers:
  • mc_ad.py       — the standalone 30s promo CLI (fresh GPT ad script).
  • mc_social.py   — the daily Instagram Reel / X video (cold-open of the real
                     episode's conversational_script), via `build_daily_reel`.

Design principles (match the rest of the pipeline):
  • Nothing here is wired to raise out to the daily pipeline — `build_daily_reel`
    returns None on any problem (missing creds, TTS failure, render error) so the
    caller degrades to the static card.
  • Captions are burned in and synced *by construction*: one image-clip per caption
    cue, each held for its share of the segment's real TTS audio duration. No
    Whisper / forced alignment needed.
  • Output is IG-safe: H.264 + AAC, MP4, yuv420p, `-movflags +faststart` (moov atom
    front-loaded — Instagram rejects files without it).

The cover art lives under data/ which is .dockerignore'd, so on Fargate the
background loader falls back to fetching the public site copy (same trick as
mc_social._load_cover).
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import textwrap
import time
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from mc_config import (
    DATA_DIR,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ELIZABETH,
    ELEVENLABS_VOICE_JESSE,
    ELEVENLABS_VOICE_MIKE,
    SCRIPT_DIR,
    SITE_BASE_URL,
    TODAY,
)

logger = logging.getLogger("mikecast.video")

# ---------------------------------------------------------------------------
# Video constants
# ---------------------------------------------------------------------------
VIDEO_W, VIDEO_H = 1080, 1920
FPS = 30

# IG Reels-tab eligibility is 9:16 + 5–90s. Keep the daily reel comfortably inside.
REEL_TARGET_SECS = 75
REEL_MAX_SECS = 88          # hard cap (leave headroom under IG's 90s)
REEL_MAX_SEGMENTS = 7       # cap TTS calls / cost per reel

COVER_PATH   = DATA_DIR / "cover.png"
COVER_URL    = f"{SITE_BASE_URL}data/cover.png"
ASSETS_DIR   = SCRIPT_DIR / "assets"
FONT_BOLD    = ASSETS_DIR / "fonts" / "InterDisplay-Bold.ttf"
FONT_REGULAR = ASSETS_DIR / "fonts" / "Inter-Medium.ttf"

# Brand palette (mirrors mc_ad / mc_social)
COLOR_BG    = (10, 14, 32)
COLOR_CYAN  = (0, 191, 255)
COLOR_WHITE = (255, 255, 255)
COLOR_DARK  = (15, 20, 45)

SPEAKER_COLORS = {
    "MIKE":      (0, 191, 255),    # cyan
    "ELIZABETH": (176, 102, 255),  # purple
    "JESSE":     (255, 133, 51),   # orange
}
SPEAKER_LABELS = {
    "MIKE":      "MIKE  ·  Host",
    "ELIZABETH": "ELIZABETH  ·  Tech & Markets",
    "JESSE":     "JESSE  ·  Sports",
}


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    if path.exists():
        return ImageFont.truetype(str(path), size)
    fallbacks = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold
        else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    )
    for fb in fallbacks:
        if Path(fb).exists():
            return ImageFont.truetype(fb, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Cover art — local first, public site copy as Fargate fallback
# ---------------------------------------------------------------------------

def _load_cover() -> Image.Image | None:
    """Load the MikeCast cover art. data/cover.png locally; fetch the public site
    copy on Fargate (where data/ is .dockerignore'd). None if neither works."""
    if COVER_PATH.exists():
        try:
            return Image.open(COVER_PATH).convert("RGB")
        except Exception as exc:
            logger.warning("Could not open local cover %s: %s", COVER_PATH, exc)
    from mc_utils import _safe_request
    resp = _safe_request(COVER_URL)
    if resp is not None:
        try:
            return Image.open(BytesIO(resp.content)).convert("RGB")
        except Exception as exc:
            logger.warning("Could not decode remote cover %s: %s", COVER_URL, exc)
    logger.warning("No cover art available — using solid background.")
    return None


def _build_background() -> Image.Image:
    """1080×1920 blurred-brand background; solid brand color if no cover."""
    base = Image.new("RGB", (VIDEO_W, VIDEO_H), COLOR_BG)
    cover = _load_cover()
    if cover is None:
        return base
    scale = max(VIDEO_W / cover.width, VIDEO_H / cover.height)
    nw, nh = int(cover.width * scale), int(cover.height * scale)
    cover = cover.resize((nw, nh), Image.LANCZOS)
    cx, cy = (nw - VIDEO_W) // 2, (nh - VIDEO_H) // 2
    cover = cover.crop((cx, cy, cx + VIDEO_W, cy + VIDEO_H))
    cover = cover.filter(ImageFilter.GaussianBlur(radius=10))
    return Image.blend(cover, base, alpha=0.60)


def _build_logo(size: int = 680) -> Image.Image | None:
    """Clean RGBA square cover logo for overlaying, or None if unavailable."""
    cover = _load_cover()
    if cover is None:
        return None
    return cover.convert("RGBA").resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Sound-wave visual (static bars, colored per speaker)
# ---------------------------------------------------------------------------
_WAVE_HEIGHTS = [35, 55, 85, 65, 100, 75, 45, 90, 60, 80, 100, 70, 40, 55, 35]
_BAR_W, _BAR_GAP = 10, 10


def _draw_wave(draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple) -> None:
    total_w = len(_WAVE_HEIGHTS) * (_BAR_W + _BAR_GAP) - _BAR_GAP
    x0 = cx - total_w // 2
    for i, bh in enumerate(_WAVE_HEIGHTS):
        bx = x0 + i * (_BAR_W + _BAR_GAP)
        draw.rounded_rectangle(
            [(bx, cy - bh // 2), (bx + _BAR_W, cy + bh // 2)],
            radius=_BAR_W // 2,
            fill=(*color, 230),
        )


# ---------------------------------------------------------------------------
# Shared frame elements (logo, date badge, CTA bar) — used by both the per-cue
# caption frame and the headline slate frame.
# ---------------------------------------------------------------------------

def _draw_logo(img: Image.Image, logo: Image.Image | None, logo_y: int = 100) -> int:
    """Paste the centered cover logo at ``logo_y``; returns its bottom y."""
    if logo is not None:
        lx = (VIDEO_W - logo.width) // 2
        img.paste(logo, (lx, logo_y), logo)
        return logo_y + logo.height
    return logo_y


def _draw_date_badge(draw: ImageDraw.ImageDraw, top_y: int, date_str: str) -> int:
    """Draw the centered date pill starting at ``top_y``; returns its bottom y."""
    f_date = _font(36)
    bbox   = draw.textbbox((0, 0), date_str, font=f_date)
    dw, dh = bbox[2] - bbox[0] + 50, bbox[3] - bbox[1] + 22
    draw.rounded_rectangle(
        [(VIDEO_W // 2 - dw // 2, top_y), (VIDEO_W // 2 + dw // 2, top_y + dh)],
        radius=dh // 2, fill=(0, 0, 0, 160),
    )
    draw.text((VIDEO_W // 2, top_y + dh // 2), date_str,
              font=f_date, fill=COLOR_CYAN, anchor="mm")
    return top_y + dh


def _draw_cta_bar(draw: ImageDraw.ImageDraw) -> None:
    """Draw the fixed bottom brand/CTA bar shared by every frame."""
    cta_top = VIDEO_H - 175
    draw.rectangle([(0, cta_top), (VIDEO_W, VIDEO_H)], fill=(0, 0, 0, 220))
    draw.text((VIDEO_W // 2, cta_top + 55), "MikeCast Daily Briefing",
              font=_font(46, bold=True), fill=COLOR_CYAN, anchor="mm")
    draw.text((VIDEO_W // 2, cta_top + 118), "Full episode → mikecast.io",
              font=_font(36), fill=(180, 190, 210), anchor="mm")


# ---------------------------------------------------------------------------
# Frame renderer (one composed RGB frame for a single caption cue)
# ---------------------------------------------------------------------------

def render_frame(
    bg: Image.Image,
    logo: Image.Image | None,
    speaker: str,
    text: str,
    date_str: str,
) -> np.ndarray:
    """Compose one RGBA frame → RGB numpy array (H×W×3) for moviepy."""
    img  = bg.copy().convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")
    color = SPEAKER_COLORS.get(speaker, COLOR_CYAN)

    logo_bottom = _draw_logo(img, logo)
    date_bottom = _draw_date_badge(draw, logo_bottom + 30, date_str)

    # --- Sound wave ---
    wave_y = date_bottom + 70
    _draw_wave(draw, VIDEO_W // 2, wave_y, color)

    # --- Speaker pill ---
    pill_y = wave_y + 80
    f_pill = _font(40, bold=True)
    label  = SPEAKER_LABELS.get(speaker, speaker)
    lb     = draw.textbbox((0, 0), label, font=f_pill)
    pw, ph = lb[2] - lb[0] + 64, lb[3] - lb[1] + 28
    draw.rounded_rectangle(
        [(VIDEO_W // 2 - pw // 2, pill_y), (VIDEO_W // 2 + pw // 2, pill_y + ph)],
        radius=ph // 2, fill=(*color, 230),
    )
    draw.text((VIDEO_W // 2, pill_y + ph // 2), label,
              font=f_pill, fill=COLOR_BG, anchor="mm")

    # --- Subtitle box (big, high-contrast, muted-proof) ---
    sub_top = pill_y + ph + 45
    f_sub   = _font(56, bold=True)
    lines   = textwrap.wrap(text, width=24)[:3]  # a cue is short → ≤3 lines
    line_h  = 74
    block_h = len(lines) * line_h
    pad     = 38
    draw.rounded_rectangle(
        [(60, sub_top - pad), (VIDEO_W - 60, sub_top + block_h + pad)],
        radius=28, fill=(0, 0, 0, 190),
    )
    for i, line in enumerate(lines):
        draw.text((VIDEO_W // 2, sub_top + i * line_h + line_h // 2),
                  line, font=f_sub, fill=COLOR_WHITE, anchor="mm")

    _draw_cta_bar(draw)
    return np.array(img.convert("RGB"))


# ---------------------------------------------------------------------------
# Headline slate — a static "Today's Top Stories" opening frame for the reel,
# so the day's highlights are visible even muted / at a glance, the way the
# static card used to show them before reels replaced it as the default.
# ---------------------------------------------------------------------------

SLATE_SECS = 3.5  # how long the slate holds before the captioned cold-open begins


def _truncate_words(text: str, limit: int) -> str:
    """Word-boundary truncate with an ellipsis, so a long headline never gets
    silently cut mid-word/mid-thought inside the slate's fixed 2-line bullets."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip() + "…"


def render_slate_frame(
    bg: Image.Image,
    logo: Image.Image | None,
    headlines: list[str],
    date_str: str,
) -> np.ndarray:
    """Compose the opening 'Today's Top Stories' slate → RGB numpy array."""
    img  = bg.copy().convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")

    logo_bottom = _draw_logo(img, logo)

    # --- Title ---
    title_y = logo_bottom + 60
    draw.text((VIDEO_W // 2, title_y), "TODAY'S TOP STORIES",
              font=_font(52, bold=True), fill=COLOR_CYAN, anchor="mm")

    date_bottom = _draw_date_badge(draw, title_y + 45, date_str)

    # --- Headline bullets ---
    f_head  = _font(50, bold=True)
    line_h  = 64
    wrap_width, max_lines = 28, 2
    trimmed = [_truncate_words(h.strip(), wrap_width * max_lines) for h in headlines[:3] if h and h.strip()]
    wrapped = [textwrap.wrap(h, width=wrap_width)[:max_lines] for h in trimmed]
    block_h = sum(len(w) * line_h + 40 for w in wrapped)

    panel_top = date_bottom + 60
    draw.rounded_rectangle(
        [(60, panel_top), (VIDEO_W - 60, panel_top + block_h + 40)],
        radius=28, fill=(0, 0, 0, 190),
    )
    cy = panel_top + 40
    for lines in wrapped:
        draw.ellipse([(96, cy + 14), (120, cy + 38)], fill=COLOR_CYAN)
        for i, line in enumerate(lines):
            draw.text((140, cy + i * line_h), line, font=f_head, fill=COLOR_WHITE)
        cy += len(lines) * line_h + 40

    _draw_cta_bar(draw)
    return np.array(img.convert("RGB"))


# ---------------------------------------------------------------------------
# ElevenLabs TTS — one segment at a time (returns its real duration)
# ---------------------------------------------------------------------------

def elevenlabs_configured() -> bool:
    return bool(ELEVENLABS_API_KEY and ELEVENLABS_VOICE_MIKE
                and ELEVENLABS_VOICE_ELIZABETH and ELEVENLABS_VOICE_JESSE)


def tts_segment(speaker: str, text: str, output_path: Path) -> float:
    """Generate an MP3 for one speaker segment; return duration in seconds."""
    import requests
    voice_map = {
        "MIKE":      ELEVENLABS_VOICE_MIKE,
        "ELIZABETH": ELEVENLABS_VOICE_ELIZABETH,
        "JESSE":     ELEVENLABS_VOICE_JESSE,
    }
    voice_id = voice_map[speaker]
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.55,
            "similarity_boost": 0.75,
            "style": 0.20,
            "use_speaker_boost": True,
        },
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=90)
    resp.raise_for_status()
    output_path.write_bytes(resp.content)
    try:
        from mutagen.mp3 import MP3
        return MP3(str(output_path)).info.length
    except Exception:
        return len(text.split()) / 2.5  # ~2.5 words/sec fallback


# ---------------------------------------------------------------------------
# Caption chunking — split a segment into short, timed cues
# ---------------------------------------------------------------------------

def chunk_caption(text: str, max_words: int = 8) -> list[str]:
    """
    Split a segment's text into short caption cues (~max_words each), preferring
    to break after sentence-ending punctuation so a cue is a readable phrase.
    Short-form is watched muted, so cues stay small and legible.
    """
    words = text.split()
    if not words:
        return []
    cues: list[str] = []
    cur: list[str] = []
    for w in words:
        cur.append(w)
        ends_sentence = w.endswith((".", "!", "?", "…"))
        if len(cur) >= max_words or (ends_sentence and len(cur) >= 4):
            cues.append(" ".join(cur))
            cur = []
    if cur:
        cues.append(" ".join(cur))
    return cues


# ---------------------------------------------------------------------------
# The shared render entry point
# ---------------------------------------------------------------------------

def _faststart(path: Path) -> None:
    """Rewrite the MP4 with the moov atom at the front (IG requires faststart).

    moviepy doesn't reliably front-load moov, so do a lossless remux pass. If
    ffmpeg is missing or fails, leave the original file in place (best effort)."""
    import shutil
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found — cannot enforce +faststart on %s", path.name)
        return
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False, dir=path.parent) as tmp:
            tmp_path = Path(tmp.name)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-i", str(path),
             "-c", "copy", "-movflags", "+faststart", str(tmp_path)],
            capture_output=True, check=True, timeout=180,
        )
        tmp_path.replace(path)
        logger.info("Applied +faststart to %s", path.name)
    except Exception as exc:
        logger.warning("faststart remux failed for %s: %s", path.name, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def render_captioned_video(
    segments: list[tuple[str, str, Path, float]],
    out_path: Path,
    date_display: str,
    headlines: list[str] | None = None,
) -> Path:
    """
    Render a 1080×1920 H.264/AAC MP4 from pre-rendered per-segment audio.

    ``segments`` = [(speaker, text, audio_path, duration_secs)]. Each segment's
    text is chunked into caption cues; each cue becomes one image-clip held for
    its proportional share of the segment's audio duration, so captions stay in
    sync with the audio by construction. The full audio is the segments played
    back-to-back. Output is faststart'd for Instagram.

    ``headlines``, if given, prepends a silent ``SLATE_SECS`` "Today's Top
    Stories" title card ahead of the captioned cold-open — the highlights stay
    visible for a beat even muted / thumbnail-only, the way the static card did
    before reels replaced it. Omitted (default) for callers like mc_ad.py that
    have no daily headlines to show.
    """
    from moviepy import (
        AudioClip, AudioFileClip, ImageClip, concatenate_audioclips, concatenate_videoclips,
    )

    background = _build_background()
    logo = _build_logo(size=680)

    video_clips = []
    audio_clips = []

    headlines = [h for h in (headlines or []) if h and h.strip()]
    if headlines:
        slate_frame = render_slate_frame(background, logo, headlines, date_display)
        video_clips.append(ImageClip(slate_frame, duration=SLATE_SECS).with_fps(FPS))
        audio_clips.append(AudioClip(
            frame_function=lambda t: np.array([0.0, 0.0]),
            duration=SLATE_SECS, fps=44100,
        ))

    for speaker, text, audio_path, duration in segments:
        duration = max(float(duration), 0.5)
        cues = chunk_caption(text) or [text]
        # distribute the segment's duration across cues, proportional to words
        weights = [max(len(c.split()), 1) for c in cues]
        total_w = sum(weights)
        acc = 0.0
        for i, (cue, w) in enumerate(zip(cues, weights)):
            if i == len(cues) - 1:
                cue_dur = max(duration - acc, 0.3)   # remainder → exact sum
            else:
                cue_dur = duration * (w / total_w)
                acc += cue_dur
            frame = render_frame(background, logo, speaker, cue, date_display)
            video_clips.append(ImageClip(frame, duration=cue_dur).with_fps(FPS))
        audio_clips.append(AudioFileClip(str(audio_path)))

    final_video = concatenate_videoclips(video_clips, method="compose")
    final_audio = concatenate_audioclips(audio_clips)
    final = final_video.with_audio(final_audio)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Rendering reel → %s (%.1fs, %d cues)",
                out_path, final.duration, len(video_clips))
    final.write_videofile(
        str(out_path),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="fast",
        ffmpeg_params=["-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        logger=None,
    )
    # write_videofile's faststart isn't guaranteed with all builds — enforce it.
    _faststart(out_path)
    logger.info("Reel done: %s (%.1fs, ~%dMB)",
                out_path, final.duration, out_path.stat().st_size // 1_000_000)
    return out_path


# ---------------------------------------------------------------------------
# Daily reel builder — cold-open of the real episode script
# ---------------------------------------------------------------------------

_WORDS_PER_SEC = 2.5  # conservative estimate for cold-open budgeting


def _split_sentences(text: str) -> list[str]:
    """Split into sentences, keeping terminal punctuation. Falls back to the whole
    text as a single 'sentence' when there's no sentence-ending punctuation."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _truncate_to_words(text: str, max_words: float) -> str:
    """
    Return the leading whole sentences whose cumulative word count stays within
    ``max_words``. Always returns at least the first sentence (so a caption never
    cuts mid-sentence), even if that one sentence already exceeds ``max_words``.
    """
    out: list[str] = []
    words = 0
    for s in _split_sentences(text):
        w = len(s.split())
        if out and words + w > max_words:
            break
        out.append(s)
        words += w
        if words >= max_words:
            break
    return " ".join(out)


def _select_cold_open(
    segments: list[tuple[str, str]],
    target_secs: float = REEL_TARGET_SECS,
    max_segments: int = REEL_MAX_SEGMENTS,
) -> list[tuple[str, str]]:
    """
    Build the cold-open teaser from the leading segments, up to ~target_secs of
    speech (estimating ~2.5 words/sec). Whole segments are kept while they fit; the
    segment that would overflow the budget is **truncated to its first few
    sentences rather than dropped** — otherwise a short host intro followed by a
    long segment yields a thin, intro-only reel (e.g. a 15s "good morning" clip).
    Truncation is at sentence boundaries, so caption/audio sync stays exact.
    Always returns at least one (possibly truncated) segment.
    """
    budget = target_secs * _WORDS_PER_SEC
    chosen: list[tuple[str, str]] = []
    used = 0.0
    for speaker, text in segments:
        if used >= budget or len(chosen) >= max_segments:
            break
        seg_words = len(text.split())
        if used + seg_words <= budget:
            chosen.append((speaker, text))
            used += seg_words
        else:
            trimmed = _truncate_to_words(text, budget - used)
            if trimmed:
                chosen.append((speaker, trimmed))
            break
    if not chosen and segments:
        speaker, text = segments[0]
        chosen = [(speaker, _truncate_to_words(text, budget) or text)]
    return chosen


def build_daily_reel(
    episode_data: dict,
    out_path: Path,
    target_secs: float = REEL_TARGET_SECS,
    headlines: list[str] | None = None,
) -> Path | None:
    """
    Build the day's Instagram Reel / X video from the cold-open of the episode's
    conversational_script: real script text + freshly-TTS'd real voices + burned-in
    captions. Returns the MP4 path, or None on any problem (missing ElevenLabs
    creds, no script, TTS/render failure) so the caller falls back to the card.

    Per-segment episode audio isn't cached by mc_audio (it concatenates in-memory
    bytes then discards them), so we re-TTS the handful of cold-open segments here.
    Cost is a few segments of ElevenLabs audio per day (~60–75s).

    ``headlines`` (the day's top-3 stories, e.g. ``card_bullets``), if given, are
    shown on a title slate ahead of the cold-open — see ``render_captioned_video``.
    """
    if not elevenlabs_configured():
        logger.info("ElevenLabs not configured — cannot build reel; will use card.")
        return None

    from mc_generate import parse_conversational_script
    script = episode_data.get("conversational_script") or ""
    segments = parse_conversational_script(script)
    if not segments:
        logger.warning("No conversational_script segments — cannot build reel.")
        return None

    cold_open = _select_cold_open(segments, target_secs=target_secs)
    date_display = episode_data.get("date_display", TODAY)

    tmp_dir = Path(tempfile.mkdtemp(prefix="mc_reel_"))
    rendered: list[tuple[str, str, Path, float]] = []
    total = 0.0
    try:
        for i, (speaker, text) in enumerate(cold_open):
            audio_path = tmp_dir / f"seg_{i:02d}_{speaker}.mp3"
            logger.info("Reel TTS [%s] (%d words)…", speaker, len(text.split()))
            duration = tts_segment(speaker, text, audio_path)
            # Stop before we blow past IG's 90s ceiling — but keep ≥1 segment.
            if rendered and total + duration > REEL_MAX_SECS:
                logger.info("Reel hit %.0fs cap — using %d segment(s).", REEL_MAX_SECS, len(rendered))
                break
            rendered.append((speaker, text, audio_path, duration))
            total += duration
            time.sleep(0.3)

        if not rendered:
            return None
        return render_captioned_video(rendered, out_path, date_display, headlines=headlines)
    except Exception as exc:
        logger.error("Reel build failed (%s) — caller will fall back to card.", exc)
        return None
