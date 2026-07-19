#!/usr/bin/env python3
"""
mc_ad.py — Generate a 30-second vertical video ad for MikeCast.

Creates a social-media-ready 1080×1920 (9:16) MP4 promo using the 3 ElevenLabs
voices (Mike, Elizabeth, Jesse) with burned-in captions over the MikeCast brand
background. The shared render engine lives in `mc_video.py`; this module only
writes the fresh promo script (via GPT-4o) and drives the render.

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
import time
from pathlib import Path

from mc_config import DATA_DIR, OPENAI_API_KEY, TODAY
from mc_video import render_captioned_video, tts_segment

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("mc_ad")


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
# Build the ad
# ---------------------------------------------------------------------------

def build_ad(episode_data: dict, output_path: Path) -> None:
    date_display = episode_data.get("date_display", TODAY)

    logger.info("Generating ad script via GPT-4o…")
    ad_script = generate_ad_script(episode_data)
    logger.info("Ad script:\n---\n%s\n---", ad_script)

    segments = parse_segments(ad_script)
    if not segments:
        raise RuntimeError("No speaker segments parsed — check GPT output format")
    logger.info("Parsed %d segments: %s", len(segments), [s for s, _ in segments])

    tmp_dir = Path(tempfile.mkdtemp(prefix="mc_ad_"))
    rendered: list[tuple[str, str, Path, float]] = []
    for i, (speaker, text) in enumerate(segments):
        audio_path = tmp_dir / f"seg_{i:02d}_{speaker}.mp3"
        logger.info("ElevenLabs TTS [%s] (%d words)…", speaker, len(text.split()))
        duration = tts_segment(speaker, text, audio_path)
        logger.info("  → %.2fs", duration)
        rendered.append((speaker, text, audio_path, duration))
        time.sleep(0.3)

    render_captioned_video(rendered, output_path, date_display)


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
