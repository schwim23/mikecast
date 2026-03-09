#!/usr/bin/env python3
"""
MikeCast Daily Briefing — entry point.

Orchestrates the full pipeline:
  0.  Plan today's searches with xAI Grok (adaptive — identifies breaking news)
  1.  Collect news from all sources (parallel I/O) + dynamic queries from step 0
  2.  Deduplicate against 7-day history
  3.  Cluster similar stories (gpt-4o-mini — cheap pre-filter)
  4.  Score and rank articles (parallel per-category gpt-4o agents) + trending context
  5.  Select top articles (proportional across categories)
  6.  Enrich top stories (fetch body + gpt-4o-mini 'why it matters')
  7.  Process Mike's Picks
  8.  Generate HTML briefing + both podcast scripts (parallel GPT calls)
  8b. Critic pass — GPT-4o scores each section; regenerates weak ones
  9.  Generate ElevenLabs 3-voice audio + OpenAI TTS single-voice backup
  10. Save daily JSON, update manifest, update RSS feed, send email

All secrets are read from environment variables — nothing is hardcoded.
Run with --force to regenerate today's briefing even if one already exists.
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor

from mc_collect import (
    cluster_articles,
    collect_all_news,
    deduplicate,
    enrich_top_stories,
    filter_stale_articles,
    process_picks,
    score_and_rank_articles,
    select_top_articles,
)
from mc_config import DATA_DIR, ELEVENLABS_API_KEY, TODAY, TODAY_DISPLAY
from mc_audio import generate_elevenlabs_audio, generate_podcast_audio
from mc_critic import run_critic_pass
from mc_deliver import generate_manifest, generate_rss_feed, save_daily_data, send_email
from mc_generate import generate_conversational_script, generate_html_briefing, generate_podcast_script
from mc_plan import fetch_grok_articles, plan_daily_searches

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("mikecast")


def main() -> None:
    force = "--force" in sys.argv

    logger.info("=" * 60)
    logger.info("MikeCast Daily Briefing — %s", TODAY_DISPLAY)
    logger.info("=" * 60)

    # Idempotency guard: don't overwrite a completed briefing unless --force
    daily_path = DATA_DIR / f"{TODAY}.json"
    if daily_path.exists() and not force:
        logger.warning(
            "Today's briefing (%s) already exists. "
            "Re-run with --force to regenerate. Exiting.",
            TODAY,
        )
        sys.exit(0)

    # 0. Plan today's searches + fetch live articles via xAI Grok
    logger.info("Step 0/10: Planning today's searches and fetching live articles via xAI Grok…")
    dynamic_queries: dict[str, list[str]] = {}
    trending_context: str = ""
    grok_articles: dict[str, list[dict]] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        with _TPE(max_workers=2) as _ex:
            _f_queries  = _ex.submit(plan_daily_searches)
            _f_articles = _ex.submit(fetch_grok_articles)
            dynamic_queries, trending_context = _f_queries.result()
            grok_articles = _f_articles.result()
        if dynamic_queries:
            total_dyn = sum(len(v) for v in dynamic_queries.values())
            logger.info("Planning complete: %d dynamic queries generated.", total_dyn)
        total_grok = sum(len(v) for v in grok_articles.values())
        logger.info("Grok live articles fetched: %d across %d categories.", total_grok, len(grok_articles))
    except Exception as exc:
        logger.warning("Planning step failed (non-fatal): %s", exc)

    # 1. Collect news (parallel I/O across all sources)
    logger.info("Step 1/10: Collecting news…")
    raw_news = collect_all_news(dynamic_queries=dynamic_queries or None)
    raw_total = sum(len(v) for v in raw_news.values())
    if raw_total == 0:
        logger.critical("No articles collected from any source — aborting.")
        sys.exit(1)
    if raw_total < 5:
        logger.warning(
            "Very few articles collected (%d) — possible widespread API failure.",
            raw_total,
        )

    # 1b. Inject Grok live articles (verified, current) before dedup
    if grok_articles:
        for cat, arts in grok_articles.items():
            if cat in raw_news:
                raw_news[cat] = arts + raw_news[cat]  # Grok articles first (highest priority)
        logger.info("Injected Grok articles into raw news collection.")

    # 2. Deduplicate (against 7-day rolling history)
    logger.info("Step 2/10: Deduplicating…")
    deduped = deduplicate(raw_news)

    # 2b. Drop stale articles (older than 3 days — prevents recirculated old content)
    deduped = filter_stale_articles(deduped, max_age_days=3)

    # 3. Cluster similar stories (cheap gpt-4o-mini — reduces count before scoring)
    logger.info("Step 3/10: Clustering duplicate stories…")
    clustered = cluster_articles(deduped)

    # 4. Score and rank articles (parallel per-category gpt-4o agents)
    logger.info("Step 4/10: Scoring and ranking articles…")
    scored = score_and_rank_articles(clustered, trending_context=trending_context)

    # 5. Select top articles (proportional across categories, ~25 total)
    logger.info("Step 5/10: Selecting top articles…")
    top_articles = select_top_articles(scored, total=25)
    total = sum(len(v) for v in top_articles.values())
    logger.info("Selected %d articles across %d categories.", total, len(top_articles))
    if total == 0:
        logger.warning("All articles were duplicates — briefing will have no new stories.")

    # 6. Enrich top 8 stories (fetch article body + 'why it matters' via gpt-4o-mini)
    logger.info("Step 6/10: Enriching top stories…")
    top_articles = enrich_top_stories(top_articles, top_n=8)

    # 7. Process Mike's Picks (user-submitted URLs / PDFs / text)
    logger.info("Step 7/10: Processing Mike's Picks…")
    picks = process_picks()

    # 8. Generate HTML briefing + both podcast scripts in parallel
    logger.info("Step 8/10: Generating HTML briefing and podcast scripts…")
    html: str = ""
    single_voice_script: str = ""
    conversational_script: str = ""

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_html   = ex.submit(generate_html_briefing, top_articles, picks)
        f_single = ex.submit(generate_podcast_script, top_articles, picks)
        f_conv   = ex.submit(generate_conversational_script, top_articles, picks)
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

    # 8b. Critic pass — score each section; regenerate weak ones
    logger.info("Step 8b/10: Running quality critic pass…")
    try:
        html, single_voice_script, conversational_script = run_critic_pass(
            html,
            single_voice_script,
            conversational_script,
            top_articles,
            picks,
        )
    except Exception as exc:
        logger.warning("Critic pass failed entirely (non-fatal): %s", exc)

    # 9. Generate audio
    logger.info("Step 9/10: Generating audio…")
    audio_path        = DATA_DIR / f"MikeCast_{TODAY}.mp3"
    el_audio_path     = DATA_DIR / f"MikeCast_3voice_{TODAY}.mp3"
    audio_ok          = False
    el_audio_ok       = False
    audio_filename    = None
    el_audio_filename = None

    # ElevenLabs 3-voice (preferred for RSS feed)
    if conversational_script and ELEVENLABS_API_KEY:
        logger.info("Generating ElevenLabs 3-voice audio…")
        el_audio_ok = generate_elevenlabs_audio(conversational_script, el_audio_path)
        if not el_audio_ok and el_audio_path.exists():
            el_audio_path.unlink()
            logger.warning("Removed partial ElevenLabs audio: %s", el_audio_path)
        el_audio_filename = el_audio_path.name if el_audio_ok else None

    # OpenAI TTS single-voice (only as fallback when ElevenLabs fails/unavailable)
    script_for_tts = single_voice_script or conversational_script
    if script_for_tts and not el_audio_ok:
        logger.info("Generating OpenAI TTS single-voice audio (ElevenLabs unavailable)…")
        audio_ok = generate_podcast_audio(script_for_tts, audio_path)
        if not audio_ok and audio_path.exists():
            audio_path.unlink()
            logger.warning("Removed partial OpenAI TTS audio: %s", audio_path)
        audio_filename = audio_path.name if audio_ok else None

    # Primary podcast audio = ElevenLabs if available, else OpenAI TTS
    primary_audio_file = el_audio_filename or audio_filename
    primary_audio_path = (
        el_audio_path if el_audio_ok else (audio_path if audio_ok else None)
    )

    # 10. Save data, update feeds, send email
    logger.info("Step 10/10: Saving data & sending email…")
    save_daily_data(
        html,
        top_articles,
        picks,
        single_voice_script,
        primary_audio_file,
        conversational_script=conversational_script,
        elevenlabs_audio_filename=el_audio_filename,
    )
    generate_manifest()
    generate_rss_feed()
    email_ok = send_email(
        html,
        single_voice_script or conversational_script,
        primary_audio_path,
    )

    logger.info(
        "Run summary — articles: %d | picks: %d | "
        "elevenlabs: %s | openai_tts: %s | email: %s",
        total,
        len(picks),
        "ok" if el_audio_ok else ("skip" if not ELEVENLABS_API_KEY else "FAILED"),
        "ok" if audio_ok else "FAILED",
        "ok" if email_ok else "FAILED",
    )
    logger.info("MikeCast briefing complete.")


if __name__ == "__main__":
    main()
