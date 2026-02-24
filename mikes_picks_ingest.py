#!/usr/bin/env python3
"""
MikeCast — Mike's Picks Ingestion Utility
==========================================
Allows Big Mike to submit URLs, PDFs, or raw text for inclusion in the next
daily briefing under the "Mike's Picks" section.

Usage:
    python mikes_picks_ingest.py --url "https://example.com/article"
    python mikes_picks_ingest.py --pdf "/path/to/file.pdf"
    python mikes_picks_ingest.py --text "Newsletter content here..."
    python mikes_picks_ingest.py --url "https://..." --title "Optional title"

Items are appended to mikes_picks.json and consumed by the briefing script.
"""

import argparse
import json
import os
import sys
import logging
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PICKS_FILE = os.path.join(SCRIPT_DIR, "mikes_picks.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("mikes_picks_ingest")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_picks() -> list:
    """Load existing picks from the JSON file, or return an empty list."""
    if not os.path.exists(PICKS_FILE):
        return []
    try:
        with open(PICKS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, list):
                return data
            logger.warning("mikes_picks.json is not a list — resetting.")
            return []
    except (json.JSONDecodeError, IOError) as exc:
        logger.warning("Could not read %s: %s — starting fresh.", PICKS_FILE, exc)
        return []


def save_picks(picks: list) -> None:
    """Persist the picks list to disk."""
    with open(PICKS_FILE, "w", encoding="utf-8") as fh:
        json.dump(picks, fh, indent=2, ensure_ascii=False)
    logger.info("Saved %d pick(s) to %s", len(picks), PICKS_FILE)


def build_pick(*, pick_type: str, content: str, title: str | None = None) -> dict:
    """Create a single pick record."""
    pick = {
        "type": pick_type,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "processed": False,
    }
    if title:
        pick["title"] = title
    return pick


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add an item to Mike's Picks for the next MikeCast briefing.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--url",
        type=str,
        help="URL of an article or webpage to include.",
    )
    group.add_argument(
        "--pdf",
        type=str,
        help="Path to a PDF file to include.",
    )
    group.add_argument(
        "--text",
        type=str,
        help="Raw text / newsletter content to include.",
    )
    parser.add_argument(
        "--title",
        type=str,
        default=None,
        help="Optional human-readable title for the pick.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    picks = load_picks()

    if args.url:
        pick = build_pick(pick_type="url", content=args.url, title=args.title)
        logger.info("Adding URL pick: %s", args.url)
    elif args.pdf:
        pdf_path = os.path.abspath(args.pdf)
        if not os.path.isfile(pdf_path):
            logger.error("PDF file not found: %s", pdf_path)
            sys.exit(1)
        pick = build_pick(pick_type="pdf", content=pdf_path, title=args.title)
        logger.info("Adding PDF pick: %s", pdf_path)
    elif args.text:
        pick = build_pick(pick_type="text", content=args.text, title=args.title)
        logger.info("Adding text pick (%d chars)", len(args.text))
    else:
        logger.error("No input provided.")
        sys.exit(1)

    picks.append(pick)
    save_picks(picks)
    print(f"✓ Pick added successfully. Total pending picks: {len([p for p in picks if not p.get('processed')])}")


if __name__ == "__main__":
    main()
