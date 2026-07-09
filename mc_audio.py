"""
MikeCast — audio generation.

Handles both TTS backends:
  - OpenAI TTS (single voice, "alloy") — always generated as backup
  - ElevenLabs TTS (3-voice: MIKE / ELIZABETH / JESSE) — preferred for RSS feed

Both functions return True on success, False on failure, and never raise.
"""

import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from mc_config import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_VOICE_ELIZABETH,
    ELEVENLABS_VOICE_JESSE,
    ELEVENLABS_VOICE_MIKE,
    OPENAI_API_KEY,
)
from mc_generate import parse_conversational_script

logger = logging.getLogger("mikecast")


def _concat_mp3_segments(segments: list[bytes], output_path: Path) -> bool:
    """
    Concatenate MP3 byte *segments* into a single clean MP3 at *output_path*.

    Each TTS segment (from ElevenLabs or OpenAI) is a self-contained MP3 file
    with its own ID3 + VBR (Xing/Info) header. Joining them as raw bytes leaves
    those embedded headers mid-stream: the leading Xing header then describes
    only the *first* segment's frame count, so streaming players (Apple
    Podcasts, Spotify) mis-estimate the total duration and replay the tail to
    fill the perceived remaining time ("the end plays twice").

    Instead we decode every segment and re-encode one continuous stream with
    ffmpeg's concat demuxer. The result has exactly one valid Xing/LAME header
    and an accurate duration, so seek tables are correct everywhere.

    We also re-encode as **constant bitrate** (CBR, 128 kbps, 44.1 kHz) rather
    than VBR. A metadata-perfect VBR MP3 (correct Xing frame count + accurate
    itunes:duration) STILL makes Spotify replay the last ~20 seconds: Spotify
    ignores both and estimates length from file-size ÷ bitrate, overshoots on a
    VBR file, and loops the tail to fill the phantom time (Apple decodes real
    frames and is fine). For CBR, size ÷ bitrate equals the true duration
    exactly, so every player — Spotify included — agrees. Don't switch back to
    VBR (``-q:a``); it reintroduces the tail-replay on Spotify.

    Falls back to raw byte concatenation only if ffmpeg is unavailable or the
    concat fails — never raises, so delivery is never blocked.
    """
    def _raw_concat() -> bool:
        with open(output_path, "wb") as fh:
            for seg in segments:
                fh.write(seg)
        return True

    if not segments:
        logger.warning("No audio segments to concatenate for %s.", output_path.name)
        return False

    if not shutil.which("ffmpeg"):
        logger.warning(
            "ffmpeg not found — falling back to raw MP3 concatenation for %s "
            "(streaming players may mis-seek).", output_path.name,
        )
        return _raw_concat()

    try:
        with tempfile.TemporaryDirectory(dir=output_path.parent) as td:
            tmp = Path(td)
            seg_paths: list[Path] = []
            for i, seg in enumerate(segments):
                seg_path = tmp / f"seg_{i:04d}.mp3"
                seg_path.write_bytes(seg)
                seg_paths.append(seg_path)
            # concat demuxer list file; paths are simple temp names (no quoting hazards)
            list_path = tmp / "concat.txt"
            list_path.write_text("".join(f"file '{p.name}'\n" for p in seg_paths))
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-y",
                 "-f", "concat", "-safe", "0", "-i", str(list_path),
                 # CBR 128k @ 44.1kHz — see docstring: keeps Spotify from
                 # mis-estimating a VBR file's length and replaying the tail.
                 "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100",
                 str(output_path)],
                capture_output=True,
                check=True,
                timeout=300,
            )
        logger.info("Concatenated %d segment(s) → %s", len(segments), output_path.name)
        return True
    except Exception as exc:
        logger.error(
            "ffmpeg concat failed for %s (%s) — falling back to raw concatenation.",
            output_path.name, exc,
        )
        return _raw_concat()


def _stamp_mp3_duration(path: Path) -> None:
    """
    Write the actual audio duration into the MP3's ID3 TLEN tag so that
    email clients (and other tools that read only metadata) show the correct
    length instead of 0 or just the first-chunk duration.
    """
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, TLEN, error as ID3Error
        audio = MP3(path)
        duration_ms = int(audio.info.length * 1000)
        try:
            tags = ID3(path)
        except ID3Error:
            tags = ID3()
        tags["TLEN"] = TLEN(encoding=3, text=str(duration_ms))
        tags.save(path)
        logger.info("Stamped MP3 duration: %dms → %s", duration_ms, path.name)
    except Exception as exc:
        logger.warning("Could not stamp MP3 duration for %s: %s", path.name, exc)


