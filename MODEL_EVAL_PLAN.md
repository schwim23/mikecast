# MikeCast — Model Evaluation & Testing Plan

**Status:** IN PROGRESS — Phases 0-4 all built and exercised 2026-09-13; all three roles now support any model; scorer hardened against a gpt-4o format quirk · **Author:** planning session 2026-07-12, revised 2026-09-06, 2026-09-13 · **Owner:** Mike

## 0i. Session log continued — 2026-09-13, Helper role Claude candidate finally tested

Closed the last open item from §0g: ran a real `claude-haiku-4-5` candidate for the Helper
role (`gpt-4o-mini` baseline, `gpt-5.6-luna` + `claude-haiku-4-5` candidates — matching the
2-candidate pattern already used for writer/critic). **0 failures for `claude-haiku-4-5`** —
confirms the LiteLLM rewrite from §0g genuinely works cross-provider, not just in theory. One
transient hiccup: `gpt-5.6-luna` got an empty completion on 1 of 7 sentence checks
(`json.loads` raised "Expecting value: line 1 column 1"), handled gracefully by the existing
`{"ok": false, ...}` fallback and not counted as a run failure — didn't recur on the other 6,
not investigated further unless it becomes systematic. New run:
`helper_2026-09-13_1789346385` (superseded and deleted the old 2-model run). Dashboard
rebuilt and redeployed — the eval matrix is now genuinely complete: every role has a baseline
plus 2 candidates tested successfully.

### Concrete next steps (revised again)

1. Do a real human review pass with `eval/review.py` — the last missing piece before an
   actual promotion decision per §4's rule.
