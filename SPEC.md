# CLIP ENGINE — Build Specification

**Deliverable:** A niche-agnostic clip-and-schedule system that runs "logo campaigns" at scale. Point it at a niche, give it source accounts, a logo, a caption style, and destination accounts. It discovers source videos, transcribes them, ranks the strongest moments, cuts 9:16 clips, burns word-by-word captions, applies a watermark and an outro, dedupes so nothing repeats, exhausts each source, queues everything for human review, schedules approved clips, and pulls analytics back weekly.
**Demo config:** Fitness (personal fitness, muscle growth, protein/supplement education). Fitness is only the seeded example. Nothing in the code may hardcode a niche, a brand, or an asset.
**Operator additions:** (a) The frontend must include a campaign-creation wizard — launch a new campaign per niche from the UI, upload all required assets, and it fills the YAML (sources, social URLs, destinations) so the system runs autonomously for whatever niche/accounts the operator specifies. (b) X/Twitter is a supported destination platform (operator X profile: https://x.com/viciresearch).

---

## 0. Non-negotiables

1. **No secrets in code.** Every credential is read from an environment variable. Never hardcode the Apify token, Postiz key, or any account credential. Expected env vars in §11.
2. **Interchangeable by design.** Niche, sources, logo, caption style, hook style, watermark, outro, and destinations are all per-campaign inputs in `campaigns/<name>.yaml`. Adding a new niche = adding a new YAML file (or using the wizard). No code change.
3. **Human review gate is mandatory.** Nothing posts automatically. Clips land in a review queue (the PWA). A human approves or rejects before anything is scheduled.
4. **Dedupe + full-source coverage.** A source video is processed once and never re-clipped. Within a source, track which time ranges have already been turned into clips so the source can be exhausted across runs without duplicate or overlapping clips.
5. **Copyright default is conservative.** Per-source clip cap defaults to `max_clips_per_source` (default 8). "Exhaust the whole source" is a per-campaign toggle (`exhaust_source: true`) the operator switches on knowingly. Always credit the original source handle on-screen and in the caption.
6. **Content-quality filter lives in the ranking prompt.** Configurable per campaign (`ranking_rules`). Default rules prefer genuinely useful/interesting moments and exclude content that would get the account banned or that makes unsafe claims.

---

## 1. Architecture

Single repo, service-oriented:

- **`core/`** — Postgres models, config loader, shared utilities, Apify client wrapper.
- **`producer/`** — the clip pipeline (one run per campaign, triggered by cron).
- **`web/`** — the review PWA + campaign wizard + a thin API.
- **`scheduler/`** — pushes approved clips to Postiz and pulls analytics back.
- **`campaigns/`** — one YAML per campaign. `fitness.yaml` ships as the demo.
- **`assets/`** — per-campaign logos, outros, fonts.

Stack: Python 3.11 for producer and scheduler (ffmpeg, faster-whisper as fallback, apify-client). FastAPI + static installable PWA for `web/` (manifest + service worker). Postgres for state. Everything deploys as Railway services.

---

## 2. Config-driven campaigns

A campaign is one YAML file — the entire interface for running a new niche. Ship `campaigns/fitness.yaml` as the demo; every field is real.

```yaml
# campaigns/fitness.yaml
name: fitness
enabled: true

sources:
  youtube:
    search_terms:
      - "hypertrophy science explained"
      - "protein intake muscle growth"
      - "progressive overload podcast"
    channels: []            # optional explicit channel URLs
    min_view_count: 20000
    uploaded_within: "year" # hour|day|week|month|year
  tiktok:
    profiles: []
    hashtags: ["fitnesstips", "hypertrophy"]
  instagram:
    profiles: []

ranking:
  clip_length: [20, 60]     # seconds, min/max
  max_clips_per_source: 8
  exhaust_source: false
  min_score: 0.6
  ranking_rules: |
    Prefer moments that are genuinely useful or interesting on their own:
    a clear explanation of a mechanism, a specific actionable tip, a
    surprising-but-true fact, a vivid story, a strong opinion with reasoning.
    Each clip must stand alone with a hook in the first 2 seconds.
    EXCLUDE: unsafe or extreme advice, dangerous dieting/cutting, anything
    promoting disordered eating, medical claims presented as fact, and
    anything that would violate TikTok/Instagram community guidelines.
    When in doubt, skip it.

template:
  aspect: "9:16"
  resolution: [1080, 1920]
  captions:
    style: "word_by_word"   # CapCut-style, active word highlighted
    font: "assets/fitness/Montserrat-ExtraBold.ttf"
    base_color: "#FFFFFF"
    highlight_color: "#00E5FF"
    outline_color: "#000000"
    outline_px: 6
    position: "upper_mid"
    max_words_per_line: 4
  hook:
    enabled: true
    show_seconds: [0, 8]
    source: "ranking"
    font: "assets/fitness/Montserrat-ExtraBold.ttf"
    box_color: "#111111CC"
  lower_third:
    show_source_handle: true
    format: "via @{source_handle}"
  watermark:
    image: "assets/fitness/logo.png"
    position: "center"
    opacity: 0.18
    scale: 0.5
  corner_badge:
    image: "assets/fitness/logo_circle.png"
    position: "top_right"
    opacity: 1.0
    scale: 0.12
  outro:
    enabled: true
    clip: "assets/fitness/outro.mov"
    audio: "keep"           # keep|mute

destinations:
  postiz_channels: ["tiktok_fitness", "instagram_fitness"]  # may include X channels
  schedule:
    posts_per_day: 1
    times: ["17:00"]
    timezone: "America/New_York"
  caption_template: |
    {hook}

    via @{source_handle}
    {hashtags}
  hashtags: ["#fitness", "#hypertrophy", "#protein", "#gymtok"]
  autopost: false           # false = draft only, human approves in Postiz too

analytics:
  track: true
  pull_day: "monday"
```

The config loader validates every field, resolves asset paths, and fails loudly with a clear message if an asset is missing.

---

## 3. Apify integration

All Apify calls go through one client wrapper (`core/apify.py`) reading `APIFY_TOKEN` from env. Use the `apify-client` Python SDK: `client.actor("<id>").call(run_input=...)`, then iterate `client.dataset(run["defaultDatasetId"]).iterate_items()`. Handle error items (records carrying an `error`/`errorCode` field) by logging and skipping — never crash the run.

| Purpose | Actor ID | Notes |
|---|---|---|
| YouTube search + metadata | `streamers/youtube-scraper` | Search by term or channel URL; filter by date/view count; returns id, url, title, channelName, viewCount, date, duration. |
| YouTube transcript | `pintostudio/youtube-transcript-scraper` | One video URL per call; returns `{start, dur, text}` segments. |
| TikTok discovery | `clockworks/free-tiktok-scraper` | Hashtag/profile/search → posts with playCount, diggCount, webVideoUrl, durations. |
| TikTok comments | `clockworks/tiktok-comments-scraper` | Attribute every comment to its post via `videoWebUrl`/post URL. |
| TikTok transcript | `agentx/tiktok-transcript` | Per-video URL; returns `transcript.segments` start/end/text. $0.38/video — gate behind selection. |
| Instagram reels | `apify/instagram-reel-scraper` | Profile or reel URLs → reels with videoUrl, transcript, engagement. |
| Instagram recent reels | `apify/instagram-scraper` | `resultsType: reels` + `onlyPostsNewerThan: "1 week"`. |

**Cost discipline:** discover and rank on cheap metadata first; only transcribe selected candidates; never transcribe an already-processed video (check dedupe first).

**Downloading:** `yt-dlp` for YouTube; `videoUrl`/`downloadedVideo` fields for TikTok/Instagram. Clean up raw downloads after clips render.

---

## 4. Producer pipeline (one run = one campaign)

1. **Discover** — query sources via actors; apply `min_view_count` / `uploaded_within` / duration filters.
2. **Dedupe (source level)** — stable `source_id` (platform + native id); skip status `done`.
3. **Comment signal (TikTok, optional)** — aggregate per-post signal; every comment stored with its `post_url`.
4. **Transcript** — selected sources only; normalize to `[{start, end, text}]` seconds; persist so re-runs never re-fetch.
5. **Rank** — LLM with `ranking_rules` + comment summary → structured JSON array of `{start, end, score, hook, reason}`. Enforce `clip_length`, `min_score`, `max_clips_per_source`, and **non-overlap** against `used_ranges`. If `exhaust_source: true`, loop until usable ranges consumed.
6. **Cut + reframe 9:16** — ffmpeg cut; face/subject-aware crop (OpenCV/MediaPipe, motion smoothing, hard-cut pans on jumps); target `template.resolution`.
7. **Word-by-word captions** — word timestamps (transcript if word-level, else local faster-whisper on the cut clip); ASS karaoke: base color line, `highlight_color` on the active word, outline+shadow; honor `max_words_per_line` and `position`; burn with ffmpeg.
8. **Hook overlay** — ranker's `hook` as bold boxed overlay for `show_seconds`, placed clear of the caption bar.
9. **Watermark + badge + credit** — centered semi-transparent watermark, corner badge, lower-third `via @{source_handle}`.
10. **Outro** — if enabled, concat outro (normalize fps/resolution/audio).
11. **Queue** — write mp4 to storage; insert `clips` row status `pending_review`; mark source `done` or `partially_done`.

---

## 5. Review PWA (`web/`)

Phone-first, installable (valid manifest + service worker; critical state from the API, not localStorage).

- **Queue view:** newest first — thumbnail + inline player, hook, score, source credit, destination channels, proposed slot.
- **Approve / Reject / Edit caption** per clip. Approve → scheduler; reject archives with optional reason.
- **Per-campaign filter** + "today's batch" view.
- **Analytics tab:** week-by-week per channel and per clip.
- **New-clip notifications.**
- **Campaign wizard:** create/edit campaigns from the UI — niche name, sources (YouTube terms/channels, TikTok profiles/hashtags, IG profiles), ranking settings + rules, caption/hook styling, asset uploads (logo, corner badge, outro, font), destinations (Postiz channels incl. X), schedule, hashtags. Writes `campaigns/<name>.yaml` + saves assets to `assets/<name>/`.

Design: quiet and legible on a phone; one primary action per card (Approve); empty state shows next run time; active-verb copy ("Schedule" → "Scheduled").

---

## 6. Scheduler + posting

Use **Postiz** (self-hosted on Railway) via REST API.

- On approval, create a Postiz post per destination channel with rendered caption + clip at the next open slot per `destinations.schedule`.
- `autopost: false` → **draft** in Postiz; only `autopost: true` schedules directly.
- Record Postiz post id + scheduled time on the `clips` row.
- Platform OAuth (TikTok/Instagram/X) configured once manually in Postiz Settings → Providers. Do not automate OAuth.
- X captions must respect X length limits (truncate hashtags first).

---

## 7. Analytics (weekly)

On `analytics.pull_day`, for each posted clip fetch stats from (1) Postiz analytics where exposed, (2) Apify scrapers on the destination profiles (TikTok `clockworks/free-tiktok-scraper`; IG `apify/instagram-scraper`/`instagram-reel-scraper` with `onlyPostsNewerThan: "1 week"`). Match scraped posts to `clips` rows via stored permalink/handle (fallback: caption/hash matching). Write time-series rows (`clip_id`, `platform`, `pulled_at`, metrics). PWA analytics tab reads these.

---

## 8. Data model (Postgres, minimum)

- **campaigns** — name, enabled, loaded-config snapshot, timestamps.
- **sources** — source_id (unique), campaign, platform, url, title, metadata JSON, status (`pending|selected|done|partially_done`), used_ranges JSON, processed_at.
- **transcripts** — source_id (fk), segments JSON, word_level bool.
- **clips** — id, campaign, source_id, start, end, hook, score, reason, file_path, caption, destination_channels JSON, status (`pending_review|approved|rejected|scheduled|posted`), postiz_post_ids JSON, posted_permalinks JSON, reject_reason, timestamps.
- **comments** — source_id (fk), post_url, text, likes, created_at.
- **analytics** — clip_id (fk), platform, pulled_at, views, likes, comments, shares.

Indexes on `sources.source_id`, `clips.status`, `clips.campaign`, `analytics.clip_id`.

---

## 9. Orchestration

- One cron per enabled campaign → producer run (default daily, ahead of post time).
- Weekly cron → analytics pull-back.
- Idempotent runs; dedupe + `used_ranges` prevent duplicates.
- Small worker pool; cap parallel ffmpeg jobs to CPU count.

## 10. Performance (CPU-only VPS)

- Prefer Apify transcript actors over local Whisper; local `faster-whisper` (small/base) only for word timing on cut clips.
- ffmpeg `-c:v libx264 -preset veryfast -crf 20`; re-encode only when compositing requires it.
- Clean up raw/intermediate files after each render.

## 11. Environment variables

```
APIFY_TOKEN=
DATABASE_URL=
POSTIZ_API_URL=
POSTIZ_API_KEY=
LLM_API_KEY=
LLM_MODEL=
STORAGE_DIR=/data/clips
WEB_ADMIN_PASSWORD=
TZ=America/New_York
```

Set only in Railway variables. Rotate the Apify token if it was ever shared.

## 12. Railway deployment

Services: `web`, `producer` (cron), `scheduler` (cron), `postiz`, Railway Postgres. Volume for `STORAGE_DIR`. Health checks on `web`. Structured logging; log Apify run ids and costs per run.

## 13. Build order (ship in slices)

1. Core scaffold + config loader + DB + Apify wrapper (fitness.yaml validates).
2. Discovery + dedupe + transcript for one YouTube source (no re-processing).
3. Ranking with non-overlap.
4. Render one full clip end to end.
5. Review PWA + approve flow.
6. Postiz draft creation on approve.
7. TikTok + Instagram sources; weekly analytics.
8. Cron + Railway deploy; PWA pinned to home screen.
