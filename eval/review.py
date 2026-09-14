#!/usr/bin/env python3
"""
eval/review.py — Phase 3 blind human-in-the-loop review (MODEL_EVAL_PLAN.md).

Local Flask app (mirrors server.py's pattern) that pairs the baseline against
each candidate from a run_eval.py run and shows them side-by-side as blinded
"A"/"B" (which side is which is randomized per pair and only revealed after
scoring) — the rendered HTML briefing (in an iframe) plus both podcast scripts.
A human enters a 1-5 score per side, a forced A/B winner, and optional notes.
Scores are written to eval/out/<run_id>/human_scores.json.

Writer/critic show the rendered HTML briefing + both scripts side by side, since
those roles produce prose. Helper produces per-sentence fact-check verdicts
instead — same input sentences for baseline and candidate, so what differs is
the judgment (supported: yes/no/unclear + reasoning), not the text — so its
review page shows each sentence once with both models' blinded verdict and
reasoning underneath, not an iframe.

--with-audio (generate + play candidate audio) is NOT built — deferred, since it
burns real ElevenLabs credits and the plan marks it explicitly opt-in.

Usage:
  python eval/review.py --run writer_2026-09-13_1789344065        # -> http://localhost:8081
  python eval/review.py --run writer_2026-09-13_1789344065 --port 8090
  python eval/review.py --latest writer                            # today's run, no run_id needed
                                                                     # (reads eval/out/latest.json,
                                                                     # written by run_daily_eval.sh)
  python eval/review.py --latest helper                             # sentence-by-sentence verdict view
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

OUT_DIR = Path(__file__).parent / "out"
_REVIEWABLE_ROLES = {"writer", "critic", "helper"}


def _safe_name(model: str) -> str:
    from eval.run_eval import _safe_name as _sn
    return _sn(model)


def _pair_id(baseline: str, candidate: str) -> str:
    return hashlib.sha1(f"{baseline}::{candidate}".encode()).hexdigest()[:10]


def _a_is_baseline(pair_id: str) -> bool:
    """Stable per-pair randomization — same pair always gets the same A/B
    assignment across page reloads, without persisting it before scoring
    (so there's nothing to leak by inspecting files mid-review)."""
    return int(hashlib.sha1(pair_id.encode()).hexdigest(), 16) % 2 == 0


def load_run(run_id: str) -> dict:
    run_dir = OUT_DIR / run_id
    summary = json.loads((run_dir / "summary.json").read_text())
    if summary["role"] not in _REVIEWABLE_ROLES:
        raise ValueError(f"role={summary['role']!r} not reviewable — only {_REVIEWABLE_ROLES}")
    return summary


def _read_prose(k0: Path, date: str) -> dict:
    return {
        "html": (k0 / f"{date}.html").read_text(),
        "podcast": (k0 / f"{date}.podcast.txt").read_text() if (k0 / f"{date}.podcast.txt").exists() else "",
        "conversational": (k0 / f"{date}.conversational.txt").read_text() if (k0 / f"{date}.conversational.txt").exists() else "",
    }


def _read_verdicts(k0: Path, date: str) -> dict:
    data = json.loads((k0 / f"{date}.verdicts.json").read_text())
    return {"verdicts": data.get("verdicts", [])}


def build_pairs(run_id: str, summary: dict) -> list[dict]:
    date = summary["date"]
    role = summary["role"]
    run_dir = OUT_DIR / run_id
    baseline_model = next(m for m, r in summary["results"].items() if r["label"] == "baseline")
    candidates = [m for m, r in summary["results"].items() if r["label"] == "candidate"]

    def _read(model: str) -> dict:
        k0 = sorted((run_dir / _safe_name(model)).glob("k*"))[0]
        return _read_verdicts(k0, date) if role == "helper" else _read_prose(k0, date)

    baseline_content = _read(baseline_model)
    pairs = []
    for candidate in candidates:
        pid = _pair_id(baseline_model, candidate)
        pairs.append({
            "pair_id": pid,
            "baseline_model": baseline_model,
            "candidate_model": candidate,
            "baseline_content": baseline_content,
            "candidate_content": _read(candidate),
            "a_is_baseline": _a_is_baseline(pid),
        })
    return pairs


def _human_scores_path(run_id: str) -> Path:
    return OUT_DIR / run_id / "human_scores.json"


def load_human_scores(run_id: str) -> dict:
    p = _human_scores_path(run_id)
    return json.loads(p.read_text()) if p.exists() else {}


def save_human_score(run_id: str, pair_id: str, record: dict) -> None:
    p = _human_scores_path(run_id)
    scores = load_human_scores(run_id)
    scores[pair_id] = record
    p.write_text(json.dumps(scores, indent=2))


def create_app(run_id: str):
    from flask import Flask, redirect, render_template_string, request, url_for

    app = Flask(__name__)
    summary = load_run(run_id)
    pairs_by_id = {p["pair_id"]: p for p in build_pairs(run_id, summary)}

    INDEX_TMPL = """
    <!doctype html><html><head><title>MikeCast Eval Review</title>
    <style>
      body{background:#1a1a2e;color:#e0e0e0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:720px;margin:auto;padding:24px;}
      h1{color:#4fc3f7} a{color:#81d4fa} li{margin-bottom:10px}
      .done{color:#81f0ae}
    </style></head><body>
    <h1>🎙️ MikeCast Eval Review — {{ role }} / {{ date }}</h1>
    <p>Run: <code>{{ run_id }}</code></p>
    <ul>
    {% for p in pairs %}
      <li><a href="{{ url_for('review_pair', pair_id=p.pair_id) }}">baseline vs candidate #{{ loop.index }}</a>
      {% if p.pair_id in scored %}<span class="done">&nbsp;&#10003; scored</span>{% endif %}</li>
    {% endfor %}
    </ul>
    </body></html>
    """

    REVIEW_TMPL = """
    <!doctype html><html><head><title>Review — {{ pair_id }}</title>
    <style>
      body{background:#1a1a2e;color:#e0e0e0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:1400px;margin:auto;padding:24px;}
      h1{color:#4fc3f7} h2{color:#ffb74d}
      .cols{display:flex;gap:16px}
      .col{flex:1;min-width:0}
      iframe{width:100%;height:500px;border:1px solid #444;border-radius:6px;background:#fff}
      pre{white-space:pre-wrap;background:#22223a;border:1px solid #444;border-radius:6px;padding:12px;max-height:220px;overflow:auto;font-size:0.85em}
      label{display:block;margin-top:10px}
      select,textarea,button{background:#22223a;color:#e0e0e0;border:1px solid #444;border-radius:4px;padding:6px}
      textarea{width:100%;height:60px}
      button{margin-top:16px;padding:10px 20px;background:#1e4a5f;color:#81d4fa;cursor:pointer;font-weight:600}
      .scorebox{background:#22223a;border:1px solid #444;border-radius:6px;padding:16px;margin-top:16px}
    </style></head><body>
    <h1>Blind Review — pair {{ pair_id }}</h1>
    <p>Score each side 1-5, then pick the overall winner. The model mapping is hidden until you submit.</p>
    <div class="cols">
      <div class="col">
        <h2>Side A</h2>
        <iframe srcdoc="{{ a_html|e }}"></iframe>
        <h3>Podcast script</h3><pre>{{ a_podcast }}</pre>
        <h3>Conversational script</h3><pre>{{ a_conv }}</pre>
      </div>
      <div class="col">
        <h2>Side B</h2>
        <iframe srcdoc="{{ b_html|e }}"></iframe>
        <h3>Podcast script</h3><pre>{{ b_podcast }}</pre>
        <h3>Conversational script</h3><pre>{{ b_conv }}</pre>
      </div>
    </div>
    <form class="scorebox" method="post">
      <label>Side A score (1-5): <select name="score_a">{% for i in range(1,6) %}<option value="{{i}}">{{i}}</option>{% endfor %}</select></label>
      <label>Side B score (1-5): <select name="score_b">{% for i in range(1,6) %}<option value="{{i}}">{{i}}</option>{% endfor %}</select></label>
      <label>Overall winner:
        <select name="winner"><option value="A">Side A</option><option value="B">Side B</option><option value="tie">Tie</option></select>
      </label>
      <label>Notes: <textarea name="notes"></textarea></label>
      <button type="submit">Submit &amp; reveal</button>
    </form>
    </body></html>
    """

    HELPER_REVIEW_TMPL = """
    <!doctype html><html><head><title>Review — {{ pair_id }}</title>
    <style>
      body{background:#1a1a2e;color:#e0e0e0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:900px;margin:auto;padding:24px;}
      h1{color:#4fc3f7}
      .sentence{background:#22223a;border:1px solid #444;border-radius:6px;padding:14px 16px;margin-bottom:14px;}
      .claim{color:#e0e0e0;margin-bottom:10px;}
      .verdicts{display:flex;gap:14px}
      .verdict{flex:1;min-width:0;background:#1a1a2e;border:1px solid #333;border-radius:6px;padding:10px 12px;}
      .verdict h4{margin:0 0 4px;color:#ffb74d;font-size:0.9em}
      .tag{display:inline-block;padding:1px 8px;border-radius:100px;font-size:0.8em;font-weight:600;margin-bottom:6px}
      .tag.yes{background:#1b4d3e;color:#81f0ae}
      .tag.no{background:#4d1b2e;color:#ff8a80}
      .tag.unclear{background:#3a3a55;color:#bbb}
      .reasoning{color:#aaa;font-size:0.88em;margin-top:4px}
      label{display:block;margin-top:10px}
      select,textarea,button{background:#22223a;color:#e0e0e0;border:1px solid #444;border-radius:4px;padding:6px}
      textarea{width:100%;height:60px}
      button{margin-top:16px;padding:10px 20px;background:#1e4a5f;color:#81d4fa;cursor:pointer;font-weight:600}
      .scorebox{background:#22223a;border:1px solid #444;border-radius:6px;padding:16px;margin-top:16px}
    </style></head><body>
    <h1>Blind Review — pair {{ pair_id }}</h1>
    <p>Same sentences were checked against the same source articles for both sides — only the
    verdict and reasoning differ. Score which side's fact-checking judgment was more accurate.
    The model mapping is hidden until you submit.</p>
    {% for row in rows %}
    <div class="sentence">
      <div class="claim">&ldquo;{{ row.sentence }}&rdquo;</div>
      <div class="verdicts">
        <div class="verdict">
          <h4>Side A</h4>
          <span class="tag {{ row.a.supported }}">{{ row.a.supported }}</span>
          <div class="reasoning">{{ row.a.reasoning }}</div>
        </div>
        <div class="verdict">
          <h4>Side B</h4>
          <span class="tag {{ row.b.supported }}">{{ row.b.supported }}</span>
          <div class="reasoning">{{ row.b.reasoning }}</div>
        </div>
      </div>
    </div>
    {% endfor %}
    <form class="scorebox" method="post">
      <label>Side A score (1-5, overall fact-checking judgment): <select name="score_a">{% for i in range(1,6) %}<option value="{{i}}">{{i}}</option>{% endfor %}</select></label>
      <label>Side B score (1-5): <select name="score_b">{% for i in range(1,6) %}<option value="{{i}}">{{i}}</option>{% endfor %}</select></label>
      <label>Overall winner:
        <select name="winner"><option value="A">Side A</option><option value="B">Side B</option><option value="tie">Tie</option></select>
      </label>
      <label>Notes: <textarea name="notes"></textarea></label>
      <button type="submit">Submit &amp; reveal</button>
    </form>
    </body></html>
    """

    REVEAL_TMPL = """
    <!doctype html><html><head><title>Revealed</title>
    <style>body{background:#1a1a2e;color:#e0e0e0;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:640px;margin:auto;padding:24px}
    h1{color:#4fc3f7} a{color:#81d4fa} .win{color:#81f0ae;font-weight:600}</style></head><body>
    <h1>Recorded</h1>
    <p>Side A was: <code>{{ a_model }}</code>{% if a_is_baseline %} (baseline){% endif %}</p>
    <p>Side B was: <code>{{ b_model }}</code>{% if not a_is_baseline %} (baseline){% endif %}</p>
    <p>Your pick: <span class="win">{{ winner_model }}</span></p>
    <p><a href="{{ url_for('index') }}">&larr; back to review list</a></p>
    </body></html>
    """

    @app.route("/")
    def index():
        scored = set(load_human_scores(run_id).keys())
        return render_template_string(
            INDEX_TMPL, role=summary["role"], date=summary["date"], run_id=run_id,
            pairs=list(pairs_by_id.values()), scored=scored,
        )

    @app.route("/review/<pair_id>", methods=["GET", "POST"])
    def review_pair(pair_id):
        pair = pairs_by_id.get(pair_id)
        if not pair:
            return "Unknown pair", 404

        a_content = pair["baseline_content"] if pair["a_is_baseline"] else pair["candidate_content"]
        b_content = pair["candidate_content"] if pair["a_is_baseline"] else pair["baseline_content"]
        a_model = pair["baseline_model"] if pair["a_is_baseline"] else pair["candidate_model"]
        b_model = pair["candidate_model"] if pair["a_is_baseline"] else pair["baseline_model"]

        if request.method == "POST":
            winner_side = request.form.get("winner", "tie")
            winner_model = {"A": a_model, "B": b_model, "tie": "tie"}[winner_side]
            record = {
                "baseline_model": pair["baseline_model"],
                "candidate_model": pair["candidate_model"],
                "a_model": a_model,
                "b_model": b_model,
                "score_a": int(request.form.get("score_a", 0)),
                "score_b": int(request.form.get("score_b", 0)),
                "winner_side": winner_side,
                "winner_model": winner_model,
                "notes": request.form.get("notes", ""),
                "scored_at": datetime.now(timezone.utc).isoformat(),
            }
            save_human_score(run_id, pair_id, record)
            return redirect(url_for("revealed", pair_id=pair_id))

        if summary["role"] == "helper":
            rows = [
                {"sentence": a_v["sentence"], "a": a_v, "b": b_v}
                for a_v, b_v in zip(a_content["verdicts"], b_content["verdicts"])
            ]
            return render_template_string(HELPER_REVIEW_TMPL, pair_id=pair_id, rows=rows)

        return render_template_string(
            REVIEW_TMPL, pair_id=pair_id,
            a_html=a_content["html"], a_podcast=a_content["podcast"], a_conv=a_content["conversational"],
            b_html=b_content["html"], b_podcast=b_content["podcast"], b_conv=b_content["conversational"],
        )

    @app.route("/revealed/<pair_id>")
    def revealed(pair_id):
        record = load_human_scores(run_id).get(pair_id)
        if not record:
            return redirect(url_for("review_pair", pair_id=pair_id))
        return render_template_string(
            REVEAL_TMPL, a_model=record["a_model"], b_model=record["b_model"],
            a_is_baseline=record["a_model"] == record["baseline_model"],
            winner_model=record["winner_model"],
        )

    return app


def _resolve_latest(role: str) -> str:
    """Reads eval/out/latest.json (written by run_daily_eval.sh each morning)
    so a run_id never needs to be copy-pasted by hand for the daily review."""
    latest_path = OUT_DIR / "latest.json"
    if not latest_path.exists():
        raise SystemExit(f"{latest_path} doesn't exist yet — has run_daily_eval.sh run at least once? Use --run instead.")
    latest = json.loads(latest_path.read_text())
    key = f"{role}_run"
    run_id = latest.get(key)
    if not run_id:
        raise SystemExit(f"{latest_path} has no {key!r} (date={latest.get('date')!r}) — use --run instead.")
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", help="run_id, e.g. writer_2026-09-13_1789344065")
    group.add_argument("--latest", choices=["writer", "critic", "helper"], help="Use today's run for this role from eval/out/latest.json instead of an exact run_id")
    parser.add_argument("--port", type=int, default=8081, help="Local port (default 8081 — server.py already uses 8080)")
    args = parser.parse_args()

    run_id = args.run or _resolve_latest(args.latest)
    app = create_app(run_id)
    print(f"Blind review for {run_id} -> http://localhost:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
