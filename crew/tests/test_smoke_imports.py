"""
Import-smoke test for every pipeline module.

Catches the cheap failure class before it ever reaches prod: a module-level
typo, a bad import, a syntax error introduced by an editor/formatter, a
renamed name a caller forgot to update. This is deliberately dumb (import
and nothing else) — it will NOT catch a NameError inside a function body
that only runs on a flag-gated path (see test_eval_fixture_dump.py for
that class of bug), but it's near-free and catches a different, equally
common class, so both run in CI before every deploy.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_ROOT_MODULES = [
    "mc_ad", "mc_audio", "mc_collect", "mc_config", "mc_critic",
    "mc_deliver", "mc_dist_state", "mc_edit", "mc_generate", "mc_metrics",
    "mc_plan", "mc_social", "mc_tracing", "mc_utils", "mc_video", "mc_youtube",
    "mikecast_briefing", "mikes_picks_ingest", "server",
]

_CREW_MODULES = [
    "crew.agents", "crew.context", "crew.critic_crew", "crew.distribution_crew",
    "crew.llm", "crew.model_compat", "crew.picks_crew", "crew.planning_crew",
    "crew.research_crew", "crew.sports_research_crew", "crew.tools", "crew.writing_crew",
]


@pytest.mark.parametrize("module_name", _ROOT_MODULES + _CREW_MODULES)
def test_module_imports_cleanly(module_name):
    importlib.import_module(module_name)
