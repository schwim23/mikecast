"""
Drift lint for README.md and CLAUDE.md (see DOCS_SYNC_PLAN.md, layer 2).

Deterministic, no network, no LLM. Catches the kind of rot found in the 2026-11-10 audit:
  1. a backticked repo path (`mc_foo.py`, `crew/tests/test_x.py`) that isn't a tracked file
  2. a backticked code identifier (`WIRE_RSS_FEEDS`, `_expand_if_short`, `mc_x.func()`)
     that no tracked source file contains any more
  3. a tracked mc_*.py / crew/*.py module missing from CLAUDE.md's module map or README

Only tracked files count (`git ls-files`), so the result matches CI — `git add` a new
file before referencing it. Inline code only; fenced code blocks (shell examples, the
project tree) are skipped for checks 1–2.
"""
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = ["README.md", "CLAUDE.md"]

# Backticked tokens that look like paths/identifiers but legitimately aren't in the repo.
PATH_ALLOW = {
    "mikecast/.env",          # git-ignored local secrets file
    "briefing_history.json",  # runtime state, .dockerignore'd
    "mikes_picks.json",
    "data/dist/",
    "data/social/",
    "eval/out/latest.json",   # written by run_daily_eval.sh
    "eval/out/",
    "eval/daily_eval.log",
    "mikecast.log",
    "task-definition.json",
    "facts/officeholders.json",  # S3 key (s3://mikecast-io-data/facts/...)
}
IDENT_ALLOW = {
    # SSM parameter names / AWS / external API names mentioned in prose
    "SIGNUP_HMAC_SECRET",
    "RESEND_API_URL",
    "MIKECAST_SIGNUP_ENDPOINT",
    "SSM_PREFIX",
    "AWS_DEFAULT_REGION",     # AWS SDK env var
}

_PATH_EXT = r"(?:py|sh|yml|yaml|md|json|js|html|css|txt|png)"
_PATH_RE = re.compile(rf"^[\w.\-/]+\.{_PATH_EXT}$|^[\w.\-]+(?:/[\w.\-]+)*/$")
_UPPER_RE = re.compile(r"^_?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_PRIVATE_RE = re.compile(r"^_[a-z][a-z0-9_]+(?:\(\))?$")
_DOTTED_RE = re.compile(r"^(?:mc_\w+|crew(?:\.\w+)+)\.([A-Za-z_]\w*)(?:\(\))?$")
_CALL_RE = re.compile(r"^([a-z_][a-z0-9_]{3,})\(\)$")


@lru_cache(maxsize=None)
def _tracked() -> tuple[str, ...]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return tuple(out.stdout.split())


@lru_cache(maxsize=None)
def _source_blob() -> str:
    exts = (".py", ".yml", ".yaml", ".sh", ".js", ".html", "Dockerfile")
    parts = []
    for rel in _tracked():
        if rel.endswith(exts) and not rel.startswith("lambda/newsletter_signup/build/") \
                and rel != "crew/tests/test_docs_drift.py":  # its docstring names dead identifiers
            try:
                parts.append((ROOT / rel).read_text(errors="ignore"))
            except (FileNotFoundError, IsADirectoryError):
                pass  # deleted in the working tree but still in the index
    return "\n".join(parts)


def _inline_code(doc: str) -> list[str]:
    text = (ROOT / doc).read_text()
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"<!-- BEGIN GENERATED.*?END GENERATED: \w+ -->", "", text, flags=re.S)
    return re.findall(r"`([^`\n]+)`", text)


def _path_candidates(doc: str) -> set[str]:
    out = set()
    for tok in _inline_code(doc):
        tok = tok.split("::")[0].strip()
        first = tok.split()[0] if tok.split() else ""
        if not first or first.startswith(("~", "/", ".venv", "s3://", "http", "-")):
            continue
        if any(c in first for c in "<>*{}$") or "YYYY" in first or "mikecast.io" in first:
            continue
        if _PATH_RE.match(first):
            out.add(first)
    return out


def _path_exists(path: str) -> bool:
    tracked = _tracked()
    if path.endswith("/"):
        return any(t.startswith(path) for t in tracked)
    if path in tracked:
        return True
    if "/" not in path:  # bare filename: accept it anywhere in the tree
        return any(t == path or t.endswith("/" + path) for t in tracked)
    return False


@pytest.mark.parametrize("doc", DOCS)
def test_backticked_paths_exist(doc):
    missing = sorted(p for p in _path_candidates(doc) if p not in PATH_ALLOW and not _path_exists(p))
    assert not missing, (
        f"{doc} references file(s) that aren't tracked in git: {missing}. "
        "Fix the doc (renamed/moved/deleted?) or `git add` the file."
    )


def _identifiers(doc: str) -> set[str]:
    out = set()
    for tok in _inline_code(doc):
        tok = tok.strip()
        if _UPPER_RE.match(tok) or _PRIVATE_RE.match(tok):
            out.add(tok.removesuffix("()"))
        elif m := _DOTTED_RE.match(tok):
            out.add(m.group(1))
        elif m := _CALL_RE.match(tok):
            out.add(m.group(1))
        else:
            # `NAME = {...}` / `NAME = 7` style snippets: check the assigned name
            m = re.match(r"^(_?[A-Za-z]\w+)\s*=", tok)
            if m and (_UPPER_RE.match(m.group(1)) or _PRIVATE_RE.match(m.group(1))):
                out.add(m.group(1))
    return out


@pytest.mark.parametrize("doc", DOCS)
def test_backticked_identifiers_exist_in_code(doc):
    blob = _source_blob()
    missing = sorted(
        name for name in _identifiers(doc)
        if name not in IDENT_ALLOW and not re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", blob)
    )
    assert not missing, (
        f"{doc} names identifier(s) no tracked source file contains: {missing}. "
        "Renamed or removed? Update the doc (or add a deliberate historical mention to IDENT_ALLOW)."
    )


def _modules() -> list[str]:
    return sorted(
        t for t in _tracked()
        if re.fullmatch(r"mc_\w+\.py|crew/[a-z]\w*\.py", t) and (ROOT / t).exists()
    )


def test_every_module_is_in_claude_module_map():
    text = (ROOT / "CLAUDE.md").read_text()
    section = text.split("## Module Map", 1)[1].split("\n## ", 1)[0]
    missing = [m for m in _modules() if m not in section and Path(m).name not in section]
    assert not missing, f"CLAUDE.md '## Module Map' is missing: {missing}"


def test_every_module_is_in_readme():
    text = (ROOT / "README.md").read_text()
    missing = [m for m in _modules() if Path(m).name not in text]
    assert not missing, f"README.md (project structure) is missing: {missing}"
