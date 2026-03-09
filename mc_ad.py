#!/usr/bin/env python3
"""
mc_ad.py — Generate 30-second vertical video ads for MikeCast.

Creates social-media-ready 1080×1920 (9:16) MP4 ads for Meta/Google Reels/Stories.
Each ad uses the 3 ElevenLabs voices (Mike, Elizabeth, Jesse) and shows
animated subtitles over the MikeCast brand background.

Usage:
    python3 mc_ad.py                      # Uses today's episode
    python3 mc_ad.py --date 2026-03-07    # Specific date
    python3 mc_ad.py --output my_ad.mp4   # Custom output path
    python3 mc_ad.py --dry-run            # Script only, no audio/video
"""

import argparse
import json
import logging
import re
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from mc_config import (
    DATA_DIR,
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ELIZABETH,
    ELEVENLABS_VOICE_JESSE,
    ELEVENLABS_VOICE_MIKE,
    OPENAI_API_KEY,
    SCRIPT_DIR,
    TODAY,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("mc_ad")

# ---------------------------------------------------------------------------
# Video constants
# ---------------------------------------------------------------------------
VIDEO_W, VIDEO_H = 1080, 1920
FPS = 30
COVER_PATH   = DATA_DIR / "cover.png"
ASSETS_DIR   = SCRIPT_DIR / "assets"
FONT_BOLD    = ASSETS_DIR / "fonts" / "InterDisplay-Bold.ttf"
FONT_REGULAR = ASSETS_DIR / "fonts" / "Inter-Medium.ttf"

# Brand palette
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
# Font helpers
# ---------------------------------------------------------------------------

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    if path.exists():
        return ImageFont.truetype(str(path), size)
    # Fallback to any available system font
    fallbacks_bold = [
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    ]
    fallbacks_reg = [
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    ]
    fallbacks = fallbacks_bold if bold else fallbacks_reg
    for fb in fallbacks:
        if Path(fb).exists():
            return ImageFont.truetype(fb, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Ad script generation via GPT-4o
# ---------------------------------------------------------------------------
_AD_SYSTEM = (
    "You write punchy, energetic 30-second podcast promo scripts. "
    "Be specific: name real companies, people, and headlines. "
    "High energy — like an actual radio promo spot."
)

def generate_ad_script(episode_data: dict) -> str:
    """Ask GPT-4o to write a 30-second 3-voice promo from today's episode."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    articles = episode_data.get("articles", {})
    headlines = []
    for cat, arts in articles.items():
        for a in (arts or [])[:2]:
            headlines.append(f"[{cat}] {a.get('title', '')}")
    headlines_text = "\n".join(headlines[:9])

    conv_script = episode_data.get("conversational_script", "")
    # Feed the first ~800 chars of the real episode as flavor
    episode_intro = conv_script[:800] if conv_script else ""

    prompt = f"""Write a 30-second podcast promo for MikeCast — a daily AI-voiced news briefing.

Today's headlines:
{headlines_text}

Episode intro (for flavor/voice):
{episode_intro}

Format with EXACTLY these speaker tags on their own line:
[MIKE]
[ELIZABETH]
[JESSE]

Script structure (word targets in parentheses):
[MIKE] — Hook: what's in today's episode, get listener excited. (~20 words)
[ELIZABETH] — Tease ONE specific tech or business story. Name the company/person. (~22 words)
[JESSE] — Tease ONE specific sports story, passionate. (~14 words)
[MIKE] — CTA: "That's MikeCast — your free daily briefing. Subscribe wherever you get your podcasts." (exact wording)

Rules:
- Total word count: 75–90 words
- Be specific — name real companies, real people
- No filler like "today we have a great show" — just the hook
- Each speaker sounds distinct
"""

    from openai import OpenAI
    resp = OpenAI().chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _AD_SYSTEM},
            {"role": "user",   "content": prompt},
        ],
        max_tokens=400,
        temperature=0.85,
    )
    return resp.choices[0].message.content.strip()


# ---------------------------------------------------------------------------
# ElevenLabs TTS — one segment at a time
# ---------------------------------------------------------------------------

def tts_segment(speaker: str, text: str, output_path: Path) -> float:
    """Generate MP3 for one speaker segment, return duration in seconds."""
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
        # Rough fallback: average speaking rate ~2.5 words/second
        return len(text.split()) / 2.5


# ---------------------------------------------------------------------------
# Parse script into (speaker, text) segments
# ---------------------------------------------------------------------------

def parse_segments(script: str) -> list[tuple[str, str]]:
    parts = re.split(r'\[([A-Z]+)\]', script.strip())
    segments = []
    for i in range(1, len(parts), 2):
        speaker = parts[i].strip()
        text    = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if speaker in ("MIKE", "ELIZABETH", "JESSE") and text:
            segments.append((speaker, text))
    return segments


# ---------------------------------------------------------------------------
# Background & logo assets (built once, reused per frame)
# ---------------------------------------------------------------------------

def _build_background() -> Image.Image:
    """
    Create a 1080×1920 blurred-brand-image background.
    The cover logo is embedded prominently in the top half.
    """
    cover = Image.open(COVER_PATH).convert("RGB")
    # Scale to fill full vertical canvas
    scale = max(VIDEO_W / cover.width, VIDEO_H / cover.height)
    nw, nh = int(cover.width * scale), int(cover.height * scale)
    cover = cover.resize((nw, nh), Image.LANCZOS)
    # Center crop
    cx, cy = (nw - VIDEO_W) // 2, (nh - VIDEO_H) // 2
    cover = cover.crop((cx, cy, cx + VIDEO_W, cy + VIDEO_H))
    # Blur + darken so text stays readable
    cover = cover.filter(ImageFilter.GaussianBlur(radius=10))
    dark  = Image.new("RGB", (VIDEO_W, VIDEO_H), COLOR_BG)
    return Image.blend(cover, dark, alpha=0.60)


def _build_logo(size: int = 680) -> Image.Image:
    """Load the cover and return a clean RGBA square for overlaying."""
    logo = Image.open(COVER_PATH).convert("RGBA")
    logo = logo.resize((size, size), Image.LANCZOS)
    return logo


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
# Frame renderer
# ---------------------------------------------------------------------------

def render_frame(
    bg: Image.Image,
    logo: Image.Image,
    speaker: str,
    text: str,
    date_str: str,
) -> np.ndarray:
    """
    Compose one RGBA frame and return as RGB numpy array (H×W×3).

    Layout (top → bottom):
      120px  — top padding
      logo   — 680px centered logo
       40px  — gap
      date   — small badge
       60px  — gap
      wave   — sound-wave bars (120px tall zone)
       30px  — gap
      pill   — speaker label pill
       50px  — gap
      subs   — subtitle text box
      CTA    — bottom 180px bar
    """
    img  = bg.copy().convert("RGBA")
    draw = ImageDraw.Draw(img, "RGBA")
    color = SPEAKER_COLORS[speaker]

    # --- Logo ---
    logo_y = 100
    lx = (VIDEO_W - logo.width) // 2
    img.paste(logo, (lx, logo_y), logo)

    # --- Date badge ---
    date_y = logo_y + logo.height + 30
    f_date = _font(36)
    bbox   = draw.textbbox((0, 0), date_str, font=f_date)
    dw, dh = bbox[2] - bbox[0] + 50, bbox[3] - bbox[1] + 22
    draw.rounded_rectangle(
        [(VIDEO_W // 2 - dw // 2, date_y),
         (VIDEO_W // 2 + dw // 2, date_y + dh)],
        radius=dh // 2,
        fill=(0, 0, 0, 160),
    )
    draw.text(
        (VIDEO_W // 2, date_y + dh // 2), date_str,
        font=f_date, fill=COLOR_CYAN, anchor="mm",
    )

    # --- Sound wave ---
    wave_y = date_y + dh + 70
    _draw_wave(draw, VIDEO_W // 2, wave_y, color)

    # --- Speaker pill ---
    pill_y = wave_y + 80
    f_pill = _font(40, bold=True)
    label  = SPEAKER_LABELS[speaker]
    lb     = draw.textbbox((0, 0), label, font=f_pill)
    pw, ph = lb[2] - lb[0] + 64, lb[3] - lb[1] + 28
    draw.rounded_rectangle(
        [(VIDEO_W // 2 - pw // 2, pill_y),
         (VIDEO_W // 2 + pw // 2, pill_y + ph)],
        radius=ph // 2,
        fill=(*color, 230),
    )
    draw.text(
        (VIDEO_W // 2, pill_y + ph // 2), label,
        font=f_pill, fill=COLOR_BG, anchor="mm",
    )

    # --- Subtitle text box ---
    sub_top = pill_y + ph + 45
    f_sub   = _font(54)
    lines   = textwrap.wrap(text, width=26)[:4]  # max 4 lines
    line_h  = 68
    block_h = len(lines) * line_h
    pad     = 36

    draw.rounded_rectangle(
        [(70, sub_top - pad),
         (VIDEO_W - 70, sub_top + block_h + pad)],
        radius=28,
        fill=(0, 0, 0, 185),
    )
    for i, line in enumerate(lines):
        draw.text(
            (VIDEO_W // 2, sub_top + i * line_h + line_h // 2),
            line, font=f_sub, fill=COLOR_WHITE, anchor="mm",
        )

    # --- CTA bottom bar ---
    cta_top = VIDEO_H - 175
    draw.rectangle([(0, cta_top), (VIDEO_W, VIDEO_H)], fill=(0, 0, 0, 220))
    f_cta1 = _font(46, bold=True)
    f_cta2 = _font(36)
    draw.text(
        (VIDEO_W // 2, cta_top + 55), "MikeCast Daily Briefing",
        font=f_cta1, fill=COLOR_CYAN, anchor="mm",
    )
    draw.text(
        (VIDEO_W // 2, cta_top + 118), "Subscribe FREE · Available Everywhere",
        font=f_cta2, fill=(180, 190, 210), anchor="mm",
    )

    # Convert RGBA → RGB for moviepy
    return np.array(img.convert("RGB"))


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def build_ad(episode_data: dict, output_path: Path) -> None:
    from moviepy import AudioFileClip, ImageClip, concatenate_videoclips

    date_display = episode_data.get("date_display", TODAY)

    logger.info("Generating ad script via GPT-4o…")
    ad_script = generate_ad_script(episode_data)
    logger.info("Ad script:\n---\n%s\n---", ad_script)

    segments = parse_segments(ad_script)
    if not segments:
        raise RuntimeError("No speaker segments parsed — check GPT output format")
    logger.info("Parsed %d segments: %s", len(segments), [s for s, _ in segments])

    logger.info("Pre-loading visual assets…")
    background = _build_background()
    logo       = _build_logo(size=680)

    tmp_dir = Path(tempfile.mkdtemp(prefix="mc_ad_"))
    clips   = []
    total_words = sum(len(t.split()) for _, t in segments)
    logger.info("Total words: %d (target: 75–90)", total_words)

    for i, (speaker, text) in enumerate(segments):
        audio_path = tmp_dir / f"seg_{i:02d}_{speaker}.mp3"
        logger.info("ElevenLabs TTS  [%s] (%d words)…", speaker, len(text.split()))
        duration = tts_segment(speaker, text, audio_path)
        logger.info("  → %.2fs", duration)
        time.sleep(0.3)

        frame  = render_frame(background, logo, speaker, text, date_display)
        vclip  = ImageClip(frame, duration=duration).with_fps(FPS)
        aclip  = AudioFileClip(str(audio_path))
        clips.append(vclip.with_audio(aclip))

    final = concatenate_videoclips(clips, method="compose")
    logger.info(
        "Rendering MP4 → %s  (%.1fs total, %d segments)",
        output_path, final.duration, len(clips),
    )
    final.write_videofile(
        str(output_path),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="fast",
        ffmpeg_params=["-pix_fmt", "yuv420p"],
        logger=None,
    )
    logger.info("Done!  %s", output_path)
    logger.info("Video specs: 1080×1920, %.1fs, ~%dMB",
                final.duration, output_path.stat().st_size // 1_000_000)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a 30-second vertical video ad for MikeCast",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--date",    default=TODAY,  help="Episode date YYYY-MM-DD (default: today)")
    parser.add_argument("--output",  default=None,   help="Output MP4 path (default: data/MikeCast_Ad_DATE.mp4)")
    parser.add_argument("--dry-run", action="store_true", help="Print generated script only, no TTS/video")
    args = parser.parse_args()

    episode_file = DATA_DIR / f"{args.date}.json"
    if not episode_file.exists():
        sys.exit(f"ERROR: No episode data for {args.date} ({episode_file})")

    with open(episode_file) as f:
        episode_data = json.load(f)

    if args.dry_run:
        script = generate_ad_script(episode_data)
        print("\n=== GENERATED AD SCRIPT ===")
        print(script)
        segs = parse_segments(script)
        total = sum(len(t.split()) for _, t in segs)
        print(f"\n{len(segs)} segments, {total} total words")
        return

    output_path = Path(args.output) if args.output else DATA_DIR / f"MikeCast_Ad_{args.date}.mp4"
    build_ad(episode_data, output_path)


if __name__ == "__main__":
    main()
