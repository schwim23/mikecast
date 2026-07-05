#!/usr/bin/env python3
"""
mc_edit.py — Edit and republish a past MikeCast episode, and manage its social posts.

Operates strictly on the --date you pass (never the import-time TODAY), so editing
an old episode is safe. Edits preserve episode_num and audio_file and stamp an
`edited_at` — they deliberately do NOT call save_daily_data() (which would recompute
episode_num / generated_at).

Subcommands:
    show          --date D                          Print episode meta + html_briefing
    set-html      --date D --file body.html         Replace html_briefing from a file
    regen         --date D                          Re-run the writing crew (HTML only)
    publish       --date D                          Push JSON to S3 + rebuild manifest/RSS + invalidate CloudFront
    repost-social --date D [--only x|ig] [--reuse-copy]
    delete-social --date D [--only x]

Typical edit flow:
    python3 mc_edit.py set-html --date 2026-07-01 --file fixed.html
    python3 mc_edit.py publish  --date 2026-07-01
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from mc_config import CLOUDFRONT_DIST_ID, DATA_DIR, S3_BUCKET
from mc_utils import _atomic_write_json, invalidate_cloudfront

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("mikecast.edit")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Episode load / local save (S3 push happens only in `publish`)
# ---------------------------------------------------------------------------

def _load(date: str) -> dict | None:
    local = DATA_DIR / f"{date}.json"
    if local.exists():
        return json.loads(local.read_text(encoding="utf-8"))
    if S3_BUCKET:
        from mc_utils import s3_load_json
        return s3_load_json(S3_BUCKET, f"data/{date}.json")
    return None


def _save_local(date: str, data: dict) -> Path:
    """Persist the edited episode locally (atomic). `publish` pushes it to S3."""
    data["edited_at"] = _now_iso()
    out = DATA_DIR / f"{date}.json"
    _atomic_write_json(out, data, indent=2, ensure_ascii=False)
    logger.info("Saved edited episode locally: %s (run `publish --date %s` to go live)", out, date)
    return out


def _require(date: str) -> dict:
    data = _load(date)
    if not data:
        sys.exit(f"ERROR: no episode data for {date}")
    return data


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_show(args) -> None:
    data = _require(args.date)
    meta = {k: data.get(k) for k in
            ("date", "date_display", "episode_num", "episode_description",
             "audio_file", "elevenlabs_audio_file", "generated_at", "edited_at")}
    print("=== EPISODE META ===")
    print(json.dumps(meta, indent=2, ensure_ascii=False))
    print("\n=== HTML BRIEFING ===")
    print(data.get("html_briefing", "(none)"))


def cmd_set_html(args) -> None:
    data = _require(args.date)
    body = Path(args.file).read_text(encoding="utf-8")
    data["html_briefing"] = body
    _save_local(args.date, data)


def cmd_regen(args) -> None:
    data = _require(args.date)
    from crew.writing_crew import run_writing
    html, _single, _conv = run_writing(
        data.get("articles", {}),
        data.get("mikes_picks", []),
        data.get("trending", []),
    )
    if not html or html.strip() == "<p>Briefing generation failed.</p>":
        sys.exit("ERROR: regen produced no usable HTML — leaving episode unchanged.")
    data["html_briefing"] = html
    _save_local(args.date, data)
    logger.info("Regenerated HTML briefing (%d chars). Review with `show`, then `publish`.", len(html))


def cmd_publish(args) -> None:
    data = _require(args.date)
    date = args.date

    # Write the (possibly edited) JSON. In S3 mode S3 is authoritative for the site.
    _atomic_write_json(DATA_DIR / f"{date}.json", data, indent=2, ensure_ascii=False)
    if S3_BUCKET:
        from mc_utils import s3_save_json
        s3_save_json(S3_BUCKET, f"data/{date}.json", data, indent=2, ensure_ascii=False)

    # Rebuild the manifest + RSS (episode_num ordering is derived from the set of
    # dates, which this edit doesn't change).
    from mc_deliver import generate_manifest, generate_rss_feed
    generate_manifest()
    generate_rss_feed()

    # Bust the CDN cache for the affected data files so the edit is visible now.
    invalidate_cloudfront(CLOUDFRONT_DIST_ID, [
        f"/data/{date}.json",
        "/data/manifest.json",
        "/data/feed.xml",
    ])
    logger.info("Published %s.", date)


def cmd_repost_social(args) -> None:
    from mc_dist_state import append_repost, load_dist_state, record_send, record_social_copy
    import mc_social

    date = args.date
    episode = _require(date)
    link = mc_social.deep_link(date)
    state = load_dist_state(date)

    # Resolve copy
    if args.reuse_copy and state.get("social_copy"):
        copy = {"x_text": state["social_copy"].get("x_text", ""),
                "ig_caption": state["social_copy"].get("ig_caption", "")}
        logger.info("Reusing persisted social copy.")
    else:
        try:
            from crew.distribution_crew import run_distribution
            copy = run_distribution(episode, link)
        except Exception as exc:
            logger.warning("Copywriter crew failed (%s) — using fallback template.", exc)
            copy = mc_social._fallback_copy(episode)
        record_social_copy(date, copy["x_text"], copy["ig_caption"])

    want_x = args.only in (None, "x")
    want_ig = args.only in (None, "ig")

    if want_x:
        old_id = (state.get("x") or {}).get("tweet_id")
        if old_id:
            logger.info("Deleting old tweet %s…", old_id)
            mc_social.delete_tweet(old_id)
        x_text = mc_social._fit_x(copy["x_text"], link)
        new_id = mc_social.post_to_x(x_text)
        if new_id:
            append_repost(date, "x", old_id, new_id)
            record_send(date, "x", {"tweet_id": new_id, "text": x_text})
            logger.info("Reposted to X: %s", new_id)
        else:
            logger.error("X repost failed (see above).")

    if want_ig:
        old = state.get("instagram") or {}
        if old.get("media_id"):
            print("\n⚠️  Instagram has no delete API. Manually delete the old post in the "
                  f"Instagram app first (media_id={old['media_id']}), then press Enter to continue…")
            try:
                input()
            except EOFError:
                pass
        n = len([r for r in state.get("repost_history", []) if r.get("channel") == "ig"]) + 1
        card_path = DATA_DIR / f"MikeCast_card_{date}_r{n}.png"
        mc_social.generate_card(episode, card_path)
        image_url = mc_social.upload_card(card_path, date, suffix=f"_r{n}")
        if not image_url:
            logger.error("Could not upload card (no public URL) — aborting IG repost.")
        else:
            caption = mc_social._fit_ig(copy["ig_caption"])
            media_id, container_id = mc_social.post_to_instagram(image_url, caption)
            if media_id:
                append_repost(date, "ig", old.get("media_id"), media_id)
                record_send(date, "instagram", {
                    "media_id": media_id, "container_id": container_id,
                    "image_url": image_url, "caption": caption,
                })
                logger.info("Reposted to Instagram: %s", media_id)
            else:
                logger.error("Instagram repost failed (see above).")


def cmd_delete_social(args) -> None:
    from mc_dist_state import load_dist_state
    import mc_social

    state = load_dist_state(args.date)
    want_x = args.only in (None, "x")
    want_ig = args.only in (None, "ig")

    if want_x:
        tweet_id = (state.get("x") or {}).get("tweet_id")
        if tweet_id:
            mc_social.delete_tweet(tweet_id)
        else:
            logger.info("No recorded tweet for %s.", args.date)

    if want_ig:
        media_id = (state.get("instagram") or {}).get("media_id")
        if media_id:
            print(f"\n⚠️  Instagram has no delete API. Delete media_id={media_id} manually "
                  "in the Instagram app.")
        else:
            logger.info("No recorded Instagram post for %s.", args.date)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Edit and republish a past MikeCast episode; manage its social posts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show"); p.add_argument("--date", required=True); p.set_defaults(func=cmd_show)
    p = sub.add_parser("set-html"); p.add_argument("--date", required=True); p.add_argument("--file", required=True); p.set_defaults(func=cmd_set_html)
    p = sub.add_parser("regen"); p.add_argument("--date", required=True); p.set_defaults(func=cmd_regen)
    p = sub.add_parser("publish"); p.add_argument("--date", required=True); p.set_defaults(func=cmd_publish)
    p = sub.add_parser("repost-social"); p.add_argument("--date", required=True); p.add_argument("--only", choices=["x", "ig"]); p.add_argument("--reuse-copy", action="store_true"); p.set_defaults(func=cmd_repost_social)
    p = sub.add_parser("delete-social"); p.add_argument("--date", required=True); p.add_argument("--only", choices=["x", "ig"]); p.set_defaults(func=cmd_delete_social)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
