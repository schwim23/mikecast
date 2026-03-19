"""
Test _filter_trending_to_articles against today's known-bad trending topics.

Expected results:
  DROPPED: "OpenAI unveils groundbreaking AI model today"
  DROPPED: "Federal Reserve announces unexpected rate cut"
  KEPT:    "Global markets react to oil price surge"  (real oil/Qatar articles exist)

Usage:
  source ~/.profile && .venv/bin/python3 test_trending_filter.py
"""
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

with open("data/2026-03-19.json") as f:
    d = json.load(f)

from mc_generate import _filter_trending_to_articles

trending_in = d["trending"]
categories = d["articles"]

print(f"\nInput topics ({len(trending_in)}):")
for t in trending_in:
    print(f"  - {t['topic']}")

kept = _filter_trending_to_articles(trending_in, categories)

print(f"\nKept after filter ({len(kept)}):")
for t in kept:
    print(f"  KEPT:    {t['topic']}")

dropped = [t for t in trending_in if t not in kept]
for t in dropped:
    print(f"  DROPPED: {t['topic']}")

# Assertions
dropped_topics = {t["topic"] for t in dropped}
assert "OpenAI unveils groundbreaking AI model today" in dropped_topics, \
    "FAIL: OpenAI hallucination was NOT dropped"
assert "Federal Reserve announces unexpected rate cut" in dropped_topics, \
    "FAIL: Fed hallucination was NOT dropped"

print("\nAll assertions passed.")
