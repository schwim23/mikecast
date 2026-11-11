"""
README.md / CLAUDE.md generated blocks must match the code (see DOCS_SYNC_PLAN.md).

Fails when a model default, env var, article budget, word target, threshold or the
schedule changed without `python scripts/gen_docs.py` being re-run — or when two files
that must agree (both critics' thresholds, writer prompts vs. eval/score.py) disagree.
Because the CI `deploy` job `needs: test`, stale docs block a deploy.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load_gen_docs():
    spec = importlib.util.spec_from_file_location("gen_docs", ROOT / "scripts" / "gen_docs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen_docs = _load_gen_docs()


def _load_make_diagrams():
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("make_diagrams", ROOT / "scripts" / "make_diagrams.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generated_doc_blocks_are_current():
    stale = gen_docs.stale_docs()
    assert not stale, (
        "Generated blocks in " + ", ".join(stale) + " are stale — run: python scripts/gen_docs.py\n\n"
        + "\n".join(stale.values())
    )


def test_every_mc_config_env_var_is_documented():
    # render_env raises DocsGenError on an undocumented (or no-longer-read) var.
    gen_docs.render_env()


def test_missing_marker_is_an_error():
    with pytest.raises(gen_docs.DocsGenError):
        gen_docs._apply("no markers here", "env", "body", "X.md")


def test_disagreeing_sources_are_an_error():
    with pytest.raises(gen_docs.DocsGenError):
        gen_docs.same("threshold", {"a.py": 7, "b.py": 6})


def test_readme_diagrams_are_current():
    # docs/diagrams/*.svg embed model names + numbers read from code.
    stale = _load_make_diagrams().stale()
    assert not stale, f"Stale diagrams: {stale} — run: python scripts/make_diagrams.py"