def _normalize_loudness(path: Path, target_lufs: float = -16.0) -> None:
    """
    Normalize the MP3 at *path* to *target_lufs* (default −16 LUFS, the
    Apple Podcasts / Spotify podcast standard) using ffmpeg's two-pass
    loudnorm filter. Replaces the file in place. Logs a warning and returns
    without modifying the file if ffmpeg is unavailable or fails — never
    blocks delivery.

    Two-pass approach:
      Pass 1: measure actual integrated loudness, true peak, LRA.
      Pass 2: apply linear gain using measured values for accurate normalization.
    """
    try:
        # Pass 1: measure loudness stats
        pass1 = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-i", str(path),
                "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # loudnorm JSON is written to stderr
        stderr = pass1.stderr
        json_start = stderr.rfind("{")
        json_end = stderr.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            logger.warning("loudnorm pass 1 produced no JSON — skipping normalization for %s", path.name)
            return
        stats = json.loads(stderr[json_start:json_end])

        # Pass 2: apply normalization with measured values
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, dir=path.parent) as tmp:
            tmp_path = Path(tmp.name)
        try:
            af = (
                f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
                f":measured_I={stats['input_i']}"
                f":measured_TP={stats['input_tp']}"
                f":measured_LRA={stats['input_lra']}"
                f":measured_thresh={stats['input_thresh']}"
                f":offset={stats['target_offset']}"
                f":linear=true:print_format=summary"
            )
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-y", "-i", str(path), "-af", af,
                 # CBR 128k @ 44.1kHz — this is the final encode of the shipped
                 # episode, so it must stay constant-bitrate (see
                 # _concat_mp3_segments docstring re: Spotify tail-replay).
                 "-codec:a", "libmp3lame", "-b:a", "128k", "-ar", "44100",
                 str(tmp_path)],
                capture_output=True,
                check=True,
                timeout=180,
            )
            tmp_path.replace(path)
            logger.info("Loudness normalized to %.1f LUFS: %s", target_lufs, path.name)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    except Exception as exc:
        logger.warning("Loudness normalization failed for %s: %s", path.name, exc)


# ---------------------------------------------------------------------------
# Pre-TTS text normalization
# ---------------------------------------------------------------------------
# ElevenLabs (and to a lesser degree OpenAI TTS) sometimes produces garbled
# audio when the script contains bare colons in times (e.g. "7:05 PM ET"),
# bare hyphens between numbers (scores like "122-113"), or stray markdown.
# The writer prompts already tell Claude to spell these out, but we belt-and-
# braces here so a single slip doesn't ship to listeners.

_HOUR_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
    0: "twelve",
}
_OH_MIN_WORDS = {  # 1–9 in the "oh five" form (used when minutes are single-digit)
    1: "oh one", 2: "oh two", 3: "oh three", 4: "oh four", 5: "oh five",
    6: "oh six", 7: "oh seven", 8: "oh eight", 9: "oh nine",
}
_TEEN_WORDS = {  # 10–19
    10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
}
_DIGIT_WORDS = {  # 1–9 in their bare form (used after a tens word: "forty-five")
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine",
}
_TENS_WORDS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


def _minutes_to_words(m: int) -> str:
    if m == 0:
        return ""
    if m < 10:
        return _OH_MIN_WORDS[m]
    if m < 20:
        return _TEEN_WORDS[m]
    tens, ones = divmod(m, 10)
    return _TENS_WORDS[tens] if ones == 0 else f"{_TENS_WORDS[tens]} {_DIGIT_WORDS[ones]}"


_TIME_RE = re.compile(
    r"\b(\d{1,2}):(\d{2})\s*(PM|AM|pm|am)?(?:\s*(ET|EST|EDT))?\b"
)


def _time_to_words(match: "re.Match[str]") -> str:
    h = int(match.group(1))
    m = int(match.group(2))
    ampm = (match.group(3) or "").upper()
    tz = match.group(4)
    h12 = h % 12 or 12
    hour_word = _HOUR_WORDS.get(h12, str(h12))
    if m == 0:
        out = f"{hour_word} o'clock"
    else:
        out = f"{hour_word} {_minutes_to_words(m)}"
    if ampm:
        out += f" {ampm}"
    if tz:
        out += " Eastern"
    return out


def _tts_normalize(text: str) -> str:
    """
    Make a script chunk less likely to garble in TTS.

    - Spell out clock times (``7:05 PM ET`` → ``seven oh five PM Eastern``).
    - Convert ``ET``/``EST``/``EDT`` suffix to ``Eastern``.
    - Strip markdown bold/italic markers (``**x**`` and ``*x*``).
    - Leave dollar amounts, comma-separated big numbers, and other digit
      formats alone — modern TTS handles those reasonably well, and we don't
      want to over-rewrite the writer's prose.
    """
    if not text:
        return text
    text = _TIME_RE.sub(_time_to_words, text)
    # Markdown bold then italic (order matters — ** must run first).
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    return text


