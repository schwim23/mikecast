"""
Step 8b — Critic Crew.

Scorer (GPT-4o) reads the produced HTML briefing and grades each category
section 1-10. Sections with score < 7 are flagged as weak. A Patcher
(Claude) rewrites only the weak sections, except NY Sports — which is
NEVER patched (matches the legacy NEVER_PATCH_NORMALIZED guard in
mc_critic.py). If the NY Sports section were ever patched in a future
iteration, the Fact-Checker (validate_claim_against_articles) would gate
the patch — for now, the policy of "leave NY Sports alone" is preserved
verbatim from the legacy path.

After any HTML patch, both podcast scripts are regenerated against the
(unchanged) article set — same semantics as legacy.

max_iter = 1 (no critic loop).
"""

from __future__ import annotations

import json
import logging
import re

from crewai import Crew, Process, Task

from crew import agents as _agents
from crew.agents import make_section_patcher
from crew.model_compat import supports_custom_temperature
from crew.tools import validate_claim_tool
from crew.writing_crew import _kickoff_single_task
from eval.dump import dump_fixture
from mc_config import TODAY
from mc_critic import _extract_html_summary  # reuse the legacy HTML summariser

logger = logging.getLogger("mikecast.crew.critic")

# NY Sports section is never patched — matches legacy invariant.
_NEVER_PATCH_NORMALIZED = {"ny sports"}
_WEAK_THRESHOLD = 7


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

def _llm_complete(llm, messages: list[dict]) -> tuple[str, dict]:
    """
    Direct LiteLLM completion — deliberately bypasses CrewAI's Agent/Task/Crew
    ReAct-style executor. Needed because that executor's format-error retry
    path has NO max_iter bound (confirmed 2026-09-13 against installed crewai
    0.86.0, agents/crew_agent_executor.py::_invoke_loop: the `except
    OutputParserException` branch recurses back into _invoke_loop without
    checking self.iterations against self.max_iter — only the successful-parse
    path increments that counter). A model that doesn't reliably produce the
    "Thought:/Action:"/"Final Answer:" text CrewAI expects for a no-tool task
    (confirmed with gpt-5.6-terra as this scorer) hangs forever retrying the
    FORMAT, not the actual task — no timeout on our side can fix a loop with no
    exit condition. A plain completion call has no such format requirement and
    works identically across every provider LiteLLM supports.
    """
    import litellm

    params = {
        "model": llm.model,
        "api_key": llm.api_key,
        "messages": messages,
        "max_tokens": llm.max_tokens,
    }
    if llm.temperature is not None and supports_custom_temperature(llm.model):
        params["temperature"] = llm.temperature
    resp = litellm.completion(**params)
    text = resp.choices[0].message.content or ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    if getattr(resp, "usage", None):
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
        }
    return text, usage


def _run_scorer(html: str, categorised: dict[str, list[dict]]) -> tuple[dict, dict]:
    """Returns (result, usage) — result is {category_scores, issues, overall_passed}."""
    summary = _extract_html_summary(html, categorised)
    # Called via the crew.agents module reference (not imported directly) so
    # eval/run_eval.py's `mock.patch("crew.agents.openai_critic_llm", ...)`
    # still takes effect here — same "patch where it's used" requirement that
    # applied when this LLM was built inside an Agent defined in crew.agents.
    llm = _agents.openai_critic_llm()
    prompt = (
        "Below is a compact summary of today's MikeCast HTML briefing. Score each "
        "category section 1-10 on:\n"
        "  - depth: does it have 3+ substantive stories?\n"
        "  - analysis: does it go beyond mere headlines?\n"
        "  - substance: does it include specific facts, numbers, or implications?\n\n"
        "A score of 7+ means acceptable. Below 7 means the section needs improvement.\n\n"
        f"BRIEFING SUMMARY:\n{summary}\n\n"
        "Return ONLY valid JSON (no markdown, no commentary):\n"
        '{"category_scores": {"AI & Tech": 8, ...}, '
        '"issues": {"Business & Markets": "Only 1 story, lacks analysis"}, '
        '"overall_passed": true}'
    )
    try:
        raw, usage = _llm_complete(llm, [{"role": "user", "content": prompt}])
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("```")).strip()
        return json.loads(raw), usage
    except Exception as exc:
        logger.warning("[Critic Crew] scorer failed (non-fatal): %s", exc)
        return {"category_scores": {}, "issues": {}, "overall_passed": True}, {"prompt_tokens": 0, "completion_tokens": 0}


