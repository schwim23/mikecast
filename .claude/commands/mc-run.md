Run the MikeCast briefing pipeline.

First, check if today's briefing has already been generated:
```bash
ls data/$(date +%Y-%m-%d).json 2>/dev/null && echo "EXISTS" || echo "NOT FOUND"
```

If it EXISTS, ask the user: "Today's briefing already exists. Run with --force to regenerate?"

Then run the appropriate command based on their answer (or if it was NOT FOUND, run immediately):

**Normal run:**
```bash
source ~/.profile && .venv/bin/python3 mikecast_briefing.py 2>&1 | tee -a mikecast.log
```

**Force regenerate:**
```bash
source ~/.profile && .venv/bin/python3 mikecast_briefing.py --force 2>&1 | tee -a mikecast.log
```

After the run completes, show the user the final run summary line from the log:
```bash
grep "Run summary" mikecast.log | tail -1
```

And confirm what files were generated:
```bash
ls -lh data/$(date +%Y-%m-%d)* 2>/dev/null
```
