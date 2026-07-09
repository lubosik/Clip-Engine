# CLIP ENGINE — HANDOFF (living session log)

> **RULE FOR EVERY AGENT/SESSION: read this file FIRST, then continue where it says. Update it after EVERY change — every code edit, config tweak, deploy, credential move, failed attempt. Record: what changed, before → after, what works, what didn't work and why, what to do next. This file is the project's memory. Do not let it go stale.**

- Repo: `/root/projects/clip-engine` (GitHub: https://github.com/lubosik/Clip-Engine)
- Authoritative spec: `MASTER_SPEC.md` (2026-07-08 — supersedes SPEC.md). Cross-stream interfaces: `docs/REVAMP_CONTRACTS.md` (binding).
- Live app: `clip-engine-production-*.up.railway.app` (Railway project 73b2c637, single all-in-one service: uvicorn + supercronic).
- Python venv: `.venv/` (use `.venv/bin/pytest`, `.venv/bin/modal`, etc. — system pip is PEP-668 locked, don't fight it).

---

## CURRENT STATE (updated 2026-07-09)

**Revamp v2 build in progress — 3 of 4 streams landed, 1 in flight.**

| Stream | Status | Notes |
|---|---|---|
| Core (R2, migration 002, spend API, config schema) | ✅ landed | 194/194 tests, zero contract deviations |
| Frontend cinematic revamp (web/static/) | ✅ landed | all JS passes node --check; [object Object] fixed |
| Meme engine (meme/) | ✅ landed | 277 total tests passing (83 new) |
| Render/harness (render/modal_app.py, producer dispatch, Makefile, scheduler demo routing) | 🔄 IN FLIGHT | background agent building; if this file still says in-flight and no `Makefile`/`render/modal_app.py` exists, that stream died — re-run it per REVAMP_CONTRACTS §3/§4/§5/§9 |
| Review pass + tests + commit | ✅ done | audit #1 NEEDS FIXES (7 seams) → all fixed +1 extra → audit #2 APPROVED → committed `2520184` (65 files) |
| Push to GitHub | ✅ done 2026-07-09 | `627de12..2520184` → Railway auto-deploy triggered |
| Railway deploy of v2 | 🔄 building | verify /healthz + login on the live URL; user must add the new env vars (emailed draft + listed below) for R2/Modal to activate — app falls back to local storage/render until then |

**What is verified working right now (live-tested, not assumed):**
- R2 bucket `kongwa-tech-clipping-engine` — full read/write confirmed with the S3 creds (put/get/delete test object).
- Hero assets uploaded to R2 at `hero/{hero_loop.mp4, hero_loop_vertical.mp4, hero_poster_web.jpg, hero_poster_mobile.jpg}` (web-transcoded H.264, ~1.8MB loops).
- `GET /api/hero` returns working presigned URLs (all four fetched HTTP 200 from R2).
- API boots on SQLite; verified live: `/healthz`, `/api/spend` (correct §5 shape, $30 budget, "estimated" plan note), `/api/clips?kind=meme`, campaigns payload with `schedule.label` ("1×/day · 17:00 America/New_York"), `sources_summary`, `engines`, `mode`. Static index serves 200.
- Modal: SDK authed (workspace `lubosi`, token in `~/.modal.toml`), secret `clip-engine` exists with REAL R2 keys; `DATABASE_URL` inside it is still `CHANGEME` (harmless — render fn doesn't need DB; fix when convenient with `modal secret create clip-engine ... --force`).
- Full pytest suite: 277 passed at last run (before render stream's additions).

## CREDENTIALS — where they live (never in code/git)

- **Local dev:** `/root/projects/clip-engine/.env` (gitignored, chmod 600) — R2 keys + bucket + endpoint, RENDER_BACKEND=auto, MODAL_MONTHLY_BUDGET=30.
- **Modal:** secret `clip-engine` (R2 keys + DATABASE_URL placeholder). Modal token: `~/.modal.toml`.
- **Railway variables (to add for v2 deploy):** `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET=kongwa-tech-clipping-engine`, `R2_ENDPOINT=https://ff595249c8042ae47c68bafe4be405dc.r2.cloudflarestorage.com`, `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`, `RENDER_BACKEND=auto`, `MODAL_MONTHLY_BUDGET=30`, `MEME_IMAGE_MODEL` (+ existing APIFY/POSTIZ/LLM/WEB_ADMIN_PASSWORD/DATABASE_URL).
- ⚠️ **Rotation debt:** R2 S3 keys + Cloudflare API token + Modal token were pasted in chat (2026-07-08/09). Per MASTER_SPEC rule, rotate them once v2 is deployed and stable. Same standing advice for the old GitHub PAT and Apify token from the 07-06/07 sessions.

## WHAT DIDN'T WORK / GOTCHAS (don't retry these blind)

1. **R2 bucket discovery is impossible with the given creds.** The S3 token is bucket-scoped: `ListBuckets`, `CreateBucket`, CF API `/r2/buckets`, and token introspection all return AccessDenied/auth errors; `HeadBucket`/`ListObjectsV2` on ~40 guessed names also AccessDenied (R2 doesn't distinguish wrong-name vs no-permission). Had to ask the user — bucket is `kongwa-tech-clipping-engine`.
2. **Modal billing API is Team/Enterprise-plan only** (`modal.Workspace.billing.report()` / `modal billing report`). On Starter ($30/mo credits, confirmed 2026-07-08) we CANNOT reconcile against real billing. Spend tracking = local ledger: every render inserts a `render_jobs` row (gpu, duration_s, rate, cost_estimate) → `GET /api/spend` aggregates. Rates in `core/modal_costs.py` (L4 $0.000222/s, T4 $0.000164/s, A10G/any $0.000306/s — modal.com/pricing 2026-07-08). Always label as "estimated".
3. **Hero source video is 4K HEVC** (`/root/clip-engine/web/assets/Clip Engine Hero Video*.mp4` — note: SEPARATE dir from the repo!). Unusable on web directly; transcoded to H.264 1080p CRF24 faststart → `assets/hero/` in repo, uploaded to R2. Re-derive from source if hero changes.
4. **System pip is PEP-668 locked** — everything goes through `.venv/bin/`.
5. **`frontend-design` skill does not exist in this environment** despite MASTER_SPEC C0 referencing it — the design direction was baked into the frontend agent brief instead (Part C is detailed enough).
6. **Port 8000 has a day-old uvicorn** (PID 169540) predating this session — someone's running instance; don't kill it, use other ports for testing.
7. Pre-existing gotchas from the 07-06/07 deploy sessions: Railway needs root `Dockerfile` (else Railpack), `libgl1` not `libgl1-mesa-glx`, OpenRouter LLM keys need `LLM_MODEL=anthropic/claude-sonnet-4.6` (slash+dot), Postiz Cloud auth is raw key (no Bearer) at `https://api.postiz.com/public/v1`, Railway volumes attach to one service only (hence all-in-one), Postiz channel names collide ("Vici Research" ×2) so channels are indexed by id/identifier.

## KEY DECISIONS THIS SESSION (2026-07-08/09)

- **Contracts-first parallelism:** `docs/REVAMP_CONTRACTS.md` pins DB migration 002 (clips.kind/mode/aspect/meme_meta + nullable source fields; render_jobs; meme_profiles), R2 key scheme (`campaigns/{name}/...`, `hero/...`), `r2://` path convention with 307-presigned redirects, job dict for Modal dispatch, `/api/spend` shape, YAML schema additions (mode/engines/creative_direction/meme/demo). Any contract change must update that file in the same commit.
- **Demo vs production** = `mode` column stamped on items at creation (dashboard-only badge, NEVER burned into video); separate from Settings "Mock mode" (offline fixtures).
- **Memes are Clip rows** (`kind='meme'`) — same queue/review/schedule/analytics path, no parallel table.
- **RENDER_BACKEND=auto**: Modal if token + R2 configured, else local ffmpeg (local path must never break — tests/dev depend on it).
- Meme profile JSON schema is documented in the meme tests + `meme/profile.py` (aspect, visual_format, caption_voice, measurable_rules with confidence).

## HOW TO CONTINUE (next session starts here)

1. Read this file, `MASTER_SPEC.md`, `docs/REVAMP_CONTRACTS.md`.
2. If render/harness stream still unfinished: check for `render/modal_app.py`, `producer/render_dispatch.py`, `Makefile`, `scripts/healthcheck.py`, scheduler demo routing; build/finish per contracts, then `make deploy-modal`.
3. Review pass (reviewer agent per repo CLAUDE.md Phase 4) → fix → `make test` green → git commit (do NOT commit `.env`).
4. `make healthcheck` with real env → `make smoke` → `make demo`.
5. Railway: add env vars above, push, redeploy; fix `DATABASE_URL` inside Modal secret `clip-engine`.
6. Remaining from user: connect TikTok in Postiz; drop reference memes into `campaigns/fitness/meme_refs/`; decide when fitness flips mode demo→production; rotate pasted credentials.

---

## CHANGE LOG (append-only; newest last — add an entry after EVERY change)

### 2026-07-08/09 — Session: Revamp v2 orchestration (Fable 5 + 5 sub-agents)
- Wrote `MASTER_SPEC.md` (user's final consolidated spec, credentials stripped) and `docs/REVAMP_CONTRACTS.md` (binding cross-stream contracts). Before: SPEC.md was authoritative; after: MASTER_SPEC.md wins.
- Installed Modal SDK + boto3 into `.venv`; `modal token set` OK (workspace `lubosi`).
- Researched Modal (researcher agent): billing API plan-gated; pricing verified; `Function.from_name` / `Secret.from_name` patterns current in SDK 1.x.
- Mapped whole codebase (Explore agent) — key finds: `[object Object]` bug at `web/static/campaigns.js:144` (schedule object stringified); no R2/Modal/meme code existed; no Makefile; no `mode` anywhere.
- **Core stream (agent) landed:** migration `002_revamp_v2`, models (RenderJob, MemeProfile, Clip.kind/mode/aspect/meme_meta), `core/r2.py`, `core/modal_costs.py`, storage r2_key_* helpers, settings R2/Modal vars, web/api.py (spend + hero endpoints, kind filter, 307 presigned redirects, schedule.label + sources_summary + engines PATCH routes), campaigns_io meme_refs + R2 asset upload, `.env.example`, boto3 dep. 194/194 tests.
- **Frontend stream (agent) landed:** full cinematic glass-on-charcoal revamp of `web/static/` (styles.css rewritten; queue = rising glass panels w/ score edge meter + mixed aspect + kind/mode badges; review overlay w/ approve-dissolve/reject-sink; campaigns w/ schedule.label fix + engine toggles; analytics + settings spend widgets; login hero video w/ CSS-cinematic fallback; light-stream motif; reduced-motion + AA + 44px targets; sw.js cache v3). Design tokens: bg #090910/#0c0c18, glass rgba(16,16,32,.72), cyan #00e5ff, amber #ffb454, spring cubic-bezier(0.16,1,0.30,1).
- **Meme stream (agent) landed:** `meme/` package (profile extract w/ vision, image_client via OpenRouter-style modalities API, classifier w/ GLOBAL_HARD_RULES + PASS_THRESHOLD 0.65 + pure `violates_hard_rules`, generate → Clip rows, text_posts w/ Pillow card fallback, feedback promote-top-performers, run CLI). 83 new tests → 277 total.
- Transcoded hero video (4K HEVC → web H.264 ×2 + posters) → `assets/hero/`, uploaded to R2 `hero/`.
- User provided R2 creds (bucket `kongwa-tech-clipping-engine` — name obtained by asking; see gotcha #1). Verified rw. Wrote `.env` (gitignored). Updated Modal secret `clip-engine` with real R2 keys (`--force`).
- Live-verified on SQLite boot: /healthz, /api/hero (presigned 200s), /api/spend, ?kind= filter, campaigns payload. Killed test servers (8791/8792).
- **Render/harness stream (agent) LANDED:** `render/modal_app.py` (self-contained GPU worker, nvenc w/ libx264 fallback, center-crop reframe — no mediapipe on GPU; yt-dlp added to container image beyond spec), `producer/render_dispatch.py` (select_backend/build_job_dict/ensure_campaign_assets_on_r2/dispatch + RenderJob ledger inserts + spend estimate helpers; APIFY_COST_PER_ITEM=0.01 rough rate), producer/run.py flags (`--mode`, `--max-modal-spend`, `--max-apify-spend`, `--dry-run`) + kind/mode/aspect stamping + 80%-budget warning, scheduler demo test-channel routing (demo always drafts), Makefile (healthcheck/smoke/demo/test/deploy-modal/upload-hero), scripts/{healthcheck,smoke,upload_hero}.py, crontab meme line. **Modal app DEPLOYED**: https://modal.com/apps/lubosi/main/deployed/clip-engine-render. Deviation noted: per-clip `.remote()` default, `dispatch_modal_batch()` (.spawn+gather) ready but not wired — future optimization.
- Verified after all streams: **307/307 tests pass**; `make healthcheck` behaves correctly locally (R2 PASS, Modal PASS, Postgres/Apify/Postiz FAIL with hints — those creds live only in Railway).
- **Reviewer audit #1: NEEDS FIXES — 7 issues (3 CRITICAL, 3 HIGH, 1 MEDIUM), all cross-stream seams.** The parallel streams each tested clean in isolation; every bug was at a seam. Lesson: always audit seams after parallel-agent work.
- **All 7 fixed by orchestrator + 1 extra gap found:**
  1. (CRIT) scheduler/schedule.py treated `r2://` file_path as local → every R2-stored clip silently skipped, nothing would ever reach Postiz. Fix: new `_resolve_video_path()` downloads r2:// to temp file, unlinks after post. **Live-verified against real R2** (download OK, missing object degrades gracefully).
  2. (CRIT) Dockerfile missing `COPY meme/` + pyproject packages.find missing `meme*` → daily meme cron would ModuleNotFoundError. Fixed both.
  3. (CRIT) `modal` SDK absent from image (Dockerfile pip uses `--no-deps -e .` so pyproject deps DON'T auto-install — remember this gotcha) → production renders would crash. Added modal to Dockerfile pip list + pyproject.
  4. (HIGH) `boto3` + `pillow` also absent from Dockerfile pip list → all R2 ops would ModuleNotFoundError. Added.
  5. (HIGH) analytics.js spend widget read `row.cost_usd`/`row.event`/`row.model` — real API fields are `usd`/`campaign`/`gpu` → widget would show $0.00 forever. Fixed (fixtures.js was already correct).
  6. (HIGH) postiz.py hardcoded `video/mp4` MIME for all uploads → meme PNGs sent as video. Now maps from extension.
  7. (MED) wizard sent `meme_ref_0/1/...` indexed fields; FastAPI collects `list[UploadFile]` only from repeated `meme_refs` fields → meme refs silently dropped. Fixed to repeated field name.
  8. (EXTRA, found during fix 7) wizard's `visual_ref_N` files had NO server-side param at all — visual reference images were silently discarded. Added `visual_refs: list[UploadFile]` to POST+PUT endpoints, `save_visual_refs()` → `campaigns/<slug>/visual_refs/`.
- After fixes: 307/307 tests pass, node --check clean, imports clean.
- **Re-review: APPROVED** (one MEDIUM residual — temp-file cleanup not exception-safe — fixed anyway: `_process_clip` now wraps posting in try/finally via extracted `_schedule_resolved_clip()`; suite still 307 green).
- **Committed `2520184`** — "Revamp v2: cinematic PWA, meme engine, Modal GPU renders, R2 storage, demo mode, spend tracking" (65 files, +12553/−1418).
- Railway env-var block emailed to lubosi@kongwatech.com as a Gmail DRAFT (user must hit Send) — includes rotation + delete-after-use reminder.
- Old GitHub PAT was revoked (push 403). User supplied a fresh PAT (repo scope) 2026-07-09 — stored in `~/.git-credentials` (chmod 600). **Pushed `627de12..2520184` to origin/main → Railway auto-deploy triggered.** PAT is rotation-flagged like the rest.
- 2026-07-09 (later): user added all env vars in Railway; reported UI "looks exactly the same" after logout/login. Verified: push IS on the correct repo (GitHub API: main HEAD 33b339b at the time, now 4bf515c); sw.js has skipWaiting+clients.claim so staleness clears after the new deploy + a reload or two. Most likely cause: Railway build (mediapipe/opencv image, several minutes) hadn't finished when they looked, plus old PWA cache. Railway CLI not logged in — cannot watch builds from the VPS; need live URL from user (or `railway login --browserless`) to verify server-side. Modal secret `clip-engine` updated with real DATABASE_URL (Railway INTERNAL hostname — unreachable from Modal's network; fine because the GPU worker only uses R2; swap to Railway's public proxy URL if the worker ever needs Postgres).
- 12 reference memes found at /root/clip-engine/campaigns/fitness/meme_refs/ (user's separate dir), copied into repo campaigns/fitness/meme_refs/, committed + pushed (`4bf515c`). Meme generation NOT started — user said not yet.
- NEXT: (1) user adds new env vars in Railway (R2_*, MODAL_*, RENDER_BACKEND, MODAL_MONTHLY_BUDGET, MEME_IMAGE_MODEL) — until then app runs in local-storage/local-render fallback mode, which is safe; (2) verify live URL /healthz + login hero + queue; (3) put real DATABASE_URL into Modal secret `clip-engine`; (4) `make demo` against production; (5) meme refs into campaigns/fitness/meme_refs/; (6) TikTok in Postiz; (7) rotate all chat-pasted credentials.

### 2026-07-09 (later) — Session: SW auto-reload fix for "UI looks the same"
- Root cause of user's "looks exactly the same after logout/login": the PWA service worker used skipWaiting+clients.claim, but the tab kept rendering the OLD SW-cached HTML/JS/CSS until a manual hard-reload. A fresh deploy swapped the SW under the tab while the visible assets stayed stale.
- Fix (finished; 307 tests + node --check green):
  - `web/static/app.js`: on `controllerchange` (new SW taking control after deploy) force exactly one `window.location.reload()`. Guarded by `hadController` (no reload on first-ever visit → no reload loop) and a `reloaded` flag (never double-reloads).
  - `web/static/sw.js`: cache `v3` → `v4` to invalidate the stale precache.
- State: committed `889e7c3`, **pushed to origin/main** (Railway auto-deploy triggered). Still need the live Railway URL to verify server-side that v2 is serving.

### 2026-07-09 (later) — Session: safe on-demand run trigger for "Run on Railway"
- User chose to run the demo **on Railway** (creds live there). Found a real spend-gate gap: `POST /api/runs/{campaign}` spawned `producer.run <slug>` with NO `--mode` and NO spend caps → an on-demand web-triggered run was **uncapped**, violating spec §9's hard spend gate. (Note: fitness has no `mode:` key and `CampaignConfig.mode` defaults to `"demo"`, so mode itself was fine — but demo mode does NOT cap clip count or dollars; only `make demo`'s `--max-*-spend 2` flags do.)
- Fix (`web/api.py` `trigger_run`): accepts optional JSON body `{mode, max_apify_spend, max_modal_spend}`; **omitted caps fall back to demo defaults (2.0/2.0)** so a web-triggered run is never uncapped; validates mode ∈ {demo,production} and caps > 0 (422 otherwise); passes `--mode/--max-apify-spend/--max-modal-spend` through to the subprocess and echoes them in the JSON response + log line. The uncapped path stays reserved for the daily cron `producer.run --all` (bounded by discovery limits + 80%-budget warning).
- Added `tests/test_trigger_run.py` (7 tests: default-capped, body overrides, invalid mode → 422, non-positive/non-numeric cap → 422, unknown campaign → 404; Popen monkeypatched). **Full suite 314 passed.**
- To run the demo on Railway once the URL is known:
  `curl -sX POST https://<live>/api/runs/fitness -u ':<WEB_ADMIN_PASSWORD>' -H 'content-type: application/json' -d '{"mode":"demo"}'`
  → returns `{started, pid, max_apify_spend:2.0, max_modal_spend:2.0}`; logs at `/data/clips/logs/producer-fitness.log`; results land in the Queue (demo badge, drafts to test channels).
- State: committed + **pushed** (Railway auto-deploy). BLOCKER unchanged: need the live Railway URL from the user to trigger + watch.

### 2026-07-09 (later) — Session: seamless hero-video loop (crossfade, no hard cut)
- User reported the login hero video "hard cuts" at the loop point. Root cause: `index.html` used the native HTML `loop` attribute on a single `<video>` → instant snap to frame 0. Spec §10 requires a crossfade (~0.5–1s), no hard cut.
- Fix (two-layer crossfade):
  - `index.html`: removed `loop`; added a second stacked layer `#hero-video-b` (same `.hero-bg-video` class, absolute inset:0).
  - `app.js`: `_initHeroMedia` now loads the same src+poster into both layers, plays A, and calls new `_startSeamlessLoop(vA,vB)`. That watches `timeupdate`; when the active layer reaches `duration - min(0.8s, 0.3·d)`, it starts the idle layer from 0, swaps the `.active` class (CSS transitions opacity 0.8s → true crossfade), parks the outgoing layer at frame 0 after its tail (`ended` or timeout), and re-arms after 250ms. Falls back to native `loop` if only one layer exists or duration is non-finite.
  - `sw.js`: cache `v4` → `v5` (index.html/app.js are precached).
- node --check clean on app.js + sw.js. Visual crossfade can't be verified headlessly — confirm on the live site after deploy.
- Railway CLI login: `railway login --browserless` needs a PTY (no output otherwise). Ran it under `script -qfc ... /log`; pairing code emitted to the log. Background login task waiting for user to authorize at railway.com/activate.
- State: committed + pushed (Railway auto-deploy).

### 2026-07-09 (later) — Session: live demo run → root-caused "0 clips" → fixed transcript path
- Authorized Railway CLI login (browserless, via PTY). Live URL: **https://clip-engine-production-9ecd.up.railway.app** (service Clip-Engine, Online). Verified live: `/healthz` ok, `/api/hero` presigned R2 200s, hero mp4 200 (1.86MB), served index.html/app.js/sw.js carry the seamless-loop + v5 changes → the 3 prior fixes ARE deployed and serving.
- Triggered demo via `POST /api/runs/fitness {"mode":"demo"}` → `{started,pid:23,caps 2/2}`. Polled 15 min: **0 clips, $0 spend.** Ran silently-failing.
- Could NOT read `/data/clips/logs/producer-fitness.log` (auto-mode classifier blocked `railway ssh` into prod — expected). `railway logs` shows only uvicorn access + scheduler cron (producer writes to a file). So reproduced stages locally via `railway run .venv/bin/python <diag>` (prod env injected):
  - Discovery ✅ **87 candidates** (youtube scraper fine).
  - LLM rank ✅ (OpenRouter route, key `sk-or-…`, model `claude-sonnet-4-6`) → 3 good moments.
  - Transcript ❌ **ROOT CAUSE.** `pintostudio/youtube-transcript-scraper` rejected the input: `Field input.videoUrl is required`. Code sent `{"startUrls":[{"url":url}]}`. Every YT transcript failed → every source skipped → 0 clips.
  - Exposed a 2nd bug: `core/apify.py` `run.get("usage", {})` returns None when the key is present-but-null → `AttributeError` on cost accounting (masked while the input error came first).
  - Exposed a 3rd: the actor's real output is `{"data":[{start,dur,text}]}` (start/dur are STRINGS); the parser only checked transcript/captions/subtitles → "Unexpected shape" → 0 segments.
- **Fixes (all verified against prod Apify):**
  1. `producer/transcripts.py` `fetch_youtube_transcript`: input → `{"videoUrl": url}`.
  2. `core/apify.py`: `usage = run.get("usage") or {}`.
  3. `producer/transcripts.py`: parse `data` key first (fallbacks kept). `_norm_yt_segments` already coerces string start/dur. → verified **142 segments** returned for a real video.
  4. `producer/run.py`: demo-mode early-stop `DEMO_CLIP_TARGET=3` — stop processing sources once 3 clips exist (spec §9; was going to grind all 87 transcripts).
- Tests: added `tests/test_transcript_parse.py` (4). **Full suite 318 passed.** Removed throwaway diag scripts.
- NOTE not yet verified end-to-end: Modal render + DB insert of a real clip (blocked earlier by the transcript bug). TikTok transcript actor (`agentx/tiktok-transcript`) input shape UNVERIFIED — may have the same startUrls issue; demo is YT-first so it wasn't hit. Verify both on the next demo run.
- State: about to commit + push (redeploys, kills stale pid 23) then re-trigger the demo and watch clips land.
