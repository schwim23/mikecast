#!/usr/bin/env python3
"""
eval/grounding_backfill.py — run crew/grounding_check.py over already-published episodes.

Measures how often the writers reach past their sources, using each day's saved
episode JSON (s3://<S3_BUCKET>/data/<date>.json, or local data/<date>.json) — no
pipeline run, no deploy. Also counts what mc_facts.fix_stale_titles would rewrite.

Usage:
  python eval/grounding_backfill.py --dates 2026-10-01 2026-10-06
  python eval/grounding_backfill.py --last 7 --email   # mails the summary to GMAIL_TO
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
from datetime import date, timedelta
from email.mime.text import MIMEText
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

OUT_DIR = Path(__file__).parent / "out"


def load_episode(day: str) -> dict | None:
    local = _REPO_ROOT / "data" / f"{day}.json"
    if local.exists():
        return json.loads(local.read_text())
    from mc_config import S3_BUCKET
    if not S3_BUCKET:
        return None
    from mc_utils import s3_load_json
    return s3_load_json(S3_BUCKET, f"data/{day}.json")


def run(days: list[str]) -> dict:
    from crew.grounding_check import check_grounding
    from mc_facts import fix_stale_titles

    os.environ["MIKECAST_GROUNDING_CHECK"] = "1"
    report: dict = {"days": {}}
    for day in days:
        ep = load_episode(day)
        if not ep:
            report["days"][day] = {"missing": True}
            continue
        # The ESPN blocks the writers saw aren't in the episode JSON — take them from that
        # day's writer fixture (prod dumps one daily) when it exists.
        sports: dict = {}
        try:
            from eval.run_eval import load_fixture
            inp = load_fixture("writer", day)["input"]
            sports = {"verified_sports_facts": inp.get("verified_sports_facts"),
                      "ny_team_updates": inp.get("ny_team_updates")}
        except Exception:
            pass
        t0 = time.time()
        g = check_grounding(ep.get("html_briefing", ""), ep.get("conversational_script", ""),
                            ep.get("articles", {}), ep.get("mikes_picks", []), **sports)
        stale = []
        for field in ("html_briefing", "podcast_script", "conversational_script"):
            stale += [f"{field}: {c}" for c in fix_stale_titles(ep.get(field, ""))[1]]
        report["days"][day] = {**g, "stale_titles": stale, "seconds": round(time.time() - t0, 1)}
    return report


def summarize(report: dict) -> str:
    lines = ["MikeCast grounding backfill (log-only fact-check over published episodes)", ""]
    tot_c = tot_u = 0
    for day, r in report["days"].items():
        if r.get("missing"):
            lines.append(f"{day}: no episode found")
            continue
        tot_c += r["checked"] - r["not_claims"] - r.get("analysis", 0)
        tot_u += r["unsupported"]
        factual = r["checked"] - r["not_claims"] - r.get("analysis", 0)
        lines.append(f"{day}: {r['unsupported']}/{factual} factual sentences unsupported, "
                     f"{r['unclear']} unclear, {r.get('analysis', 0)} writer analysis, "
                     f"{len(r['stale_titles'])} stale titles, {r['seconds']}s")
    rate = f"{100 * tot_u / tot_c:.1f}%" if tot_c else "n/a"
    lines += ["", f"TOTAL: {tot_u}/{tot_c} factual sentences unsupported ({rate})", "", "FLAGGED SENTENCES:"]
    for day, r in report["days"].items():
        for f in r.get("flagged", []):
            lines.append(f"- [{day} {f['where']}] {f['sentence'][:220]}")
            lines.append(f"    why: {(f.get('reasoning') or '')[:220]}")
        for s in r.get("stale_titles", []):
            lines.append(f"- [{day} stale title] {s}")
    return "\n".join(lines)


def email(subject: str, body: str) -> None:
    sender, to, pw = os.environ["GMAIL_FROM"], os.environ["GMAIL_TO"], os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEText(body)
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(sender, pw)
        s.send_message(msg)


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dates", nargs="+", help="YYYY-MM-DD ... (two dates = inclusive range)")
    g.add_argument("--last", type=int, help="the last N days, including today")
    ap.add_argument("--email", action="store_true", help="email the summary to GMAIL_TO")
    ap.add_argument("--subject-prefix", default="")
    ap.add_argument("--preamble", default="", help="text placed above the report (e.g. a reminder)")
    args = ap.parse_args()

    if args.last:
        days = [(date.today() - timedelta(days=i)).isoformat() for i in range(args.last - 1, -1, -1)]
    elif len(args.dates) == 2 and args.dates[0] < args.dates[1]:
        d0, d1 = date.fromisoformat(args.dates[0]), date.fromisoformat(args.dates[1])
        days = [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]
    else:
        days = args.dates

    report = run(days)
    run_dir = OUT_DIR / f"grounding_backfill_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    text = summarize(report) + f"\n\nFull report: {run_dir / 'report.json'}"
    if args.preamble:
        text = args.preamble.replace("\\n", "\n") + "\n\n" + text
    print(text)
    if args.email:
        email(f"{args.subject_prefix}MikeCast grounding check: {days[0]} to {days[-1]}", text)


if __name__ == "__main__":
    main()
