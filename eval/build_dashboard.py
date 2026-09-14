#!/usr/bin/env python3
"""
eval/build_dashboard.py — Phase 3+4 results dashboard (MODEL_EVAL_PLAN.md).

Renders every eval/out/<run_id>/summary.json into a single static HTML page and
uploads it to S3 at evals/index.html (reachable at mikecast.io/evals/index.html —
see MODEL_EVAL_PLAN.md §0b for why no CloudFront change is needed for that path).

Phase 2 (eval/score.py — grounding/hallucination hard gate, format-contract
checks, editorial score) is built and runs automatically every morning via
eval/run_daily_eval.sh. Phase 3 (eval/review.py, blind human A/B) is also
built, but no real review has been completed yet — someone still has to
actually run it. LLM-judge blind A/B is the one piece of Gate B not built.
A promotion decision needs a completed human review on top of these
automated scores, not this page alone — see the plan's promotion rule (§4).

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
        if not (d.is_dir() and f.exists()):
            continue
        run = json.loads(f.read_text())
        scores_f = d / "scores.json"
        run["scores"] = json.loads(scores_f.read_text())["models"] if scores_f.exists() else {}
        runs.append(run)
    return sorted(runs, key=lambda r: (r["role"], r["date"], r["run_id"]))


def _row(role: str, date: str, run_id: str, k: int, model: str, r: dict, score: dict | None) -> str:
    e = html.escape
    latency = f'{r["avg_latency_s"]:.1f}s' if r["avg_latency_s"] is not None else "—"
    cost = f'${r["avg_cost_usd"]:.4f}' if r["avg_cost_usd"] is not None else "—"
    label_class = "baseline" if r["label"] == "baseline" else "candidate"
    fail_class = " fail" if r["failures"] else ""

    # data-* attributes carry raw sortable/filterable values — separate from the
    # formatted display text in each <td>, so JS never has to re-parse "$0.1126"
    # or "6/40" back out of rendered strings.
    data_attrs = {
        "role": role,
        "date": date,
        "model": model,
        "label": r["label"],
        "latency": r["avg_latency_s"] if r["avg_latency_s"] is not None else "",
        "cost": r["avg_cost_usd"] if r["avg_cost_usd"] is not None else "",
        "failures": r["failures"],
        "runid": run_id,
    }

    if score is None:
        gate_cell = '<td class="num">—</td>'
        fmt_cell = '<td class="num">—</td>'
        ground_cell = '<td class="num">—</td>'
        edit_cell = '<td class="num">—</td>'
        data_attrs.update(gate="", format="", grounding="", editorial="")
    else:
        gate_ok = score["hard_gate_passed"]
        fmt_ok = score["format_contract"]["passed"]
        g = score["grounding"]
        gate_cell = f'<td class="num"><span class="tag {"pass" if gate_ok else "fail-tag"}">{"PASS" if gate_ok else "FAIL"}</span></td>'
        fmt_cell = f'<td class="num"><span class="tag {"pass" if fmt_ok else "fail-tag"}">{"OK" if fmt_ok else "FAIL"}</span></td>'
        ground_cell = f'<td class="num{" fail" if g["unsupported"] else ""}">{g["unsupported"]}/{g["checked"]}</td>'
        edit_cell = f'<td class="num">{score["editorial"]["mean_score"]}</td>'
        data_attrs.update(
            gate="pass" if gate_ok else "fail",
            format="pass" if fmt_ok else "fail",
            grounding=g["unsupported"],
            editorial=score["editorial"]["mean_score"] if score["editorial"]["mean_score"] is not None else "",
        )

    data_str = " ".join(f'data-{k}="{e(str(v))}"' for k, v in data_attrs.items())

    return (
        f'<tr class="{label_class}" {data_str}>'
        f'<td>{e(role)}</td><td>{e(date)}</td>'
        f'<td class="model">{e(model)}</td>'
        f'<td><span class="tag {label_class}">{e(r["label"])}</span></td>'
        f'<td class="num">{e(latency)}</td>'
        f'<td class="num">{e(cost)}</td>'
        f'<td class="num{fail_class}">{r["failures"]}/{k}</td>'
        f'{gate_cell}{fmt_cell}{ground_cell}{edit_cell}'
        f'<td class="run-id">{e(run_id)}</td>'
        f'</tr>'
    )


def render(runs: list[dict]) -> str:
    rows = []
    for run in runs:
        scores = run.get("scores", {})
        for model, r in run["results"].items():
            rows.append(_row(run["role"], run["date"], run["run_id"], run["k"], model, r, scores.get(model)))
    rows_html = "\n".join(rows) if rows else '<tr><td colspan="12" class="empty">No runs yet.</td></tr>'

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
  .toolbar {{
    display:flex; flex-wrap:wrap; align-items:center; gap:10px 18px;
    background:#22223a; border:1px solid #444; border-radius:8px;
    padding:12px 16px; margin-bottom:16px; font-size:0.88em;
  }}
  .toolbar-group {{ display:flex; align-items:center; gap:6px; }}
  .toolbar-label {{ color:#888; font-size:0.85em; }}
  .toolbar button.chip {{
    background:#1a1a2e; color:#ccc; border:1px solid #444; border-radius:100px;
    padding:4px 12px; font-size:0.88em; cursor:pointer; font-family:inherit;
  }}
  .toolbar button.chip:hover {{ border-color:#4fc3f7; }}
  .toolbar button.chip.active {{ background:#1e4a5f; color:#81d4fa; border-color:#4fc3f7; }}
  .toolbar select, .toolbar input[type=search] {{
    background:#1a1a2e; color:#e0e0e0; border:1px solid #444; border-radius:6px;
    padding:5px 10px; font-size:0.88em; font-family:inherit;
  }}
  .toolbar input[type=search] {{ width:200px; }}
  .toolbar button.reset {{
    background:none; color:#81d4fa; border:none; cursor:pointer; font-size:0.88em;
    font-family:inherit; text-decoration:underline; padding:0; margin-left:auto;
  }}
  #row-count {{ color:#888; font-size:0.85em; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.92em; }}
  th, td {{ padding:8px 10px; text-align:left; border-bottom:1px solid #333; }}
  th {{ color:#4fc3f7; border-bottom:1px solid #4fc3f7; font-weight:600; cursor:pointer; user-select:none; white-space:nowrap; }}
  th:hover {{ color:#81d4fa; }}
  th .arrow {{ color:#666; font-size:0.85em; margin-left:3px; }}
  th.sorted .arrow {{ color:#81d4fa; }}
  tr.hidden-row {{ display:none; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.model {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:0.9em; }}
  td.run-id {{ font-family:ui-monospace,Menlo,Consolas,monospace; font-size:0.78em; color:#777; }}
  td.empty {{ text-align:center; color:#777; padding:32px; }}
  tr.candidate {{ background:rgba(79,195,247,0.04); }}
  .tag {{ padding:2px 8px; border-radius:10px; font-size:0.8em; font-weight:600; }}
  .tag.baseline {{ background:#3a3a55; color:#bbb; }}
  .tag.candidate {{ background:#1e4a5f; color:#81d4fa; }}
  .tag.pass {{ background:#1b4d3e; color:#81f0ae; }}
  .tag.fail-tag {{ background:#4d1b2e; color:#ff8a80; }}
  td.num.fail {{ color:#ff8a80; font-weight:600; }}
  footer {{ margin-top:32px; color:#666; font-size:0.85em; border-top:1px solid #444; padding-top:16px; }}
  footer a {{ color:#81d4fa; }}
  details {{ margin-bottom:20px; }}
  summary {{ cursor:pointer; color:#81d4fa; font-weight:600; padding:4px 0; }}
  dl {{ background:#22223a; border:1px solid #444; border-radius:6px; padding:16px 20px; margin-top:8px; }}
  dt {{ color:#4fc3f7; font-weight:600; margin-top:12px; }}
  dt:first-child {{ margin-top:0; }}
  dd {{ margin:2px 0 0; color:#ccc; line-height:1.5; }}
  dd code {{ background:#1a1a2e; padding:1px 5px; border-radius:3px; }}
</style>
</head>
<body>
  <h1>🎙️ MikeCast Model Eval Results</h1>
  <p class="subtitle">Replay + automated scoring — baseline vs. candidate models, per role</p>

  <div class="notice">
    <strong>Not a promotion decision by itself.</strong> Gate/Format/Grounding/Editorial
    columns come from <code>eval/score.py</code> (Phase 2, built — runs automatically every
    morning) — a "—" means that run hasn't been scored. <strong>Helper-role runs are never
    scored here</strong> (their output is already a fact-check artifact, not prose to
    fact-check). Blind human review (<code>eval/review.py</code>, Phase 3) is built too, but
    <strong>no run below has actually been through it yet</strong> — someone still has to sit
    down and do it. Blind LLM-judge A/B is the one piece not built. See
    <code>MODEL_EVAL_PLAN.md</code> in the repo for the full design, promotion rule, and
    decision log.
  </div>

  <details>
    <summary>What do these columns mean?</summary>
    <dl>
      <dt>Role</dt>
      <dd>Which of the three swappable LLM roles this row tests — <code>writer</code> (HTML
        briefing + podcast scripts), <code>critic</code> (section scorer that decides what
        gets patched), or <code>helper</code> (NY Sports fact-checker).</dd>
      <dt>Fixture date</dt>
      <dd>Which day's captured real production input (articles, picks, trending, etc.) was
        replayed — a snapshot, not live data.</dd>
      <dt>Model</dt>
      <dd>The exact LiteLLM-style model string tested (e.g. <code>anthropic/claude-sonnet-5</code>).</dd>
      <dt>Label</dt>
      <dd><code>baseline</code> = the model currently live in production for that role.
        <code>candidate</code> = a model being evaluated as a possible replacement.</dd>
      <dt>Avg latency</dt>
      <dd>Average wall-clock time per replay call, averaged across the <code>k</code> repeats
        (writer runs use k=2+ to capture variance; critic/helper typically use k=1).</dd>
      <dt>Avg cost</dt>
      <dd>Average $ per replay call, computed from actual token usage × <code>eval/pricing.py</code>'s
        rate table.</dd>
      <dt>Failures</dt>
      <dd>How many of the <code>k</code> repeats errored or timed out, shown as <code>x/k</code>.</dd>
      <dt>Gate</dt>
      <dd>PASS/FAIL on the hallucination hard gate — every substantive sentence in the output
        is checked against the source articles; a candidate <strong>FAILS</strong> if it has
        <em>more</em> unsupported claims than the baseline. Baseline is always PASS by
        definition.</dd>
      <dt>Format</dt>
      <dd>OK/FAIL on the format contract — HTML parses, all 7 required sections present, not
        truncated, podcast script in the 900-1000 word target, all 3 speaker tags present in
        the conversational script.</dd>
      <dt>Grounding</dt>
      <dd><code>X/Y unsupported</code> — of <code>Y</code> sentences checked against source
        articles, <code>X</code> came back unsupported (a hallucination signal). This is the
        raw count behind the Gate column; lower is better.</dd>
      <dt>Editorial</dt>
      <dd>Mean 1-10 quality score across categories (depth/analysis/substance), from the same
        scorer the production critic uses.</dd>
      <dt>Run ID</dt>
      <dd>The harness run identifier — matches the <code>eval/out/&lt;run_id&gt;/</code>
        directory if you want to inspect the raw generated content behind any row.</dd>
    </dl>
  </details>

  <div class="toolbar">
    <div class="toolbar-group">
      <span class="toolbar-label">Role</span>
      <button class="chip active" data-filter-role="">All</button>
      <button class="chip" data-filter-role="writer">Writer</button>
      <button class="chip" data-filter-role="critic">Critic</button>
      <button class="chip" data-filter-role="helper">Helper</button>
    </div>
    <div class="toolbar-group">
      <span class="toolbar-label">Gate</span>
      <select id="gate-filter">
        <option value="">All</option>
        <option value="pass">Pass</option>
        <option value="fail">Fail</option>
        <option value="none">Unscored</option>
      </select>
    </div>
    <div class="toolbar-group">
      <span class="toolbar-label">Model</span>
      <input type="search" id="model-search" placeholder="e.g. sonnet-5">
    </div>
    <button class="reset" id="reset-filters">Reset</button>
  </div>
  <p id="row-count"></p>

  <table id="results-table">
    <thead>
      <tr>
        <th data-key="role" data-type="text">Role<span class="arrow"></span></th>
        <th data-key="date" data-type="text">Fixture date<span class="arrow"></span></th>
        <th data-key="model" data-type="text">Model<span class="arrow"></span></th>
        <th data-key="label" data-type="text">Label<span class="arrow"></span></th>
        <th data-key="latency" data-type="num">Avg latency<span class="arrow"></span></th>
        <th data-key="cost" data-type="num">Avg cost<span class="arrow"></span></th>
        <th data-key="failures" data-type="num">Failures<span class="arrow"></span></th>
        <th data-key="gate" data-type="text">Gate<span class="arrow"></span></th>
        <th data-key="format" data-type="text">Format<span class="arrow"></span></th>
        <th data-key="grounding" data-type="num">Grounding<span class="arrow"></span></th>
        <th data-key="editorial" data-type="num">Editorial<span class="arrow"></span></th>
        <th data-key="runid" data-type="text">Run ID<span class="arrow"></span></th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>

  <script>
  (function() {{
    var table = document.getElementById('results-table');
    var tbody = table.tBodies[0];
    var allRows = Array.prototype.slice.call(tbody.querySelectorAll('tr[data-role]'));
    var rowCount = document.getElementById('row-count');
    var state = {{ role: '', gate: '', search: '', sortKey: null, sortType: 'text', sortDir: 1 }};

    function applyFilters() {{
      var visible = 0;
      allRows.forEach(function(row) {{
        var matchesRole = !state.role || row.dataset.role === state.role;
        var matchesGate = !state.gate || (state.gate === 'none' ? row.dataset.gate === '' : row.dataset.gate === state.gate);
        var matchesSearch = !state.search || row.dataset.model.toLowerCase().indexOf(state.search) !== -1;
        var show = matchesRole && matchesGate && matchesSearch;
        row.classList.toggle('hidden-row', !show);
        if (show) visible++;
      }});
      rowCount.textContent = 'Showing ' + visible + ' of ' + allRows.length + ' rows';
    }}

    function applySort() {{
      if (!state.sortKey) return;
      var key = state.sortKey, type = state.sortType, dir = state.sortDir;
      var sorted = allRows.slice().sort(function(a, b) {{
        var av = a.dataset[key], bv = b.dataset[key];
        if (type === 'num') {{
          var an = parseFloat(av), bn = parseFloat(bv);
          var aNaN = isNaN(an), bNaN = isNaN(bn);
          if (aNaN && bNaN) return 0;
          if (aNaN) return 1;   // unscored/empty always sinks to the bottom
          if (bNaN) return -1;
          return (an - bn) * dir;
        }}
        return av.localeCompare(bv) * dir;
      }});
      sorted.forEach(function(row) {{ tbody.appendChild(row); }});
    }}

    document.querySelectorAll('#results-table th').forEach(function(th) {{
      th.addEventListener('click', function() {{
        var key = th.dataset.key;
        if (state.sortKey === key) {{
          state.sortDir *= -1;
        }} else {{
          state.sortKey = key;
          state.sortType = th.dataset.type;
          state.sortDir = 1;
        }}
        document.querySelectorAll('#results-table th').forEach(function(h) {{
          h.classList.remove('sorted');
          h.querySelector('.arrow').textContent = '';
        }});
        th.classList.add('sorted');
        th.querySelector('.arrow').textContent = state.sortDir === 1 ? '▲' : '▼';
        applySort();
      }});
    }});

    document.querySelectorAll('[data-filter-role]').forEach(function(btn) {{
      btn.addEventListener('click', function() {{
        state.role = btn.dataset.filterRole;
        document.querySelectorAll('[data-filter-role]').forEach(function(b) {{ b.classList.remove('active'); }});
        btn.classList.add('active');
        applyFilters();
      }});
    }});

    document.getElementById('gate-filter').addEventListener('change', function(e) {{
      state.gate = e.target.value;
      applyFilters();
    }});

    document.getElementById('model-search').addEventListener('input', function(e) {{
      state.search = e.target.value.toLowerCase();
      applyFilters();
    }});

    document.getElementById('reset-filters').addEventListener('click', function() {{
      state.role = ''; state.gate = ''; state.search = '';
      document.getElementById('gate-filter').value = '';
      document.getElementById('model-search').value = '';
      document.querySelectorAll('[data-filter-role]').forEach(function(b) {{ b.classList.remove('active'); }});
      document.querySelector('[data-filter-role=""]').classList.add('active');
      applyFilters();
    }});

    applyFilters();
  }})();
  </script>

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
