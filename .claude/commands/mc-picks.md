Add an item to Mike's Picks queue for today's (or tomorrow's) briefing.

Ask the user what they want to add if not already specified. The ingest script supports three modes:

**URL** (article or webpage):
```bash
source ~/.profile && .venv/bin/python3 mikes_picks_ingest.py --url "USER_URL_HERE"
```

**PDF** (local file path):
```bash
source ~/.profile && .venv/bin/python3 mikes_picks_ingest.py --pdf "PATH_TO_PDF"
```

**Text / paste** (requires a title):
```bash
source ~/.profile && .venv/bin/python3 mikes_picks_ingest.py --text "PASTE_TEXT_HERE" --title "TITLE_HERE"
```

After running, confirm what's now in the queue:
```bash
python3 -c "import json; picks=json.load(open('mikes_picks.json')); print(f'{len(picks)} item(s) in queue:'); [print(f'  - {p[\"title\"]}') for p in picks]"
```

Remind the user: picks are consumed and cleared during the next briefing run (Step 7). If today's briefing already ran, the pick will appear in tomorrow's edition.
