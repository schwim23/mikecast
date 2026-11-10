#!/usr/bin/env python3
"""
Regenerate the code-derived blocks of README.md and CLAUDE.md.

    python scripts/gen_docs.py          # rewrite stale blocks in place
    python scripts/gen_docs.py --check  # exit 1 if any block is stale (CI runs this via
                                        # crew/tests/test_docs_in_sync.py)

A block is the text between
    <!-- BEGIN GENERATED: <name> (scripts/gen_docs.py — do not edit by hand) -->
    <!-- END GENERATED: <name> -->
Everything outside the markers is hand-written prose and is never touched.

Values are read by parsing source (ast / regex), never by importing the project, so the
output doesn't depend on the caller's environment (a locally exported
CLAUDE_WRITER_MODEL would otherwise leak into the docs) and needs only the stdlib.

To document a new env var read in mc_config.py, add it to ENV_DOCS below — generation
fails until you do, so a new var can't ship undocumented. See DOCS_SYNC_PLAN.md.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DocsGenError(Exception):
    """The source is inconsistent or a doc entry is missing — fix the code/ENV_DOCS."""


# ── Source readers ───────────────────────────────────────────────────────────

def _tree(rel: str) -> ast.Module:
    return ast.parse((ROOT / rel).read_text(), filename=rel)


def _literal(node: ast.AST, rel: str, name: str):
    # timedelta(hours=N) → "Nh" (MAX_FUTURE_SKEW)
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "timedelta":
        parts = [f"{ast.literal_eval(k.value)}{k.arg[0]}" for k in node.keywords]
        return " ".join(parts)
    try:
        return ast.literal_eval(node)
    except ValueError as exc:
        raise DocsGenError(f"{rel}: {name} is not a literal") from exc


def module_const(rel: str, name: str):
    """Value of a module-level `NAME = <literal>` (or `A, B = 1, 2`)."""
    for node in _tree(rel).body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                return _literal(node.value, rel, name)
            if isinstance(t, ast.Tuple) and isinstance(node.value, ast.Tuple):
                for el, val in zip(t.elts, node.value.elts):
                    if isinstance(el, ast.Name) and el.id == name:
                        return _literal(val, rel, name)
    raise DocsGenError(f"{rel}: module-level constant {name} not found")


def env_defaults(rel: str) -> dict[str, str]:
    """Every os.environ.get("VAR", default) in *rel*, in source order."""
    out: dict[str, str] = {}
    for node in ast.walk(_tree(rel)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("get", "getenv")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            src = ast.unparse(node.func.value)
            if src not in ("os.environ", "os"):
                continue
            default = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                default = node.args[1].value
            out.setdefault(node.args[0].value, default)
    return dict(sorted(out.items(), key=lambda kv: _lineno(rel, kv[0])))


def _lineno(rel: str, var: str) -> int:
    for i, line in enumerate((ROOT / rel).read_text().splitlines()):
        if f'"{var}"' in line:
            return i
    return 10**9


def call_kwarg(rel: str, func: str, kw: str):
    """Literal values of kwarg *kw* in every call to *func* (bare name or attribute)."""
    vals = []
    for node in ast.walk(_tree(rel)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == func:
                for k in node.keywords:
                    if k.arg == kw:
                        vals.append(_literal(k.value, rel, f"{func}({kw}=)"))
    if not vals:
        raise DocsGenError(f"{rel}: no call {func}({kw}=...) found")
    return vals


def func_default(rel: str, func: str, arg: str):
    for node in ast.walk(_tree(rel)):
        if isinstance(node, ast.FunctionDef) and node.name == func:
            args = node.args.args
            defaults = node.args.defaults
            offset = len(args) - len(defaults)
            for i, a in enumerate(args):
                if a.arg == arg and i >= offset:
                    return _literal(defaults[i - offset], rel, f"{func}({arg})")
            for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if a.arg == arg and d is not None:
                    return _literal(d, rel, f"{func}({arg})")
    raise DocsGenError(f"{rel}: default for {func}({arg}) not found")


def same(label: str, values: dict[str, object]):
    """All sources must agree; return the shared value."""
    distinct = set(map(repr, values.values()))
    if len(distinct) != 1:
        detail = ", ".join(f"{k}={v!r}" for k, v in values.items())
        raise DocsGenError(f"{label} disagrees across files: {detail}")
    return next(iter(values.values()))


def deploy_schedule() -> str:
    text = (ROOT / ".github/workflows/deploy.yml").read_text()
    expr = re.search(r'--schedule-expression\s+"([^"]+)"', text)
    tz = re.search(r'--schedule-expression-timezone\s+"([^"]+)"', text)
    if not (expr and tz):
        raise DocsGenError("deploy.yml: schedule expression/timezone not found")
    return f"`{expr.group(1)}` {tz.group(1)}"


# ── Hand-kept descriptions (the only prose the generator owns) ───────────────

# var → (required, purpose). Order in the table follows mc_config.py.
ENV_DOCS: dict[str, tuple[str, str]] = {
    "NYTAPIKEY": ("Yes", "NYT Top Stories + Article Search"),
    "OPENAI_API_KEY": ("Yes", "OpenAI scorer/helper models (scoring, clustering, enrichment, merge/repeat filters, grounding) + OpenAI TTS fallback"),
    "GMAIL_APP_PASSWORD": ("Yes", "Gmail SMTP (16-digit app password)"),
    "GMAIL_FROM": ("Yes", "Sender address"),
    "GMAIL_TO": ("Yes", "Recipient address"),
    "RESEND_API_KEY": ("No", "Resend key — enables the daily email newsletter broadcast"),
    "RESEND_AUDIENCE_ID": ("No", 'Resend "MikeCast Daily" audience id (required with the key)'),
    "RESEND_FROM": ("No", "Newsletter sender"),
    "RESEND_REPLY_TO": ("No", "Newsletter reply-to"),
    "ELEVENLABS_API_KEY": ("No", "3-voice audio (Mike/Elizabeth/Jesse)"),
    "ELEVENLABS_VOICE_MIKE": ("No", "ElevenLabs voice ID"),
    "ELEVENLABS_VOICE_ELIZABETH": ("No", "ElevenLabs voice ID"),
    "ELEVENLABS_VOICE_JESSE": ("No", "ElevenLabs voice ID"),
    "XAI_API_KEY": ("No", "Grok adaptive search planning (Step 0)"),
    "X_API_KEY": ("No", "X auto-post (OAuth 1.0a, Step 11)"),
    "X_API_SECRET": ("No", "X auto-post"),
    "X_ACCESS_TOKEN": ("No", "X auto-post"),
    "X_ACCESS_TOKEN_SECRET": ("No", "X auto-post"),
    "META_ACCESS_TOKEN": ("No", "Instagram auto-post (Graph API, Step 11)"),
    "IG_USER_ID": ("No", "Instagram Business account id"),
    "DD_API_KEY": ("No", "Datadog OTLP tracing (`mc_tracing.py`) + run metrics (`mc_metrics.py`); both no-op when unset"),
    "DD_SITE": ("No", "Datadog site"),
    "SOCIAL_MEDIA_KIND": ("No", "`card` or `reel` (the prod task def sets `reel`)"),
    "CLOUDFRONT_DIST_ID": ("No", "CloudFront distribution invalidated by `mc_edit.py` after a republish"),
    "ANTHROPIC_API_KEY": ("Crew only", "Claude for the writing crew, patcher and social copywriter. Locally lives in git-ignored `mikecast/.env` (loaded by `mc_config.py` via python-dotenv), NOT `~/.profile` — a globally exported key forces Claude Code onto API-key auth. Prod gets it from SSM via the task def."),
    "CLAUDE_WRITER_MODEL": ("No", "LiteLLM model for writers, section patcher, social copywriter"),
    "OPENAI_SCORER_MODEL": ("No", "LiteLLM model for article scorer, duplicate-merge/repeat-event filters, grounding check"),
    "OPENAI_CRITIC_MODEL": ("No", "LiteLLM model for the section-quality critic scorer (despite the name, defaults to Claude)"),
    "OPENAI_HELPER_MODEL": ("No", "LiteLLM model for helpers: clustering, enrichment, picks, NY Sports fact-checker"),
    "S3_BUCKET": ("Prod", "Enables S3 output mode; set in the task def (`mikecast-io-data`), unset locally"),
}

# Defaults not worth printing (personal addresses / account ids live in code, not docs).
HIDE_DEFAULT = {"GMAIL_FROM", "GMAIL_TO", "RESEND_REPLY_TO", "CLOUDFRONT_DIST_ID"}

MODEL_ROLES = [
    ("HTML / single-voice / 3-voice writers + section patcher + social copywriter", "CLAUDE_WRITER_MODEL"),
    ("Per-category article scorer, duplicate-merge + repeat-event filters, grounding check", "OPENAI_SCORER_MODEL"),
    ("Section quality scorer (critic)", "OPENAI_CRITIC_MODEL"),
    ("Clustering, enrichment, picks, planner orchestration, NY Sports fact-checker", "OPENAI_HELPER_MODEL"),
]


# ── Block renderers ──────────────────────────────────────────────────────────

def render_env() -> str:
    found = env_defaults("mc_config.py")
    undocumented = [v for v in found if v not in ENV_DOCS]
    if undocumented:
        raise DocsGenError(
            f"mc_config.py reads env var(s) with no entry in ENV_DOCS: {undocumented} — "
            "add a (required, purpose) entry in scripts/gen_docs.py"
        )
    gone = [v for v in ENV_DOCS if v not in found]
    if gone:
        raise DocsGenError(f"ENV_DOCS documents var(s) mc_config.py no longer reads: {gone}")
    rows = ["| Variable | Required | Default | Purpose |", "|---|---|---|---|"]
    for var, default in found.items():
        req, purpose = ENV_DOCS[var]
        shown = "(set in code)" if var in HIDE_DEFAULT and default else (f"`{default}`" if default else "—")
        rows.append(f"| `{var}` | {req} | {shown} | {purpose} |")
    return "\n".join(rows)


def render_models() -> str:
    defaults = env_defaults("mc_config.py")
    grok = same("Grok planner model", {"mc_plan.py": call_kwarg("mc_plan.py", "create", "model")[0]})
    rows = ["| Role | Default model | Env var |", "|---|---|---|"]
    for role, var in MODEL_ROLES:
        rows.append(f"| {role} | `{defaults[var]}` | `{var}` |")
    rows.append(f"| Search planning (Step 0, xAI live search) | `{grok}` | — (hardcoded in `mc_plan.py`) |")
    return "\n".join(rows)


def render_numbers() -> str:
    wmin = module_const("eval/score.py", "_PODCAST_WORD_MIN")
    wmax = module_const("eval/score.py", "_PODCAST_WORD_MAX")
    for rel in ("crew/writing_crew.py", "crew/agents.py"):
        text = (ROOT / rel).read_text()
        if str(wmin) not in text or str(wmax) not in text:
            raise DocsGenError(
                f"{rel} doesn't mention the {wmin}–{wmax} word target from eval/score.py — "
                "the writer prompts and the scorer must agree"
            )
    threshold = same("Critic threshold _WEAK_THRESHOLD", {
        "mc_critic.py": module_const("mc_critic.py", "_WEAK_THRESHOLD"),
        "crew/critic_crew.py": module_const("crew/critic_crew.py", "_WEAK_THRESHOLD"),
    })
    stale = same("Stale-article window max_age_days", {
        "crew/research_crew.py": call_kwarg("crew/research_crew.py", "filter_stale_articles", "max_age_days")[0],
        "mikecast_briefing.py": call_kwarg("mikecast_briefing.py", "filter_stale_articles", "max_age_days")[0],
    })
    enrich = same("Enriched article count", {
        "crew/research_crew.py": func_default("crew/research_crew.py", "run_research", "enrich_top_n"),
        "mikecast_briefing.py": call_kwarg("mikecast_briefing.py", "enrich_top_stories", "top_n")[0],
    })
    rows = [
        "| Setting | Value | Source of truth |",
        "|---|---|---|",
        f"| Daily schedule (EventBridge) | {deploy_schedule()} | `.github/workflows/deploy.yml` (re-applied every deploy) |",
        f"| Article budget | {module_const('mc_config.py', 'TOP_ARTICLES_TOTAL')} (each category ≥3) | `TOP_ARTICLES_TOTAL` in `mc_config.py` |",
        f"| Articles enriched with full text | top {enrich} | `run_research(enrich_top_n=)` / legacy `enrich_top_stories(top_n=)` |",
        f"| Stale-article cutoff | {stale} days | `filter_stale_articles(max_age_days=)` calls (crew + legacy) |",
        f"| Max future-dated skew | {module_const('mc_collect.py', 'MAX_FUTURE_SKEW')} | `MAX_FUTURE_SKEW` in `mc_collect.py` |",
        f"| Repeat-event lookback | {module_const('mc_collect.py', 'REPEAT_LOOKBACK_DAYS')} episodes | `REPEAT_LOOKBACK_DAYS` in `mc_collect.py` |",
        f"| Podcast script target | {wmin}–{wmax} words (~8 min) | `_PODCAST_WORD_MIN/MAX` in `eval/score.py`; must match `crew/writing_crew.py` + `crew/agents.py` |",
        f"| Expansion pass | below {module_const('crew/writing_crew.py', '_EXPAND_BELOW_WORDS')} words; kept if ≤{module_const('crew/writing_crew.py', '_EXPAND_ACCEPT_MAX_WORDS')} | `_EXPAND_BELOW_WORDS` / `_EXPAND_ACCEPT_MAX_WORDS` in `crew/writing_crew.py` |",
        f"| Critic patch threshold | below {threshold}/10 | `_WEAK_THRESHOLD` in `mc_critic.py` + `crew/critic_crew.py` |",
    ]
    return "\n".join(rows)


BLOCKS: dict[str, list[tuple[str, callable]]] = {
    "CLAUDE.md": [("env", render_env), ("numbers", render_numbers)],
    "README.md": [("models", render_models)],
}

_BEGIN = "<!-- BEGIN GENERATED: {name} (scripts/gen_docs.py — do not edit by hand) -->"
_END = "<!-- END GENERATED: {name} -->"


def _apply(text: str, name: str, body: str, doc: str) -> str:
    begin, end = _BEGIN.format(name=name), _END.format(name=name)
    if text.count(begin) != 1 or text.count(end) != 1:
        raise DocsGenError(f"{doc}: expected exactly one '{begin}' … '{end}' pair")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n{body}\n{end}{tail}"


def render_all() -> dict[str, tuple[str, str]]:
    """doc → (current text, regenerated text)."""
    out = {}
    for doc, blocks in BLOCKS.items():
        current = (ROOT / doc).read_text()
        new = current
        for name, fn in blocks:
            new = _apply(new, name, fn(), doc)
        out[doc] = (current, new)
    return out


def stale_docs() -> dict[str, str]:
    """doc → unified diff, for every doc whose generated blocks are out of date."""
    return {
        doc: "".join(difflib.unified_diff(
            cur.splitlines(True), new.splitlines(True), f"{doc} (committed)", f"{doc} (generated)"))
        for doc, (cur, new) in render_all().items() if cur != new
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="don't write; exit 1 if anything is stale")
    args = ap.parse_args()
    try:
        if args.check:
            stale = stale_docs()
            for diff in stale.values():
                sys.stdout.write(diff)
            if stale:
                print(f"\nStale generated docs: {', '.join(stale)} — run: python scripts/gen_docs.py")
                return 1
            print("Generated doc blocks are up to date.")
            return 0
        for doc, (cur, new) in render_all().items():
            if cur != new:
                (ROOT / doc).write_text(new)
                print(f"updated {doc}")
        return 0
    except DocsGenError as exc:
        print(f"gen_docs: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