def _split_text_for_tts(text: str, max_chunk: int = 4000) -> list[str]:
    """
    Split *text* on sentence boundaries into chunks of at most *max_chunk*
    characters. Required because both OpenAI TTS and ElevenLabs enforce
    per-request character limits.
    """
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_chunk:
            chunks.append(remaining)
            break
        # Prefer splitting after a sentence-ending period + space
        split_at = remaining[:max_chunk].rfind(". ")
        if split_at == -1:
            split_at = max_chunk
        else:
            split_at += 2  # include the period and space in the current chunk
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    return chunks


# ---------------------------------------------------------------------------
# OpenAI TTS (single voice — backup / email attachment)
# ---------------------------------------------------------------------------

def generate_podcast_audio(script: str, output_path: Path) -> bool:
    """
    Generate MP3 audio from *script* using OpenAI TTS (voice: alloy).
    Writes the result to *output_path*. Returns True on success.
    """
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set — skipping OpenAI TTS.")
        return False

    try:
        from openai import OpenAI
        client = OpenAI()

        chunks = _split_text_for_tts(_tts_normalize(script))
        audio_segments: list[bytes] = []

        for i, chunk in enumerate(chunks):
            logger.info("OpenAI TTS chunk %d/%d (%d chars)…", i + 1, len(chunks), len(chunk))
            response = client.audio.speech.create(
                model="tts-1-hd",
                voice="alloy",
                input=chunk,
            )
            audio_segments.append(response.content)
            time.sleep(0.5)

        if not _concat_mp3_segments(audio_segments, output_path):
            return False

        logger.info(
            "OpenAI TTS audio saved: %s (%.1f MB)",
            output_path, output_path.stat().st_size / 1e6,
        )
        _normalize_loudness(output_path)
        _stamp_mp3_duration(output_path)
        return True

    except Exception as exc:
        logger.error("OpenAI TTS generation failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# ElevenLabs TTS (3-voice — preferred for RSS podcast feed)
# ---------------------------------------------------------------------------

def generate_elevenlabs_audio(
    conversational_script: str,
    output_path: Path,
) -> bool:
    """
    Generate a 3-voice MP3 using ElevenLabs TTS.

    Parses [MIKE] / [ELIZABETH] / [JESSE] speaker tags from
    *conversational_script*, calls the ElevenLabs API for each segment with
    the matching voice ID, then concatenates the raw MP3 bytes into a single
    file at *output_path*. Returns True on success.
    """
    if not ELEVENLABS_API_KEY:
        logger.warning("ELEVENLABS_API_KEY not set — skipping ElevenLabs audio.")
        return False

    voice_map = {
        "MIKE":      ELEVENLABS_VOICE_MIKE,
        "ELIZABETH": ELEVENLABS_VOICE_ELIZABETH,
        "JESSE":     ELEVENLABS_VOICE_JESSE,
    }
    missing = [name for name, vid in voice_map.items() if not vid]
    if missing:
        logger.warning("ElevenLabs voice IDs missing for: %s — skipping.", missing)
        return False

    segments = parse_conversational_script(conversational_script)
    if not segments:
        logger.warning("No segments parsed from conversational script.")
        return False

    def _tts_segment(speaker: str, text: str) -> list[bytes]:
        """
        Call ElevenLabs for one speaker segment, returning one MP3 blob per
        chunk. Returning the chunks separately (rather than raw-joining them)
        lets the caller hand every blob to the ffmpeg concat step, so even a
        long, multi-chunk segment produces a single cleanly-framed stream.
        """
        voice_id = voice_map[speaker]
        chunks = _split_text_for_tts(_tts_normalize(text), max_chunk=4500)
        audio_parts: list[bytes] = []
        for chunk in chunks:
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
            headers = {
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            }
            payload = {
                "text": chunk,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.55,
                    "similarity_boost": 0.75,
                    "style": 0.20,
                    "use_speaker_boost": True,
                },
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=90)
            resp.raise_for_status()
            audio_parts.append(resp.content)
        return audio_parts

    audio_segments: list[bytes] = []
    for i, (speaker, text) in enumerate(segments):
        logger.info(
            "ElevenLabs TTS segment %d/%d [%s] (%d chars)…",
            i + 1, len(segments), speaker, len(text),
        )
        try:
            audio_segments.extend(_tts_segment(speaker, text))
            time.sleep(0.3)  # gentle rate-limit buffer between segments
        except Exception as exc:
            logger.error("ElevenLabs segment %d [%s] failed: %s", i + 1, speaker, exc)
            return False

    try:
        if not _concat_mp3_segments(audio_segments, output_path):
            return False
        logger.info(
            "ElevenLabs audio saved: %s (%.1f MB, %d segments)",
            output_path, output_path.stat().st_size / 1e6, len(segments),
        )
        _normalize_loudness(output_path)
        _stamp_mp3_duration(output_path)
        return True
    except Exception as exc:
        logger.error("Failed to write ElevenLabs audio: %s", exc)
        return False
