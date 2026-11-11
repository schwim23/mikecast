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


def test_every_pipeline_stage_is_drawn():
    """Each step_span() name in the pipeline code must be a box in pipeline.svg.

    The diagram's numbers are generated, but its boxes are hand-written layout; this
    catches a stage added to (or removed from) mikecast_briefing.py without the diagram
    following. Map a new stage with box(..., stage="<name>") in scripts/make_diagrams.py.
    """
    import re
    import subprocess

    tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    spans = set()
    for rel in tracked:
        if rel.startswith("crew/tests/") or not (ROOT / rel).exists():
            continue
        spans |= set(re.findall(r'step_span\(\s*"([^"]+)"', (ROOT / rel).read_text()))
    assert spans, "found no step_span() calls — did the tracing helper get renamed?"
    drawn = set(re.findall(r'data-stage="([^"]+)"', (ROOT / "docs/diagrams/pipeline.svg").read_text()))
    assert not spans - drawn, (
        f"pipeline stage(s) {sorted(spans - drawn)} have no box in docs/diagrams/pipeline.svg — "
        'add box(..., stage="<name>") in scripts/make_diagrams.py and run it'
    )
    assert not drawn - spans, (
        f"pipeline.svg draws stage(s) {sorted(drawn - spans)} that no step_span() uses any more — "
        "update scripts/make_diagrams.py and run it"
    )
