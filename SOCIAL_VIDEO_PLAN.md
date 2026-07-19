# MikeCast — Social Video (audio + captions) Plan for Instagram & X

**Status:** BUILT (2026-07-18) — code complete + locally verified; not yet live in prod ·
**Author:** planning session 2026-07-12 · **Owner:** Mike

## Build status (2026-07-18)
All phases implemented and locally verified end-to-end:
- **`mc_video.py`** — shared render engine (extracted from `mc_ad.py`): `render_frame`,
  `chunk_caption`, `render_captioned_video(segments, out, date)`, `build_daily_reel(episode, out)`,
  `tts_segment`. Fargate-safe cover loader (remote fallback since `data/` is `.dockerignore`'d).
  `_faststart()` remux enforces the moov atom front-loading. `mc_ad.py` now imports from it.
- **`mc_social.py`** — `upload_reel` (S3 `data/social/MikeCast_reel_<date>.mp4`),
  `post_reel_to_instagram` (media_type=REELS, 24×5s poll), `upload_video_to_x` (chunked
  INIT/APPEND/FINALIZE/STATUS, amplify_video), `post_to_x(text, media_ids=)`. `run_social_distribution`
  gained a `media` kind ("card"|"reel", default `SOCIAL_MEDIA_KIND` env). Reel-build failure → card (IG)
  / text+link (X). `--media` CLI flag.
- **`mc_edit.py`** `repost-social` supports `--media reel` (reel repost for both channels).
- **`mc_config.py`** `SOCIAL_MEDIA_KIND` (default "card"). **`requirements.txt`** +`moviepy>=2.0`, `numpy`.
- Daily Step 11 is unchanged in code — it already passes `media=None`, so flipping card→reel is a
  **one-env-var change** (`SOCIAL_MEDIA_KIND=reel` in the ECS task-def), no deploy.

**Local verify (2026-07-18):** rendered a real reel from `data/2026-05-17.json` cold-open
(2 segments, 34 caption cues, 75.5s). ffprobe: H.264 **1080×1920**, AAC 44.1k stereo, dur 75.5s,
**faststart OK** (moov@36 < mdat@83329), 2.4MB. Extracted frame confirms big burned-in captions,
speaker pill, wave, date badge, mikecast.io CTA — muted-proof.

**Remaining to go live (manual):** (1) merge to `main` → GH Actions builds the image (now includes
moviepy+ffmpeg); (2) one live IG test for a past date: `mc_social.py --media reel --only ig --date <past> --force`;
(3) if it looks right, set `SOCIAL_MEDIA_KIND=reel` on the ECS task-def (new revision) + repoint the
scheduler. X video is best-effort (free-tier 17/day INIT/FINALIZE cap) — falls back to text+link.

---

Goal: instead of (or in addition to) the static 1080×1080 image card, post a **short vertical video
that plays the podcast audio with burned-in captions**, so the clip communicates even when the viewer
has the app muted (which is the default for IG/X autoplay). Video drives to the full episode
(mikecast.io / Spotify / Apple).

---

## 0. TL;DR feasibility

- **Instagram: fully feasible.** IG Graph API publishes Reels with `media_type=REELS` + a public
  `video_url`, same 3-step container→poll→publish flow already used for images in `mc_social.py`.
  You already have `META_ACCESS_TOKEN` + `IG_USER_ID` with `instagram_content_publish`.
- **X: feasible but gated by the free API tier.** X supports chunked video upload
  (INIT/APPEND/FINALIZE/STATUS, `media_category=amplify_video`) then attaching the `media_id` to a
  tweet. BUT the **free tier caps the media `/initialize` + `/finalize` endpoints at 17 requests/24h**
  — one video/day fits, but it's tight and historically flaky on free tier. Plan: attempt X video,
  **fall back to the current text+link tweet (optionally with the static card image)** if upload is
  blocked or rate-limited. Revisit if upgrading to X Basic (~$200/mo).
- **Captions:** burn them into the video frames (always visible, muted-proof, no platform CC
  dependency). This is exactly the muted-viewing requirement. IG's API can't attach the music
  library or reliably attach SRTs, so burned-in is the right call for both platforms.

## 1. The big reuse — `mc_ad.py` already renders this

`mc_ad.py` already produces a **1080×1920 (9:16) H.264 MP4** with the 3 ElevenLabs voices and
**animated burned-in subtitles over the MikeCast brand background** (via `moviepy`). Its structure is
directly reusable:
- `parse_segments()` — splits `[MIKE]/[ELIZABETH]/[JESSE]` tagged text into (speaker, text) segments.
- `tts_segment()` — one ElevenLabs MP3 per segment; returns its duration (via `mutagen`).
- `_build_background()` / `_render_frame()` — brand bg, wordmark, speaker pill, animated waveform,
  subtitle box, CTA footer bar.
- moviepy `concatenate_videoclips` + `write_videofile(..., yuv420p)` — stitches one image-clip per
  segment (held for that segment's audio duration) → **captions are synced by construction** (each
  subtitle shows for exactly its audio segment; no Whisper/forced-alignment needed).

**Gap vs. today:** `mc_ad.py` writes a *30s promo* (fresh GPT-4o ad script + fresh TTS) and is a
standalone CLI (`SOC-Reels` backlog item, never wired into the daily pipeline or any publisher).

## 2. Design decisions

1. **Content = real podcast teaser, not a promo.** Reuse the day's `conversational_script` (already
   `[MIKE]/[ELIZABETH]/[JESSE]` tagged). Take the **cold-open / first ~60–90s of segments** → real
   podcast audio + real captions, no extra LLM call. (Alternative kept in back pocket: `mc_ad.py`'s
   fresh 30s promo, or an LLM-picked highlight window. See §7.)
   - Audio reuse: if `mc_audio.py` already TTS'd those segments for the episode, splice the existing
     segment MP3s instead of re-calling ElevenLabs → **zero extra TTS cost**. Fall back to a fresh
     `tts_segment()` per teaser segment only if per-segment audio isn't cached.
