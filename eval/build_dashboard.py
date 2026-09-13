#!/usr/bin/env python3
"""
eval/build_dashboard.py — Phase 3+4 results dashboard (MODEL_EVAL_PLAN.md).

Renders every eval/out/<run_id>/summary.json into a single static HTML page and
uploads it to S3 at evals/index.html (reachable at mikecast.io/evals/index.html —
see MODEL_EVAL_PLAN.md §0b for why no CloudFront change is needed for that path).

Cost/latency only for now. Quality scores (Phase 2, eval/score.py — grounding/
hallucination hard gate, format-contract checks, editorial score, LLM-judge) are
not built yet, so the page says so explicitly rather than implying a verdict it
can't back up. A promotion decision needs Phase 2 + Phase 3 (human review) per
the plan's three gates (§3), not this page alone.

Usage:
  S3_BUCKET=mikecast-io-data python eval/build_dashboard.py
  python eval/build_dashboard.py --local-only out/dashboard.html   # skip S3 upload
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

OUT_DIR = Path(__file__).parent / "out"


def _load_runs() -> list[dict]:
    runs = []
    for d in sorted(OUT_DIR.iterdir()):
        f = d / "summary.json"
        if d.is_dir() and f.exists():
            runs.append(json.loads(f.read_text()))
    return sorted(runs, key=lambda r: (r["role"], r["date"], r["run_id"]))


def _row(role: str, date: str, run_id: str, k: int, model: str, r: dict) -> str:
    e = html.escape
    latency = f'{r["avg_latency_s"]:.1f}s' if r["avg_latency_s"] is not None else "—"
    cost = f'${r["avg_cost_usd"]:.4f}' if r["avg_cost_usd"] is not None else "—"
    label_class = "baseline" if r["label"] == "baseline" else "candidate"
    fail_class = " fail" if r["failures"] else ""
    return (
        f'<tr class="{label_class}">'
        f'<td>{e(role)}</td><td>{e(date)}</td>'
        f'<td class="model">{e(model)}</td>'
        f'<td><span class="tag {label_class}">{e(r["label"])}</span></td>'
        f'<td class="num">{e(latency)}</td>'
        f'<td class="num">{e(cost)}</td>'
        f'<td class="num{fail_class}">{r["failures"]}/{k}</td>'
        f'<td class="run-id">{e(run_id)}</td>'
        f'</tr>'
    )


def render(runs: list[dict]) -> str:
    rows = []
    for run in runs:
        for model, r in run["results"].items():
            rows.append(_row(run["role"], run["date"], run["run_id"], run["k"], model, r))
    rows_html = "\n".join(rows) if rows else '<tr><td colspan="8" class="empty">No runs yet.</td></tr>'
    generated_at = runs[-1]["run_id"] if runs else "n/a"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MikeCast Model Eval Results</title>
<style>
  body {{
    background:#1a1a2e; color:#e0e0e0; font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
    max-width:960px; margin:auto; padding:24px;
  }}
  h1 {{ color:#4fc3f7; margin:0 0 4px; }}
  .subtitle {{ color:#888; margin:0 0 24px; }}
  .notice {{
    background:#22223a; border:1px solid #444; border-left:4px solid #ffb74d;
    border-radius:6px; padding:14px 18px; margin-bottom:28px; color:#ddd; line-height:1.6;
  }}
  .notice a {{ color:#81d4fa; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.92em; }}
  th, td {{ padding:8px 10px; text-align:left; border-bottom:1px solid #333; }}
  th {{ color:#4fc3f7; border-bottom:1px solid #4fc3f7; font-weight:600; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.model {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:0.9em; }}
  td.run-id {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:0.78em; color:#777; }}
  td.empty {{ text-align:center; color:#777; padding:32px; }}
  tr.candidate {{ background:rgba(79,195,247,0.04); }}
  .tag {{ padding:2px 8px; border-radius:10px; font-size:0.8em; font-weight:600; }}
  .tag.baseline {{ background:#3a3a55; color:#bbb; }}
  .tag.candidate {{ background:#1e4a5f; color:#81d4fa; }}
  td.num.fail {{ color:#ff8a80; font-weight:600; }}
  footer {{ margin-top:32px; color:#666; font-size:0.85em; border-top:1px solid #444; padding-top:16px; }}
  footer a {{ color:#81d4fa; }}
</style>
</head>
<body>
  <h1>🎙️ MikeCast Model Eval Results</h1>
  <p class="subtitle">Phase 1 replay results — baseline vs. candidate models, per role</p>

  <div class="notice">
    <strong>Cost/latency only.</strong> This page does not yet show quality scores —
    hallucination/grounding checks, format-contract validation, editorial scoring, and
    blind LLM-judge A/B (Phase 2, <code>eval/score.py</code>) aren't built yet. A model
    promotion decision requires those plus a human review pass (Phase 3), not this table
    alone. See <code>MODEL_EVAL_PLAN.md</code> in the repo for the full design and
    decision log.
  </div>

  <table>
    <thead>
      <tr>
        <th>Role</th><th>Fixture date</th><th>Model</th><th>Label</th>
        <th>Avg latency</th><th>Avg cost</th><th>Failures</th><th>Run ID</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>

  <footer>
    Generated by <code>eval/build_dashboard.py</code> · {len(runs)} run(s) rendered ·
    part of the <a href="https://mikecast.io/">MikeCast</a> model-evaluation harness.
  </footer>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local-only", metavar="PATH", help="Write to this local path instead of uploading to S3")
    args = parser.parse_args()

    runs = _load_runs()
    page = render(runs)

    if args.local_only:
        out_path = Path(args.local_only)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(page)
        print(f"Wrote {out_path} ({len(runs)} run(s))")
        return

    from mc_config import S3_BUCKET
    if not S3_BUCKET:
        print("S3_BUCKET is not set — pass --local-only <path> or export S3_BUCKET=mikecast-io-data", file=sys.stderr)
        sys.exit(1)

    from mc_utils import s3_upload_text
    s3_upload_text(S3_BUCKET, "evals/index.html", page, content_type="text/html; charset=utf-8", cache_control="no-cache")
    print(f"Uploaded {len(runs)} run(s) -> s3://{S3_BUCKET}/evals/index.html")
    print("Live at: https://mikecast.io/evals/index.html")


if __name__ == "__main__":
    main()
