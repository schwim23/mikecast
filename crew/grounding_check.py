"""
Read-only grounding check over the final briefing (Step 8c, crew path).

Generalizes crew/critic_crew.py's NY-Sports-only fact-checker to everything the
audience sees or hears: every substantive sentence of the HTML briefing and of the
3-voice script, each checked against all of today's articles (not just its own
section's — the writers sometimes file a story under a different header than
the article's category, which read as unsupported in the 9/30–10/09 backfill). mc_facts' officeholder
list is passed as known-true context, so "President Trump" is supported when an
article only says "Trump" — but "former President Trump" is not. The ESPN
verified-facts / results blocks count as sources, as they do for the writers.
It runs on the critic's output, before mc_facts.fix_stale_titles, so it measures
what the writers actually produced.

Sentences that are the writer's own analysis of supported facts (Key Trends, What
to Watch) get their own "analysis" verdict and are counted, not flagged.

LOG-ONLY: unsupported sentences are logged at WARNING ("[Grounding] UNSUPPORTED")
and counted; nothing is rewritten. The point is to measure how often writers
reach past their sources before deciding whether to enforce.

NY Sports and the [JESSE] block are skipped — fact_check_ny_sports() already
covers them every critic pass. Disable with MIKECAST_GROUNDING_CHECK=0.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

from mc_config import OPENAI_SCORER_MODEL, TODAY

logger = logging.getLogger(__name__)

MAX_SENTENCES = 80   # bounds cost: ~80 checker calls (title+description context) per run
# The helper model (gpt-4o-mini) misread the articles on ~half its flags in the backfill
# ("none of the articles mention OpenAI" when two did), so the check uses the scorer model.
GROUNDING_MODEL = os.environ.get("MIKECAST_GROUNDING_MODEL", "") or OPENAI_SCORER_MODEL
_WORKERS = 8
_LINK_RESIDUE_RE = re.compile(r"(\s+(?:and\s+)?\.)+\s*$")   # what's left where source links were removed
_JESSE_BLOCK_RE = re.compile(r"\[JESSE\].*?(?=\[(?:MIKE|ELIZABETH)\]|$)", re.DOTALL)


def with_sports_sources(
    top_articles: dict[str, list[dict]],
    verified_sports_facts: dict[str, str] | None = None,
    ny_team_updates: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """Add the ESPN-sourced blocks the writers are given as pseudo-articles, so a
    correctly-stated score or next game doesn't read as unsupported."""
    extra = [{"title": f"ESPN verified facts: {team}", "description": text, "source": "ESPN"}
             for team, text in (verified_sports_facts or {}).items()]
    if ny_team_updates:
        from crew.sports_research_crew import format_ny_team_updates_block
        extra += [{"title": f"ESPN results/schedule: {u.get('team', '')}",
                   "description": format_ny_team_updates_block([u]), "source": "ESPN"}
                  for u in ny_team_updates]
    if not extra:
        return top_articles
    return {**top_articles, "NY Sports": list(top_articles.get("NY Sports", [])) + extra}


def _all_articles(top_articles: dict[str, list[dict]], picks: list[dict] | None) -> list[dict]:
    union = [a for arts in top_articles.values() for a in arts]
    # Mike's Picks are a legitimate source for the picks segment.
    union += [{"title": p.get("title", ""), "description": p.get("summary", ""), "source": "Mike's Picks"}
              for p in (picks or [])]
    return union


def collect_sentences(
    html: str,
    conversational_script: str,
    top_articles: dict[str, list[dict]],
    picks: list[dict] | None = None,
) -> list[tuple[str, str, list[dict]]]:
    """(where, sentence, articles-to-check-against) for every checkable sentence."""
    from crew.critic_crew import _split_sentences
    from bs4 import BeautifulSoup
    from eval.score import _extract_sections, _is_source_list

    everything = _all_articles(top_articles, picks)
    items: list[tuple[str, str, list[dict]]] = []

    # Each story ends with its source links ("<a>Mistral Large 4</a> and <a>…</a>"); without
    # a break, their text runs into the next story's first sentence and gets checked as a claim.
    soup = BeautifulSoup(html or "", "html.parser")
    for a in soup.find_all("a"):
        a.replace_with(" . ")
    for header, text in _extract_sections(str(soup)).items():
        if "sports" in header.lower():
            continue
        for s in _split_sentences(text):
            s = _LINK_RESIDUE_RE.sub("", s)
            if not _is_source_list(s):
                items.append((f"html:{header}", s, everything))

    script = _JESSE_BLOCK_RE.sub("", conversational_script or "")
    for s in _split_sentences(script):
        items.append(("conversational", s, everything))
    return items


def check_grounding(
    html: str,
    conversational_script: str,
    top_articles: dict[str, list[dict]],
    picks: list[dict] | None = None,
    verified_sports_facts: dict[str, str] | None = None,
    ny_team_updates: list[dict] | None = None,
) -> dict:
    """Run the check; log unsupported sentences. Never raises. Returns counts + flagged items."""
    empty = {"checked": 0, "unsupported": 0, "unclear": 0, "not_claims": 0, "analysis": 0,
             "skipped_over_cap": 0, "flagged": []}
    if os.environ.get("MIKECAST_GROUNDING_CHECK", "1") == "0":
        return empty
    try:
        from crew.tools import validate_claim_tool
        from eval.dump import dump_fixture
        from mc_facts import facts_lines

        sources = with_sports_sources(top_articles, verified_sports_facts, ny_team_updates)
        items = collect_sentences(html, conversational_script, sources, picks)
        background = facts_lines()
        capped = items[:MAX_SENTENCES]

        def _check(item: tuple[str, str, list[dict]]) -> dict:
            where, sentence, articles = item
            r = validate_claim_tool._run(claim=sentence, articles=articles, background=background,
                                     model=GROUNDING_MODEL, allow_analysis=True)
            return {"where": where, "sentence": sentence, **{k: r.get(k) for k in ("ok", "supported", "reasoning", "evidence")}}

        with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
            verdicts = list(ex.map(_check, capped))

        result = dict(empty, skipped_over_cap=max(0, len(items) - MAX_SENTENCES))
        for v in verdicts:
            if not v["ok"]:
                continue
            result["checked"] += 1
            if v["supported"] == "no":
                result["unsupported"] += 1
                result["flagged"].append(v)
                logger.warning("[Grounding] UNSUPPORTED (%s): %r — %s",
                               v["where"], v["sentence"][:200], (v["reasoning"] or "")[:200])
            elif v["supported"] == "unclear":
                result["unclear"] += 1
            elif v["supported"] == "n/a":
                result["not_claims"] += 1
            elif v["supported"] == "analysis":
                result["analysis"] += 1

        logger.info(
            "[Grounding] checked %d sentences: %d unsupported, %d unclear, %d not claims, "
            "%d writer analysis, %d over cap (log-only — nothing rewritten)",
            result["checked"], result["unsupported"], result["unclear"], result["not_claims"],
            result["analysis"], result["skipped_over_cap"],
        )
        dump_fixture("grounding", TODAY, {"input": {"items": [(w, s) for w, s, _ in capped]},
                                          "output": {"verdicts": verdicts}})
        return result
    except Exception as exc:
        logger.warning("[Grounding] check failed (non-fatal): %s", exc)
        return empty