2. **Length:** target **60–90s** (IG Reels-tab eligibility is 5–90s at 9:16). A shorter, punchier clip
   beats the full 6-min episode for social; the full episode stays on Spotify/Apple/site.
3. **Caption granularity:** chunk each segment into ≤2-line cues (~7–10 words) timed proportionally
   across the segment's audio duration, rather than one long block per speaker. Big, high-contrast,
   inside the safe area. (Refinement over `mc_ad.py`'s whole-segment subtitle — not a blocker.)
4. **One master asset, 9:16, 1080×1920.** IG Reels needs 9:16; X accepts vertical too, so a single
   render serves both. (Optional later: a 1:1 variant for X feed.)
5. **Encoding contract (critical for IG):** H.264 + AAC (48kHz max, stereo), MP4, `yuv420p`, and
   **`-movflags +faststart`** so the `moov` atom is at the front — IG rejects files without it.
6. **Hosting:** upload the MP4 to S3 `data/social/MikeCast_reel_<date>.mp4` (same
   `upload_card`/CloudFront pattern) → public URL for IG's `video_url`. X uploads the local bytes
   directly (chunked), no URL needed.
7. **Safety/gating (match existing pipeline):** never raise to the daily run; per-channel try/except;
   dist-state gating (`mc_dist_state`); `--force` re-render; a `--media {reel,card}` flag so you can
   choose per run, defaulting to `card` until the reel path is proven, then flip to `reel`.

## 3. Build plan — phases

### Phase 0 — Extract the shared video engine
- Pull the reusable renderer out of `mc_ad.py` into a new `mc_video.py`:
  `render_captioned_video(segments, out_path, aspect="9:16", target_secs=75) -> Path`, where
  `segments = [(speaker, text, audio_path_or_None)]`. Keep `mc_ad.py` working (import from `mc_video`).
- Add the caption-chunking helper (segment text → timed ≤2-line cues).
- Ensure `-movflags +faststart` / faststart in the moviepy `write_videofile` params (or a post-pass
  ffmpeg `-c copy -movflags +faststart`).

### Phase 1 — Daily teaser builder
- `build_daily_reel(episode_data, out_path) -> Path`: select the cold-open segments from
  `conversational_script`, reuse cached per-segment audio if present (else `tts_segment`), call
  `mc_video.render_captioned_video`. Trim to ≤90s.

### Phase 2 — Instagram Reels publishing
- Add to `mc_social.py`: `post_reel_to_instagram(video_url, caption) -> (media_id, container_id)`.
  - `POST {IG_USER_ID}/media` with `media_type=REELS`, `video_url=`, `caption=`, `access_token=`.
  - Poll `{container_id}?fields=status_code` until `FINISHED` — **video processing is slower than
    images**, so bump the poll loop (e.g. up to ~24 attempts × 5s) vs. the current 12×3s.
  - `POST {IG_USER_ID}/media_publish` with `creation_id`.
- `upload_reel(local_path, date)` → S3 + public URL (mirror `upload_card`, content_type `video/mp4`).

### Phase 3 — X video publishing (best-effort, with fallback)
- Add `upload_video_to_x(path) -> media_id|None`:
  - INIT: `POST https://api.x.com/2/media/upload` `command=INIT, media_type=video/mp4,
    total_bytes, media_category=amplify_video` → `media_id`.
  - APPEND: ≤5MB chunks, `command=APPEND, media_id, segment_index=N`.
  - FINALIZE: `command=FINALIZE`; if `processing_info` present, poll `command=STATUS` until
    `succeeded`.
  - Attach: extend `post_to_x` to accept `media_ids=[media_id]` in the `POST /2/tweets` body.
- **Fallback:** on any INIT/APPEND/FINALIZE failure or rate-limit (free-tier 17/day on INIT/FINALIZE),
  log and post the existing text+link tweet (optionally attach the static card image instead). X video
  is additive, never a hard dependency.

### Phase 4 — Wire into the pipeline
- `run_social_distribution(..., media="reel")`: when `media=="reel"`, build the reel once, upload,
  publish to IG as a Reel and to X as video; record in dist-state with the media kind + ids.
  `mc_edit.py` repost path handles versioned filenames (IG re-fetches a new URL; X delete+repost).
- Keep `card` as the default until a few live reels look right, then flip the default (and/or set it
  in the ECS task-def command).

### Phase 5 — Verify & roll out
- Local: `mc_social.py --media reel --dry-run --date <past>` → inspect the MP4 (open it, confirm audio
  + captions + faststart via `ffprobe`).
- Live: one manual ECS run posting to IG (and X) for a past date; confirm the Reel plays with captions
  muted; confirm X either posts video or cleanly falls back. Instant rollback = `--media card`.

## 4. Cost & latency notes
- **TTS:** ~zero extra if splicing cached episode audio; otherwise a few ElevenLabs segments (~60–90s).
- **Render:** moviepy/ffmpeg for a ~75s 1080×1920 clip is a minute or two of Fargate CPU — within the
  ~20-min daily budget, but measure; if tight, lower FPS (24) or pre-render the static bg once.
- **Storage/egress:** one MP4/day on S3 (~5–15MB) — negligible.
- **X:** no extra $ on current free tier, but the 17/day INIT/FINALIZE cap is the real constraint.

## 5. Risks / gotchas
- **X free-tier media limits** are the top risk — treat X video as best-effort with fallback.
- **IG `moov` atom / faststart** — must be front-loaded or IG rejects the file. Enforce in encode.
- **No IG library music via API** — any audio must be embedded in the file (we embed the podcast
  audio — fine).
- **Reels-tab eligibility** requires 9:16 + 5–90s; longer/other-ratio publishes as a plain video post.
- **Render time on Fargate** — validate it doesn't blow the daily window; degrade FPS if needed.
- **moviepy dependency** — `mc_ad.py` already uses it, but confirm it's in `requirements.txt` and the
  Docker image (ffmpeg is already present for `mc_audio.py`).
- Keep the pipeline's **never-raise** discipline: a reel failure must fall back to the static card, not
  break the daily run.

## 6. Files touched (summary)
- New: `mc_video.py` (shared renderer), reel builder (in `mc_social.py` or `mc_video.py`).
- Edit: `mc_social.py` (reel build + IG Reels publish + X video upload + `--media` flag + gating),
  `mc_ad.py` (import shared renderer), `mc_dist_state.py` (record media kind), `mc_edit.py` (reel
  repost), `requirements.txt`/Dockerfile (confirm moviepy), `crew/distribution_crew.py` (optional:
  a dedicated short "teaser caption" if the reel wants different copy than the card).

## 7. Open decisions (defaults noted — confirm on resume)
1. **Teaser content** — default: **first ~60–90s of the real daily `conversational_script`** (real
   podcast audio + captions). Alternatives: `mc_ad.py`'s fresh 30s promo; or an LLM-picked highlight.
2. **Post reel *and* card, or reel *instead of* card?** — default: **reel replaces the card on IG**;
   keep card as the instant fallback. (IG allows both a Reel and a feed image on different days, but
   one post/day is the pattern.)
3. **X scope** — default: **attempt video, fall back to text+link (or text+card image)**. Or: skip X
   video entirely for v1 and ship IG-only.
4. **Caption style** — default: 2-line word-chunk cues, cyan/white on dark, speaker-colored name pill
   (reuse `SPEAKER_COLORS`).

## 8. Next action on resume
Start **Phase 0 + Phase 1**: extract `mc_video.py` from `mc_ad.py`, add caption chunking + faststart,
and build `build_daily_reel` off the day's `conversational_script`. Then Phase 2 (IG Reels publish) is
the smallest net-new API surface and the highest-value channel — ship IG first, add X after.

## Sources (verified 2026-07-12)
- Instagram content publishing / Reels: <https://developers.facebook.com/docs/instagram-platform/content-publishing/>,
  <https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/>,
  <https://postproxy.dev/blog/instagram-reels-api-publishing-guide/>
- X chunked media upload: <https://docs.x.com/x-api/media/quickstart/media-upload-chunked>,
  free-tier limits discussion <https://devcommunity.x.com/t/new-chunked-media-upload-initialize-and-finalize-endpoint-limits-too-low/242138>
