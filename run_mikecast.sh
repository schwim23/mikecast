#!/bin/bash
# Load environment variables (API keys, email credentials) from ~/.profile
source /home/mike-schwimmer/.profile

cd /home/mike-schwimmer/mikecast
.venv/bin/python3 mikecast_briefing.py

git add data/ briefing_history.json
git commit -m "MikeCast briefing $(date +%Y-%m-%d)"
git push
