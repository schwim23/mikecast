#!/usr/bin/env python3
"""
eval/run_eval.py — Phase 1 offline replay harness (MODEL_EVAL_PLAN.md).

Replays ONE role's stage against a captured fixture (eval/fixtures/live/<date>/<role>.json,
written by --dump-eval-fixture on mikecast_briefing.py) for a baseline model and one or
more candidates, swapping the model via a monkeypatched LLM factory rather than touching
crew/llm.py's production model-selection logic. Writes outputs + token/cost/latency meta
under eval/out/<run_id>/.

This is Phase 1 only: replay + capture. Automated scoring (grounding/format/editorial —
Phase 2, eval/score.py) and human review (Phase 3, eval/review.py) are separate, later
steps per MODEL_EVAL_PLAN.md.

Usage:
  python eval/run_eval.py --role writer --date 2026-09-13 \
      --baseline anthropic/claude-sonnet-4-6 \
      --candidate anthropic/claude-sonnet-5 --candidate openai/gpt-5.6-sol --k 3

  python eval/run_eval.py --role critic --date 2026-09-13 \
      --baseline openai/gpt-4o \
      --candidate openai/gpt-5.6-terra --candidate anthropic/claude-sonnet-5

  python eval/run_eval.py --role helper --date 2026-09-13 \
      --baseline openai/gpt-4o-mini --candidate openai/gpt-5.6-luna
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from unittest import mock

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mikecast.eval.run_eval")

# Hard wall-clock cap per replay call — see the comment at its use site in main().
# 240s covers the slowest legitimate case observed (writer: ~70s; critic with several
# sequential section patches: a handful x ~30-60s each) with margin, while still
# bounding a genuinely stuck model/framework incompatibility loop.
REPLAY_TIMEOUT_S = 240

# Allow running as `python eval/run_eval.py` from anywhere — the mikecast repo root
# (parent of eval/) needs to be on sys.path for `import mc_config`, `import crew.*`, etc.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "live"
OUT_DIR = Path(__file__).parent / "out"


# ---------------------------------------------------------------------------
# Fixture loading — local first, S3 fallback (production/ECS runs land in S3;
# see eval/dump.py)
# ---------------------------------------------------------------------------

def load_fixture(role: str, date: str) -> dict:
    local_path = FIXTURES_DIR / date / f"{role}.json"
    if local_path.exists():
        with open(local_path) as f:
            return json.load(f)

    from mc_config import S3_BUCKET
    if S3_BUCKET:
        from mc_utils import s3_load_json
        key = f"eval/fixtures/live/{date}/{role}.json"
        data = s3_load_json(S3_BUCKET, key)
        if data is not None:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            with open(local_path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info("Fetched fixture from s3://%s/%s -> cached at %s", S3_BUCKET, key, local_path)
            return data

    raise FileNotFoundError(
        f"No fixture for role={role!r} date={date!r} — checked {local_path} and S3. "
        "Run mikecast_briefing.py --crew --eval-only --dump-eval-fixture first."
    )


# ---------------------------------------------------------------------------
# Model plumbing
# ---------------------------------------------------------------------------

# Some newer-generation models reject any temperature value other than their
# default — confirmed 2026-09-13 for anthropic/claude-sonnet-5 ("temperature is
# deprecated for this model") and openai/gpt-5.6-sol ("Only the default (1)
# value is supported"). Omit temperature entirely for these rather than
# passing a value the API 400s on — a silent per-call failure otherwise, since
# _kickoff_single_task/_run_scorer swallow the exception and return "".
_NO_CUSTOM_TEMPERATURE_MODELS = {
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-5",
    "anthropic/claude-fable-5",
    "anthropic/claude-fable-5-1",
}
_NO_CUSTOM_TEMPERATURE_PREFIXES = ("openai/gpt-5.6-", "openai/gpt-6-")


def _supports_custom_temperature(model: str) -> bool:
    if model in _NO_CUSTOM_TEMPERATURE_MODELS:
        return False
    return not model.startswith(_NO_CUSTOM_TEMPERATURE_PREFIXES)


def _make_llm(model: str, temperature: float, max_tokens: int):
    from crewai import LLM
    from mc_config import ANTHROPIC_API_KEY, OPENAI_API_KEY

    if model.startswith("anthropic/"):
        api_key = ANTHROPIC_API_KEY or None
    elif model.startswith("openai/"):
        api_key = OPENAI_API_KEY or None
    else:
        raise ValueError(f"Unrecognized model provider prefix in {model!r} (expected anthropic/ or openai/)")
    kwargs = {"model": model, "api_key": api_key, "max_tokens": max_tokens}
    if _supports_custom_temperature(model):
        kwargs["temperature"] = temperature
    else:
        logger.info("Omitting temperature override for %s — model rejects a non-default value.", model)
    return LLM(**kwargs)


class _CrewCapture:
    """Context manager: records every Crew instance that runs .kickoff() during
    the block, so usage can be attributed per-agent afterward (CrewAI 0.86.0 has
    no other way to retrieve token usage without holding the Crew reference)."""

    def __init__(self):
        self.crews: list = []
        self._patcher = None

    def __enter__(self):
        import crewai
        original_kickoff = crewai.Crew.kickoff
        captured = self.crews

        def patched_kickoff(crew_self, *a, **kw):
            result = original_kickoff(crew_self, *a, **kw)
            captured.append(crew_self)
            return result

        self._patcher = mock.patch.object(crewai.Crew, "kickoff", patched_kickoff)
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


def _usage_by_model(crews: list) -> dict[str, dict]:
    """{model_string: {prompt_tokens, completion_tokens, cached_prompt_tokens}} summed
    across every agent in every captured crew — attributes usage to whatever model each
    agent actually ran (matters for critic-role replays, where the patcher's model is
    NOT swapped and shouldn't be priced as if it were the candidate)."""
    by_model: dict[str, dict] = defaultdict(lambda: {
        "prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0,
    })
    for crew in crews:
        try:
            crew.calculate_usage_metrics()
        except Exception:
            continue
        for agent in crew.agents:
            if not hasattr(agent, "_token_process"):
                continue
            summary = agent._token_process.get_summary()
            model = getattr(getattr(agent, "llm", None), "model", "unknown")
            by_model[model]["prompt_tokens"] += summary.prompt_tokens
            by_model[model]["completion_tokens"] += summary.completion_tokens
            by_model[model]["cached_prompt_tokens"] += summary.cached_prompt_tokens
    return dict(by_model)


def _cost_from_usage(by_model: dict[str, dict]) -> tuple[float, list[str]]:
    from eval.pricing import cost_usd
    total = 0.0
    warnings: list[str] = []
    for model, usage in by_model.items():
        try:
            total += cost_usd(model, usage["prompt_tokens"], usage["completion_tokens"])
        except KeyError:
            warnings.append(f"no pricing entry for {model!r} — excluded from cost total")
    return total, warnings


# ---------------------------------------------------------------------------
# Per-role replay adapters
# ---------------------------------------------------------------------------

def replay_writer(fixture: dict, model: str) -> dict:
    from crew.writing_crew import run_writing

    inp = fixture["input"]
    llm = _make_llm(model, temperature=0.4, max_tokens=6000)
    t0 = time.time()
    with mock.patch("crew.agents.claude_writer_llm", return_value=llm), _CrewCapture() as cap:
        html, single, conv = run_writing(
            inp["top_articles"], inp["picks"], inp["trending"],
            verified_sports_facts=inp.get("verified_sports_facts"),
            ny_team_updates=inp.get("ny_team_updates"),
        )
    latency_s = time.time() - t0
    return {
        "outputs": {"html": html, "single_voice_script": single, "conversational_script": conv},
        "latency_s": latency_s,
        "usage_by_model": _usage_by_model(cap.crews),
    }


def replay_critic(fixture: dict, model: str) -> dict:
    from crew.critic_crew import run_critic_pass

    inp = fixture["input"]
    llm = _make_llm(model, temperature=0.2, max_tokens=1500)
    t0 = time.time()
    with mock.patch("crew.agents.openai_critic_llm", return_value=llm), _CrewCapture() as cap:
        html, single, conv, metrics = run_critic_pass(
            inp["html"], inp["single_voice_script"], inp["conversational_script"],
            inp["top_articles"], inp["picks"], inp["trending"],
            verified_sports_facts=inp.get("verified_sports_facts"),
        )
    latency_s = time.time() - t0
    return {
        "outputs": {
            "html": html, "single_voice_script": single, "conversational_script": conv,
            "critic_metrics": metrics,
        },
        "latency_s": latency_s,
        "usage_by_model": _usage_by_model(cap.crews),
    }


def replay_helper(fixture: dict, model: str) -> dict:
    # Helper role's only live call site (crew/tools.py::ValidateClaimTool) uses the
    # raw OpenAI SDK directly, not CrewAI — OpenAI-only candidates, see
    # MODEL_EVAL_PLAN.md §1b. Patch the module-level constant it reads (it was bound
    # by value at import time via `from mc_config import OPENAI_HELPER_MODEL`, so
    # setting the env var alone would have no effect here).
    from crew.tools import validate_claim_tool

    inp = fixture["input"]
    sentences = inp["sentences"]
    articles = inp["sports_articles"]
    t0 = time.time()
    verdicts = []
    with mock.patch("crew.tools.OPENAI_HELPER_MODEL", model):
        for sentence in sentences:
            result = validate_claim_tool._run(claim=sentence, articles=articles)
            verdicts.append({"sentence": sentence, **result})
    latency_s = time.time() - t0

    usage_by_model: dict[str, dict] = defaultdict(lambda: {
        "prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0,
    })
    for v in verdicts:
        u = v.get("usage") or {}
        usage_by_model[model]["prompt_tokens"] += u.get("prompt_tokens", 0)
        usage_by_model[model]["completion_tokens"] += u.get("completion_tokens", 0)

    checked = sum(1 for v in verdicts if v.get("ok"))
    unsupported = sum(1 for v in verdicts if v.get("supported") == "no")
    return {
        "outputs": {"verdicts": verdicts, "checked": checked, "unsupported": unsupported},
        "latency_s": latency_s,
        "usage_by_model": dict(usage_by_model),
    }


REPLAY_FNS = {"writer": replay_writer, "critic": replay_critic, "helper": replay_helper}
OPENAI_ONLY_ROLES = {"helper"}  # see MODEL_EVAL_PLAN.md §1b


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------

def _safe_name(model: str) -> str:
    return model.replace("/", "_")


def _write_run(run_dir: Path, model: str, k_index: int, date: str, role: str, result: dict) -> dict:
    model_dir = run_dir / _safe_name(model) / f"k{k_index}"
    model_dir.mkdir(parents=True, exist_ok=True)

    outputs = result["outputs"]
    if role in ("writer", "critic"):
        (model_dir / f"{date}.html").write_text(outputs.get("html", ""))
        (model_dir / f"{date}.podcast.txt").write_text(outputs.get("single_voice_script", ""))
        (model_dir / f"{date}.conversational.txt").write_text(outputs.get("conversational_script", ""))
        if role == "critic":
            (model_dir / f"{date}.critic_metrics.json").write_text(
                json.dumps(outputs.get("critic_metrics", {}), indent=2)
            )
    else:  # helper
        (model_dir / f"{date}.verdicts.json").write_text(json.dumps(outputs, indent=2))

    cost_usd, cost_warnings = _cost_from_usage(result["usage_by_model"])
    meta = {
        "model": model,
        "k_index": k_index,
        "latency_s": round(result["latency_s"], 2),
        "usage_by_model": result["usage_by_model"],
        "cost_usd": round(cost_usd, 4),
        "cost_warnings": cost_warnings,
    }
    (model_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", required=True, choices=sorted(REPLAY_FNS))
    parser.add_argument("--date", required=True, help="Fixture date, YYYY-MM-DD")
    parser.add_argument("--baseline", required=True, help="Baseline model string, e.g. anthropic/claude-sonnet-4-6")
    parser.add_argument("--candidate", action="append", required=True, dest="candidates",
                         help="Candidate model string; repeatable")
    parser.add_argument("--k", type=int, default=1, help="Repeats per model (writer: use 2-3 to capture variance)")
    args = parser.parse_args()

    if args.role in OPENAI_ONLY_ROLES:
        for m in [args.baseline, *args.candidates]:
            if not m.startswith("openai/"):
                parser.error(
                    f"role={args.role!r} only supports openai/* models (its live code path "
                    f"calls the raw OpenAI SDK directly) — got {m!r}. See MODEL_EVAL_PLAN.md §1b."
                )

    try:
        fixture = load_fixture(args.role, args.date)
    except FileNotFoundError as exc:
        parser.error(str(exc))
    replay_fn = REPLAY_FNS[args.role]

    run_id = f"{args.role}_{args.date}_{int(time.time())}"
    run_dir = OUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Run: %s (role=%s date=%s k=%d)", run_id, args.role, args.date, args.k)

    all_models = [("baseline", args.baseline)] + [("candidate", c) for c in args.candidates]
    summary: dict = {"run_id": run_id, "role": args.role, "date": args.date, "k": args.k, "results": {}}

    for label, model in all_models:
        logger.info("--- %s: %s ---", label, model)
        metas = []
        for i in range(args.k):
            logger.info("  run %d/%d...", i + 1, args.k)
            try:
                # Hard wall-clock cap. Some newer models don't reliably emit
                # CrewAI's expected Thought:/Action:/Final Answer: scaffolding for
                # a no-tool task and CrewAI retries the format-correction prompt —
                # confirmed 2026-09-13 with openai/gpt-5.6-terra on the critic
                # scorer (make_section_scorer has no max_execution_time, unlike
                # make_sports_researcher's 180s cap), which hung for 10+ minutes
                # making repeated calls before being killed manually. Without this
                # timeout a single incompatible candidate model can burn unbounded
                # real API spend and block the whole eval run.
                #
                # NOT a `with ThreadPoolExecutor() as ex:` block — its __exit__ calls
                # shutdown(wait=True), which blocks until the stuck worker thread
                # finishes, defeating the timeout entirely (confirmed 2026-09-13: the
                # first version of this guard used `with` and still hung for 6+
                # minutes). shutdown(wait=False) abandons the stuck thread instead —
                # it keeps running (and keeps spending API calls) in the background
                # until it errors out or the process exits, but the harness itself
                # moves on immediately rather than blocking on it.
                ex = ThreadPoolExecutor(max_workers=1)
                try:
                    result = ex.submit(replay_fn, fixture, model).result(timeout=REPLAY_TIMEOUT_S)
                finally:
                    ex.shutdown(wait=False)
            except FuturesTimeoutError:
                logger.error(
                    "  TIMED OUT after %ds — %s likely isn't producing CrewAI's expected "
                    "agent-loop format (see MODEL_EVAL_PLAN.md). Skipping this run.",
                    REPLAY_TIMEOUT_S, model,
                )
                metas.append({"model": model, "k_index": i, "error": f"timed out after {REPLAY_TIMEOUT_S}s"})
                continue
            except Exception as exc:
                logger.error("  FAILED: %s", exc)
                metas.append({"model": model, "k_index": i, "error": str(exc)})
                continue
            meta = _write_run(run_dir, model, i, args.date, args.role, result)
            metas.append(meta)
            logger.info("  latency=%.1fs cost=$%.4f", meta["latency_s"], meta["cost_usd"])
        ok_metas = [m for m in metas if "error" not in m]
        summary["results"][model] = {
            "label": label,
            "runs": metas,
            "avg_latency_s": round(sum(m["latency_s"] for m in ok_metas) / len(ok_metas), 2) if ok_metas else None,
            "avg_cost_usd": round(sum(m["cost_usd"] for m in ok_metas) / len(ok_metas), 4) if ok_metas else None,
            "failures": len(metas) - len(ok_metas),
        }

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    logger.info("Wrote %s", run_dir / "summary.json")

    print(f"\n{'model':<40} {'avg latency (s)':<18} {'avg cost ($)':<14} failures")
    for model, r in summary["results"].items():
        print(f"{model:<40} {str(r['avg_latency_s']):<18} {str(r['avg_cost_usd']):<14} {r['failures']}")
    print(f"\nFull output: {run_dir}")


if __name__ == "__main__":
    main()
