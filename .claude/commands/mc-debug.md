Diagnose a failed or incomplete MikeCast run.

Work through this triage checklist:

**1. Check the last run's log output:**
```bash
tail -50 mikecast.log
```
Look for `[ERROR]` or `[CRITICAL]` lines. Note which step number failed (e.g. "Step 4/10").

**2. Check if today's output was written:**
```bash
ls -lh data/$(date +%Y-%m-%d)* 2>/dev/null || echo "No output files for today"
```

**3. Verify required environment variables are set:**
```bash
source ~/.profile
for var in OPENAI_API_KEY NYTAPIKEY GMAIL_APP_PASSWORD GMAIL_FROM GMAIL_TO; do
  val="${!var}"
  if [ -z "$val" ]; then echo "MISSING: $var"; else echo "OK: $var (${#val} chars)"; fi
done
```

**4. Check optional variables:**
```bash
for var in ELEVENLABS_API_KEY XAI_API_KEY; do
  val="${!var}"
  if [ -z "$val" ]; then echo "NOT SET (optional): $var"; else echo "OK: $var"; fi
done
```

**5. If a specific step failed, investigate the relevant module:**

| Step | Module | Common Issues |
|---|---|---|
| Step 0 | `mc_plan.py` | `XAI_API_KEY` not set (non-fatal, skips gracefully) |
| Step 1 | `mc_collect.py` | API key invalid, RSS feeds down, rate limits |
| Steps 3-4 | `mc_collect.py` | `OPENAI_API_KEY` invalid or quota exceeded |
| Step 8 | `mc_generate.py` | GPT-4o quota, bad response format |
| Step 8b | `mc_critic.py` | Usually non-fatal; logs warning and continues |
| Step 9 | `mc_audio.py` | ElevenLabs quota/voice IDs; falls back to OpenAI TTS |
| Step 10 | `mc_deliver.py` | Gmail app password issue, disk space |

**6. Check briefing history isn't corrupted:**
```bash
python3 -c "import json; h=json.load(open('briefing_history.json')); print(f'History OK: {len(h)} entries')" 2>&1
```

**7. If all else fails, try a fresh run with verbose output:**
```bash
source ~/.profile && .venv/bin/python3 mikecast_briefing.py --force 2>&1 | tee -a mikecast.log
```

Summarize the findings and recommend a fix based on what you find.
