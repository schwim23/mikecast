"""
MikeCast — delivery and persistence.

Handles:
  - Saving daily briefing data as JSON (for the web dashboard)
  - Generating the episodes manifest (manifest.json)
  - Generating the podcast RSS 2.0 feed (feed.xml)
  - Sending the HTML briefing + audio via Gmail SMTP
"""

import json
import logging
import smtplib
from datetime import datetime, timezone
from email.mime.audio import MIMEAudio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from bs4 import BeautifulSoup

from mc_config import (
    DATA_DIR,
    GMAIL_APP_PASSWORD,
    GMAIL_FROM,
    GMAIL_TO,
    SITE_BASE_URL,
    TODAY,
    TODAY_DISPLAY,
)
from mc_generate import generate_episode_description
from mc_utils import _atomic_write_json

logger = logging.getLogger("mikecast")


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    """Escape special XML characters (&, <, >) in a string."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Data persistence
# ---------------------------------------------------------------------------

def save_daily_data(
    html_briefing: str,
    categorised: dict[str, list[dict]],
    picks: list[dict],
    podcast_script: str,
    audio_filename: str | None,
    conversational_script: str = "",
    elevenlabs_audio_filename: str | None = None,
) -> Path:
    """
    Save all briefing data as a JSON file for the dashboard.

    Episode number = chronological position of this date among all saved
    episodes. Today's existing file (if any) is excluded from the count so
    that --force reruns don't bump the episode number.
    """
    today_path = DATA_DIR / f"{TODAY}.json"
    existing = [p for p in DATA_DIR.glob("????-??-??.json") if p != today_path]
    episode_num = len(existing) + 1

    # Use the richer conversational script for the episode description when
    # available (it has more context than the single-voice script).
    desc_source = conversational_script if conversational_script else podcast_script
    episode_description = generate_episode_description(desc_source, episode_num)
    logger.info("Episode description: %s", episode_description)

    data = {
        "date": TODAY,
        "date_display": TODAY_DISPLAY,
        "episode_num": episode_num,
        "episode_description": episode_description,
        "html_briefing": html_briefing,
        "articles": categorised,
        "mikes_picks": picks,
        "podcast_script": podcast_script,
        "conversational_script": conversational_script,
        "audio_file": audio_filename,
        "elevenlabs_audio_file": elevenlabs_audio_filename,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path = DATA_DIR / f"{TODAY}.json"
    _atomic_write_json(out_path, data, indent=2, ensure_ascii=False)
    logger.info("Daily data saved: %s (episode #%d)", out_path, episode_num)
    return out_path


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def generate_manifest() -> None:
    """Write manifest.json listing all available briefing dates (newest first)."""
    dates = sorted(
        [p.stem for p in DATA_DIR.glob("????-??-??.json")],
        reverse=True,
    )
    _atomic_write_json(DATA_DIR / "manifest.json", {"dates": dates}, indent=2)
    logger.info("Manifest updated: %d dates", len(dates))


# ---------------------------------------------------------------------------
# RSS feed
# ---------------------------------------------------------------------------

def generate_rss_feed() -> None:
    """Generate a podcast-compatible RSS 2.0 feed at data/feed.xml."""
    from email.utils import formatdate
    import calendar

    # Build episode number map: chronological order → episode #1, #2, …
    all_episodes = sorted(DATA_DIR.glob("????-??-??.json"))
    episode_num_map = {p.stem: i + 1 for i, p in enumerate(all_episodes)}

    items: list[str] = []
    for json_path in sorted(DATA_DIR.glob("????-??-??.json"), reverse=True):
        try:
            with open(json_path) as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("Skipping %s — could not load JSON: %s", json_path.name, exc)
            continue

        date_str = data.get("date", json_path.stem)
        date_display = data.get("date_display", date_str)

        # Prefer ElevenLabs 3-voice audio when available; fall back to OpenAI TTS
        audio_file = data.get("elevenlabs_audio_file") or data.get("audio_file")
        if not audio_file:
            continue

        audio_url = f"{SITE_BASE_URL}data/{audio_file}"
        audio_path = DATA_DIR / audio_file
        file_size = audio_path.stat().st_size if audio_path.exists() else 0

        # Build pubDate in RFC 2822 format (publish at 6:45 AM ET = 11:45 UTC)
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=11, minute=45, tzinfo=timezone.utc
            )
            pub_date = formatdate(calendar.timegm(dt.timetuple()), usegmt=True)
        except ValueError:
            pub_date = formatdate(usegmt=True)

        episode_num = episode_num_map.get(json_path.stem, "?")

        # Use stored ~50-word description if available; fall back for older episodes
        if data.get("episode_description"):
            description = data["episode_description"]
        else:
            exec_summary = ""
            try:
                soup = BeautifulSoup(data.get("html_briefing", ""), "html.parser")
                for h2 in soup.find_all("h2"):
                    if "EXECUTIVE SUMMARY" in h2.get_text().upper():
                        p = h2.find_next_sibling("p")
                        if p:
                            exec_summary = p.get_text().strip()
                        break
            except Exception:
                pass
            fallback = exec_summary or f"MikeCast daily news briefing for {date_display}."
            description = f"Episode #{episode_num} — {fallback}"
            if len(description) > 4000:
                description = description[:3997] + "..."

        subtitle = description[:252] + "..." if len(description) > 255 else description

        items.append(f"""  <item>
    <title>MikeCast #{episode_num} — {_esc(date_display)}</title>
    <description>{_esc(description)}</description>
    <pubDate>{pub_date}</pubDate>
    <guid isPermaLink="false">mikecast-{date_str}</guid>
    <enclosure url="{audio_url}" type="audio/mpeg" length="{file_size}"/>
    <itunes:title>MikeCast #{episode_num} — {_esc(date_display)}</itunes:title>
    <itunes:subtitle>{_esc(subtitle)}</itunes:subtitle>
    <itunes:summary>{_esc(description)}</itunes:summary>
    <itunes:duration>0</itunes:duration>
    <itunes:explicit>false</itunes:explicit>
  </item>""")

    feed_url = f"{SITE_BASE_URL}data/feed.xml"
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
  xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>MikeCast — Daily Briefing</title>
    <link>{SITE_BASE_URL}</link>
    <description>Your daily AI-powered news briefing. Personalized news across AI &amp; Tech, Business &amp; Markets, Companies, and NY Sports.</description>
    <language>en-us</language>
    <atom:link href="{feed_url}" rel="self" type="application/rss+xml"/>
    <itunes:author>MikeCast</itunes:author>
    <itunes:owner>
      <itunes:name>MikeCast</itunes:name>
      <itunes:email>michael.schwimmer@gmail.com</itunes:email>
    </itunes:owner>
    <itunes:image href="{SITE_BASE_URL}data/cover.png"/>
    <image>
      <url>{SITE_BASE_URL}data/cover.png</url>
      <title>MikeCast — Daily Briefing</title>
      <link>{SITE_BASE_URL}</link>
    </image>
    <itunes:summary>Your daily AI-powered news briefing.</itunes:summary>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="News"/>
{chr(10).join(items)}
  </channel>
</rss>"""

    out_path = DATA_DIR / "feed.xml"
    out_path.write_text(rss, encoding="utf-8")
    logger.info("RSS feed written: %s (%d episodes)", out_path, len(items))


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def send_email(
    html_body: str,
    podcast_script: str,
    audio_path: Path | None,
) -> bool:
    """Send the HTML briefing + podcast script + audio via Gmail SMTP."""
    if not GMAIL_APP_PASSWORD:
        logger.warning("GMAIL_APP_PASSWORD not set — skipping email.")
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = GMAIL_FROM
    msg["To"] = GMAIL_TO
    msg["Subject"] = f"MikeCast Daily Briefing — {TODAY_DISPLAY}"

    subscribe_html = """
<div style="margin:2rem auto;max-width:600px;text-align:center;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <p style="color:#8b949e;font-size:13px;margin:0 0 12px;">Subscribe to MikeCast on your favourite podcast app:</p>
  <table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 auto;">
    <tr>
      <td style="padding:0 6px;">
        <a href="https://podcasts.apple.com/us/podcast/mikecast-daily-briefing/id1882539449" style="display:inline-block;text-decoration:none;border:0;">
          <img src="https://schwim23.github.io/mikecast/data/badge-apple.png" width="180" height="54" alt="Listen on Apple Podcasts" style="display:block;border:0;">
        </a>
      </td>
      <td style="padding:0 6px;">
        <a href="https://open.spotify.com/show/3SEexX9wC3nr4xStYK2jOv?si=Ia1BvyEGQLKqZ7TwByXOCQ" style="display:inline-block;text-decoration:none;border:0;">
          <img src="https://schwim23.github.io/mikecast/data/badge-spotify.png" width="180" height="54" alt="Listen on Spotify" style="display:block;border:0;">
        </a>
      </td>
      <td style="padding:0 6px;">
        <a href="https://schwim23.github.io/mikecast/data/feed.xml"
           style="display:inline-block;padding:8px 18px;border-radius:8px;background:#f97316;color:#ffffff;text-decoration:none;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;vertical-align:middle;">
          <div style="font-size:9px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:rgba(255,255,255,.75);line-height:1;margin-bottom:3px;">Subscribe via</div>
          <div style="font-size:14px;font-weight:700;color:#ffffff;line-height:1;">RSS Feed</div>
        </a>
      </td>
    </tr>
  </table>
</div>"""
    msg.attach(MIMEText(html_body + subscribe_html, "html", "utf-8"))

    # Podcast script as plain-text attachment
    script_part = MIMEText(podcast_script, "plain", "utf-8")
    script_part.add_header(
        "Content-Disposition", "attachment",
        filename=f"MikeCast_Script_{TODAY}.txt",
    )
    msg.attach(script_part)

    # Audio attachment (primary audio: ElevenLabs if available, else OpenAI TTS)
    if audio_path and audio_path.exists():
        with open(audio_path, "rb") as fh:
            audio_part = MIMEAudio(fh.read(), _subtype="mpeg")
        audio_part.add_header(
            "Content-Disposition", "attachment",
            filename=f"MikeCast_{TODAY}.mp3",
        )
        msg.attach(audio_part)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(GMAIL_FROM, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_FROM, [GMAIL_TO], msg.as_string())
        logger.info("Email sent to %s", GMAIL_TO)
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False
