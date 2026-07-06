# Clip Engine — Technical Architecture

This is the shared contract. Every agent builds against this. If a detail is missing here, SPEC.md wins; if both are silent, use judgment and note the decision in your final report.

## 1. File / folder structure

```
clip-engine/
├── CLAUDE.md / SPEC.md / PLAN.md / ARCHITECTURE.md / README.md
├── pyproject.toml                  # single Python package: clip_engine (deps below)
├── .env.example                    # every env var from SPEC §11, values blank
├── .gitignore                      # .env, assets/**/media, storage/, __pycache__, raw downloads
├── alembic.ini
├── migrations/                     # alembic migrations (owned by CORE)
├── core/
│   ├── __init__.py
│   ├── settings.py                 # env loading (pydantic-settings); fails loudly on missing required vars
│   ├── db.py                       # engine/session factory from DATABASE_URL
│   ├── models.py                   # SQLAlchemy models (§3 below) — CANONICAL, owned by CORE
│   ├── config.py                   # campaign YAML loader + pydantic schema validation + asset path resolution
│   ├── apify.py                    # ApifyClient wrapper: run_actor(actor_id, run_input) -> iterator of items; logs run id + cost; skips error items
│   ├── llm.py                      # thin LLM client (Anthropic Messages API via LLM_API_KEY/LLM_MODEL), json-mode helper
│   ├── storage.py                  # paths under STORAGE_DIR: raw/, clips/, thumbs/; cleanup helpers
│   └── logging.py                  # structured logging setup (json lines)
├── producer/
│   ├── __init__.py
│   ├── run.py                      # entrypoint: python -m producer.run <campaign>  (or --all)
│   ├── discover.py                 # §4.1 YouTube/TikTok/IG discovery via core.apify
│   ├── dedupe.py                   # §4.2 source_id computation + skip logic
│   ├── comments.py                 # §4.3 TikTok comment pull + per-post aggregation
│   ├── transcripts.py              # §4.4 fetch + normalize to [{start,end,text}] + persist
│   ├── ranker.py                   # §4.5 LLM ranking, non-overlap vs used_ranges, exhaust loop
│   ├── download.py                 # yt-dlp / direct URL download of selected sources
│   └── render/
│       ├── __init__.py             # render_clip(campaign_cfg, source, clip_range, transcript, workdir) -> final mp4 path + thumbnail
│       ├── cut.py                  # ffmpeg cut of [start,end]
│       ├── reframe.py              # face-aware 16:9 → 9:16 crop (OpenCV/MediaPipe, smoothed; centered-crop fallback)
│       ├── captions.py             # word timestamps (faster-whisper fallback) → ASS karaoke file
│       ├── overlay.py              # hook box, watermark, corner badge, lower-third credit (single ffmpeg filtergraph pass with caption burn)
│       └── outro.py                # normalize + concat outro
├── scheduler/
│   ├── __init__.py
│   ├── postiz.py                   # Postiz REST client (POSTIZ_API_URL/KEY): upload media, create post/draft per channel
│   ├── schedule.py                 # entrypoint: python -m scheduler.schedule — picks up clips status=approved, next open slot per campaign schedule, creates Postiz posts, sets status=scheduled
│   └── analytics.py                # entrypoint: python -m scheduler.analytics — weekly pull (Postiz + Apify), permalink matching, writes analytics rows
├── web/
│   ├── __init__.py
│   ├── api.py                      # FastAPI app (owned by BACKEND) — serves /api/* AND static PWA from web/static/
│   ├── auth.py                     # bearer/password check against WEB_ADMIN_PASSWORD
│   ├── campaigns_io.py             # wizard support: write campaigns/<name>.yaml, save uploaded assets to assets/<name>/
│   └── static/                     # PWA (owned by FRONTEND): index.html, app.js, styles.css, manifest.webmanifest, sw.js, icons/
├── campaigns/
│   └── fitness.yaml                # demo campaign, exactly per SPEC §2
├── assets/
│   └── fitness/                    # .gitkeep + README noting required files (logo.png, logo_circle.png, outro.mov, font .ttf)
├── deploy/
│   ├── railway.md                  # deploy runbook: services, volume, env vars, Postiz OAuth one-time setup
│   ├── Dockerfile.web
│   ├── Dockerfile.worker           # producer + scheduler image (ffmpeg, yt-dlp installed)
│   └── crontab.md                  # cron expressions per service on Railway
└── tests/
    ├── test_config.py              # fitness.yaml loads + validation failures are loud
    ├── test_ranker.py              # non-overlap + exhaustion logic (pure functions, no LLM)
    ├── test_dedupe.py
    └── test_captions.py            # ASS generation from word timings
```

Python deps (pyproject): fastapi, uvicorn, sqlalchemy>=2, alembic, psycopg2-binary, pydantic>=2, pydantic-settings, pyyaml, apify-client, yt-dlp, httpx, anthropic, faster-whisper, opencv-python-headless, mediapipe, python-multipart, jinja2 (only if needed). ffmpeg is a system dep (documented + in Dockerfiles).

## 2. Ownership map (parallel work)

