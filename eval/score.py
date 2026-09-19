#!/usr/bin/env python3
"""
eval/score.py — Phase 2 automated scoring (MODEL_EVAL_PLAN.md).

Scores each model's k0 output from a run_eval.py run (eval/out/<run_id>/) against
that fixture's source articles, for the "writer" and "critic" roles (both produce
html/podcast/conversational — same shape). "helper" isn't scored here — its output
IS already a fact-check artifact, not prose to fact-check.

Three checks per model:
  - format_contract: HTML parses, all 7 required <h2> sections present, HTML isn't
    truncated, podcast script is 900-1000 words, conversational script has all
    three speaker tags. Deterministic, no API calls.
  - grounding (HARD GATE): every substantive sentence in each HTML section is
    checked against that section's source articles via the existing
    validate_claim_against_articles tool (crew/tools.py) — the same mechanism
    production already uses for NY Sports, generalized to every section. Capped at
    _MAX_GROUNDING_CHECKS_PER_MODEL sentences to bound cost. A candidate with MORE
    unsupported claims than the baseline fails the hard gate (MODEL_EVAL_PLAN.md §4
    Phase 2 — "fail if candidate adds ANY net-new unsupported claim vs. baseline").
  - editorial: reuses crew/critic_crew.py's section scorer (1-10 per category)
    against each model's own HTML.

Only scores k0 per model — scoring makes real LLM calls, and k-repeats exist to
capture writer variance in cost/latency (Phase 1), not to be re-scored k times.

NOT built yet: LLM-judge blind A/B (Gate B's 4th check, eval/judge.py) and human
review (Phase 3). This script alone does not constitute a promotion decision — see
MODEL_EVAL_PLAN.md §3's three gates and §4's promotion rule.

Usage:
  python eval/score.py --run writer_2026-09-13_1789341103
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

OUT_DIR = Path(__file__).parent / "out"

# Reused verbatim from crew/critic_crew.py's NY-Sports-only fact-checker pattern,
# generalized here to every section.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[])")
_SPEAKER_TAG_RE = re.compile(r"^\[(MIKE|ELIZABETH|JESSE)\]\s*", re.MULTILINE)

_REQUIRED_H2_SECTIONS = [
    "EXECUTIVE SUMMARY", "AI & TECH", "BUSINESS & MARKETS",
    "COMPANIES", "NY SPORTS", "KEY TRENDS & INSIGHTS", "WHAT TO WATCH",
]
_SYNTHESIS_SECTIONS = {"executive summary", "key trends & insights", "what to watch"}
_PODCAST_WORD_MIN, _PODCAST_WORD_MAX = 900, 1000
_MAX_GROUNDING_CHECKS_PER_MODEL = 40
_SCORABLE_ROLES = {"writer", "critic", "sports_researcher"}


def _split_sentences(text: str) -> list[str]:
    if not text:
        return []
    text = _SPEAKER_TAG_RE.sub("", text)
    parts = _SENTENCE_RE.split(text)
    keepers = []
    for s in parts:
        s = s.strip()
        if len(s.split()) < 6:
            continue
        if not re.search(r"\d|[A-Z][a-z]{2,}", s):
            continue
        keepers.append(s)
    return keepers


def _extract_sections(html: str) -> dict[str, str]:
    """h2 header text (as written) -> plain-text section body (all following
    siblings up to the next h2)."""
    soup = BeautifulSoup(html, "html.parser")
    sections: dict[str, str] = {}
    for h2 in soup.find_all("h2"):
        header = h2.get_text(strip=True)
        parts = []
        for sib in h2.find_next_siblings():
            if sib.name == "h2":
                break
            parts.append(sib.get_text(" ", strip=True))
        sections[header] = " ".join(parts)
    return sections


def _articles_for_section(header: str, top_articles: dict[str, list[dict]]) -> list[dict]:
    norm = header.lower()
    for cat, articles in top_articles.items():
        if cat.lower() in norm:
            return articles
    # Synthesis sections (Executive Summary, Key Trends, What To Watch) and any
    # unmapped header fall back to the union of all articles — looser than a
    # per-category check, but still catches invented facts absent from every source.
    union: list[dict] = []
    for articles in top_articles.values():
        union.extend(articles)
    return union


def score_format_contract(html: str, single_script: str, conv_script: str) -> dict:
    try:
        soup = BeautifulSoup(html, "html.parser")
        html_parses = True
    except Exception:
        soup = None
        html_parses = False

    found_headers = [h2.get_text(strip=True).upper() for h2 in soup.find_all("h2")] if soup else []
    missing = [h for h in _REQUIRED_H2_SECTIONS if not any(h in fh for fh in found_headers)]
    html_truncated = bool(html) and not html.rstrip().endswith(("</html>", "</p>", "</ul>", "</div>"))

    podcast_words = len(single_script.split()) if single_script else 0
    podcast_ok = _PODCAST_WORD_MIN <= podcast_words <= _PODCAST_WORD_MAX

    conv_tags = sorted(set(re.findall(r"\[(MIKE|ELIZABETH|JESSE)\]", conv_script or "")))
    all_speakers_present = conv_tags == ["ELIZABETH", "JESSE", "MIKE"]

    checks = {
        "html_parses": html_parses,
        "missing_sections": missing,
        "all_sections_present": len(missing) == 0,
        "html_truncated": html_truncated,
        "podcast_word_count": podcast_words,
        "podcast_word_count_ok": podcast_ok,
        "conversational_speaker_tags": conv_tags,
        "conversational_all_speakers_present": all_speakers_present,
    }
    checks["passed"] = (
        html_parses and checks["all_sections_present"] and not html_truncated
        and podcast_ok and all_speakers_present
    )
    return checks


def score_grounding(html: str, top_articles: dict[str, list[dict]]) -> dict:
    from crew.tools import validate_claim_tool

    sections = _extract_sections(html)
    checked = 0
    unsupported = 0
    unsupported_claims = []
    for header, text in sections.items():
        if checked >= _MAX_GROUNDING_CHECKS_PER_MODEL:
            break
        articles = _articles_for_section(header, top_articles)
        if not articles:
            continue
        for sentence in _split_sentences(text):
            if checked >= _MAX_GROUNDING_CHECKS_PER_MODEL:
                break
            result = validate_claim_tool._run(claim=sentence, articles=articles)
            if not result.get("ok"):
                continue
            checked += 1
            if result.get("supported") == "no":
                unsupported += 1
                unsupported_claims.append({
                    "section": header,
                    "sentence": sentence[:200],
                    "reasoning": (result.get("reasoning") or "")[:200],
                })
    return {"checked": checked, "unsupported": unsupported, "unsupported_claims": unsupported_claims}


def score_editorial(html: str, top_articles: dict[str, list[dict]]) -> dict:
    from crew.critic_crew import _run_scorer

    result, _usage = _run_scorer(html, top_articles)
    scores = result.get("category_scores", {})
    numeric = [v for v in scores.values() if isinstance(v, (int, float))]
    return {
        "category_scores": scores,
        "issues": result.get("issues", {}),
        "mean_score": round(sum(numeric) / len(numeric), 2) if numeric else None,
        "weak_categories": [c for c, v in scores.items() if isinstance(v, (int, float)) and v < 7],
    }


# ---------------------------------------------------------------------------
# Sports Researcher — mechanical tool-fidelity check (no LLM, no articles)
# ---------------------------------------------------------------------------

_TIMING_LABELS = ("TOMORROW NIGHT", "LAST NIGHT", "TONIGHT", "TOMORROW", "YESTERDAY")
_NUMBER_RE = re.compile(r"\d+")


def score_tool_fidelity(verified: dict[str, str], tool_calls: list[dict]) -> dict:
    """Every number and every timing word (TONIGHT / LAST NIGHT / ...) in a stated
    fact must appear in what the ESPN tools actually returned, and each team named
    must have had at least one tool call. Stricter than "sounds right": a stated
    score, record or date the tools never returned counts as unsupported."""
    tool_text = json.dumps([c["result"] for c in tool_calls]).upper()
    called_teams = {str(c["args"].get("team", "")).lower() for c in tool_calls if c["tool"] == "fetch_sports_box_score"}
    unsupported, uncalled = [], []
    for team, fact in verified.items():
        if team.lower() not in called_teams and team.lower() not in " ".join(str(c["args"]).lower() for c in tool_calls):
            uncalled.append(team)
        upper = fact.upper()
        for label in _TIMING_LABELS:
            if label in upper and label not in tool_text:
                unsupported.append({"team": team, "claim": label, "kind": "timing"})
        for n in set(_NUMBER_RE.findall(fact)):
            if not re.search(rf"(?<!\d){n}(?!\d)", tool_text):
                unsupported.append({"team": team, "claim": n, "kind": "number"})
    return {"facts": len(verified), "teams": sorted(verified), "unsupported": len(unsupported),
            "unsupported_detail": unsupported, "teams_without_tool_call": uncalled}


def _score_sports_run(run_id: str, run_dir: Path, summary: dict) -> dict:
    from eval.run_eval import _safe_name, load_fixture

    date = summary["date"]
    fixture = load_fixture("sports_researcher", date)
    scores: dict = {"run_id": run_id, "role": "sports_researcher", "date": date, "models": {}}
    for model, r in summary["results"].items():
        facts_path = next(iter(sorted((run_dir / _safe_name(model)).glob(f"k*/{date}.facts.json"))), None)
        if facts_path is None:
            continue
        verified = json.loads(facts_path.read_text())["verified_sports_facts"]
        scores["models"][model] = {"label": r["label"],
                                   "tool_fidelity": score_tool_fidelity(verified, fixture["tool_calls"])}
    base = next((s for s in scores["models"].values() if s["label"] == "baseline"), None)
    for s in scores["models"].values():
        # Gate: no MORE unsupported claims / uncalled teams than the baseline.
        # A silent failure (researcher errors are swallowed -> {}) must not read as a
        # pass: a candidate that states nothing while the baseline stated facts FAILS.
        s["hard_gate_passed"] = base is None or s["label"] == "baseline" or (
            not (s["tool_fidelity"]["facts"] == 0 and base["tool_fidelity"]["facts"] > 0)
            and s["tool_fidelity"]["unsupported"] <= base["tool_fidelity"]["unsupported"]
            and len(s["tool_fidelity"]["teams_without_tool_call"]) <= len(base["tool_fidelity"]["teams_without_tool_call"]))
    return scores


def score_run(run_id: str) -> dict:
    from eval.run_eval import _safe_name, load_fixture

    run_dir = OUT_DIR / run_id
    summary = json.loads((run_dir / "summary.json").read_text())
    role, date = summary["role"], summary["date"]
    if role not in _SCORABLE_ROLES:
        raise ValueError(f"role={role!r} isn't scorable by this script (only {_SCORABLE_ROLES}) — see module docstring")

    if role == "sports_researcher":
        return _score_sports_run(run_id, run_dir, summary)
    fixture = load_fixture(role, date)
    top_articles = fixture["input"]["top_articles"]

    scores: dict = {"run_id": run_id, "role": role, "date": date, "models": {}}
    for model, r in summary["results"].items():
        model_dir = run_dir / _safe_name(model)
        k_dirs = sorted(model_dir.glob("k*"))
        if not k_dirs:
            continue
        k0 = k_dirs[0]
        html_path = k0 / f"{date}.html"
        if not html_path.exists():
            continue
        html = html_path.read_text()
        podcast_path = k0 / f"{date}.podcast.txt"
        conv_path = k0 / f"{date}.conversational.txt"
        single = podcast_path.read_text() if podcast_path.exists() else ""
        conv = conv_path.read_text() if conv_path.exists() else ""

        print(f"Scoring {model} ({r['label']})...", file=sys.stderr)
        scores["models"][model] = {
            "label": r["label"],
            "format_contract": score_format_contract(html, single, conv),
            "grounding": score_grounding(html, top_articles),
            "editorial": score_editorial(html, top_articles),
        }

    # Hard gate: candidate fails if it has MORE unsupported claims than the baseline.
    baseline_model = next((m for m, s in scores["models"].items() if s["label"] == "baseline"), None)
    baseline_unsupported = scores["models"][baseline_model]["grounding"]["unsupported"] if baseline_model else None
    for model, s in scores["models"].items():
        if s["label"] == "baseline":
            s["hard_gate_passed"] = True
            continue
        s["hard_gate_passed"] = (
            baseline_unsupported is not None
            and s["grounding"]["unsupported"] <= baseline_unsupported
        )

    return scores


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, help="run_id, e.g. writer_2026-09-13_1789341103")
    args = parser.parse_args()

    scores = score_run(args.run)
    out_path = OUT_DIR / args.run / "scores.json"
    out_path.write_text(json.dumps(scores, indent=2))

    if scores["role"] == "sports_researcher":
        print(f"\n{'model':<32} {'gate':<6} {'teams':<6} unsupported / uncalled")
        for model, s in scores["models"].items():
            f = s["tool_fidelity"]
            print(f"{model:<32} {'PASS' if s['hard_gate_passed'] else 'FAIL':<6} {f['facts']:<6} "
                  f"{f['unsupported']} / {len(f['teams_without_tool_call'])}")
        print(f"\nFull scores: {out_path}")
        return

    print(f"\n{'model':<32} {'gate':<6} {'fmt':<5} {'grounded':<12} {'editorial':<10}")
    for model, s in scores["models"].items():
        gate = "PASS" if s["hard_gate_passed"] else "FAIL"
        fmt = "OK" if s["format_contract"]["passed"] else "FAIL"
        grounded = f'{s["grounding"]["unsupported"]}/{s["grounding"]["checked"]} unsupported'
        editorial = s["editorial"]["mean_score"]
        print(f"{model:<32} {gate:<6} {fmt:<5} {grounded:<12} {editorial}")
    print(f"\nFull scores: {out_path}")


if __name__ == "__main__":
    main()