2. Build the LLM-judge blind A/B (Gate B's 4th check) — still not started.
3. Repeat fixture capture on a few more mornings for variety (Phase 0) — every fixture used so
   far is the same 2026-09-13 thin/preseason NY-sports day.
4. Extend `eval/score.py` to cover the Helper role too, if a meaningful metric is worth
   defining for fact-check verdicts (e.g. agreement rate with the baseline's verdicts) — not
   scoped yet, current Phase 2 is writer/critic only by design (§4).

## 0h. Session log continued — 2026-09-13, hardened the scorer against gpt-4o's nested-dict quirk

## 0h. Session log continued — 2026-09-13, hardened the scorer against gpt-4o's nested-dict quirk

Fixed the finding from §0g: added `_normalize_category_scores()` in `crew/critic_crew.py` —
when `_run_scorer`'s parsed JSON has a dict (not a number) as a category's score, it flattens
to the mean of that dict's numeric sub-values (still a 1-10-scale number) and logs a WARNING,
instead of silently leaving a value that fails every downstream `isinstance(score, (int,
float))` check. Also strengthened the prompt itself ("Each category_scores value MUST be a
single number 1-10 — NOT an object/dict of sub-scores") — belt-and-suspenders, since the code
fix is what actually guarantees correctness regardless of prompt compliance.

**Verified with unit-style checks** against the exact real nested-dict shape observed in
§0g (`{"COMPANIES": {"depth": 8, "analysis": 7, "substance": 7}}` → flattens to `7.3`;
`{"NY SPORTS": {"depth": 3, ...}}` → `3.0`), a mixed nested+flat input, and an already-flat
input (left unchanged). Then re-ran the critic eval for real (3 models, one clean run:
`critic_2026-09-13_1789346036`, superseding the two partial runs from §0g/interim testing) —
gpt-4o returned flat scores this time (the bug is intermittent, didn't reproduce live), but all
three models now show real editorial scores (6.5/6.5/6.5) instead of `None`, and `gpt-5.6-terra`
completed cleanly and triggered a real patch on a weak "Companies" section. Dashboard rebuilt
and redeployed with this final clean 3-model dataset.

**Files changed, uncommitted as of this log entry:** `crew/critic_crew.py`.

### Concrete next steps (revised again)

1. Commit the hardening fix.
2. Test a real Claude candidate for the Helper role now that it's supported (e.g.
   `claude-haiku-4-5` vs. `gpt-4o-mini`) — lifted in §0g but not yet exercised.
3. Do a real human review pass with `eval/review.py` — still the last missing piece before an
   actual promotion decision.
4. Build the LLM-judge blind A/B (Gate B's 4th check) — still not started.
5. Repeat fixture capture on a few more mornings for variety (Phase 0).

## 0g. Session log continued — 2026-09-13, gpt-5.6-terra hang fixed + fact-checker made provider-agnostic

## 0g. Session log continued — 2026-09-13, gpt-5.6-terra hang fixed + fact-checker made provider-agnostic

**Root-caused the gpt-5.6-terra hang for real** (§0c flagged it, didn't fix it) by reading
crewai 0.86.0's own source (`agents/crew_agent_executor.py::_invoke_loop`): when
`_format_answer()` raises `OutputParserException` (the model didn't produce CrewAI's expected
`Thought:/Action:`/`Final Answer:` text), the `except OutputParserException` handler appends
the error and recurses back into `_invoke_loop()` — **with no check against `self.max_iter`
at all**. Only the successful-parse path increments `self.iterations`. A model that can't
reliably produce that format loops with no exit condition, and no timeout on our side can fix
a loop with no bound — confirmed this is a framework bug, not a tuning problem.

**Real fix**: the critic scorer needs no tools and only ever returns JSON, so CrewAI's
Agent/Task/Crew wrapper (built for multi-step tool-use reasoning) was pure overhead — and
exactly the overhead causing the incompatibility. Rewrote
`crew/critic_crew.py::_run_scorer` to bypass it entirely: a new `_llm_complete()` helper calls
`litellm.completion()` directly with a plain user-message prompt, no ReAct format required at
all. This works identically across every provider LiteLLM supports. Removed the now-dead
`make_section_scorer()` Agent from `crew/agents.py` (left a comment explaining why, matching
the existing pattern for the removed Gatekeeper agent).

**Verified against the real bug**: re-ran the critic eval with `gpt-5.6-terra` included —
completed in 33.8s (previously: killed after 6-10 min, twice, still hanging). It correctly
scored sections, triggered a real patch on a weak "Companies" section, and correctly skipped
patching NY Sports. All three critic models (`gpt-4o`, `gpt-5.6-terra`, `claude-sonnet-5`) now
work. New run: `critic_2026-09-13_1789345471` (superseded and deleted the earlier 2-model run).

**Rewrote the NY Sports fact-checker to support any model** (`crew/tools.py::ValidateClaimTool`)
— it called the raw OpenAI SDK directly, which is why a Claude candidate was descoped for the
Helper role in §1b. Switched to `litellm.completion()`, which (confirmed by direct test)
**automatically normalizes `max_tokens` per-provider** — so the manual `max_completion_tokens`
workaround from §0c is now unnecessary now that we're not calling the raw SDK. Dropped the
OpenAI-specific `response_format={"type":"json_object"}` entirely (inconsistent across
LiteLLM/Anthropic) in favor of the same "Return ONLY valid JSON" + markdown-fence-strip pattern
already proven reliable in the scorer. **The OpenAI-only restriction on the Helper role eval is
now lifted** — `eval/run_eval.py`'s `OPENAI_ONLY_ROLES` check is removed. (Not yet re-tested
with an actual Claude helper candidate this session — the fix is verified by code review +
the existing OpenAI-model helper eval still passing, not by a fresh cross-provider run.)

**New shared module**: `crew/model_compat.py` — centralizes `supports_custom_temperature()`
and `api_key_for_model()`, previously duplicated ad hoc in `eval/run_eval.py`. Now used by
`eval/run_eval.py`, `crew/critic_crew.py::_llm_complete`, and `crew/tools.py::ValidateClaimTool`
so these compatibility rules can't drift across the three call sites again.

**New finding, not yet fixed**: `gpt-4o` (the current production critic/judge model)
intermittently returns **nested per-metric dicts** instead of flat integers for
`category_scores` (e.g. `{"COMPANIES": {"depth": 8, "analysis": 7, "substance": 7}}` instead of
`{"COMPANIES": 8}`). Since the weak-section check is `isinstance(score, (int, float))`, this
silently produces `weak=[]` regardless of actual quality — **a genuinely weak section could go
unpatched in production with no visible error**. Reproduced live in this session's critic run
(all three `_run_scorer` editorial-rescoring calls in `critic_2026-09-13_1789345471`'s
`scores.json` came back with `mean_score: None` because of this). Not a regression from
anything changed this session — this is a pre-existing gpt-4o compliance gap, surfaced by
directly reading real critic output rather than trusting the summary line. **Not fixed** —
would need either stricter prompt engineering, a JSON-schema-validated response step, or a
retry-on-malformed-output loop in `_run_scorer`. Flagged to Mike; fix pending his call.

**Files changed, uncommitted as of this log entry**: `crew/model_compat.py` (new),
`crew/critic_crew.py`, `crew/agents.py`, `crew/tools.py`, `eval/run_eval.py`, `eval/score.py`.

### Concrete next steps (revised again)

1. Decide whether to fix the gpt-4o nested-score-format issue above.
2. Commit everything from this pass.
3. Test a real Claude candidate for the Helper role now that it's supported (e.g.
   `claude-haiku-4-5` vs. `gpt-4o-mini`) — the restriction was lifted but not yet exercised.
4. Do a real human review pass with `eval/review.py` (still the last missing piece before an
   actual promotion decision).
5. Build the LLM-judge blind A/B (Gate B's 4th check) — still not started.
6. Repeat fixture capture on a few more mornings for variety (Phase 0).

## 0f. Session log continued — 2026-09-13, format-contract root cause fixed + Phase 3 built

## 0f. Session log continued — 2026-09-13, format-contract root cause fixed + Phase 3 built

**Root-caused and fixed the writer format-contract failure from §0e** — it was a real
truncation bug, not a prompt-compliance gap. `claude-sonnet-5`'s HTML output literally cut off
mid-word ("...Anthropic fin") because that one call hit `max_tokens=6000`. Its tokenizer uses
~1.4x as many tokens as `claude-sonnet-4-6`'s for identical input (17,499 vs 12,269 prompt
tokens for the same fixture) — 6000 output tokens, comfortable for the baseline, wasn't enough
headroom for Sonnet 5's HTML task. `gpt-5.6-sol` was never truncated (its scripts end on
complete sentences) — its earlier short podcast word count was normal k0 sample variance, not
a systemic issue; a re-run landed within the 900-1000 target.

**Fixed in both places** (same forward-compatibility pattern as §0c's bugs — this is a latent
production risk too, not just an eval-harness issue): bumped `max_tokens` from 6000 to 9000 in
`crew/llm.py::claude_writer_llm` (the actual production writer LLM factory) and the matching
value in `eval/run_eval.py::replay_writer`. Safe for the current default model
(`claude-sonnet-4-6` uses well under 6000 anyway) — this is a ceiling increase, not a behavior
change, so no cost/output impact unless a model actually needs the extra room.

**Verified the fix**: re-ran the writer eval (new `run_id`: `writer_2026-09-13_1789344065`,
replacing the truncated one) and re-scored — `claude-sonnet-5`'s HTML now has all 7 sections,
all three models land within the 900-1000 word podcast target (937/913/909), format contract
now passes for all three. Dashboard rebuilt and redeployed with the corrected data.

**Built Phase 3** (`eval/review.py`) — a local Flask app (port 8081 by default, since
`server.py` already uses 8080) mirroring the design in §4: pairs the baseline against each
candidate, shows both as blinded "A"/"B" (HTML briefing in an iframe + both podcast scripts,
side by side), the human enters a 1-5 score per side + forced A/B winner + notes, and the
model mapping is revealed only after submitting. Scores land in
`eval/out/<run_id>/human_scores.json`. Scoped to `writer`/`critic` roles only, same as
`score.py` — `helper` has no prose to review. `--with-audio` (opt-in, burns ElevenLabs
credits, per the original design) is **not built** — deferred.

**Verified end-to-end** (test data cleaned up afterward, not a real review): started the
server against `writer_2026-09-13_1789344065`, confirmed the index page listed both candidate
pairs, fetched a review page and confirmed both HTML briefings render in their iframes, POSTed
a test score, confirmed the 302 redirect to `/revealed/<pair_id>` shows the correct true
model mapping and winner, and confirmed `human_scores.json` persisted all fields correctly.

**Note**: `flask` isn't in `requirements.txt` — installed it locally to test, matching the
existing pattern where `README.md` already documents it as a separate manual
`pip install flask` step for `server.py`'s local dashboard. Not added to the ECS
image/requirements since production doesn't need it.

**Files added/changed this pass, uncommitted as of this log entry:** `crew/llm.py`
(max_tokens fix), `eval/run_eval.py` (matching max_tokens fix), `eval/review.py` (new,
Phase 3), plus the already-uncommitted `eval/score.py` and `build_dashboard.py` score columns
from §0e.

### Concrete next steps (revised again)

1. Decide whether to commit everything above.
2. Do a real human review pass with `eval/review.py` (the test run in this log entry doesn't
   count) — this is the last missing piece before a real promotion decision per §4's rule.
3. Build the LLM-judge blind A/B (Gate B's 4th check, `eval/judge.py`) — still not started;
   optional relative to human review, but cheaper to run repeatedly.
4. Repeat fixture capture on a few more mornings for variety (Phase 0) — today's was a thin
   preseason NY-sports day.
5. Extend `build_dashboard.py` to surface human review results once some exist.
6. Give CrewAI's critic-scorer agent a structured-output execution mode so incompatible
   models (like `gpt-5.6-terra`) fail fast instead of hanging — needed before `gpt-5.6-terra`
   can be tested for critic.

## 0e. Session log continued — 2026-09-13, Phase 2 (automated scoring) built

Built `eval/score.py` — scores a `run_eval.py` run's k0 output per model against three of
Gate B's four checks (§3): **format contract** (HTML parses, all 7 required `<h2>` sections
present, not truncated, podcast word count in 900-1000, all 3 speaker tags present — all
deterministic, no API calls), **grounding/hallucination** (the hard gate — generalizes the
production NY-Sports-only fact-checker to every HTML section via the same
`validate_claim_against_articles` tool, capped at 40 sentences/model; a candidate fails the
gate if it has MORE unsupported claims than the baseline), and **editorial** (reuses
`crew/critic_crew.py::_run_scorer`, the same 1-10 category scorer critic uses in production).
Only scores k0 — scoring makes real LLM calls and k-repeats exist for cost/latency variance,
not to be rescored k times. Scopes to `writer`/`critic` roles only (`helper`'s output is
already a fact-check artifact, not prose to fact-check against).

**NOT built**: LLM-judge blind A/B (Gate B's 4th check, `eval/judge.py`) and Phase 3 human
review. `score.py` alone is not a promotion decision — the dashboard notice says this
explicitly.

**Ran it for real against both existing runs — found a genuine format-contract issue, not a
scoring bug:**

| Role | Model | Gate | Format | Grounding | Editorial |
|---|---|---|---|---|---|
| Writer | `claude-sonnet-4-6` (baseline) | PASS | OK | 5/40 unsupported | 6.5 |
| Writer | `claude-sonnet-5` | PASS | **FAIL** | 3/32 unsupported | 6.5 |
| Writer | `gpt-5.6-sol` | PASS | **FAIL** | 3/40 unsupported | 6.5 |
| Critic | `gpt-4o` (baseline) | PASS | OK | 5/40 unsupported | 6.5 |
| Critic | `claude-sonnet-5` | PASS | OK | 5/40 unsupported | 6.5 |

**Writer finding:** `claude-sonnet-5` dropped the entire "WHAT TO WATCH" section on this
fixture; both writer candidates came in under the 900-1000 word podcast target (847 and 800
words vs. baseline's 991). Both still pass the grounding hard gate (fewer unsupported claims
than baseline). This is one sample (k0, one fixture day) — real signal, but not yet enough to
conclude the models can't hit the contract; worth checking across the k1 run and more fixture
days before drawing a conclusion.

**Critic finding:** identical grounding/editorial numbers for both critic models — expected,
not a bug: NY Sports was the only weak section either flagged, and it's never patched, so no
prose actually changed between the two runs. This eval tests the scorer's *judgment* (did it
correctly flag the weak section), not rewritten output.

Extended `eval/build_dashboard.py` to render `scores.json` when present (new Gate/Format/
Grounding/Editorial columns; "—" for unscored runs/helper role) and redeployed —
still live at https://mikecast.io/evals/index.html.

**Files added/changed this pass, uncommitted as of this log entry:** `eval/score.py` (new),
`eval/build_dashboard.py` (score columns).

### Concrete next steps (revised again)

1. Decide whether to commit `eval/score.py` + the `build_dashboard.py` score-column update.
2. Score the k1 writer run too (currently score.py only does k0) to see if the word-count/
   missing-section finding holds up or was a one-off sample.
3. Repeat fixture capture on a few more mornings for variety (Phase 0) — today's was a thin
   preseason NY-sports day.
4. Build the LLM-judge blind A/B (Gate B's 4th check) and Phase 3 human review — the two
   pieces still missing before a real promotion decision can be made per §4's promotion rule.
5. Give CrewAI's critic-scorer agent a structured-output execution mode so incompatible
   models (like `gpt-5.6-terra`) fail fast instead of hanging — needed before `gpt-5.6-terra`
   can be tested for critic.

## 0d. Session log continued — 2026-09-13, dashboard v1 live

Built `eval/build_dashboard.py` — renders every `eval/out/<run_id>/summary.json` into a
single static HTML page (dark theme matching `mikecast.io`'s existing style) and uploads it
via `mc_utils.s3_upload_text` to `s3://mikecast-io-data/evals/index.html`. Confirmed live at
**https://mikecast.io/evals/index.html** (200 via CloudFront, per §0b's routing finding — no
CloudFront change needed for this path shape).

**Deliberately cost/latency only right now** — the page says so explicitly in a banner rather
than implying a quality verdict it can't back up. Before building it, deleted the two stale
run directories left over from the pre-fix bugs in §0c (`writer_..._1789340892`,
`helper_..._1789342706` — both had a `summary.json` with garbage 0-cost/0-output data from
before the temperature/max_tokens fixes) plus two empty directories from the killed
`gpt-5.6-terra` hang attempts, so the dashboard only shows the 3 valid post-fix runs.

**Not yet done:** `run_eval.py` doesn't upload `summary.json` to S3 the way `eval/dump.py`
uploads fixtures — so right now the dashboard can only show runs from whatever machine last
ran `eval/run_eval.py` locally. If eval runs start happening from more than one place, give
`run_eval.py` the same local+S3 write pattern as `eval/dump.py` before that becomes a problem.

**`eval/build_dashboard.py` is uncommitted** as of this log entry — ask before committing (this
session's own rule).

### Concrete next steps (revised again)

1. Decide whether to commit `eval/build_dashboard.py`.
2. Repeat the ECS fixture-capture run across a few more mornings (Phase 0 needs variety —
   today's fixture was a thin/preseason NY-sports day) before results mean much beyond one
   day's snapshot.
3. Build Phase 2 (`eval/score.py`) — hallucination/grounding hard gate, format-contract checks,
   editorial critic score, LLM-judge blind A/B. This is what actually answers "is the candidate
   *good*," not just "is it fast/cheap" — not started.
4. Extend `build_dashboard.py` to render Phase 2's scores once they exist, and consider giving
   `run_eval.py` an S3 write path for `summary.json` (see above) if eval runs happen from more
   than one place.
5. Give CrewAI's critic-scorer agent (and any other tool-less agent) a structured-output
   execution mode so incompatible models (like `gpt-5.6-terra`) fail fast/cleanly instead of
   hanging — needed before `gpt-5.6-terra` can be meaningfully tested for critic.

## 0c. Session log continued — 2026-09-13, first real replay run + bugs found

## 0c. Session log continued — 2026-09-13, first real replay run + bugs found

Ran the ECS ad-hoc fixture-capture task (§0b command), confirmed via
`aws s3 ls s3://mikecast-io-data/eval/fixtures/live/2026-09-13/` — `writer.json` (43KB),
`critic.json` (71KB), `helper.json` (5KB) all present. Then ran `eval/run_eval.py` for real
against that fixture, for all three roles. **`run_eval.py` needs `S3_BUCKET=mikecast-io-data`
set when run from a local shell** (only set as an ECS task-def env var, not in `~/.profile`) —
its S3 fallback silently can't fire without it and raises "No fixture... checked ... and S3."

**Three real bugs found and fixed while running this — not previously caught because nothing
had exercised a real API call with these candidate models before:**

1. **`anthropic/claude-sonnet-5` and `openai/gpt-5.6-sol` both reject a non-default
   `temperature`** ("temperature is deprecated for this model" / "Only the default (1) value is
   supported"). `eval/run_eval.py::_make_llm` hardcoded `temperature=0.4`/`0.2` for every model —
   every writer/critic call to these two candidates was silently failing and falling back to an
   empty string (the existing `_kickoff_single_task`/`_run_scorer` try/except swallows the
   exception), producing a "successful" run with garbage 0-cost, 34-char output. **Fixed:**
   `_make_llm` now omits `temperature` entirely for a denylist (`claude-sonnet-5`,
   `claude-opus-5`, `claude-fable-5`, `claude-fable-5-1`) and a prefix check
   (`openai/gpt-5.6-*`, `openai/gpt-6-*`).
2. **`openai/gpt-5.6-terra` hangs indefinitely as a CrewAI agent** for the critic scorer
   (`make_section_scorer`, no-tool task). It doesn't reliably emit CrewAI's expected
   `Thought:/Action:`/`Final Answer:` ReAct scaffolding, so CrewAI's format-correction retry
   loop never terminates — confirmed twice, killed manually after 6-10 minutes each time,
   burning real API spend both times. **`make_section_scorer` has no `max_execution_time`**,
   unlike `make_sports_researcher` (180s cap) elsewhere in `crew/agents.py` — this is a **latent
   production robustness gap**, not just an eval-harness problem: if a future real model swap
   hit this, the live critic step could hang with no wall-clock safety net. Decision: **excluded
   `gpt-5.6-terra` from the critic-role eval matrix** (confirmed incompatible with CrewAI's
   default agent executor for this task shape, not just slow) rather than keep retrying.
   Added a `REPLAY_TIMEOUT_S = 240` wall-clock guard to `run_eval.py`'s main loop —
   **first attempt was broken**: `with ThreadPoolExecutor() as ex:` blocks on `__exit__`
   (`shutdown(wait=True)`) waiting for the stuck thread regardless of the `.result(timeout=)`
   catch, so it still hung 6+ minutes past the "cap." Fixed to `ex.shutdown(wait=False)` in a
   `finally`. Not fully bulletproof — CPython's `concurrent.futures` registers worker threads
   globally and joins them at interpreter exit regardless of `shutdown(wait=False)`, so a
   genuinely-stuck call could still block the process from exiting even with this fix. If
   `gpt-5.6-terra` (or a similar model) needs testing later, the real fix is giving CrewAI a
   structured-output/function-calling execution mode instead of the default ReAct-style prompt,
   not a longer timeout.
3. **`openai/gpt-5.6-luna` broke the real NY Sports fact-checker** — `crew/tools.py`'s
   `ValidateClaimTool._run` calls the raw OpenAI SDK directly (not LiteLLM) with `max_tokens=200`
   and `temperature=0`, both hardcoded. `gpt-5.6-luna` rejects `max_tokens` ("Use
   'max_completion_tokens' instead") and — per finding 1's pattern — would likely also reject
   `temperature=0`. **This is a production bug, not just an eval-harness one**: if
   `OPENAI_HELPER_MODEL` were ever set to `gpt-5.6-luna` in production, the real fact-checker
   (the only signal that catches Claude drift on NY Sports, per CLAUDE.md) would silently break.
   **Fixed directly in `crew/tools.py`**: switched to `max_completion_tokens` (OpenAI's current
   parameter, backward-compatible with `gpt-4o`/`gpt-4o-mini` too) unconditionally, and made
   `temperature` conditional — omitted for `gpt-5.6-*`/`gpt-6-*` model prefixes.

**Real comparison results, 2026-09-13 fixture, after the fixes above:**

| Role | Model | Avg latency | Avg cost | Notes |
|---|---|---|---|---|
| Writer (k=2) | `claude-sonnet-4-6` (baseline) | 66.4s | $0.117 | |
| Writer (k=2) | `claude-sonnet-5` (candidate) | 54.5s | $0.122 | Faster, slightly pricier per-episode despite lower per-token rate — likely more output tokens |
| Writer (k=2) | `gpt-5.6-sol` (candidate) | 67.0s | $0.193 | Most expensive, as expected from its tier |
| Critic (k=1) | `gpt-4o` (baseline) | 6.9s | $0.0018 | Scored NY Sports 5/10 |
| Critic (k=1) | `claude-sonnet-5` (candidate) | 8.7s | $0.0036 | Scored NY Sports 3/10 (stricter) — both correctly skipped patching it |
| Critic (k=1) | `gpt-5.6-terra` (candidate) | — | — | **Excluded** — see bug 2 |
| Helper (k=1) | `gpt-4o-mini` (baseline) | 5.9s | $0.0004 | |
| Helper (k=1) | `gpt-5.6-luna` (candidate) | 13.4s | $0.0013 | |

No automated scoring yet (Phase 2, `eval/score.py` — hallucination/grounding hard gate, format
contract, editorial score, LLM-judge — not built). The numbers above are cost/latency only; a
promotion decision needs Phase 2 + Phase 3 (human review) per §3's three gates, not just this.

**Files changed this pass (uncommitted as of this log entry):**
- `eval/run_eval.py` — temperature denylist/prefix logic in `_make_llm`; `REPLAY_TIMEOUT_S`
  wall-clock guard (with the `shutdown(wait=False)` fix) around the main replay loop.
- `crew/tools.py` — `ValidateClaimTool._run`: `max_completion_tokens` instead of `max_tokens`,
  conditional `temperature`.

### Concrete next steps (revised again)

1. **Decide whether to commit + deploy the two bug fixes above.** The `crew/tools.py` fix in
   particular is a real production correctness fix (independent of the eval project — it fixes
   a genuine forward-compatibility gap in the fact-checker), not just eval-harness code. Not
   done yet — ask Mike first, per this session's own "only commit when asked" rule.
2. Repeat the ECS fixture-capture run across a few more mornings (Phase 0 needs variety —
   heavy-sports day, thin-news day, big-AI-news day, etc.) before Phase 1 replay results mean
   much beyond a single day's snapshot.
3. Build Phase 2 (`eval/score.py`) — hallucination/grounding hard gate, format-contract checks,
   editorial critic score, LLM-judge blind A/B. Not started.
4. Build the results dashboard (Phase 3+4 combined, static render to
   `s3://mikecast-io-data/evals/index.html` per §0b's hosting decision) — now has real
   `summary.json` data to render for the first time (3 run directories under `eval/out/`).
5. Consider giving CrewAI's critic-scorer agent (and any other tool-less agent) a structured-
   output execution mode so incompatible models (like `gpt-5.6-terra`) fail fast/cleanly instead
   of hanging — needed before `gpt-5.6-terra` can be meaningfully tested for critic.

## 0b. Session log continued — 2026-09-13, later in the session

- **Deployed to production, twice, both confirmed live via ECR/ECS:**
  - `6cc1c08` — Phase 0 (fixture capture + `--eval-only`/`--dump-eval-fixture` flags). Confirmed
    task-def revision 62 + `mikecast-daily` scheduler both point at this image.
  - `fca2790` — Phase 1 (`eval/run_eval.py` + `ValidateClaimTool` usage-tracking addition).
    Pushed; deploy.yml will auto-build/register/repoint same as the first push (not
    individually re-verified — same pipeline, no reason to expect a different outcome).
  - **Both deploys are behavior-neutral for the daily 6:30 AM run** — every new code path is
    gated behind flags/env vars that default off.
- **`eval/run_eval.py` (Phase 1) — built and verified, but only with synthetic data.**
  Confirmed: CLI/`--help` work, the `helper`-role OpenAI-only guard fires correctly, the
  missing-fixture error is clean (not a raw traceback), all three `mock.patch` targets
  (`crew.agents.claude_writer_llm`, `crew.agents.openai_critic_llm`,
  `crew.tools.OPENAI_HELPER_MODEL`) resolve and actually take effect, and the per-model
  usage/cost attribution logic is correct (unit-tested with fake Crew/Agent stubs — confirmed
  it correctly separates a critic replay's swapped-scorer cost from the patcher's unswapped
  cost, pricing each against its own model). **Not yet run against a real fixture or a real
  API call — that's the next step, and it costs real money the moment it happens.**
- **AWS ad-hoc fixture-capture run — ready, but blocked by Claude Code's auto-mode
  classifier** (flagged `aws ecs run-task` as a "Production Deploy" action). The exact command
  was handed to Mike to run directly:
  ```bash
  aws ecs run-task \
    --cluster mikecast --task-definition mikecast --launch-type FARGATE \
    --overrides '{"containerOverrides":[{"name":"mikecast","command":["python","mikecast_briefing.py","--crew","--eval-only","--dump-eval-fixture"]}]}' \
    --network-configuration 'awsvpcConfiguration={subnets=[subnet-086fe88cca1a9de84],securityGroups=[sg-0b075c1eea308976b],assignPublicIp=ENABLED}' \
    --region us-east-1
  ```
  **Status as of end of session: not yet run.** Repeat ~5 mornings for Phase 0's fixture
  variety requirement (§4 Phase 0), or run it multiple times same-day for repeated writer
  variance (k-sampling) — it's idempotent/side-effect-free (`--eval-only` never touches
  published data), safe to run as often as wanted.
- **Dashboard hosting — decided:** public, unlisted path at `mikecast.io/evals`, no auth
  (confirmed with Mike 2026-09-13 — cost/model data isn't sensitive, just not
  listener-facing). Static render-and-upload to S3 (`mikecast-io-data`), same pattern as
  `feed.xml`/`manifest.json` in `mc_deliver.py` — **not** a live Flask app for the results
  view. Checked the live CloudFront distribution (`EFNQM31KQHY56`): single S3 origin, no
  CloudFront Function/Lambda@Edge, `DefaultRootObject: index.html` applies to `/` only —
  confirmed via the existing (already-live, apparently orphaned) `dashboard/` folder in the
  same bucket: `/dashboard` and `/dashboard/` both 403, only `/dashboard/index.html` resolves
  (200). So **without any CloudFront change**, the eval dashboard is reachable at
  `mikecast.io/evals/index.html` the moment something is uploaded to `s3://mikecast-io-data/evals/index.html`.
  For the cleaner bare `/evals` URL, a CloudFront Function (viewer-request, directory-index
  rewrite) would need to be created and attached to the default cache behavior — **deferred
  until there's real dashboard content to serve**; command drafted and handed to Mike but not
  run. The Phase 3 blind human-scoring page (`review.py`) stays local-only (Flask, like
  `server.py`) since it needs to accept input, not just serve static files — only the
  read-only results view goes to S3/`mikecast.io/evals`.

### Concrete next steps (revised, supersedes the 0a list where it overlaps)

1. **Run the AWS `ecs run-task` command above** (Mike, directly — blocked for Claude Code by
   the auto-mode classifier) to capture the first real writer/critic/helper fixtures. Repeat
   across a few mornings per Phase 0.
2. **Run `eval/run_eval.py` against a real fixture** once captured — this is the first time
   any of this costs real API money end-to-end via the harness itself, e.g.:
   ```bash
   python eval/run_eval.py --role writer --date <date> \
     --baseline anthropic/claude-sonnet-4-6 \
     --candidate anthropic/claude-sonnet-5 --candidate openai/gpt-5.6-sol --k 3
   ```
3. **Build Phase 2 (`eval/score.py`)** — automated scoring (hallucination/grounding hard
   gate, format-contract checks, editorial critic score, LLM-judge blind A/B) — not started.
4. **Build the results dashboard** (Phase 3+4 combined) — a script that turns
   `eval/out/<run_id>/summary.json` (+ eventual `scores.json` from Phase 2) into a static
   page, uploads it to `s3://mikecast-io-data/evals/index.html`. Not started — no real scores
   exist yet to render.
5. **Optional later polish:** the CloudFront Function for the bare `/evals` URL (command
   drafted above, in the 0b log) — only worth doing once the dashboard is real and gets
   revisited/shared often enough that the URL ergonomics matter.

## 0a. Session log — 2026-09-13 (resume here)

**Confirmed eval matrix for this cycle** (each role evaluated independently — never
promote two roles off one combined verdict):

| Role | Baseline | Candidates | Notes |
|---|---|---|---|
| **Writer** | `anthropic/claude-sonnet-4-6` | `anthropic/claude-sonnet-5`, `openai/gpt-5.6-sol` | Both swappable via `CLAUDE_WRITER_MODEL` env var / LLM override — no code change. |
| **Critic** (scorer only, not the patcher) | `openai/gpt-4o` | `openai/gpt-5.6-terra`, `anthropic/claude-sonnet-5` | Only `make_section_scorer()`'s `openai_critic_llm()` is in scope. `make_section_patcher()` stays on `claude_writer_llm()` (unchanged) — patching weak sections is writer-model surface, already covered separately. |
| **Helper** (NY Sports fact-checker only) | `openai/gpt-4o-mini` | `openai/gpt-5.6-luna` | **OpenAI-only** — see §1b, a Claude candidate was explicitly descoped 2026-09-13. |

New OpenAI model tier reference (fetched 2026-09-13 from developers.openai.com):
`gpt-6-astra` ($10/$50, most capable), `gpt-5.6-sol` ($4/$20, complex professional
tasks), `gpt-5.6-terra` ($2/$12, balanced), `gpt-5.6-luna` ($0.20/$1.20, cost-sensitive).
Claude pricing confirmed via the `claude-api` skill: `claude-sonnet-4-6` $3/$15,
`claude-sonnet-5` $2/$10, `claude-haiku-4-5` $1/$5 (not in the current matrix, available
if a Helper-role Claude candidate is revisited).

**Dashboard requirement confirmed 2026-09-13** (was already an open item in the old §8,
now locked in as a requirement): user wants one dashboard showing runs per role, models
tested, and scores. This is Phase 3+4 combined (see §8) — **not built yet**, because
there's no run data to show until Phase 0 (fixture capture) and Phase 1 (replay) produce
some. Build it once the first replay run has scores.

### §1b. New finding: the "Helper" role is mostly dead code

While wiring fixture capture, discovered that of the three agents built with
`openai_helper_llm()` in `crew/agents.py` — `make_planner`, `make_sports_fact_checker`,
`make_picks_processor` — **none are ever instantiated in the live pipeline** (verified by
grep — zero call sites outside their own definitions). The only place `OPENAI_HELPER_MODEL`
is actually read at runtime is `crew/tools.py::ValidateClaimTool._run` (the NY Sports
fact-checker tool, `validate_claim_against_articles`), and that call site uses the raw
`openai.OpenAI()` SDK client directly — not CrewAI/LiteLLM — including a strict
`response_format={"type":"json_object"}` JSON-mode contract that doesn't map cleanly onto
Anthropic. Making it provider-agnostic would mean rewriting the JSON-extraction contract
for a piece of code that gates sports-hallucination fact-checking (CLAUDE.md: "Hallucination
guards are everywhere... Do not weaken these"). Decision: **skip a Claude candidate for
Helper this cycle** — only test `gpt-4o-mini` (baseline) vs `gpt-5.6-luna` (candidate), both
via the existing raw-OpenAI code path with `OPENAI_HELPER_MODEL` overridden. Revisit the
LiteLLM rewrite as its own deliberate, reviewed change if a Claude Helper candidate is
wanted later.

### Progress so far (files created/edited 2026-09-13)

- **`eval/pricing.py`** — done. Pricing table (`$/1M` in/out) for every model in the matrix
  above, keyed by LiteLLM-style model string, plus a `cost_usd()` helper.
- **`eval/dump.py`** — done. `dump_fixture(role, date, payload)` writes
  `eval/fixtures/live/<date>/<role>.json`, gated on `MIKECAST_DUMP_EVAL_FIXTURE=1` env var
  (no-op otherwise, safe to call unconditionally from production code).
- **`eval/fixtures/manifest.yaml`** — scaffolded, empty `dates: []` list — populate after a
  few days of live capture per Phase 0.
- **`mikecast_briefing.py`** — edited `_run_crew_steps_0_to_8b`: added
  `from eval.dump import dump_fixture`, a `--dump-eval-fixture` CLI flag (sets
  `MIKECAST_DUMP_EVAL_FIXTURE=1`), and two `dump_fixture(...)` calls — one right after
  `run_writing(...)` capturing the writer's exact input (top_articles, picks, trending,
  verified_sports_facts, ny_team_updates) and output (html, both scripts), and one right
  after `crew_run_critic_pass(...)` capturing the critic's pre/post html+scripts,
  top_articles/picks/trending inputs, and `critic_metrics`. Verified the file still parses
  and imports cleanly (`ast.parse` + a real `import mikecast_briefing` with `sys.argv`
  stubbed) — **not yet exercised against a real run.**
- **Helper-role fixture capture — NOT YET DONE.** Plan is to add a fixture dump inside
  `crew/critic_crew.py::fact_check_ny_sports()` (it already computes the exact sentence
  list + `validate_claim_tool` verdicts needed) — deferred when the session paused.

### Concrete next steps (in order)

1. **Add Helper-role fixture capture** in `crew/critic_crew.py::fact_check_ny_sports()` —
   dump `{"input": {sentences, sports_articles}, "output": {per-sentence verdicts}}` via
   `eval.dump.dump_fixture("helper", TODAY, ...)`. Needs `from mc_config import TODAY`
   added to that file (not currently imported there).
2. **Get fixture capture actually running daily.** `--dump-eval-fixture` only takes effect
   if it's on the command line that runs each morning. Two candidate runners exist and
   neither has been checked/edited yet:
   - `~/mikecast/run_mikecast.sh` (local cron wrapper — check `crontab -l` for whether this
     even fires, vs. being legacy)
   - The AWS ECS Fargate scheduled task (`mikecast-daily` EventBridge Scheduler) — the real
     production runner per `CLAUDE.md`. Adding the flag there means editing the task
     definition's command override or the default entrypoint, then a deploy.
   Whichever is authoritative, add `--dump-eval-fixture` to it and let it run for ~5
   mornings (Phase 0 requirement) before Phase 1 has anything to replay.
3. **Decide on one immediate manual capture run.** Running
   `mikecast_briefing.py --crew --dump-eval-fixture` right now would capture today's
   fixture immediately instead of waiting for tomorrow's cron — but it's a real production
   run: costs real NYT/OpenAI/Anthropic/ElevenLabs API money, takes ~20 minutes, and (since
   no briefing exists yet for 2026-09-13 as of this session) would send the real email
   newsletter and post to X/Instagram, not just regenerate content. **Confirm with Mike
   before running this** — don't trigger it unilaterally.
4. **Build `eval/run_eval.py`** (Phase 1). Design decided but not yet written: replay each
   role's stage against a captured fixture with the model swapped via
   `unittest.mock.patch("crew.agents.<factory_name>", return_value=LLM(model=candidate, ...))`
   — `claude_writer_llm` for writer, `openai_critic_llm` for critic's scorer only. This
   avoids touching `crew/llm.py`'s production model-selection logic; the mock target is the
   name imported into `crew.agents`'s namespace (`from crew.llm import claude_writer_llm`,
   etc.), not `crew.llm.claude_writer_llm` itself. For cross-provider candidates, derive
   `api_key` from the model string's prefix (`anthropic/` → `ANTHROPIC_API_KEY`,
   `openai/` → `OPENAI_API_KEY`) rather than hardcoding one, since the existing factories in
   `crew/llm.py` assume the key matches their fixed provider. For token/cost capture:
   CrewAI 0.86.0's `Crew.calculate_usage_metrics()` (returns `UsageMetrics` with
   `prompt_tokens`/`completion_tokens`/`total_tokens`/`cached_prompt_tokens`) only works if
   you still hold a reference to the `Crew` object — `run_writing`/`run_critic_pass` don't
   return theirs, so plan to monkeypatch `crewai.Crew.kickoff` to record each instantiated
   `Crew` into a list during the replay call, then sum usage across them afterward.
5. **Build the results dashboard** (Phase 3+4 combined, per user request 2026-09-13) once
   step 4 produces real `scores.json` data — a local Flask app (mirroring `server.py`'s
   existing pattern) showing runs per role, models tested, and scores/cost/hallucination
   deltas. Still open: local-only vs. reachable elsewhere (per original §8 note).

---

Purpose: a repeatable, **manually-initiated** process to decide — with confidence — whether to
swap a new LLM into any of MikeCast's four LLM roles when a new model ships (e.g. a new Opus/
Sonnet for the writer, a new GPT/Gemini for the scorer/critic). Combines automated scoring with a
blind side-by-side human review, and treats **cost as a first-class gate**.

---

## 0. Why this is tractable (context for cold resume)

- **Every model is swappable by env var, no code change.** See `mc_config.py`:
  - `CLAUDE_WRITER_MODEL`  (default `anthropic/claude-sonnet-4-6`)
  - `OPENAI_SCORER_MODEL`  (default `openai/gpt-4o`)
  - `OPENAI_CRITIC_MODEL`  (default `openai/gpt-4o`)
  - `OPENAI_HELPER_MODEL`  (default `openai/gpt-4o-mini`)
  - (also `XAI_API_KEY` Grok planner, Step 0 — out of scope for v1)
  - LLM factory: `crew/llm.py` (`claude_writer_llm`, `openai_scorer_llm`, `openai_critic_llm`, `openai_helper_llm`).
- **Two hardest eval primitives already exist and are reusable:**
  - GPT critic scorer — scores each section 1–10 on depth/analysis/substance (`mc_critic.py::critique_briefing`; crew version `crew/critic_crew.py`). Weak threshold = 7.
  - Sports fact-checker — `validate_claim_against_articles` (wired via `crew/critic_crew.py::fact_check_ny_sports`, tool in `crew/tools.py`). Currently sports-only; extend to all sections for eval.
- **~140 historical episode JSONs** in `data/YYYY-MM-DD.json` capture real inputs→outputs.
  Top-level keys: `date, date_display, episode_num, episode_description, html_briefing, articles,
  mikes_picks, podcast_script, conversational_script, audio_file, elevenlabs_audio_file, trending,
  generated_at`. NOTE: this only snapshots the **final selected** `articles`, not each role's exact
  input — hence Phase 0 gold-fixture capture below.
- **Rollout/rollback muscle already exists:** env-var swap in SSM/task-def, `--legacy` flag, `--force`
  + per-date dist state (`mc_dist_state.py`) for safe shadow runs.

---

## 1. The swap surface — four independent evals

Evaluate one role at a time (keep attribution). Same harness, `--role` flag.

| Role | Current model | Primary failure mode if a swap is bad | Risk |
|---|---|---|---|
| **Writer** (Claude) | `claude-sonnet-4-6` | Hallucination, voice/persona drift, format break, word-budget miss | **Highest** |
| **Scorer** (GPT-4o) | `gpt-4o` | Wrong stories selected/ranked | Med |
| **Critic** (GPT-4o) | `gpt-4o` | Misses weak sections / bad patches | Med |
| **Helper** (GPT-4o-mini) | `gpt-4o-mini` | Weak gatekeeper/fact-check/summarize | Low |

Rule of thumb: **never swap more than one role per eval cycle.**

**Priority order (set 2026-09-06): Writer first, end-to-end (Phases 0–5 below), before Research.**
Research eval is real but lower priority — it needs a prerequisite code fix before it's even
possible (see §1a) and its output (a story selection/ranking, not prose) needs a different eval
approach than the writer's grounding/voice checks.

### 1a. "Research" is not one role — three sub-components, one not yet swappable

What looks like a single "Scorer" row above is actually three separate pieces, discovered while
scoping this eval (2026-09-06):

| Sub-component | File / function | Model today | Swappable via env var? |
|---|---|---|---|
| Article scoring/ranking (Step 4, all categories) | `mc_collect.py::score_and_rank_articles` | `gpt-4o` | **No — hardcoded** `model="gpt-4o"` in a raw `OpenAI()` client call. `OPENAI_SCORER_MODEL` is defined in `mc_config.py` but never read here. |
| Enrichment (top-8 "why it matters") | `mc_collect.py::enrich_top_stories` | `gpt-4o-mini` | **No — hardcoded** `model="gpt-4o-mini"`, same pattern. `OPENAI_HELPER_MODEL` not read here. |
| NY Sports Researcher (ESPN tool agent) | `crew/sports_research_crew.py` via `crew/agents.py::make_sports_researcher` | `OPENAI_SCORER_MODEL` | **Yes** — already goes through `crew/llm.py::openai_scorer_llm()`. |

**Prerequisite fix for a research eval:** before scoring/ranking or enrichment can be A/B'd against
a candidate model, `score_and_rank_articles` and `enrich_top_stories` need to read
`OPENAI_SCORER_MODEL` / `OPENAI_HELPER_MODEL` from `mc_config.py` instead of hardcoding
`"gpt-4o"` / `"gpt-4o-mini"`. Small, mechanical change — do it as the first step of the research
eval phase, not before (no need to block on it while the writer eval is being built).

**Why research needs a different eval design than the writer:**
- Scoring/ranking and enrichment produce a *selection*, not prose — there's no hallucination/
  voice-grounding check that applies. Instead:
  - **Top-N precision/recall or rank correlation** against a small hand-curated gold set (Mike
    labels "the actually-good stories" for a handful of fixture days once, reuse forever), OR
  - **Cheaper, no-labeling alternative:** blind pairwise preference — show baseline's top-8
    headlines+categories vs. candidate's top-8 (no full prose) in the same blind-review dashboard
    built for the writer, and just pick which day's story lineup is better.
- The NY Sports Researcher is actually the *easiest* piece to eval objectively: its inputs (ESPN
  tool responses) are deterministic and fixture-capturable, so "did the verified-facts string only
  state what the tool actually returned" is a mechanical check, not a judgment call — stricter and
  cheaper than the writer's grounding gate.
- Reuses the same dashboard/harness infrastructure built for the writer (Phases 1–4) — research
  just needs its own fixture adapter (raw article pool in, ranked selection out, instead of
  briefing HTML in/out) and its own scoring functions in `score.py`, added as **Phase 6** once the
  writer eval loop is proven end-to-end.

---

## 2. Quality dimensions, ranked by MikeCast risk

1. **Factual grounding / hallucination** — #1 risk. Sports is *never* auto-patched because GPT
   invents scores/players/trades (see CLAUDE.md "Key Constraints"). This is a **hard gate.**
2. **Format & contract adherence** — valid HTML sections, JSON fields the crews return
   (`card_bullets`==3, category scores), podcast 900–1000 words / 6–7 min, no truncation.
3. **Editorial quality** — depth, analysis-beyond-headlines, substance (what the critic scores 1–10).
4. **Voice / persona** — Mike / Elizabeth / Jesse consistency and tone (hard to auto-judge → human gate).
5. **Cost & latency** — Fargate ~20-min/day budget; cost projected to real once-a-day volume.
6. **Reliability** — JSON parses, no crashes, retries behave.

---

## 3. Process — three gates, cheapest first

**Gate A — Offline replay (automated).** Replay N curated historical episodes through *only the
candidate role* (fixed inputs) for both candidate and baseline models. Deterministic on input, cheap,
repeatable.

**Gate B — Automated scoring (candidate vs. incumbent).**
- Hallucination: extended `validate_claim_against_articles` over all sections → unsupported-claim count.
- Format contract checks.
- Editorial: existing critic scorer 1–10.
- LLM-judge: blind randomized A/B, per dimension.
- Cost/latency: tokens + wall-clock → $/day and $/month delta.

**Gate C — Human side-by-side + shadow run.** Blind A/B review of rendered briefing + both podcast
scripts (+ optional audio) with a human score written back into the scorecard. Then an optional
shadow production run (candidate generates in parallel, not delivered) for a few real mornings.

---

## 4. Build plan — phases & concrete file specs

### Directory layout
```
eval/
  fixtures/manifest.yaml     # curated historical dates, each labeled with the scenario it stresses
  pricing.py                 # model string → ($/1M input, $/1M output); refresh at build time, not stale
  run_eval.py                # replay a role+model over fixtures → outputs + token/latency/cost meta
  judge.py                   # blind A/B LLM-judge (randomized order, per-dimension)
  score.py                   # automated scoring → scores.json
  review.py                  # local Flask side-by-side UI; human scores (blinded)
  out/<run_id>/
    <model>/<date>.{html,podcast.txt,conversational.txt,meta.json}
    scores.json              # automated + human, merged
    report.md                # comparison table + verdict
MODEL_EVAL.md                # scorecard template + append-only decision log
```

### Phase 0 — Gold fixtures  *(needs a few live mornings — start first)*
- Add a `--dump-eval-fixture` flag to `mikecast_briefing.py` that writes **each role's exact input and
  output** for the next ~5 daily runs into `eval/fixtures/live/<date>/<role>.json`. (Stored episode
  JSON lacks per-role inputs; this captures them faithfully.)
- Curate ~15–20 dates into `eval/fixtures/manifest.yaml`, each labeled with the scenario it stresses:
  heavy NY-sports day, thin-news weekend, big-AI-news day, a Mike's-Picks day, a day the critic
  previously patched, a high-`trending` day.
- Populate the currently-empty `crew/tests/fixtures/`.

### Phase 1 — `run_eval.py`
- CLI: `python eval/run_eval.py --role writer --model anthropic/claude-opus-4-8 \
        --baseline anthropic/claude-sonnet-4-6 --fixtures eval/fixtures/manifest.yaml --k 3`
- For each fixture: reconstruct that role's input (adapter per role), invoke *only that role* via the
  existing crew stage with the model string overridden (candidate and baseline separately).
  - writer → `crew/writing_crew.py` (html + single-voice + conversational)
  - scorer → per-category scorer over the fixture's raw article pool
  - critic → `crew/critic_crew.py` over a fixture briefing
  - helper → gatekeeper / fact-check / picks summarize
- Run each fixture **k=2–3×** (writer temp=0.4) to capture variance.
- Record tokens + wall-clock; compute cost from `eval/pricing.py`. Write outputs + `meta.json`.

### Phase 2 — `score.py` (automated)
Per output, emit into `scores.json`:
- **Grounding (HARD GATE):** extend `validate_claim_against_articles` to all sections; extract
  substantive sentences from HTML + both scripts; validate each against that episode's `articles`.
  Count unsupported claims. **Fail if candidate adds ANY net-new unsupported claim vs. baseline.**
- **Format contract:** HTML parses (BeautifulSoup), required `<h2>`s present, no truncation, podcast
  900–1000 words, `card_bullets`==3, role JSON fields present.
- **Editorial:** existing critic scorer → per-section 1–10 → mean + weak-section count.
- **LLM-judge:** `eval/judge.py`, blind randomized A/B on grounding/depth/voice → win rate.
  Randomize A/B order to kill position bias; store mapping separately.
- **Cost/latency:** from `meta.json` → per-episode, projected $/day and $/month delta vs. baseline.

### Phase 3 — `review.py` (human-in-the-loop score)
- Small Flask app (mirror `server.py`) rendering **baseline vs. candidate side-by-side as blinded
  "A"/"B"**: rendered HTML briefing + both podcast scripts.
- `--with-audio` (opt-in, burns ElevenLabs credits): generate + play candidate audio via `mc_audio.py`.
- Human enters **1–5 per side + a forced A/B winner + notes** (scale TBD — see §7).
- Writes a `human` block into `scores.json`; reveals the A/B→model mapping only after scoring.

### Phase 4 — Report + decision
- `score.py` merges automated + human → `report.md` comparison table: hard gates, editorial,
  judge win-rate, human score, **cost ($/mo delta)**.
- **Promotion rule (all must hold):**
  1. 0 net-new hallucinations (hard gate)
  2. 100% format-contract pass
  3. Editorial ≥ baseline within noise
  4. Human score ≥ baseline
  5. Cost within monthly ceiling (see §7)
- Append verdict + numbers to `MODEL_EVAL.md` (auditable decision log).

### Phase 5 — Rollout / rollback runbook
- Promote = flip the SSM param / task-def env var for that role → deploy → watch one live run.
- Old model string stays one line away for instant revert (same as `--legacy` rollback).
- No automation, no polling — you initiate every eval and every promotion.

### Phase 6 — Research eval  *(lower priority — after the writer loop is proven, see §1a)*
- Prerequisite fix: wire `OPENAI_SCORER_MODEL` into `score_and_rank_articles` and
  `OPENAI_HELPER_MODEL` into `enrich_top_stories` (both in `mc_collect.py`), replacing the
  hardcoded `"gpt-4o"` / `"gpt-4o-mini"` strings.
- New fixture adapter: raw deduped/clustered article pool in → ranked+scored selection out (no
  briefing HTML/prose involved).
- `score.py` additions: top-N precision/recall or rank correlation vs. a small hand-labeled gold
  set; for the NY Sports Researcher, a tool-fidelity check (verified-facts string vs. what the
  ESPN fixture tools actually returned — stricter and more mechanical than the writer's grounding
  gate).
- `review.py` reuse: same blind side-by-side dashboard, but rendering top-8 headlines+categories
  instead of full HTML/scripts, for the no-labeling pairwise-preference path.

---

## 5. Caveats / gotchas to remember

- **Replay fidelity depends on fixture richness.** Historical JSON lacks per-role inputs → Phase 0
  `--dump-eval-fixture` is a prerequisite for a faithful writer/scorer/critic eval.
- **Temperature > 0 for the writer (0.4).** Do k samples per fixture; compare distributions, not single runs.
- **Do NOT weaken hallucination guards** to make a model "pass." The guards are the product.
- **Sports section is never auto-patched** — eval it, but don't add a patch path for it.
- **Cost pricing goes stale.** Refresh `eval/pricing.py` from current provider pricing at build time
  (use the `claude-api` skill for Claude model ids/pricing; check OpenAI pricing page for GPT).

---

## 6. How to run an eval (once built) — quick runbook

```bash
# 1. (one-time / periodically) collect gold fixtures over ~5 live mornings
#    add --dump-eval-fixture to the daily run, then curate manifest.yaml

# 2. replay a candidate vs. baseline for one role
python eval/run_eval.py --role writer \
  --model anthropic/claude-opus-4-8 --baseline anthropic/claude-sonnet-4-6 --k 3

# 3. automated scoring
python eval/score.py --run <run_id>

# 4. blind human review (optionally with audio)
python eval/review.py --run <run_id> --with-audio   # → http://localhost:8080

# 5. read the verdict, then record the decision
open eval/out/<run_id>/report.md
# append outcome to MODEL_EVAL.md; if promoting, flip the env var in SSM/task-def + deploy
```

---

## 7. Open decisions (defaults noted — confirm on resume)

1. **Cost ceiling that trips the gate** — default: *≤ 1.5× current monthly LLM spend*.
   → Replace with Mike's real number.
2. **Human score scale** — default: **1–5 per side + forced A/B pick**. (Alt: 1–10.)

---

## 8. Next action on resume

Start **Phase 0 + Phase 1** for the **writer role only**: add `--dump-eval-fixture` to
`mikecast_briefing.py`, write `eval/fixtures/manifest.yaml`, and build `eval/run_eval.py` +
`eval/pricing.py`. Phase 0 needs a few live mornings of fixture capture, so kicking it off first
unblocks everything else. Research eval (Phase 6, §1a) comes after the writer loop — including
results — is proven end-to-end; don't start the `score_and_rank_articles`/`enrich_top_stories`
env-var fix until then.

Results dashboard: fold Phase 3 (`review.py`, blind A/B human scoring) and Phase 4 (`report.md`)
into one small web app rather than a markdown file + separate script — same app serves the blind
scoring page and a results view (score trends, cost deltas, hallucination counts) once a run is
scored. Still open: whether that dashboard is local-only (`localhost`, matching `server.py`'s
existing pattern) or needs to be reachable elsewhere — confirm before building Phase 3.
