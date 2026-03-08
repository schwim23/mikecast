"""
MikeCast — YouTube upload.

Converts the daily audio to a 1920x1080 video (ffmpeg + cover image) and
uploads it to YouTube via the Data API v3.

First-time setup (one-time interactive step):
  1. Go to Google Cloud Console → create a project → enable "YouTube Data API v3"
  2. Create OAuth 2.0 credentials (type: Desktop app) → download client_secrets.json
  3. Set YOUTUBE_CLIENT_SECRETS=/path/to/client_secrets.json in ~/.profile
  4. Run once interactively to authorize:
       .venv/bin/python3 mc_youtube.py --auth
     This opens a browser, asks you to approve, and stores a token at
     ~/.mikecast_youtube_token.json — all future cron runs use that token silently.

Optional env var:
  YOUTUBE_PRIVACY   — "public", "unlisted", or "private" (default: "public")
"""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mc_config import DATA_DIR, SCRIPT_DIR, TODAY, TODAY_DISPLAY

logger = logging.getLogger("mikecast")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
YOUTUBE_CLIENT_SECRETS = os.environ.get("YOUTUBE_CLIENT_SECRETS", "")
YOUTUBE_TOKEN_FILE     = Path.home() / ".mikecast_youtube_token.json"
YOUTUBE_PRIVACY        = os.environ.get("YOUTUBE_PRIVACY", "public")

THUMBNAIL_PATH = DATA_DIR / "cover.png"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


# ---------------------------------------------------------------------------
# OAuth2 helpers
# ---------------------------------------------------------------------------

