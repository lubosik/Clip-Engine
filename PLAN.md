# CLIP ENGINE — BUILD PLAN

## PROJECT OVERVIEW
Clip Engine is a niche-agnostic, config-driven video clipping and scheduling system that runs "logo campaigns" at scale. An operator points it at a niche with a YAML config (sources, assets, caption style, destinations), and the system autonomously discovers source videos via Apify, transcribes them, ranks the strongest moments with an LLM, renders 9:16 clips with captions/watermarks/outro, queues them for human review in a phone-first PWA, and schedules approved clips through a self-hosted Postiz instance. Fitness is the seeded demo campaign only; no niche, brand, or asset is hardcoded.

---

## FEATURES IN SCOPE
1. Config-driven campaign YAML (`campaigns/<name>.yaml`); `fitness.yaml` ships as demo
2. Apify wrapper (`core/apify.py`) — YouTube search/transcript, TikTok discovery/comments/transcript, Instagram reels
3. Discovery pipeline with `min_view_count`, `uploaded_within`, and duration filters
4. Source-level dedupe via stable `source_id`; `used_ranges` JSON prevents overlapping clips across runs
5. LLM moment ranking with per-campaign `ranking_rules`, `min_score`, `max_clips_per_source` (default 8), opt-in `exhaust_source`
6. ffmpeg render pipeline: cut → face/subject-aware 9:16 reframe (OpenCV/MediaPipe) → word-by-word ASS captions → hook overlay (0–8 s) → centered watermark + corner badge + `via @{source_handle}` credit → outro concat
7. Word-level timestamps via Apify transcript actors; `faster-whisper` CPU fallback for word timing on cut clips only
8. Review PWA — phone-first, installable (manifest + service worker): queue, approve/reject/edit caption, per-campaign filter, analytics tab, new-clip notifications
9. Campaign creation wizard in PWA: enter niche/sources/ranking/destinations, upload assets → writes `campaigns/<name>.yaml` + `assets/<name>/`
10. Postiz scheduling via REST API: draft by default (`autopost: false`); X/Twitter supported as destination
11. Weekly analytics pull-back: Postiz API + Apify profile scrapers → time-series rows in Postgres
12. Postgres schema per SPEC §8 with required indexes
13. Railway deployment: `web`, `producer` (daily cron), `scheduler` (weekly cron), `postiz`, Postgres, volume at `STORAGE_DIR`

---

## FEATURES OUT OF SCOPE
- Automating platform OAuth (TikTok/IG/X configured once manually in Postiz Settings)
- Auto-posting without human review (review gate is mandatory)
- Hardcoded niche, brand, or asset anywhere in code

---

## TECH STACK
- **Producer/Scheduler:** Python 3.11, `apify-client`, `yt-dlp`, `ffmpeg` (libx264 veryfast), `opencv-python`, `mediapipe`, `faster-whisper` (small/base, CPU only)
- **LLM ranking:** OpenAI-compatible API via `LLM_API_KEY` + `LLM_MODEL` env vars (model-agnostic)
- **Web API:** FastAPI (Python 3.11), served as Railway `web` service
- **Frontend:** Static installable PWA (vanilla JS or minimal framework), `manifest.json` + service worker, bundled and served by FastAPI `/static`
- **Database:** Railway Postgres; SQLAlchemy (async) + Alembic migrations
- **Scheduling/posting:** Postiz self-hosted on Railway, REST API
- **Hosting:** Railway — 5 services: `web`, `producer`, `scheduler`, `postiz`, `postgres`; Railway Volume mounted at `STORAGE_DIR=/data/clips`
- **Config validation:** Pydantic v2 (loaded in `core/config.py`)

---

## PHASE BREAKDOWN

**Phase 1 — Core scaffold** (`core/`)
Config loader (Pydantic), DB models + Alembic migration, Apify client wrapper, shared utilities, `campaigns/fitness.yaml` placeholder. Validates env vars on startup; fails loudly if missing.
Agent: **backend**
Output: importable `core` package, DB schema applied, `fitness.yaml` parses cleanly.

**Phase 2 — Discovery + dedupe + transcript** (`producer/`)
YouTube discovery via `streamers/youtube-scraper`; source-level dedupe against `sources` table; transcript fetch via `pintostudio/youtube-transcript-scraper`; persist segments; skip already-processed sources.
Agent: **backend**
Output: `sources` + `transcripts` rows written for one YouTube search term.
*Sequential after Phase 1.*

**Phase 3 — LLM ranking with non-overlap** (`producer/`)
Build ranking prompt from `ranking_rules`; call LLM; parse structured JSON `{start, end, score, hook, reason}`; enforce `clip_length`, `min_score`, `max_clips_per_source`; check + update `used_ranges`; `exhaust_source` loop.
Agent: **backend**
Output: ranked moments persisted, ready for render.
*Sequential after Phase 2.*

**Phase 4 — Full clip render** (`producer/`)
ffmpeg cut; MediaPipe face-aware 9:16 reframe with motion smoothing; word-by-word ASS captions (base + highlight + outline); hook overlay; centered watermark + corner badge + lower-third credit; outro concat; write mp4 to `STORAGE_DIR`; insert `clips` row `status=pending_review`.
Agent: **backend**
Output: one end-to-end rendered mp4 for a fitness source clip.
*Sequential after Phase 3.*

