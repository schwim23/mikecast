"""
MikeCast — audio generation.

Handles both TTS backends:
  - OpenAI TTS (single voice, "alloy") — always generated as backup
  - ElevenLabs TTS (3-voice: MIKE / ELIZABETH / JESSE) — preferred for RSS feed

Both functions return True on success, False on failure, and never raise.
"""

import logging
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


def _strip_vbr_header(path: Path) -> None:
    """
    Nullify the Xing/Info/VBRI VBR header in the first MP3 frame.

    When ElevenLabs segments are concatenated as raw bytes, the first segment's
    Xing header (which encodes only *that segment's* frame count) becomes the
    VBR header for the whole file. Players read it and report the first
    segment's duration (e.g. 21s) instead of the real file length. Clearing
    the marker makes players fall back to bitrate×file-size estimation, which
    is accurate enough and lets the subsequent TLEN stamp take effect.
    """
    try:
        data = path.read_bytes()
        for marker in (b"Xing", b"Info", b"VBRI"):
            pos = data[:2000].find(marker)
            if pos != -1:
                patched = data[:pos] + b"\x00" * len(marker) + data[pos + len(marker):]
                path.write_bytes(patched)
                logger.info("Stripped VBR header '%s' from %s", marker.decode(), path.name)
                return
    except Exception as exc:
        logger.warning("Could not strip VBR header from %s: %s", path.name, exc)


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

        chunks = _split_text_for_tts(script)
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

        with open(output_path, "wb") as fh:
            for seg in audio_segments:
                fh.write(seg)

        logger.info(
            "OpenAI TTS audio saved: %s (%.1f MB)",
            output_path, output_path.stat().st_size / 1e6,
        )
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

    def _tts_segment(speaker: str, text: str) -> bytes:
        """Call ElevenLabs for one speaker segment; handles chunking internally."""
        voice_id = voice_map[speaker]
        chunks = _split_text_for_tts(text, max_chunk=4500)
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
        return b"".join(audio_parts)

    audio_segments: list[bytes] = []
    for i, (speaker, text) in enumerate(segments):
        logger.info(
            "ElevenLabs TTS segment %d/%d [%s] (%d chars)…",
            i + 1, len(segments), speaker, len(text),
        )
        try:
            audio_bytes = _tts_segment(speaker, text)
            audio_segments.append(audio_bytes)
            time.sleep(0.3)  # gentle rate-limit buffer between segments
        except Exception as exc:
            logger.error("ElevenLabs segment %d [%s] failed: %s", i + 1, speaker, exc)
            return False

    try:
        with open(output_path, "wb") as fh:
            for seg in audio_segments:
                fh.write(seg)
        logger.info(
            "ElevenLabs audio saved: %s (%.1f MB, %d segments)",
            output_path, output_path.stat().st_size / 1e6, len(audio_segments),
        )
        _strip_vbr_header(output_path)
        _stamp_mp3_duration(output_path)
        return True
    except Exception as exc:
        logger.error("Failed to write ElevenLabs audio: %s", exc)
        return False
