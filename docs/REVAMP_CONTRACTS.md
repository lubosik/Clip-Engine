# Revamp v2 — Shared Contracts (binding for all work streams)

Read MASTER_SPEC.md first. This file pins the cross-stream interfaces so parallel work can't drift. If a stream needs to change a contract here, it must update this file in the same commit.

## 0. DB migration `003_review_gate` (owned by review-gate stream, 2026-07-09)

**`clips` table — additions (migration 003):**
- `gate_status` VARCHAR(16) NOT NULL DEFAULT `'pending'` — `'pending' | 'ready' | 'didnt_pass' | 'overridden'`
  - `pending`: gate hasn't run yet, or had a transport error (infra unavailable)
  - `ready`: passed both Phase 1 (design) and Phase 2 (content); available for human review
  - `didnt_pass`: failed one or more gate checks; shown in "Didn't pass review" queue section
  - `overridden`: operator manually moved to the review queue despite failing the gate
- `gate_reasons` JSONB NULL — list of `{phase: str, check: str, pass: bool, reason: str}` from `run_gate()`
- `formula_score` FLOAT NULL — 0.0–1.0 average of the §6c 10-question rubric; NULL until Phase 2 runs

**Index added:** `ix_clips_gate_status` on `clips(gate_status)`.

**Downgrade:** drops the three columns permanently (no data migration).

**Gate module:** `producer/review_gate.py` — `run_gate(clip_row, video_path_or_r2, transcript_segments, campaign_cfg, session) -> GateResult`.