- **BACKEND-CORE agent** owns: pyproject, core/, migrations/, campaigns/fitness.yaml, producer/discover|dedupe|comments|transcripts|ranker|download, tests for those.
- **BACKEND-RENDER agent** owns: producer/render/* and tests/test_captions.py. Interfaces with core only through the signatures in §4.
- **BACKEND-SCHED agent** owns: scheduler/*, web/api.py, web/auth.py, web/campaigns_io.py, deploy/.
- **FRONTEND agent** owns: web/static/* only. Talks to the API contract in §5. Must not touch Python files.
No agent edits another's files. Integration fixes happen after all agents report.

## 3. Database (SQLAlchemy models in core/models.py — canonical)

Tables exactly per SPEC §8: `campaigns`, `sources`, `transcripts`, `clips`, `comments`, `analytics`.
Key details:
- `sources.source_id` = f"{platform}:{native_id}" (unique). `used_ranges` = JSON list of [start,end] floats.
- `clips.status` enum strings: pending_review|approved|rejected|scheduled|posted.
- All timestamps UTC. Indexes per SPEC §8.

## 4. Internal interfaces (module signatures — do not drift)

```python
# core/config.py
load_campaign(path: str | Path) -> CampaignConfig          # pydantic model mirroring SPEC §2 YAML
load_enabled_campaigns(dir: str = "campaigns") -> list[CampaignConfig]

# core/apify.py
class Apify:
    def run(self, actor_id: str, run_input: dict, *, max_items: int | None = None) -> list[dict]  # error items skipped+logged

# core/llm.py
def rank_moments(transcript: list[dict], rules: str, comment_summary: str | None,
                 clip_len: tuple[int,int], max_clips: int) -> list[dict]
# returns [{"start": float, "end": float, "score": float, "hook": str, "reason": str}]

# producer/ranker.py
def select_clips(candidates: list[dict], used_ranges: list[list[float]],
                 cfg: RankingConfig) -> list[dict]          # pure, tested: non-overlap, min_score, caps, exhaust

# producer/render/__init__.py
def render_clip(cfg: CampaignConfig, source_meta: dict, clip: dict,
                source_video: Path, words: list[dict] | None, workdir: Path) -> RenderResult
# RenderResult: final_path: Path, thumb_path: Path
# words: [{"word": str, "start": float, "end": float}] relative to source; None → render runs faster-whisper on the cut clip

# scheduler/postiz.py
class Postiz:
    def create_post(self, channel: str, caption: str, video_path: Path,
                    schedule_at: datetime | None, draft: bool) -> dict   # returns {"id": ..., "permalink": ...?}
```

## 5. HTTP API contract (web/api.py — FRONTEND builds against this)

Auth: every /api request sends `Authorization: Bearer <WEB_ADMIN_PASSWORD>`. 401 otherwise. The PWA prompts once and stores the token.

```
GET  /api/campaigns                          -> [{name, enabled, sources_summary, schedule, last_run_at, pending_count}]
POST /api/campaigns                          -> create campaign (multipart): fields = JSON blob `config` (wizard form → SPEC §2 shape)
                                                + files: logo, corner_badge (opt), outro (opt), font (opt). Writes YAML + assets. Returns saved config.
PUT  /api/campaigns/{name}                   -> update (same shape)
GET  /api/campaigns/{name}                   -> full config JSON
GET  /api/clips?status=pending_review&campaign=&limit=&offset=
                                             -> [{id, campaign, hook, score, reason, caption, source: {handle, url, title, platform},
                                                 start, end, duration, destination_channels, proposed_slot, created_at,
                                                 video_url: "/api/clips/{id}/video", thumb_url: "/api/clips/{id}/thumb"}]
GET  /api/clips/{id}/video                   -> mp4 (range requests supported)
GET  /api/clips/{id}/thumb                   -> jpeg
POST /api/clips/{id}/approve                 -> {status:"approved"}; body optional {caption: "..."} to override
POST /api/clips/{id}/reject                  -> body {reason?: str} -> {status:"rejected"}
PATCH /api/clips/{id}                        -> body {caption} -> updated clip
GET  /api/analytics?campaign=&weeks=8        -> {channels: [{channel, weekly: [{week_start, views, likes, comments, shares, posts}]}],
                                                 clips: [{clip_id, hook, platform, permalink, views, likes, comments, shares, posted_at}]}
GET  /api/stats                              -> {pending, approved, scheduled, posted, next_run_at}  # for empty states + badge
POST /api/runs/{campaign}                    -> trigger a producer run in background (subprocess); {started: true}
```

Notifications: the service worker polls GET /api/stats; if `pending` increased, show a local notification ("N new clips ready for review"). No push infrastructure required.

## 6. Frontend (web/static/) requirements

Vanilla JS or lightweight (no build step — must be servable as static files by FastAPI). Views:
1. **Queue** (default): card list, inline `<video>` player, hook, score badge, `via @handle`, channel chips, big Approve (primary), Reject + Edit caption (secondary). Optimistic UI. Empty state shows next run time from /api/stats.
2. **Today's batch** filter + per-campaign filter chips.
3. **Analytics**: weekly totals per channel (simple bar/line, no heavy chart lib — inline SVG fine), top clips table.
4. **Campaigns**: list + **New Campaign wizard** (multi-step form: Basics → Sources → Ranking → Look & Feel with asset upload previews → Destinations & Schedule → Review+Create). POSTs multipart to /api/campaigns.
5. PWA: manifest.webmanifest (name, icons, standalone), sw.js (cache static shell, network-first for /api), installable on iOS/Android.

## 7. Entrypoints / deployment

- web: `uvicorn web.api:app --host 0.0.0.0 --port $PORT`
- producer cron: `python -m producer.run --all` (daily, before earliest post time)
- scheduler cron: `python -m scheduler.schedule` (every 15 min) and `python -m scheduler.analytics` (weekly, per campaign pull_day)
- Railway: 3 services + postiz + postgres, shared volume mounted at STORAGE_DIR on web+producer+scheduler. All env vars in Railway only.
