# Clip Engine

Niche-agnostic clip-and-schedule system. Point it at a niche, give it source accounts, a logo, a caption style, and destination accounts — it discovers source videos, transcribes them, ranks the strongest moments with an LLM, cuts 9:16 clips with word-by-word captions, watermark and outro, dedupes so nothing repeats, queues everything for human review, schedules approved clips through Postiz (TikTok / Instagram / X), and pulls analytics back weekly.

Fitness (`campaigns/fitness.yaml`) is the seeded demo. **Nothing in the code hardcodes a niche, brand, or asset** — a new niche is a new YAML (or a few taps in the PWA's campaign wizard).

## Layout

| Path | What |
|---|---|
| `core/` | Settings (env-only), Postgres models, campaign config loader, Apify wrapper, LLM ranking client, storage, logging |
| `producer/` | The clip pipeline: discover → dedupe → comments → transcript → rank → download → render → queue |
| `producer/render/` | ffmpeg cut, face-aware 9:16 reframe, ASS karaoke captions, hook/watermark/badge/credit overlay, outro concat |
| `web/` | FastAPI API + static review PWA (queue, approve/reject, campaign wizard, analytics) |
| `scheduler/` | Postiz posting (drafts by default) + weekly analytics pull-back |
| `campaigns/` | One YAML per campaign (`fitness.yaml` demo) |
| `assets/<campaign>/` | Logo, corner badge, outro, font per campaign |
| `deploy/` | Dockerfiles, Railway runbook, cron reference |

Key docs: `SPEC.md` (full build spec), `ARCHITECTURE.md` (contracts), `POSTIZ_API.md` (verified Postiz API reference), `PLAN.md`.

## Quick start (local)

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env            # fill in values — never commit
.venv/bin/alembic upgrade head  # needs DATABASE_URL (Postgres)
.venv/bin/uvicorn web.api:app --port 8000   # PWA at http://localhost:8000
.venv/bin/python -m producer.run fitness    # one producer run
.venv/bin/python -m scheduler.schedule      # push approved clips to Postiz
.venv/bin/python -m scheduler.analytics     # weekly analytics pull
.venv/bin/pytest                            # test suite
```

System deps for the producer: `ffmpeg`, plus `yt-dlp` (installed via pip). Face-aware reframing uses MediaPipe/OpenCV with a centered-crop fallback.

## Non-negotiables

1. **No secrets in code** — everything from env vars (see `.env.example`); set them in Railway's variables tab only.
2. **Human review gate** — nothing posts automatically; clips wait in the PWA queue, and `autopost: false` (default) creates Postiz *drafts* even after approval.
3. **Dedupe** — a source video is processed once; `used_ranges` prevents overlapping clips across runs.
4. **Copyright-conservative** — `max_clips_per_source: 8` by default; `exhaust_source: true` is an explicit opt-in; source handles are credited on-screen and in captions.
5. **Editorial safety** — the per-campaign `ranking_rules` prompt excludes unsafe advice and guideline-violating content by default.

## Deploying

See `deploy/railway.md` — services (`web`, `producer` cron, `scheduler` cron, Postiz, Postgres), the shared volume for `STORAGE_DIR`, the env var checklist, and the one-time Postiz OAuth setup for TikTok/Instagram/X.
