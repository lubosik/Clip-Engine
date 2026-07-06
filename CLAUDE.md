# PROJECT: Clip Engine

## Goal
A niche-agnostic clip-and-schedule system that runs "logo campaigns" at scale. Point it at a niche, give it source accounts, a logo, a caption style, and destination accounts. It discovers source videos (YouTube/TikTok/Instagram via Apify), transcribes them, ranks the strongest moments with an LLM, cuts 9:16 clips, burns word-by-word captions, applies a watermark/badge/outro, dedupes so nothing repeats, exhausts each source across runs, queues everything for human review in a PWA, schedules approved clips through Postiz, and pulls analytics back weekly. Fitness is the seeded demo campaign only — nothing in the code may hardcode a niche, brand, or asset.

## Features in scope
- Config-driven campaigns: one YAML per niche in `campaigns/` (fitness.yaml ships as demo)
- Apify discovery + transcripts (exact actor IDs in the spec), comment-signal enrichment with per-post attribution
- LLM moment ranking with per-campaign `ranking_rules`, non-overlap tracking, `max_clips_per_source` (default 8), opt-in `exhaust_source`
- ffmpeg pipeline: cut → face-aware 9:16 reframe → word-by-word ASS captions → hook overlay (0–8s) → centered watermark + corner badge + `via @{source_handle}` credit → outro concat
- Review PWA (phone-first, installable): queue, approve/reject/edit caption, per-campaign filter, analytics tab, notifications
- **Campaign creation wizard in the frontend**: launch a new campaign from the UI — enter niche, sources, ranking rules, destinations, upload logo/badge/outro/font — it writes the YAML + assets. No code change per niche.
- Postiz scheduling (drafts unless `autopost: true`), including **X/Twitter** as a destination platform (operator's X: https://x.com/viciresearch)
- Weekly analytics pull-back (Postiz + Apify profile scrapes), time-series in Postgres
- Postgres data model per spec §8; Railway deployment (web, producer cron, scheduler cron, postiz, postgres + volume)

## Features explicitly out of scope
- Automating platform OAuth (TikTok/IG/X apps configured once manually in Postiz)
- Auto-posting without human review (review gate is mandatory)
- Any hardcoded niche/brand/asset

## Tech stack
- Producer/Scheduler: Python 3.11, ffmpeg, yt-dlp, apify-client, faster-whisper (CPU fallback only), OpenCV/MediaPipe face tracking
- Web: FastAPI (thin API) + static installable PWA (manifest + service worker)
- Database: Postgres (Railway), SQLAlchemy + Alembic
- Scheduling/posting: Postiz (self-hosted on Railway) via REST API
- Hosting: Railway (services: web, producer, scheduler, postiz; volume at STORAGE_DIR)

## Environment variables needed
- APIFY_TOKEN
- DATABASE_URL
- POSTIZ_API_URL
- POSTIZ_API_KEY
- LLM_API_KEY
- LLM_MODEL
- STORAGE_DIR=/data/clips
- WEB_ADMIN_PASSWORD
- TZ=America/New_York

## Deploy target
Railway (web + producer cron + scheduler cron + postiz + postgres)

## Reviewer notes
- No secrets in code — everything from env vars; fail loudly if missing
- Human review gate cannot be bypassed; `autopost: false` must produce Postiz drafts
- Dedupe: a source is processed once; `used_ranges` prevents overlapping clips across runs
- Copyright: `max_clips_per_source` default 8; `exhaust_source` is opt-in; source handle credited on-screen and in caption
- Ranking rules must exclude unsafe/banned content by default
- Full spec lives in SPEC.md — audit against it