**Phase 5 — Review PWA + approve/reject flow** (`web/`)
FastAPI endpoints: `GET /clips`, `POST /clips/{id}/approve`, `POST /clips/{id}/reject`, `PATCH /clips/{id}/caption`. Static PWA: queue view (thumbnail + inline player, hook, score), approve/reject/edit caption per card, per-campaign filter, installable manifest + service worker.
Agent: **frontend** (PWA) + **backend** (API endpoints)
*Can begin in parallel with Phase 4 once Phase 3 output defines the `clips` schema.*

**Phase 6 — Postiz draft creation** (`scheduler/`)
On clip approval, call Postiz REST API to create a draft post per destination channel. Respect `autopost` flag. Store `postiz_post_id` + scheduled slot on `clips` row. X/Twitter captions truncated to limit (hashtags first).
Agent: **backend**
Output: approved clips appear as drafts in Postiz.
*Sequential after Phase 5 approval flow exists.*

**Phase 7 — TikTok + Instagram sources; weekly analytics** (`producer/` + `scheduler/`)
Add TikTok/IG discovery + transcript actors to producer. Weekly analytics cron: Postiz analytics API + Apify profile scrapes; match to `clips` rows; write `analytics` time-series rows. PWA analytics tab reads these.
Agent: **backend** (analytics cron) + **frontend** (analytics tab)
*Can run in parallel: analytics cron is backend-only; analytics tab depends on Phase 5 PWA scaffold.*

**Phase 8 — Campaign wizard + Railway deploy** (`web/` + infra)
Campaign creation wizard in PWA: niche form, asset uploads, writes YAML + assets server-side via `POST /campaigns`. Railway `railway.json` / service configs; cron schedules for producer + scheduler; health check on `web`; volume mount; structured logging with Apify run IDs and costs.
Agent: **frontend** (wizard UI) + **backend** (wizard API + Railway config)
Output: fully deployed system, fitness campaign runnable end-to-end on Railway.
*Sequential last; all prior phases must be stable.*

---

## ROUTING SUMMARY
- **Project type:** Backend automation pipeline + installable PWA
- **Architecture required:** No (SPEC.md is the architecture contract)
- **Frontend owner:** Backend agent builds FastAPI; frontend agent builds PWA + wizard UI
- **Backend owner:** Backend agent owns all of `core/`, `producer/`, `scheduler/`, and API layer
- **Research owner:** Backend agent uses web tools before writing Apify actor calls and Postiz REST integration (API signatures may have changed)
- **Review owner:** Reviewer agent after Phase 4 (render), after Phase 6 (posting), and after Phase 8 (deploy)
- **Parallel phases:** Phase 5 frontend (PWA) can start after Phase 3 schema is locked; Phase 7 analytics tab can build alongside Phase 7 analytics cron
- **Sequential phases:** 1 → 2 → 3 → 4; Phase 6 after Phase 5; Phase 8 last

---

## ENVIRONMENT VARIABLES REQUIRED
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

---

## RISKS AND OPEN QUESTIONS
1. **Assets not yet uploaded.** `assets/fitness/logo.png`, `logo_circle.png`, `outro.mov`, and `Montserrat-ExtraBold.ttf` are referenced in `fitness.yaml` but do not exist in the repo. Config loader will fail loudly. Operator must supply before Phase 4 render is testable. Use placeholder/stub assets for Phases 1–3.
2. **Postiz OAuth is manual.** TikTok, Instagram, and X channel connections must be configured once by the operator in Postiz Settings → Providers before Phase 6 can post. Phase 6 can be built and tested against draft creation; actual platform delivery requires manual OAuth setup.
3. **Postiz REST API shape is undocumented publicly.** Backend agent must use web tools to fetch current Postiz API docs before writing the scheduler integration. The API may differ from assumptions.
4. **TikTok transcript actor cost.** `agentx/tiktok-transcript` costs $0.38/video. The backend must gate this behind selection (rank on metadata first, transcribe only shortlisted candidates). Confirm this gate is enforced in Phase 7 before enabling TikTok transcripts.
5. **MediaPipe face tracking on CPU-only VPS.** Face-aware reframe may be slow. Phase 4 must benchmark per-clip render time and expose a `face_tracking: false` fallback in the YAML template if it blocks throughput.
6. **LLM model not specified.** `LLM_MODEL` is operator-supplied. The ranking prompt must be model-agnostic (structured JSON output via system prompt, not tool use) so any OpenAI-compatible model works.
7. **Railway volume persistence.** `STORAGE_DIR` must be on the Railway volume, not ephemeral container storage. Verify mount path in `railway.json` before Phase 8 deploy.
8. **`WEB_ADMIN_PASSWORD` auth mechanism not specified.** The spec mentions the var but not the auth pattern (HTTP Basic? JWT? Session cookie?). Backend agent must choose and document before building the PWA login gate.

---

## COMPLEXITY RATING
**High.** Eight sequential/parallel build phases span a full async Python pipeline (Apify, LLM, ffmpeg, MediaPipe, faster-whisper), a FastAPI backend with five+ entity tables, an installable PWA with a campaign wizard and asset uploads, a Postiz REST integration with X/Twitter edge cases, a weekly analytics cron with fuzzy post-matching, and a multi-service Railway deployment with a persistent volume.