def _get_credentials():
    """Load stored OAuth2 credentials, refreshing if expired."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise RuntimeError(
            "YouTube dependencies not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )

    creds = None
    if YOUTUBE_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(YOUTUBE_TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing YouTube OAuth2 token…")
            creds.refresh(Request())
        else:
            if not YOUTUBE_CLIENT_SECRETS:
                raise RuntimeError(
                    "YOUTUBE_CLIENT_SECRETS env var not set. "
                    "Set it to the path of your OAuth2 client_secrets.json file."
                )
            if not Path(YOUTUBE_CLIENT_SECRETS).exists():
                raise RuntimeError(
                    f"client_secrets.json not found at: {YOUTUBE_CLIENT_SECRETS}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(YOUTUBE_CLIENT_SECRETS, SCOPES)
            creds = flow.run_local_server(port=0)
            logger.info("YouTube OAuth2 authorization complete.")

        YOUTUBE_TOKEN_FILE.write_text(creds.to_json())
        logger.info("Token saved to %s", YOUTUBE_TOKEN_FILE)

    return creds


def _build_youtube_client():
    try:
        from googleapiclient.discovery import build
        import google_auth_httplib2
        import httplib2
    except ImportError:
        raise RuntimeError(
            "YouTube dependencies not installed. "
            "Run: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )
    creds = _get_credentials()
    http  = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http())
    return build("youtube", "v3", http=http)


# ---------------------------------------------------------------------------
# ffmpeg: audio → video
# ---------------------------------------------------------------------------

def build_video(audio_path: Path, output_path: Path) -> bool:
    """
    Combine cover image + audio into a 1920x1080 MP4.
    Falls back to a solid black background if cover.png doesn't exist.
    Returns True on success.
    """
    if not audio_path.exists():
        logger.error("Audio file not found: %s", audio_path)
        return False

    # Check ffmpeg is available
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True)
    if result.returncode != 0:
        logger.error("ffmpeg not found. Install with: sudo apt install ffmpeg")
        return False

    if THUMBNAIL_PATH.exists():
        # Static image video
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",
            "-i", str(THUMBNAIL_PATH),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                   "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black",
            "-shortest",
            str(output_path),
        ]
    else:
        # Black background with no image
        logger.warning("cover.png not found at %s — using black background.", THUMBNAIL_PATH)
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "color=c=black:s=1920x1080:r=1",
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            str(output_path),
        ]

    logger.info("Building video: %s → %s", audio_path.name, output_path.name)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.error("ffmpeg failed:\n%s", result.stderr[-2000:])
            return False
        logger.info("Video built: %s (%.1f MB)", output_path.name, output_path.stat().st_size / 1e6)
        return True
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out after 10 minutes.")
        return False
    except Exception as exc:
        logger.error("ffmpeg error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# YouTube upload
# ---------------------------------------------------------------------------

def upload_video(
    video_path: Path,
    title: str,
    description: str,
    privacy: str = YOUTUBE_PRIVACY,
) -> str | None:
    """
    Upload video_path to YouTube. Returns the video ID on success, None on failure.
    Uses resumable upload (handles large files; retries on transient errors).
    """
    try:
        from googleapiclient.http import MediaFileUpload
        from googleapiclient.errors import HttpError
    except ImportError:
        raise RuntimeError("Run: pip install google-api-python-client")

    if not video_path.exists():
        logger.error("Video file not found: %s", video_path)
        return None

    youtube = _build_youtube_client()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["MikeCast", "daily briefing", "AI", "tech", "news", "podcast"],
            "categoryId": "25",  # 25 = News & Politics
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        mimetype="video/mp4",
        resumable=True,
        chunksize=10 * 1024 * 1024,  # 10 MB chunks
    )

    logger.info("Uploading to YouTube: '%s' [%s]…", title, privacy)
    try:
        request = youtube.videos().insert(
            part=",".join(body.keys()),
            body=body,
            media_body=media,
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                logger.info("Upload progress: %d%%", pct)

        video_id = response["id"]
        url = f"https://www.youtube.com/watch?v={video_id}"
        logger.info("YouTube upload complete: %s", url)
        return video_id

    except Exception as exc:  # HttpError or network errors
        logger.error("YouTube upload failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Set thumbnail (optional — uses cover.png after upload)
# ---------------------------------------------------------------------------

def set_thumbnail(youtube_client, video_id: str) -> bool:
    """Upload cover.png as the video thumbnail."""
    if not THUMBNAIL_PATH.exists():
        return False
    try:
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(str(THUMBNAIL_PATH), mimetype="image/png")
        youtube_client.thumbnails().set(videoId=video_id, media_body=media).execute()
        logger.info("Thumbnail set for video %s", video_id)
        return True
    except Exception as exc:
        logger.warning("Thumbnail upload failed (non-fatal): %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main entry point (called from mikecast_briefing.py)
# ---------------------------------------------------------------------------

def publish_to_youtube(
    audio_path: Path,
    episode_num: int,
    episode_description: str,
) -> str | None:
    """
    Full pipeline: audio → video → YouTube upload.
    Returns the YouTube video ID on success, None on failure.
    Cleans up the temporary video file after upload.
    """
    if not YOUTUBE_CLIENT_SECRETS and not YOUTUBE_TOKEN_FILE.exists():
        logger.info("YouTube upload skipped — YOUTUBE_CLIENT_SECRETS not set and no token found.")
        return None

    title = f"MikeCast #{episode_num} — {TODAY_DISPLAY}"

    description = (
        f"{episode_description}\n\n"
        f"📰 Full briefing: https://schwim23.github.io/mikecast/\n"
        f"🎙️ Subscribe on Apple Podcasts / Spotify via the RSS feed:\n"
        f"https://schwim23.github.io/mikecast/data/feed.xml\n\n"
        f"MikeCast is an AI-powered daily news briefing covering AI & Tech, "
        f"Business & Markets, Companies, and NY Sports."
    )

    video_path = DATA_DIR / f"MikeCast_{TODAY}.mp4"

    try:
        # Step 1: build video
        ok = build_video(audio_path, video_path)
        if not ok:
            return None

        # Step 2: upload
        youtube = _build_youtube_client()
        video_id = upload_video(video_path, title, description)
        if not video_id:
            return None

        # Step 3: set thumbnail (best-effort)
        try:
            from googleapiclient.http import MediaFileUpload
            set_thumbnail(youtube, video_id)
        except Exception:
            pass

        return video_id

    finally:
        # Clean up the video file — it's large and we don't need to keep it
        if video_path.exists():
            video_path.unlink()
            logger.info("Cleaned up temporary video file: %s", video_path.name)


# ---------------------------------------------------------------------------
# CLI: python3 mc_youtube.py --auth  (first-time setup)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="MikeCast YouTube utilities")
    parser.add_argument("--auth", action="store_true", help="Run interactive OAuth2 authorization flow")
    parser.add_argument(
        "--upload",
        metavar="AUDIO_FILE",
        help="Upload a specific audio file (uses today's episode metadata)",
    )
    parser.add_argument("--episode", type=int, default=1, help="Episode number (used with --upload)")
    parser.add_argument("--desc", default="MikeCast daily news briefing.", help="Episode description")
    args = parser.parse_args()

    if args.auth:
        print("Starting OAuth2 authorization flow…")
        _get_credentials()
        print("Authorization complete. Token stored at:", YOUTUBE_TOKEN_FILE)

    elif args.upload:
        video_id = publish_to_youtube(
            audio_path=Path(args.upload),
            episode_num=args.episode,
            episode_description=args.desc,
        )
        if video_id:
            print(f"Uploaded: https://www.youtube.com/watch?v={video_id}")
        else:
            print("Upload failed.")
            sys.exit(1)

    else:
        parser.print_help()