# ---------------------------------------------------------------------------
# Patcher
# ---------------------------------------------------------------------------

def _run_patcher(cat: str, articles: list[dict], issue: str) -> str:
    """Ask Claude to rewrite the HTML body for a single weak section."""
    patcher = make_section_patcher()

    article_lines: list[str] = []
    for i, art in enumerate(articles[:8], 1):
        title = art.get("title", "")
        desc = (art.get("description") or "")[:200]
        why = art.get("why_it_matters", "")
        block = f"{i}. {title}\n   Description: {desc}"
        if why:
            block += f"\n   Why it matters: {why}"
        article_lines.append(block)

    desc = (
        f"You are rewriting the '{cat}' section of an HTML briefing.\n\n"
        f"Quality issue identified: {issue}\n\n"
        f"Available articles ({len(articles)} total — use ONLY these):\n"
        + "\n".join(article_lines)
        + "\n\nWrite an improved HTML section that:\n"
        "  - Has 3-4 sentences of analysis per story.\n"
        "  - Includes specific facts (numbers, names, decisions) from the articles above.\n"
        "  - Uses <h3> for story headlines and <p> for analysis.\n"
        "  - Does NOT include an <h2> header — it will be added separately.\n\n"
        "Return ONLY the HTML fragment — no markdown, no explanation, no <html> wrapper. "
        "Do NOT add stories, facts, or events not explicitly stated in those articles."
    )
    return _kickoff_single_task(patcher, desc, "HTML fragment (<h3>+<p>) for this category.")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_critic_pass(
    html: str,
    single_voice_script: str,
    conversational_script: str,
    top_articles: dict[str, list[dict]],
    picks: list[dict],
    trending: list[dict],
    verified_sports_facts: dict[str, str] | None = None,
) -> tuple[str, str, str, dict]:
    """
    Run the critic + (optional) patch pass + NY Sports fact-check observability.

    Returns (html, single_voice_script, conversational_script, metrics) —
    html/scripts possibly unchanged if all sections passed. metrics is
    {"category_scores": {...}, "weak_categories": [...],
    "patched_categories": [...], "ny_sports_skipped": bool} for Datadog
    submission — purely observational, computed from values already derived
    for logging; no scoring/threshold/patch logic changes.
    """
    def _empty_metrics(scores: dict | None = None, weak: list | None = None, scorer_usage: dict | None = None) -> dict:
        weak = weak or []
        return {
            "category_scores": scores or {},
            "weak_categories": weak,
            "patched_categories": [],
            "ny_sports_skipped": any(c.lower() in _NEVER_PATCH_NORMALIZED for c in weak),
            "scorer_usage": scorer_usage or {},
        }

    # Helper so every return path runs the NY Sports fact-check (read-only).
    def _with_factcheck(h: str, s: str, c: str, metrics: dict) -> tuple[str, str, str, dict]:
        try:
            fact_check_ny_sports(h, c, top_articles)
        except Exception as exc:
            logger.warning("[Fact-Checker] NY Sports fact-check failed (non-fatal): %s", exc)
        return h, s, c, metrics

    try:
        critique, scorer_usage = _run_scorer(html, top_articles)
    except Exception as exc:
        logger.warning("[Critic Crew] scorer raised: %s — keeping originals", exc)
        return _with_factcheck(html, single_voice_script, conversational_script, _empty_metrics())

    scores = critique.get("category_scores", {})
    issues = critique.get("issues", {})
    overall_passed = critique.get("overall_passed", True)

    weak = [
        cat for cat, score in scores.items()
        if isinstance(score, (int, float)) and score < _WEAK_THRESHOLD
    ]
    logger.info("[Critic Crew] scores=%s | weak=%s | overall_passed=%s",
                scores, weak, overall_passed)

    if overall_passed and not weak:
        logger.info("[Critic Crew] briefing passed — no patches needed.")
        return _with_factcheck(html, single_voice_script, conversational_script,
                                _empty_metrics(scores, weak, scorer_usage))

    patchable = [c for c in weak if c.lower() not in _NEVER_PATCH_NORMALIZED]
    skipped = [c for c in weak if c.lower() in _NEVER_PATCH_NORMALIZED]
    if skipped:
        logger.warning(
            "[Critic Crew] Skipping patch for %s — sports sections are never auto-patched "
            "(prevents hallucinated scores/players/trades).", skipped,
        )
    if not patchable:
        return _with_factcheck(html, single_voice_script, conversational_script,
                                _empty_metrics(scores, weak, scorer_usage))

    # The scorer echoes the briefing's ALL-CAPS section headers ("COMPANIES",
    # "NY SPORTS"), but top_articles is keyed title-case ("Companies",
    # "NY Sports"). Resolve case-insensitively so the patcher actually receives
    # the section's articles — a plain top_articles.get("COMPANIES") returns []
    # and makes the patcher emit a "No Companies News Available" placeholder,
    # wiping a section that had real stories.
    articles_by_norm = {k.lower(): v for k, v in top_articles.items()}

    improved_html = html
    patched_categories: list[str] = []
    for cat in patchable:
        articles = articles_by_norm.get(cat.lower(), [])
        issue = issues.get(cat, "Section lacks depth and substance.")
        if not articles:
            # No articles resolved for a section the scorer judged weak. The
            # section is thin, not empty — patching it would only let the
            # patcher invent a "no news" placeholder. Leave the original.
            logger.warning(
                "[Critic Crew] '%s' scored weak but no articles resolved for it — "
                "leaving the original section untouched (refusing to patch to empty).",
                cat,
            )
            continue
        logger.info("[Critic Crew] patching weak section: %s — %s", cat, issue)
        try:
            new_section = _run_patcher(cat, articles, issue)
            if not new_section:
                continue
            header_pattern = (
                rf'(<h2[^>]*>[^<]*{re.escape(cat)}[^<]*</h2>)(.*?)'
                r'(?=<h2|<div\s[^>]*text-align\s*:\s*center)'
            )
            match = re.search(header_pattern, improved_html, re.DOTALL | re.IGNORECASE)
            if match:
                h2_tag = match.group(1)
                improved_html = re.sub(
                    header_pattern,
                    lambda _m: f"{h2_tag}\n{new_section}\n",
                    improved_html,
                    count=1,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                logger.info("[Critic Crew] patched section: %s", cat)
                patched_categories.append(cat)
            else:
                logger.warning("[Critic Crew] could not locate HTML section for '%s' — skip", cat)
        except Exception as exc:
            logger.warning("[Critic Crew] patch failed for '%s': %s", cat, exc)

    # We do NOT regenerate the podcast scripts after an HTML patch. The legacy
    # code did, but the scripts are produced from the same article inputs as
    # the HTML — re-rolling against unchanged inputs is a coin-flip on quality
    # and burns ~70s + Claude tokens. If a future script critic pass scores the
    # podcast specifically, we can regenerate based on its signal.
    metrics = _empty_metrics(scores, weak, scorer_usage)
    metrics["patched_categories"] = patched_categories
    return _with_factcheck(improved_html, single_voice_script, conversational_script, metrics)


# ---------------------------------------------------------------------------
# NY Sports fact-check observability (no patching)
# ---------------------------------------------------------------------------

# Sentence splitter — handles "., ", "! ", "? ", and the en-dash separators
# Claude sometimes uses. Quotation marks are dropped before splitting so a
# sentence-final period inside quotes doesn't break the split.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\[])")
_SPEAKER_TAG_RE = re.compile(r"^\[(MIKE|ELIZABETH|JESSE)\]\s*", re.MULTILINE)


def _extract_ny_sports_text(html: str) -> str:
    """
    Pull the plain text inside the NY SPORTS HTML section. Returns "" if the
    section can't be located (writer omitted it, or the layout drifted).
    """
    m = re.search(
        r"<h2[^>]*>[^<]*NY SPORTS[^<]*</h2>(.*?)(?=<h2|<div\s[^>]*text-align\s*:\s*center)",
        html, re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return ""
    inner = re.sub(r"<[^>]+>", " ", m.group(1))
    return re.sub(r"\s+", " ", inner).strip()


def _split_sentences(text: str) -> list[str]:
    if not text:
        return []
    # Drop speaker tags so they don't end up as solo "sentences"
    text = _SPEAKER_TAG_RE.sub("", text)
    parts = _SENTENCE_RE.split(text)
    # Keep only sentences with enough substance to fact-check (>=6 words, contains a digit
    # or proper-noun-looking token). Cuts headers, transitions, generic filler.
    keepers: list[str] = []
    for s in parts:
        s = s.strip()
        if len(s.split()) < 6:
            continue
        if not re.search(r"\d|[A-Z][a-z]{2,}", s):
            continue
        keepers.append(s)
    return keepers


def fact_check_ny_sports(
    html: str,
    conversational_script: str,
    top_articles: dict[str, list[dict]],
) -> int:
    """
    Run validate_claim_against_articles on every substantive sentence in the
    NY Sports section of the HTML briefing AND the [JESSE] block of the
    conversational script. Logs unsupported sentences as WARNING. Does NOT
    patch — NY Sports stays in NEVER_PATCH_NORMALIZED.

    Returns the count of unsupported sentences (for the run-summary line).
    """
    sports_articles = top_articles.get("NY Sports") or []
    if not sports_articles:
        logger.info("[Fact-Checker] No NY Sports articles — skipping.")
        return 0

    html_text = _extract_ny_sports_text(html)
    jesse_text = ""
    if conversational_script:
        # Grab everything between [JESSE] and the next speaker tag (or end).
        jm = re.search(r"\[JESSE\]\s*(.+?)(?=\[(?:MIKE|ELIZABETH)\]|$)",
                       conversational_script, re.DOTALL)
        if jm:
            jesse_text = jm.group(1).strip()

    sentences = _split_sentences(html_text) + _split_sentences(jesse_text)
    if not sentences:
        logger.info("[Fact-Checker] No NY Sports sentences with substance — skipping.")
        return 0

    unsupported = 0
    checked = 0
    verdicts: list[dict] = []
    for sentence in sentences[:30]:  # hard cap so a chatty critic can't bloat the bill
        result = validate_claim_tool._run(claim=sentence, articles=sports_articles)
        verdicts.append({"sentence": sentence, **result})
        if not result.get("ok"):
            continue
        checked += 1
        if result.get("supported") == "no":
            unsupported += 1
            logger.warning(
                "[Fact-Checker] UNSUPPORTED NY Sports claim: %r — reasoning: %s",
                sentence[:200], result.get("reasoning", "")[:200],
            )

    logger.info(
        "[Fact-Checker] NY Sports section: checked %d sentences, %d unsupported "
        "(section NOT auto-patched — see NEVER_PATCH_NORMALIZED).",
        checked, unsupported,
    )
    dump_fixture("helper", TODAY, {
        "input": {"sentences": sentences[:30], "sports_articles": sports_articles},
        "output": {"verdicts": verdicts, "checked": checked, "unsupported": unsupported},
    })
    return unsupported
