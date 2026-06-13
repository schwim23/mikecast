#!/usr/bin/env python3
"""
MikeCast Daily Briefing — entry point.

Two execution paths:

  --legacy (default while migration is in flight)
    The original procedural 10-step pipeline. Untouched.

  --crew
    Steps 0–8b are executed via the CrewAI crews in the `crew/` package
    (planning, research, sports research, picks, writing, critic).
    Steps 9 (audio) and 10 (deliver/email/RSS) are unchanged regardless
    of which path runs.

All secrets are read from environment variables — nothing is hardcoded.
Run with --force to regenerate today's briefing even if one already exists.
"""

import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from mc_config import DATA_DIR, ELEVENLABS_API_KEY, TODAY, TODAY_DISPLAY
from mc_audio import generate_elevenlabs_audio, generate_podcast_audio
from mc_deliver import (
    generate_manifest,
    generate_rss_feed,
    save_daily_data,
    send_email,
    send_newsletter_broadcast,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("mikecast")


# ---------------------------------------------------------------------------
# Legacy path (Steps 0–8b)
# ---------------------------------------------------------------------------

def _run_legacy_steps_0_to_8b(trending_holder: list):
    """Legacy procedural pipeline. Returns (html, single_script, conv_script, top_articles, picks)."""
    from mc_collect import (
        cluster_articles,
        collect_all_news,
        deduplicate,
        enrich_top_stories,
        filter_stale_articles,
        filter_sports_by_trusted_sources,
        process_picks,
        score_and_rank_articles,
        select_top_articles,
    )
    from mc_critic import run_critic_pass
    from mc_generate import (
        generate_conversational_script,
        generate_html_briefing,
        generate_podcast_script,
    )
    from mc_plan import plan_daily_searches

    logger.info("Step 0/10: Planning today's searches with xAI Grok…")
    dynamic_queries: dict[str, list[str]] = {}
    trending_context: str = ""
    trending: list[dict] = []
    try:
        dynamic_queries, trending_context, trending = plan_daily_searches()
        if dynamic_queries:
            total_dyn = sum(len(v) for v in dynamic_queries.values())
            logger.info("Planning complete: %d dynamic queries generated.", total_dyn)
        else:
            logger.info("Planning skipped or returned no queries (XAI_API_KEY may not be set).")
    except Exception as exc:
        logger.warning("Planning step failed (non-fatal): %s", exc)

    trending_holder.append(trending)

    logger.info("Step 1/10: Collecting news…")
    raw_news = collect_all_news(dynamic_queries=dynamic_queries or None)
    raw_total = sum(len(v) for v in raw_news.values())
    if raw_total == 0:
        logger.critical("No articles collected from any source — aborting.")
        sys.exit(1)
    if raw_total < 5:
        logger.warning("Very few articles collected (%d) — possible widespread API failure.", raw_total)

    logger.info("Step 2/10: Deduplicating…")
    deduped = deduplicate(raw_news)
    deduped = filter_stale_articles(deduped, max_age_days=3)
    deduped = filter_sports_by_trusted_sources(deduped)

    logger.info("Step 3/10: Clustering duplicate stories…")
    clustered = cluster_articles(deduped)

    logger.info("Step 4/10: Scoring and ranking articles…")
    scored = score_and_rank_articles(clustered, trending_context=trending_context)

    logger.info("Step 5/10: Selecting top articles…")
    top_articles = select_top_articles(scored, total=25)
    total = sum(len(v) for v in top_articles.values())
    logger.info("Selected %d articles across %d categories.", total, len(top_articles))
    if total == 0:
        logger.warning("All articles were duplicates — briefing will have no new stories.")

    logger.info("Step 6/10: Enriching top stories…")
    top_articles = enrich_top_stories(top_articles, top_n=15)

    logger.info("Step 7/10: Processing Mike's Picks…")
    picks = process_picks()

    logger.info("Step 8/10: Generating HTML briefing and podcast scripts…")
    html: str = ""
    single_voice_script: str = ""
    conversational_script: str = ""

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_html = ex.submit(generate_html_briefing, top_articles, picks, trending)
        f_single = ex.submit(generate_podcast_script, top_articles, picks, trending)
        f_conv = ex.submit(generate_conversational_script, top_articles, picks, trending)
        try:
            html = f_html.result()
        except Exception as exc:
            logger.error("HTML briefing generation failed: %s", exc)
            html = "<p>Briefing generation failed.</p>"
        try:
            single_voice_script = f_single.result()
        except Exception as exc:
            logger.error("Single-voice script generation failed: %s", exc)
        try:
            conversational_script = f_conv.result()
        except Exception as exc:
            logger.error("Conversational script generation failed: %s", exc)

    logger.info("Step 8b/10: Running quality critic pass…")
    try:
        html, single_voice_script, conversational_script = run_critic_pass(
            html, single_voice_script, conversational_script, top_articles, picks,
        )
    except Exception as exc:
        logger.warning("Critic pass failed entirely (non-fatal): %s", exc)

    return html, single_voice_script, conversational_script, top_articles, picks


# ---------------------------------------------------------------------------
# CrewAI path (Steps 0–8b)
# ---------------------------------------------------------------------------

def _run_crew_steps_0_to_8b(trending_holder: list):
    """CrewAI pipeline. Returns (html, single_script, conv_script, top_articles, picks)."""
    from crew.critic_crew import run_critic_pass as crew_run_critic_pass
    from crew.picks_crew import run_picks
    from crew.planning_crew import run_planning
    from crew.research_crew import run_research
    from crew.sports_research_crew import fetch_all_ny_upcoming_games, run_sports_research
    from crew.writing_crew import run_writing

    logger.info("Step 0/10 [crew]: Planning Crew…")
    dynamic_queries, trending_context, trending = run_planning()
    trending_holder.append(trending)

    logger.info("Steps 1–6/10 [crew]: Research Crew…")
    top_articles = run_research(
        dynamic_queries=dynamic_queries,
        trending_context=trending_context,
        total_target=25,
        enrich_top_n=15,
    )
    total = sum(len(v) for v in top_articles.values())
    if total == 0:
        logger.warning("Research Crew returned no articles — briefing will be sparse.")

    logger.info("Steps 1–6/10 [crew]: NY Sports Research Crew (Gatekeeper + Researcher)…")
    try:
        verified_sports_facts = run_sports_research(top_articles)
    except Exception as exc:
        logger.warning("Sports Research Crew failed (non-fatal): %s", exc)
        verified_sports_facts = {}

    # Deterministic upcoming-game fetch — covers the case where today's article
    # batch misses an imminent NY-team game (e.g. a Knicks playoff). Bypasses
    # the LLM and pulls directly from ESPN.
    try:
        upcoming_ny_games = fetch_all_ny_upcoming_games()
    except Exception as exc:
        logger.warning("Upcoming-games fetch failed (non-fatal): %s", exc)
        upcoming_ny_games = []

    logger.info("Step 7/10 [crew]: Picks Crew…")
    picks = run_picks()

    logger.info("Step 8/10 [crew]: Writing Crew (3 parallel Claude writers)…")
    html, single_voice_script, conversational_script = run_writing(
        top_articles, picks, trending,
        verified_sports_facts=verified_sports_facts,
        upcoming_ny_games=upcoming_ny_games,
    )

    logger.info("Step 8b/10 [crew]: Critic Crew…")
    try:
        html, single_voice_script, conversational_script = crew_run_critic_pass(
            html, single_voice_script, conversational_script,
            top_articles, picks, trending,
            verified_sports_facts=verified_sports_facts,
        )
    except Exception as exc:
        logger.warning("Critic Crew failed entirely (non-fatal): %s", exc)

    return html, single_voice_script, conversational_script, top_articles, picks


# ---------------------------------------------------------------------------
# Shared Steps 9 & 10 (audio + deliver) — unchanged by migration
# ---------------------------------------------------------------------------

def _run_steps_9_and_10(html, single_voice_script, conversational_script, top_articles, picks, trending):
    """Audio generation + save + RSS + email. Identical for legacy and crew paths."""
    logger.info("Step 9/10: Generating audio…")
    audio_path = DATA_DIR / f"MikeCast_{TODAY}.mp3"
    el_audio_path = DATA_DIR / f"MikeCast_3voice_{TODAY}.mp3"
    audio_ok = False
    el_audio_ok = False
    audio_filename = None
    el_audio_filename = None

    if conversational_script and ELEVENLABS_API_KEY:
        logger.info("Generating ElevenLabs 3-voice audio…")
        el_audio_ok = generate_elevenlabs_audio(conversational_script, el_audio_path)
        if not el_audio_ok and el_audio_path.exists():
            el_audio_path.unlink()
            logger.warning("Removed partial ElevenLabs audio: %s", el_audio_path)
        el_audio_filename = el_audio_path.name if el_audio_ok else None

    script_for_tts = single_voice_script or conversational_script
    if script_for_tts and not el_audio_ok:
        logger.info("Generating OpenAI TTS single-voice audio (ElevenLabs unavailable)…")
        audio_ok = generate_podcast_audio(script_for_tts, audio_path)
        if not audio_ok and audio_path.exists():
            audio_path.unlink()
            logger.warning("Removed partial OpenAI TTS audio: %s", audio_path)
        audio_filename = audio_path.name if audio_ok else None

    primary_audio_file = el_audio_filename or audio_filename
    primary_audio_path = el_audio_path if el_audio_ok else (audio_path if audio_ok else None)

    logger.info("Step 10/10: Saving data & sending email…")
    save_daily_data(
        html,
        top_articles,
        picks,
        single_voice_script,
        primary_audio_file,
        conversational_script=conversational_script,
        elevenlabs_audio_filename=el_audio_filename,
        trending=trending,
    )
    generate_manifest()
    generate_rss_feed()
    email_ok = send_email(html, single_voice_script or conversational_script, primary_audio_path)

    # Additive newsletter broadcast to public Resend subscribers. Same HTML body
    # as the personal email; skips gracefully (and never raises) if Resend isn't
    # configured, so a newsletter problem can't break the daily pipeline.
    try:
        send_newsletter_broadcast(html)
    except Exception as exc:
        logger.warning("Newsletter broadcast raised (non-fatal): %s", exc)

    return audio_ok, el_audio_ok, email_ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    force = "--force" in sys.argv
    use_crew = "--crew" in sys.argv
    use_legacy = "--legacy" in sys.argv
    if use_crew and use_legacy:
        logger.error("Cannot pass both --crew and --legacy. Pick one.")
        sys.exit(2)
    if not use_crew and not use_legacy:
        # CUTOVER: default is now --crew. The legacy procedural pipeline is
        # kept reachable via the explicit --legacy flag for fast rollback.
        # To revert the cutover: change this line to `use_legacy = True` and
        # merge; the next GH Actions deploy returns the scheduler to legacy.
        use_crew = True

    path_label = "crew" if use_crew else "legacy"

    logger.info("=" * 60)
    logger.info("MikeCast Daily Briefing — %s — path=%s", TODAY_DISPLAY, path_label)
    logger.info("=" * 60)

    # Idempotency guard
    daily_path = DATA_DIR / f"{TODAY}.json"
    if daily_path.exists() and not force:
        logger.warning(
            "Today's briefing (%s) already exists. "
            "Re-run with --force to regenerate. Exiting.",
            TODAY,
        )
        sys.exit(0)

    started_at = time.time()
    trending_holder: list = []

    if use_crew:
        html, single_voice_script, conversational_script, top_articles, picks = _run_crew_steps_0_to_8b(trending_holder)
    else:
        html, single_voice_script, conversational_script, top_articles, picks = _run_legacy_steps_0_to_8b(trending_holder)

    trending = trending_holder[0] if trending_holder else []
    total = sum(len(v) for v in top_articles.values())

    audio_ok, el_audio_ok, email_ok = _run_steps_9_and_10(
        html, single_voice_script, conversational_script, top_articles, picks, trending,
    )

    runtime_s = int(time.time() - started_at)
    logger.info(
        "Run summary [%s] — runtime: %dm%02ds | articles: %d | picks: %d | "
        "elevenlabs: %s | openai_tts: %s | email: %s",
        path_label, runtime_s // 60, runtime_s % 60, total, len(picks),
        "ok" if el_audio_ok else ("skip" if not ELEVENLABS_API_KEY else "FAILED"),
        "ok" if audio_ok else "FAILED",
        "ok" if email_ok else "FAILED",
    )
    logger.info("MikeCast briefing complete.")


if __name__ == "__main__":
    main()