**Override endpoint:** `POST /api/clips/{id}/override-gate` (auth'd) — sets `gate_status='overridden'`.

**Style refs for vision prompt:** `campaigns/{campaign}/style_refs/*.jpg` — copied into the campaign directory by the review-gate stream; passed as reference images in the Phase 1 vision call.

**Discovery guard keys (config):**
- `sources.youtube.exclude_channels: []` — channel name substrings to skip (case-insensitive)
- `sources.exclude_keywords: []` — title keywords to skip across all platforms (case-insensitive)

## 1. DB migration `002_revamp_v2` (single migration, owned by Core stream)

**`clips` table — additions:**
- `kind` VARCHAR(8) NOT NULL DEFAULT `'clip'` — `'clip' | 'meme'`
- `mode` VARCHAR(12) NOT NULL DEFAULT `'production'` — `'demo' | 'production'` (stamped at creation from campaign mode)
- `aspect` VARCHAR(8) NOT NULL DEFAULT `'9:16'` — `'9:16' | '1:1' | '4:5'`
- `meme_meta` JSONB NULL — for memes: `{concept, classifier_scores, profile_version}`
- `source_id`, `start`, `end` become NULLABLE (memes have no source video)

**New table `render_jobs`** (Modal spend ledger):
- `id` PK, `clip_id` FK clips NULL, `campaign` VARCHAR, `backend` VARCHAR (`'modal'|'local'`),
  `gpu` VARCHAR NULL, `duration_s` FLOAT, `rate_per_s` FLOAT, `cost_estimate` FLOAT,
  `status` VARCHAR (`'ok'|'error'`), `error` TEXT NULL, `created_at` TIMESTAMP default now
- Every Modal (and local-fallback) render inserts one row. This powers /api/spend.

**New table `meme_profiles`:**
- `id` PK, `campaign` VARCHAR, `version` INT, `profile` JSONB, `created_at` TIMESTAMP
- unique (campaign, version); latest version is active.

## 2. Campaign YAML schema additions (core/config.py)

```yaml
mode: demo            # demo | production — default for runs/items; default 'demo' for new campaigns
engines:
  clips: true
  memes: false
creative_direction: ""   # free-text brief, fed to ranking + render guidance
meme:                  # required only when engines.memes
  refs_dir: campaigns/<name>/meme_refs   # or assets/<name>/meme_refs
  image_model: ""      # env MEME_IMAGE_MODEL fallback
  hard_rules: []       # merged with global: no em-dashes, no medical claims, no unsafe dieting
demo:
  test_channels: []    # Postiz channel ids used when posting demo items
hook:
  show_seconds: [0, 8] # already exists in template.hook — builder exposes it
```
All additions are optional with defaults → existing fitness.yaml stays valid.

## 3. Storage: R2-first with local fallback (core/storage.py + new core/r2.py)

- If `R2_BUCKET` + `R2_ENDPOINT` + keys are set → R2 mode; else current local mode (dev/tests unchanged).
- `core/r2.py`: boto3 S3 client (endpoint from `R2_ENDPOINT`), `upload_file(local, key)`, `download_file(key, local)`, `presign(key, expires=3600)`, `put_bytes`, `exists`, `healthcheck()`.
- **Key scheme:** `campaigns/{campaign}/clips/{clip_id}.mp4`, `campaigns/{campaign}/thumbs/{clip_id}.jpg`, `campaigns/{campaign}/memes/{clip_id}.png`, `campaigns/{campaign}/assets/{filename}`, `campaigns/{campaign}/raw/{source_id}.mp4`, `hero/{filename}`.
- `Clip.file_path` / `thumb_path` store `r2://{key}` when in R2 mode; local absolute paths otherwise.
- Web `/api/clips/{id}/video` and `/thumb`: if path starts `r2://` → 307 redirect to presigned URL; else stream local file (current behavior). Browser never sees keys.
- After successful R2 upload, local temp files are deleted.

## 4. Render dispatch (producer → Modal)

- New `render/modal_app.py` (Modal app `clip-engine-render`, fn `render_clip`, gpu `["l4","t4","any"]`, timeout 1800, secret `clip-engine` holding R2 keys + DATABASE_URL). Deployed with `modal deploy render/modal_app.py`.
- Producer: if `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` set (or ~/.modal.toml) and `RENDER_BACKEND != 'local'` → dispatch job dict to Modal via `modal.Function.from_name("clip-engine-render", "render_clip").remote(job)`; else current local ffmpeg path.
- **Job dict:** `{clip_id, campaign, mode, source: {r2_raw_key | url}, start, end, template: <full template cfg>, caption/hook data, words, asset_keys: {font, watermark, badge, outro}, output: {video_key, thumb_key}}`
- **Return:** `{status, video_key, thumb_key, gpu, duration_s, error?}` → producer inserts `render_jobs` row and the Clip row.
- Batch: `.map()` / `.spawn()` over a run's jobs.
- Assets must be uploaded to R2 under `campaigns/{campaign}/assets/` before dispatch (producer ensures on first run; builder does it at save).

## 5. Spend API + guards

- `GET /api/spend?months=1` → `{estimated: true, budget_usd, month_to_date_usd, remaining_credit_usd, by_campaign: [{campaign, usd, jobs}], recent: [{clip_id, campaign, gpu, duration_s, usd, created_at}], plan_note}`
  Computed from `render_jobs`. `budget_usd` = env `MODAL_MONTHLY_BUDGET` (default 30).
- GPU rate table lives in `core/modal_costs.py`: `{"l4": 0.000222, "t4": 0.000164, "a10g": 0.000306, "any": 0.000306}` USD/s + CPU/mem components optional. Verified 2026-07-08 from modal.com/pricing; labelled estimates.
- Producer flag `--max-modal-spend X`: before dispatch, estimate `n_clips × avg_recent_cost (fallback $0.03)`; abort with clear message if it exceeds X. Warn at 80% of `MODAL_MONTHLY_BUDGET`.
- Apify guard `--max-apify-spend` analogous (pre-existing ask, Part I).

## 6. Web API additions/changes (web/api.py)

- Clip payloads gain: `kind`, `mode`, `aspect`.
- `GET /api/clips` gains `?kind=clip|meme` filter.
- Campaign list payload: `schedule` becomes a **formatted object** `{posts_per_day, times: [...], timezone, label: "1×/day · 17:00 ET"}` (frontend renders `label` — fixes [object Object]); add `mode`, `engines: {clips, memes}`, `sources_summary: [{platform, count, label}]`.
- `PATCH /api/campaigns/{name}/engines` body `{clips?: bool, memes?: bool}` → updates YAML + snapshot.
- `PATCH /api/campaigns/{name}/mode` body `{mode}` → updates YAML default mode.
- `GET /api/spend` (above). All auth'd.
- Campaign create/update (`POST/PUT /api/campaigns`) accepts the new fields (mode, engines, creative_direction, meme refs upload, demo.test_channels, hook.show_seconds, watermark opacity/placement) and uploads assets to R2 in R2 mode.
- `GET /api/hero` (no auth): returns `{video, video_vertical, poster, poster_mobile}` presigned/public URLs for hero assets if present in R2 (`hero/…`), else nulls — login page falls back to CSS-gradient cinematic backdrop.

## 7. Meme engine (meme/)

- `meme/profile.py`: extract `meme_style_profile.json` from refs via LLM vision → store in `meme_profiles` (versioned).
- `meme/generate.py`: concept+caption from profile → image via `MEME_IMAGE_MODEL` (OpenRouter image-capable model; refs passed as style references) → upload to R2 → insert Clip row (`kind='meme'`, `aspect` from profile, `status='pending_review'`, mode stamped).
- `meme/classifier.py`: LLM-judge scores {on_format, on_voice, on_brand, legibility, compliance} 0-1; any compliance fail or avg < threshold → status `rejected` with reason, never enters review.
- `meme/feedback.py`: weekly — top performers (from analytics) promoted to refs; re-extract profile as version+1.
- Entrypoint `python -m meme.run <campaign>`; only runs when `engines.memes`.

## 8. Env vars (update .env.example)

`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT`,
`MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `RENDER_BACKEND` (auto|modal|local), `MODAL_MONTHLY_BUDGET` (30),
`MEME_IMAGE_MODEL`, plus existing. R2 endpoint: `https://ff595249c8042ae47c68bafe4be405dc.r2.cloudflarestorage.com` (account `ff595249c8042ae47c68bafe4be405dc`) — values via env only, never committed.

## 9. Makefile (new, repo root)

Targets: `healthcheck` (Postgres, R2 rw, Apify, Postiz, Modal token+deployed fn — PASS/FAIL table),
`smoke` (one known YouTube URL → rendered clip in R2 → Queue), `demo` (full pipeline in demo mode, spend-capped),
`test` (pytest), `deploy-modal` (`modal deploy render/modal_app.py`).

## 10. Frontend contract notes

- Frontend consumes only the shapes above; mock fixtures in `web/static/fixtures.js` must be updated to match (add kind/mode/aspect, spend payload, formatted schedule).
- Demo badge: amber glass pill; production: cyan/green pill. Shown on queue panels, review view, campaign cards, analytics rows. Never rendered into video (server-side concern; no video change).
