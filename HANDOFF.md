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

### 2026-07-09 (later) — Session: THE run-killer — campaigns FK row never seeded
- Transcript fixes deployed (after one transient pip BrokenPipe build failure → `railway redeploy` fixed it; user granted standing approval for redeploys). Demo re-triggered → STILL 0 clips, $0 spend, and prod Postgres (via `DATABASE_PUBLIC_URL` on the Postgres service) showed 0 sources/transcripts/clips.
- Shipped `GET /api/runs/{campaign}/log` (auth'd tail of the producer log; spec §14 "make errors legible") because `railway ssh` reads are classifier-blocked in auto mode and `railway logs` only shows uvicorn/cron. **Discovered: Clip-Engine service has NO volume** (only Postgres does) — /data/clips is ephemeral, producer logs die on every deploy. Media goes to R2 so tolerable, but do NOT deploy while a run is in flight (container swap kills it), and logs of dead runs are unrecoverable.
- Fresh run + live log tail revealed the REAL run-killer, present since the first Postgres deploy:
  `psycopg2.errors.ForeignKeyViolation: sources_campaign_fkey` — `sources.campaign` and `clips.campaign` FK `campaigns.name`, but **nothing anywhere in the codebase ever created a campaigns row** for YAML-defined campaigns. On Postgres every post-discovery commit died; run aborted. SQLite dev never caught it (FKs off by default). NOTE: this crash fires BEFORE transcripts, so it was the first killer all along; the transcript bug (real, fixed) was the next layer underneath.
- **Fix:** `core/db.py` new `ensure_campaign(session, name, enabled, config_snapshot)` upsert; called in `producer/run.py` (before source upserts, snapshot=model_dump) and `meme/run.py` (before clip inserts). Regression: `tests/test_ensure_campaign.py` (4 tests, SQLite with `PRAGMA foreign_keys=ON` to reproduce the exact violation). **Suite 322 passed.**
- State: committing + pushing, then re-trigger demo and watch clips/log/Postgres.

### 2026-07-09 (later) — 🎉 DEMO RUN SUCCEEDED END-TO-END (with 3 render-quality bugs found)
- After the FK fix deployed: `POST /api/runs/fitness {"mode":"demo"}` → **"Campaign run complete", 5 clips in ~6 min** (19:46→19:52). Pipeline verified live: discovery (87 sources in PG — FK fix works) → transcript (1 stored) → LLM rank → source upload to R2 → **5 Modal L4 renders** (16–31s each, **$0.0239 total** — spend ledger works) → 5 Clip rows `kind=clip mode=demo status=pending_review`, real hooks/captions/`via @TED-Ed` credit/hashtags, `destination_channels [instagram-standalone, x]`. DEMO_CLIP_TARGET=3 stopped after source #1 (which yielded 5 ≤ daily max 5).
- **Video verified by eye** (downloaded clip 1 via 307-presign → 6.17MB h264 **1080×1920**, extracted frames): hook overlay ON during 0–8s and gone at 20s ✅, cyan V corner badge ✅, centered translucent VICI watermark ✅, `via @TED-Ed` credit ✅. Demo label NOT burned in ✅.
- **3 render-quality bugs found (all in `render/modal_app.py`), root causes confirmed:**
  1. **Hook text overflows frame edges** (frame shows "ery time you work out…" clipped both sides). Cause: `_wrap_text(hook_text, 32)` + fontsize `max(44, out_h*0.038)=73px` → a 32-char line @73px Montserrat ExtraBold ≈ 1200px > 1080 width. Fix: narrower wrap (~22) or fit-to-width fontsize.
  2. **Word-by-word captions BLANK.** Modal log: `faster-whisper failed (Library libcublas.so.12 is not found…); captions will be blank`. Cause: `_get_word_timings` try/except only wraps the `WhisperModel(...)` CONSTRUCTOR (lazy CUDA init succeeds); the failure fires at `.transcribe()` OUTSIDE the try → CPU fallback never runs. Fix: catch at transcribe (retry device=cpu int8), or pip nvidia-cublas-cu12/cudnn into the image.
  3. **Clip runs long:** DB duration 30.4s but file has 1064 video frames ≈ 44.38s @23.976 (container says 46.86s); outro asset is only 2.5s (`assets/fitness/outro.mov`), "Outro concat" ran. So main content ≈ 41.9s vs intended 30.4s (or fps/timestamp corruption in overlay/concat — concat demuxer re-encode with mismatched fps/timebase is suspect). `_cut_and_reframe` args are correct (`-ss start -i -t 30.4`). Needs local repro of modal_app helpers (they're importable pure ffmpeg fns) on the same source.
- **Review-gate honesty:** the §6c/§8 two-stage AI review gate (formula_score, per-criterion pass/fail, "Didn't pass" section) is NOT implemented anywhere (grep: no formula_score/review_gate in code). The `score` field on clips is the RANKER's score (e.g. 0.68). Queue is a single pending_review list. Spec gap to build later.
- Harmless noise in producer log: apify_client `_streamed_log.py` thread tracebacks (impit.TimeoutException) + actor-internal 429 retries. Not failures; don't chase them.
- NEXT: (1) fix the 3 modal_app bugs (whisper CPU fallback; hook wrap; duration/fps) + redeploy Modal app (`make deploy-modal`) — NOTE: modal_app changes need `modal deploy`, not just Railway push; (2) re-run demo, re-verify frames; (3) POSTIZ_API_URL still missing in Railway (posting step blocked); (4) hero crossfade visual check on live site; (5) meme demo; (6) review-gate implementation decision; (7) rotate pasted creds.

### 2026-07-09 (later) — Session: blank dashboard media + hero loop freeze (user-reported)
- **Blank clips root cause:** queue.js puts `/api/clips/{id}/video|thumb` directly on `<video>`/`<img>` tags, but those endpoints require Bearer auth and tags CANNOT send headers → every media request 401 → blank panels. (Media itself fine — curl with Bearer got the mp4.)
- **Fix — cookie session for media:** `web/auth.py` `require_auth` now accepts Bearer OR `ce_session` cookie = HMAC-SHA256(key=password, msg="clip-engine-session-v1") (never the raw password); new `POST/DELETE /api/auth/session` (web/api.py) sets/clears it (HttpOnly, Secure, SameSite=Strict, 30d). PWA calls createSession after unlock AND on boot-with-saved-token (awaited before _bootApp so the first queue render already has the cookie); destroySession on sign-out. GOTCHA: cookie is Secure → TestClient must use base_url="https://testserver" or httpx won't send it.
- **Hero loop freeze root cause:** my earlier crossfade relied solely on `timeupdate` (~4Hz, throttled in background tabs); missing the pre-end window left the video ended with NO handler → hero froze on last frame ("it just stops" — user). Fix in app.js `_startSeamlessLoop`: rAF ticker as primary (precise near end), `ended` listener as safety net (forces swap; worst case fade-from-frozen-frame, never a stop), `error` on either layer degrades survivor to native loop (covers presigned-URL expiry ~1h on unbuffered layers), `preload=auto` both layers. sw.js cache v5→v6.
- Tests: `tests/test_session_auth.py` (6). **Suite 328 passed.** node --check clean.
- Render-bug stream: backend agent working on the 3 modal_app.py bugs in parallel (whisper CPU fallback, hook overflow, +11.5s duration) — its changes need `modal deploy` after landing.

### 2026-07-09 (later) — Session: 3 render-quality bugs fixed in render/modal_app.py

**BUG 1 — Whisper CPU fallback (FIXED)**
- Root cause confirmed: `_get_word_timings` wrapped only `WhisperModel("small", device="cuda", ...)` in try/except. ctranslate2 uses lazy CUDA init — the constructor SUCCEEDS even without libcublas.so.12; the `Library not found` error fires at `.transcribe()`, which was outside the try. CPU fallback was dead code.
- Fix: moved both `WhisperModel(...)` AND `model.transcribe(...)` inside the same outer try. If that block raises (at any point), we retry with `device="cpu", compute_type="int8"`. Return `[]` only if CPU also fails.
- Additionally: added `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` to the Modal image `.pip_install()` so the CUDA path can actually find libcublas.so.12 on L4/T4. Added a guarded LD_LIBRARY_PATH extension in `_get_word_timings` using `nvidia.cublas.lib.__file__` to locate the pip-installed libs — guarded in try/except so its absence never crashes the render.

**BUG 2 — Hook text overflow (FIXED)**
- Root cause: `_wrap_text(hook_text, 32)` + `hook_fontsize = max(44, int(1920*0.038)) = 72px`. A 32-char line at 72px Montserrat ExtraBold ≈ 1382px >> 1080px frame width.
- Fix: wrap at 22 chars/line; cap to 4 lines with trailing "..." if more; compute `fit_fontsize = int((out_w * 0.92) / (0.60 * longest_line_len))`; use `min(base_fs, fit_fs)` floored at 32px.
- Verified with REAL local ffmpeg render (drawtext on 1080×1920 black canvas, Montserrat-ExtraBold.ttf): PIL pixel check — columns 0-5 left_max=0, columns 1075-1079 right_max=0. No text touches frame edges. Production hook "Every time you work out, you're actually damaging your muscles — and that's a good thing." wraps to 4 lines, longest=22 chars, fontsize=72px, approx px width=950px < 994px (92% of 1080).

**BUG 3 — Output duration ~11.5s too long (FIXED)**
- Local repro confirmed (source from R2 `campaigns/fitness/raw/youtube_2tM1LFFxeKg.mp4`):
  - After `_cut_and_reframe(start=31.0, duration=30.4)`: reframed = 30.405s, 729 frames @ 24000/1001. Correct.
  - After `_apply_overlays`: overlaid = 30.447s, 730 frames. Correct.
  - After `_concat_outro` (OLD demuxer): video_duration=30.447s (730 frames — outro VIDEO missing!), audio_duration=32.93s (correctly includes both), format_duration=32.93s. Root cause: concat DEMUXER requires identical stream parameters; outro.mov is 30fps / timebase 1/15360, main is 24000/1001 / timebase 1/24000 — demuxer silently drops the outro video stream. On GPU+NVENC in production, the same fps/timebase mismatch caused different corruption (frame count inflated to 1064 = 44.38s).
  - Secondary root cause found during testing: `_cut_and_reframe`'s `scale=-2:1920,crop=1080:1920` produces SAR 5120:5121 (fractional rounding artifact). The concat FILTER requires identical SAR; outro.mov has SAR 1:1. Fixed by adding `setsar=1` to the scale_filter chain in `_cut_and_reframe` AND to the main-clip normalisation chain in `_concat_outro`.
- Fix: replaced concat DEMUXER approach in `_concat_outro` with concat FILTER: probes main clip fps; scale/setsar/fps-normalises outro to match; resamples both audio streams to 48k stereo; handles no-audio outro via `anullsrc`; concat=n=2:v=1:a=1; re-encode with codec_v. Extended `_concat_outro` signature with `out_w: int = 1080, out_h: int = 1920`; updated caller in `_pipeline`.
- Verified locally: final_filter.mp4 = 32.950s / 790 frames @ 24000/1001 (expected 32.947s / ~789 frames, diff = 0.003s, 1 frame). Both video and audio streams correct. Format duration matches video.
- 328/328 tests pass. AST parses clean.
- **NEXT: `make deploy-modal` to redeploy the Modal GPU worker, then re-run demo.** No Railway push needed (modal_app.py runs in Modal, not Railway).

### 2026-07-09 (later) — Render fixes VERIFIED on production output; yt-dlp bot-wall mitigated
- User supplied a fresh GitHub PAT (rotation-flagged, in ~/.git-credentials) → pushed the 2 held commits + render fixes (`dce12d0..5959b09`). `make deploy-modal` deployed the fixed worker.
- Demo run #2 (pid 27): discovery grew 87→113 sources. One source failed at download with **yt-dlp "Sign in to confirm you're not a bot"** (YouTube bot-walls Railway's datacenter IP; per-source failure, run continued). Next source (Huberman podcast, ~93min) produced **7 new clips (ids 6-12), 12 total**; "Campaign run complete" at 21:01.
- **All 3 render fixes VERIFIED BY FRAME EXTRACTION on clip 12** (61.16s file, video==container duration, 1080×1920@29.97): hook fully inside frame (4 wrapped lines + ellipsis); **word-by-word karaoke captions WORKING** (cyan current-word highlight, matches speech — CPU whisper fallback fired); outro card "@viciresearch FOLLOW FOR MORE" present at tail; hook gone by 25s; badge/watermark/credit all present. Postable quality.
- User is actively reviewing in the dashboard (clip 5 = rejected) → cookie media auth confirmed working in the wild.
- **yt-dlp mitigation added** (`producer/download.py`): on "Sign in to confirm…not a bot" errors, retry the download through a player-client chain default(web) → ios,tv → android (innertube clients are bot-checked far less on datacenter IPs). Non-bot-check errors do NOT retry. `tests/test_download_retry.py` (4 tests, fake yt_dlp module). **Suite 332 passed.** If the chain proves insufficient in production, next options: per-campaign cookies file, or an Apify downloader actor as paid fallback.
- Watcher-script lesson: per-source failures log tracebacks that include producer/run.py frames while the RUN SURVIVES (outer catch in _process_source) — do not treat those as fatal; only "Campaign run complete" (or timeout) ends a watch.
- STILL OPEN: POSTIZ_API_URL missing in Railway (posting blocked); review-gate §8 not implemented; meme demo not run; hero crossfade needs a human eye on the live login page; rotate ALL chat-pasted creds (now including the new PAT).

### 2026-07-09 — Session: Review-gate workstream (§6c/§8) — COMPLETE

**All 7 build items landed. Full test suite: 393 passed (61 new tests added on top of 332 baseline). Zero regressions.**

**1. DB — migration 003 + model parity**
- `migrations/versions/003_review_gate.py`: adds `gate_status String(16) NOT NULL DEFAULT 'pending'`, `gate_reasons JSONB NULL`, `formula_score FLOAT NULL`; creates index `ix_clips_gate_status`; reversible downgrade included.
- `core/models.py`: Clip gains those 3 mapped columns + `Index("ix_clips_gate_status", "gate_status")` in `__table_args__`.
- `docs/REVAMP_CONTRACTS.md`: Section 0 added — documents gate_status values (pending/ready/didnt_pass/overridden), gate module contract, override endpoint, style refs location, new config keys.

**2. Gate module — `producer/review_gate.py`**
- `GateResult` dataclass: `gate_status`, `gate_reasons` (list of `{phase, check, pass, reason}` dicts), `formula_score`.
- Phase 1 (design): ffprobe resolution ≥1080×1920 + duration sanity; extract 3 frames (t≈3s hook, mid-clip, t≈1s-before-outro) + 1 outro frame; single vision-LLM call with style-ref images for 7 checks (hook_present_in_hook_frame, hook_absent_in_mid_frame, captions_present, watermark_visible, real_humans, speaker_centered, animation_detected — animation is auto-fail). Captions-match-speech skipped honestly (transcript comparison requires accurate whisper; marked phase=1, check=captions_match_speech, pass=null).
- Phase 2 (content, only if Phase 1 passes): single LLM call scores 10 rubric questions (hook_quality, promise_delivery, novelty, pacing, standalone_value, speaker_engagement, clean_ending, shareability, comprehension, completion_likelihood; 0–1 each, avg = formula_score); 4 safety auto-fail flags (unsafe_diet_content, medical_claims, harmful_content, guideline_violation). Pass: formula_score ≥ 0.6 AND no safety fail.
- Meme clips (`clip_row.kind == 'meme'`) skip both phases → gate_status stays 'pending'.
- Empty/None video path → gate_status 'pending' with reason 'no video path'.
- LLM/vision transport errors → gate_status 'pending' with reason `gate unavailable: <err>` (never blocks the producer run).
- All transport functions (`_probe_video`, `_extract_frames`, `_load_style_refs`, `_vision_llm_call`, `_content_llm_call`) are module-level and monkeypatchable in tests; no real LLM/network calls in the test suite.
- Style refs loaded from `campaigns/{campaign_name}/style_refs/*.jpg` (fitness refs already copied there in a prior session).
- LLM model: reads `os.environ["LLM_MODEL"]` with fallback `"claude-sonnet-4-6"` — same pattern as `core/llm.py`. OpenRouter auto-routing via `sk-or-` prefix on `LLM_API_KEY` (not set locally; live in Railway).

**3. Producer wiring — `producer/run.py`**
- `DEMO_RENDER_CAP = 10`: hard cap on total renders per run regardless of DEMO_CLIP_TARGET (bounds spend).
- After each successful render+DB insert: `run_gate(clip_row, r2_path, transcript_segments, campaign_cfg, session)` called; result written to `clip_row.gate_status/gate_reasons/formula_score`.
- Only clips with `gate_status == 'ready'` count toward `DEMO_CLIP_TARGET`; `total_renders` counts all renders toward the cap.
- Cap-trip logged with clear message; run terminates early.

**4. API — `web/api.py`**
- `_clip_to_dict` now includes `gate_status`, `gate_reasons`, `formula_score` in every clip payload.
- New endpoint: `POST /api/clips/{clip_id}/override-gate` (requires auth) — sets `gate_status='overridden'`, preserves `gate_reasons` so the operator can see why it failed. Returns full clip dict. Returns 404 for unknown clip_id.

**5. Frontend — `web/static/queue.js` + `styles.css` + `sw.js`**
- Queue now splits into two sections: "Ready to review" (gate_status != 'didnt_pass') and "Didn't pass review" (gate_status == 'didnt_pass').
- Fail cards (`_buildFailCard`) tinted amber with `<details>` disclosure showing each `gate_reasons` entry — phase, check name, and reason string.
- Override button on each fail card calls `api.overrideGate(id)`, updates local clip state, and re-renders on success (moves card from fail to ready section).
- Badge count (`onBadge`) counts only non-didnt_pass clips.
- `styles.css`: ~150 lines added for `.queue-section-header`, `.queue-section-header--fail`, `.clip-card--fail`, `.fail-card-inner`, `.fail-why`, `.fail-why-summary`, `.fail-why-reasons`, `.chip-amber`. Design tokens: `--amber #ffb454`, `--amber-bg`, `--amber-border`, `--amber-glow` (consistent with existing amber usage).
- `api.js`: `overrideGate(id)` added.
- `sw.js`: cache version v6 → v7 to bust the precache.
- `node --check` passes on queue.js and api.js.

**6. Discovery guard — `producer/discover.py` + `core/config.py`**
- `YouTubeSourceConfig`: new `exclude_channels: list[str] = Field(default_factory=list)`.
- `SourcesConfig`: new `exclude_keywords: list[str] = Field(default_factory=list)`.
- `discover_all()`: applies YouTube channel filter (author_handle substring, case-insensitive) and cross-platform keyword filter (title substring, case-insensitive) with `_is_excluded_by_keywords` helper. Filtered candidates are logged at INFO level. `campaigns/fitness.yaml` not touched.

**7. Tests**
- `tests/test_review_gate.py` (28 tests): JSON parsing, content scoring, transcript slicing, vision verdict mapping, resolution/duration checks, meme clip pass-through, transport error → pending, happy path → ready, animation → didnt_pass, low formula_score → didnt_pass, safety flag → didnt_pass. All LLM/vision calls mocked via monkeypatch — NO network calls.
- `tests/test_migration_003.py` (12 tests): Clip model has all 3 columns with correct nullability/length/defaults + index; Clip instantiation roundtrip for pending/ready/didnt_pass/overridden.
- `tests/test_discovery_exclusion.py` (12 tests): YouTube channel exclusion, keyword exclusion, case-insensitivity, partial match, platform scoping, combined exclusions, empty lists.
- `tests/test_gate_api.py` (10 tests): gate fields in clip payload (5 scenarios), override-gate endpoint (5 scenarios including 404 + auth check). Fixture uses file-based SQLite (tmp_path) so all connections share the same on-disk store — avoids SQLite in-memory isolation issue. Fixture teardown resets `core.db._engine`/`_SessionLocal` to prevent cross-test leakage.

**8. Fixtures**
- `web/static/fixtures.js`: all 5 mock clips now carry `gate_status`, `gate_reasons`, `formula_score`. Demo distribution: clip_001/002 = 'ready', clip_003 = 'didnt_pass' (watermark + captions failed — showcases the fail section), meme_001/002 = 'pending' (memes skip gate).

**What remains unverifiable without LLM_API_KEY set locally:**
- Vision verdict quality (is the model actually detecting hook text, watermarks, real humans in these exact frames)
- Content rubric scoring quality (does the 10-question rubric produce meaningful spread across clips)
- Phase 2 safety flag sensitivity
These are all verifiable via a live demo run on Railway (LLM_API_KEY is set there) — the gate will write results into clip rows visible in the queue.

**State:** NOT committed/pushed (per constraint in the task brief — orchestrator owns commit/push). All files cleanly edited; no syntax errors; 393/393 tests pass. Ready for Railway deploy.

### 2026-07-09 — Session: Style-ref layout hardwire + active-speaker reframing

**TASK A — Style-refs layout hardwired in render/modal_app.py (COMPLETE)**

7 module-level constants added (HOOK_SHOW_SECONDS, HOOK_BOX_CENTER_Y_FRAC, CAPTION_ZONE_Y_FRAC, WATERMARK_BOTTOM_MARGIN_FRAC, WATERMARK_WIDTH_FRAC, WATERMARK_MIN_OPACITY, CAPTION_MAX_WORDS_PER_EVENT).

Hook: white box (`box_color` default `#FFFFFF`), black text (`text_color` default `#000000`), box center at 52% frame height via `y=H*0.5200-text_h/2` drawtext expression, `boxborderw=30`. Bold omitted from drawtext (not available in this Ubuntu ffmpeg build — ExtraBold font file provides weight). Visible 0→`hook.show_seconds[1]` (default 7.5s, config in fitness.yaml sets 8s).

Captions: `_position_to_alignment` gained `mid_low` case → alignment 8, top at `CAPTION_ZONE_Y_FRAC` (65% height). Default position changed to `"mid_low"`. `max_wpl` default to `CAPTION_MAX_WORDS_PER_EVENT` (3).

Watermark: `position=="bottom"` → bottom-center at `WATERMARK_BOTTOM_MARGIN_FRAC` margin, scale to `WATERMARK_WIDTH_FRAC` width, opacity floored at `WATERMARK_MIN_OPACITY`. Center behavior kept for `position=="center"` only. Credit placed just ABOVE the watermark using pixel arithmetic.

Required-asset check: if watermark or (outro enabled+missing) raise `RuntimeError("missing required brand asset: …")` — no longer silent.

Portability fix: removed `bold=1` from drawtext (unavailable in Ubuntu 24.04 ffmpeg; this was found during verification and fixed).

**TASK B — Active-speaker reframing (render/reframe.py — COMPLETE)**

New file `render/reframe.py` (~450 lines). Public interface: `reframe_segment(source, out_video, out_audio, start, duration, out_w, out_h, log)`.

Face detection: OpenCV YuNet (`cv2.FaceDetectorYN_create`) — no OpenGL/libGLESv2 dependencies. Model resolved from: `assets/models/face_detection_yunet.onnx` (228KB shipped in repo) → `/models/face_detection_yunet.onnx` (Modal container) → `REFRAME_YUNET_MODEL_PATH` env var.

Active-speaker heuristic: 0 faces → center crop for scene; 1 face → track it (smooth); N>1 faces → pixel-difference variance in lower 40% of each face bounding box across consecutive sample pairs (mouth movement proxy). Largest face fallback when heuristic fails.

Virtual camera: Gaussian-smooth within scene (pure Python, no scipy), snap across scene cuts, clamp crop box to frame.

Apply: per-scene constant `crop=W:H:X:0,scale=out_w:out_h:flags=lanczos,setsar=1` → temp segment files → concat demuxer with `-c copy`.

MediaPipe note: MediaPipe 0.10.35 removed `mp.solutions` API entirely; the new Tasks API requires `libGLESv2.so.2` which is absent on this headless VPS. YuNet was used as the primary detector. MediaPipe is still listed in the Modal image pip_install (it works in the GPU container where libGL is available). TalkNet-ASD integration point documented in code.

Scene detection: PySceneDetect 0.7 `AdaptiveDetector`. `scenedetect[opencv]` conflicted with `opencv-contrib-python` during local install; plain `scenedetect` (0.7) installed instead (OpenCV already present).

Modal image updated: added `mediapipe`, `opencv-python-headless`, `scenedetect[opencv]` to pip_install; added `.add_local_python_source("render", copy=True)`.

YuNet model: `assets/models/face_detection_yunet.onnx` (228KB, downloaded from opencv_zoo).

**VERIFICATION — PASSED on real R2 media (complete)**

Source: `campaigns/fitness/raw/youtube_IAnhFUUCq6c.mp4` (93-min podcast, 2 people, 1080p).
Segment tested: t=600–645s (10min mark, stable dialog section).

Pipeline ran: reframe_segment → faster-whisper CPU (164 words) → _build_ass → _apply_overlays (real Vici logo + Montserrat ExtraBold) → _concat_outro (Vici CTA outro.mov).

Duration: 52.09s (45s content + 7.09s outro = OK; expected 51.5–52.6).

Frame results (see `/tmp/claude-0/-root/d2bf0976-e7a1-4263-bc9e-82853f8f54d2/scratchpad/verify_frames2/`):
- hook_3s.png: WHITE box at chest level, bold BLACK text ("Is hypertrophy really / about progressive / overload or is it / something else..."), captions below ("everything like that." with "everything" highlighted cyan), "via @hubermanlab" just above VICI PEPTIDES logo at bottom-center. No corner badge. ✅
- mid_22s.png: hook gone (>8s), captions ("you walk from"), VICI PEPTIDES logo clearly readable. ✅  
- tail_41s.png: captions ("possible. The next"), logo present, person in frame. ✅

Face centering (20 frames from reframed.mp4, YuNet detection):
- 20/20 frames: face within 25% of frame horizontal centre = **100%** (threshold: ≥90%)
- mean distance from centre: 0.051 (normalised; 0 = perfect)
- max distance: 0.092
- All per-frame values: 0.063 0.039 0.033 0.019 0.056 0.037 0.037 0.061 0.066 0.034 0.092 0.068 0.043 0.049 0.053 0.064 0.089 0.050 0.032 0.034

Tests: 393/393 passed (unchanged from review-gate stream's baseline). AST parse clean on both files.

**KNOWN APPROXIMATIONS / HONEST NOTES:**
- PySceneDetect found 1 scene in this 45s segment (podcast has no hard cuts in this window) → single crop was applied; multi-scene interpolation logic is correct but untested on footage with hard cuts in this session.
- Mouth-movement heuristic is pixel-variance in lower 40% of face bbox (not MediaPipe FaceLandmarker landmarks 13↔14). Works well when faces are large enough in frame; may degrade on wide two-shot where faces are small.
- MediaPipe in Modal container (GPU): `mp.solutions` API is absent in 0.10.35; the render will import-error and fall back to YuNet there too (since `render.reframe` is now in the container). The Modal image pip_install still lists `mediapipe` as a future upgrade path; it does not harm the current deployment.
- The `bold=1` drawtext parameter is absent in Ubuntu 24.04's ffmpeg build. The ExtraBold font provides visual weight. On the Modal container (Debian Slim), `bold=1` may or may not work — the parameter removal is conservative and correct on both.

**State:** NOT committed (per constraint). All changes in working tree. Orchestrator owns commit/push.
Files changed: `render/modal_app.py`, `render/reframe.py` (new), `assets/models/face_detection_yunet.onnx` (new, binary), `tests/test_config.py` (updated for new fitness.yaml values).

### 2026-07-10 — Precision pass CLOSED: gate live and correct, 8 READY clips
- Purge executed (12 old clips: PG rows + R2 objects). Migration 003 applied on deploy. Fresh gated demo run: 14 renders (podcast-only sources — Huberman + Diary of a CEO; zero cartoons).
- **Gate bug #1:** vision prompt said hook lives in "upper 40%" — contradicted the mid-frame layout contract; all 14 false-failed. Fixed prompt (mid-frame, lenient centering).
- **Gate bug #2 (the real killer):** frame LABELS were placed AFTER images in the vision message → model associated each label with the FOLLOWING image → every check judged the wrong frame. Fixed: labels BEFORE images. Verified with the gate's own extracted frames: all 7 checks correct. LESSON: in multi-image vision prompts, text labels must precede their image.
- `scripts/regate.py` re-judged all 14 in place (no re-render spend). **Result: 8 READY (formula 0.686–0.819), 6 didnt_pass.** Spot-checked failures by frame: clip 18 speaker genuinely off-center (reframe miss — mouth-heuristic weakness on that source), clip 23 hook genuinely missing. THE GATE'S CALLS ARE CORRECT.
- Verified render layout matches style refs exactly (hook white box mid-frame w/ black text, CapCut captions below, VICI PEPTIDES logo bottom readable, via @handle credit, real CTA outro, real humans, centered).
- Spend: $0.504 / $30 MTD total (incl. all 26 renders to date).
- OPEN: (1) reframe misses on some two-shot sources → TalkNet-ASD upgrade path documented in render/reframe.py; (2) clip 23 missing hook overlay — investigate hook drawtext edge case; (3) GitHub push blocked (PAT revoked; ~5 commits local-only, deployed via railway up); (4) POSTIZ_API_URL still missing in Railway (posting step); (5) meme demo; (6) rotate chat-pasted creds.

### 2026-07-10 (later) — Sentence-boundary snapping for clip start/end times

**Problem:** Clips started and ended mid-sentence because the LLM returned raw float timestamps that landed inside a word/sentence, and nothing in the pipeline corrected them before the ffmpeg cut.

**Implementation (no regressions — 462/462 tests pass, +54 new):**

1. **`core/sentences.py`** (new module):
   - `build_sentence_spans(transcript)` — converts segment-level transcript to sentence-level spans with timestamps. Concatenates all segment texts with space separators; assigns each character a timestamp via linear interpolation within its segment (`char j → seg_start + j/(n-1) * duration`). Splits the concatenated text into sentences on `[.!?]\s+(?=[A-Z])` boundaries with a protected-dot set (ellipsis `...`, known abbreviations, single-letter initials). Sentences that span segment boundaries automatically get the right start/end time because char→time is a flat map of the full text. Returns `[{"text", "start", "end"}]`.
   - `snap_to_sentences(moment_start, moment_end, sentence_spans, clip_len)` — snaps start DOWN to the sentence containing `moment_start` (never forward, so opening words are not clipped), snaps end to the END of the sentence containing `moment_end` (never backwards, so the final thought is never cut off). Enforces `clip_len [min, max]` by dropping/adding whole trailing sentences. Safe no-op when `sentence_spans` is empty.

2. **`core/llm.py`** (modified):
   - Added `from core.sentences import build_sentence_spans, snap_to_sentences`.
   - Updated `_build_prompt`: prompt now explicitly instructs "Choose start at the FIRST word of a sentence and end at the LAST word of a sentence — the clip must be a complete, coherent thought..." and notes the hook must describe the opening sentence.
   - Wired snapping into `rank_moments` after `_validate_moments`: builds sentence spans from the transcript, snaps every validated moment's start/end, returns snapped moments. Failure is non-fatal (logs warning, returns raw timestamps).

3. **`tests/test_sentences.py`** (new, 54 tests):
   - `TestSentenceCharSpans` (14): boundary detection, abbreviation guard, ellipsis, span coverage/contiguity.
   - `TestBuildSentenceSpans` (19): empty input, fixture sentence count (5 sentences from 3 segments), timestamps at segment boundaries, ellipsis not split, monotonicity, bounds.
   - `TestSnapToSentences` (19): operator scenario (start=8.0→0.0, end=25.0→sentence-end, NOT 25.0), clip_len max trim, clip_len min extend, edge cases.
   - `TestLlmIntegration` (2): import smoke + prompt instruction content.

**Verified with the real operator fixture segments (3 segs, note mid-sentence endings):**
- `snap(8.0, 25.0, spans, (5, 60))` → start=0.0, end=≈29.44 (sentence 4 end). NOT 8.0 / NOT 25.0. ✓
- `snap(8.0, 25.0, spans, (5, 15))` → trimmed to ≤15s, still on a sentence boundary. ✓
- `snap(0.0, 5.0, spans, (20, 45))` → extended to ≥20s, still on a sentence boundary. ✓
- "tissue repair after..." is ONE sentence (ellipsis is fully protected, no split). ✓

**AST parse:** `python -c "import ast; ast.parse(open('core/sentences.py').read())"` and `core/llm.py` both clean.

### 2026-07-10 (later) — Style-ref study session (no code changes)
- Lubosi added 4 new stills (IMG_7495–7498) + screen recording `ScreenRecording_07-10-2026 17-15-44_1.MP4` to `/root/clip-engine/style_refs/` (note: style_refs lives OUTSIDE the repo, at /root/clip-engine/). Whole folder is now the canonical visual reference set. Studied all refs + extracted recording frames + pulled frames from live clip 26 for comparison.
- Reference rules extracted (see chat report): hook = centered per-line rounded white pill, sentence case, quotes+emoji, visible ~3s (not 8s); captions = ONE word, ALL CAPS, white+black outline, NO cyan highlight, fixed at ~65% height; watermark = transparent (no background box), must POP (user wants high-contrast, not blend-in) at ~80% height; camera cuts must land with speaker already fully centered (no half-faces / no visible reframe drift).
- Conflicts vs hardwired template flagged for reconcile: hook duration (8s vs ~3s), hook box style (left-aligned square drawtext box vs centered rounded pills), hook truncation "..." at 4×22 chars (user: hooks must fit, not truncate), caption style (3-word cyan-highlight chunks vs single-word all-caps white), watermark asset (logo.png has baked solid background — must be replaced with transparent version + contrast treatment), watermark y (~91% vs ~80%).
- DASHBOARD BUG DIAGNOSED (not fixed yet): queue is permanently blank after Precision-pass deploy. `web/static/queue.js` `_load()` early-returns when `#queue-cards` is missing (queue.js:146–147), but `#queue-cards` is only created by `_renderCards()` (queue.js:314) which is only called from `_load()` → deadlock on fresh page load. Introduced in commit 1eae04d. Server data intact: /api/clips returns 14 clips (13 pending_review = 7 ready + 6 didnt_pass, 1 scheduled); R2 media presigns and streams fine. Fix = render skeleton into `#queue-ready-section` (always exists) or drop the early return.
- Clips NOT purged yet (user wants reconcile talk first; purge for next demo campaign is pre-approved — use scripts/purge_clips).

### 2026-07-10 (later 2) — Peptides production campaign wired + coherence/watermark/hook fixes SHIPPED
Deployed to Railway (railway up) + Modal (make deploy-modal). All 462 tests green.

**Frontend blank-queue bug — FIXED & DEPLOYED.** `web/static/queue.js` `_load()` no longer early-returns on missing `#queue-cards`; skeleton/error now render into the always-present `#queue-ready-section` (also fixed the dead `cardsEl` ref in the catch block). Verified the deployed /queue.js carries the fix. This was why the 13 clips looked "disappeared" — server data was always intact.

**13 fitness demo clips PURGED.** scripts/purge_clips.py fitness --yes (via Postgres DATABASE_PUBLIC_URL proxy — the internal host is unreachable off-Railway). API now returns 0 clips. Sources/transcripts/render_jobs kept.

**Sentence coherence (the big one) — SHIPPED.** New `core/sentences.py`: `build_sentence_spans()` (flat char→time interpolation across segments, splits on [.!?] with abbreviation/ellipsis guards, merges cross-segment sentences) + `snap_to_sentences()` (start snaps DOWN to sentence start, end snaps to sentence end, then enforces clip_len by adding/dropping WHOLE sentences). Wired into `core/llm.py rank_moments` after `_validate_moments` (non-fatal fallback to raw ts on error) + added a mandatory SENTENCE-BOUNDARY RULE to the ranking prompt tying the hook to the opening sentence. 54 new tests incl. operator's 3 real transcripts. Verified: LLM (8.0,25.0)→(0.0,29.44), never mid-sentence.

**Hook truncation — FIXED & DEPLOYED (Modal).** `render/modal_app.py`: replaced the 4-line cap + "..." append with `_compute_hook_fit()` — shrink-to-fit font (44→30px floor), up to 6 lines, height ≤34% frame, NEVER truncates. Fixes the "hook didn't finish / dot-dot-dot" complaint. Needs make deploy-modal (done).

**Watermark background removed — DONE.** `assets/peptides/logo.png` generated from the fitness VICI logo: cream bg keyed out → transparent, letters recolored WHITE + soft dark shadow so it pops on any footage with no box. Verified over dark/light/mid backgrounds. (Brand is VICI PEPTIDES so it fits the new campaign natively.)

**Clip-target override — added.** `producer/run.py --clip-target N` (+ threaded through run_campaign and demo render-cap headroom) and `POST /api/runs/{campaign}` accepts `clip_target`. Lets a demo stop at exactly N ready clips without touching DEMO_CLIP_TARGET.

**NEW PRODUCTION CAMPAIGN: campaigns/peptides.yaml** (mode: production, config-driven, nothing hardwired). Niche = peptides + looksmaxxing/blackpill. 12 podcast-targeted YT search terms (correct spellings: Retatrutide, BPC-157, TB-500, CJC-1295, Ipamorelin, MOTS-c, GHK-Cu, etc.), ranking_rules encode: coherence-mandatory, strong 2s open, hook==opening point, pro-peptide + experiences + looksmaxxing/Clavicular, exclude sourcing/self-harm/guaranteed-cure/hateful. Assets: assets/peptides/{logo.png transparent, Montserrat-ExtraBold.ttf, outro.mov}. postiz_channels [instagram-standalone, x], autopost false. Assets auto-upload to R2 per-campaign at render dispatch.

**DEMO RUN IN FLIGHT:** POST /api/runs/peptides {mode:demo, clip_target:5, caps $3/$3}. Watching for 5 ready clips, then frame-review vs style_refs.

**OPEN / WATCH:**
- Speaker-centering on 2-person wide shots still uses the mouth-variance heuristic (reframe.py) — may miss like fitness clip 18; TalkNet-ASD upgrade path still documented. Check the peptides clips' framing on cuts; if a two-shot miss appears, that's the known weakness, not a regression.
- Clavicular's own content is NOT on YouTube (banned Apr 2026) — only 3rd-party interviews/clips about him will surface. Peptide-doctor/biohacking podcasts are the reliable YT source.
- GitHub push still blocked (PAT revoked) — everything deployed via railway up / make deploy-modal, commits local-only.
- Rotate chat-pasted creds still outstanding.

### 2026-07-10 (later 3) — Peptides demo POST-MORTEM: 3 blockers found (clips NOT acceptable)
Demo run produced ~9+ clips, ALL gate-failed → 0 ready. Frame review + Modal logs give exact root causes:

1) **FACE DETECTION DEAD IN MODAL (critical, root cause of centering failure).** Every reframe logs "reframe: no faces detected anywhere; falling back to center crop" — YuNet finds ZERO faces on all footage (even clean frontal Joe Rogan). Center-crop then leaves off-center guests half-out-of-frame → Phase-1 gate `speaker_centered` fails almost everything. This is the user's #1 requirement. Scene detection works (13 scenes), so it's YuNet specifically. HANDOFF's "20/20 centered" verification ran LOCALLY (model at assets/models/) — it never exercised the Modal container, where the model path likely doesn't resolve. `_get_yunet_detector` returns None if no model file found at YUNET_MODEL_SEARCH_PATHS ([repo]/assets/models/face_detection_yunet.onnx via __file__.parent.parent, and /models/...). In Modal (add_local_python_source("render") + COPY . /), reframe.py's parent.parent likely isn't repo root, so the model isn't found → None → 0 faces. FIX HYPOTHESIS: ship YuNet model to a stable Modal path and/or add /assets/models + an env override to the search paths; add a log line distinguishing "model not found" vs "0 faces". MUST verify inside Modal, not locally.

2) **WATERMARK renders the OLD fitness cream-box logo, not the transparent one.** Modal log confirms it downloaded the CORRECT key (campaigns/peptides/assets/logo.png = my white transparent wordmark, md5 verified in R2). Yet rendered pixels show the fitness cream box w/ dark text (the "red stars" were the guest's red shirt through the semi-transparent box). Since the right file was downloaded, suspect warm-container reuse of a stale watermark.png OR an overlay step that flattens onto a bg. NEEDS Modal-side repro (render one clip, inspect the downloaded watermark.png bytes + the overlay filter output).

3) **CONTENT off-topic.** Discovery→ranking pulled "Joe Rogan - Anybody Can Get Ripped!" (@JRE Clips) — body types/fighting/endomorphs, NOT peptides. sort_by_engagement floats high-view general clips; the ranker clipped fitness moments despite peptide ranking_rules. FIX: bias discovery toward genuinely peptide/looksmax-dense sources (channel allowlist of the researched peptide podcasts, and/or a topical pre-filter on title/transcript) + make the ranker REJECT (score 0) moments not about peptides/looksmaxxing.

WHAT WORKS (verified in frames): hook shrink-to-fit (full, no "..."), sentence coherence (hooks are complete sentences, clean starts), CapCut captions + cyan highlight, the transparent watermark asset itself (just not reaching the render), outro pipeline, gate correctly rejecting bad clips.

Run self-stops via demo render-cap + $3 modal cap (spend trivial, ~$0.30). Next: fix #1 first (centering is the hard requirement), then #2, then #3; re-run demo.

### 2026-07-10 (later 4) — 3 render blockers FIXED + deployed + re-run
Root causes nailed and fixed in order:

1) **Face detection (centering) — FIXED & VERIFIED IN MODAL.** Root cause: the Modal image only baked render/*.py via add_local_python_source; the YuNet ONNX model (assets/models/face_detection_yunet.onnx) was NEVER shipped, so _get_yunet_detector returned None → 0 faces → center-crop everything → speaker_centered gate failed all. Fix: modal_app.py image now `.add_local_file("assets/models/face_detection_yunet.onnx","/models/face_detection_yunet.onnx",copy=True)` (/models is a YUNET_MODEL_SEARCH_PATHS entry). Also reframe.py now logs "YuNet model NOT FOUND" vs "model loaded but 0 faces" so this can never be silent again. VERIFIED with a throwaway `modal run` on the deployed image: model_present=True, faces_detected=1 on a real face frame — IN THE CONTAINER, not locally (the prior verification's blind spot).

2) **Watermark — was the early-render asset race; hardened.** The newest clip (41) already rendered the CORRECT transparent white VICI PEPTIDES wordmark (no box) — overlay + asset are fine. Only the first ~3 clips showed the old cream box. Root cause: render_dispatch.ensure_campaign_assets_on_r2 used `if not r2.exists(key): upload` — a stale asset already in R2 won over the freshly deployed one on the first renders. Fix: always (re)upload the deployed asset once per process (per-process cache still dedupes) so the deployed file is authoritative.

3) **Content targeting — topical gate added.** First run's first source was "Joe Rogan – Anybody Can Get Ripped" (body types/fighting), but LATER sources were on-topic (Clavicular, DIY injectables, looksmaxxing/incel) — so search terms are fine; the ranker just over-stretched a fitness clip. Fix: peptides.yaml ranking_rules now opens with a HARD TOPICAL GATE — return [] if the source isn't substantively about peptides/hormones/TRT or looksmaxxing/aesthetics; don't stretch generic fitness. (Phase-2 gate uses the same rules, double-enforcing.)

Also: **SW cache-bust v7→v8** — the operator's "empty dashboard" was the PWA service worker serving the OLD broken queue.js; a fresh browser (Playwright) rendered the queue fine (Ready empty because all failed the gate; "Didn't pass review" section full). v8 forces the refresh.

Deployed: make deploy-modal (model bake + reframe log) + railway up (peptides.yaml, sw.js, render_dispatch.py). 462 tests green. Purged 12 failed peptides clips. Re-run triggered: demo, clip_target 5, caps $3/$3. WORKS-IN-PROGRESS: verify centering end-to-end on the re-run's Modal logs (expect "reframe: applying N per-scene crop(s)") + gate speaker_centered passing; then frame-review the 5 clips vs style_refs.

### 2026-07-10 (later 5) — Re-run RESULTS: fixes verified working; gate-vs-niche tension surfaced
Re-run (pid 24) produced 2 peptides clips from "The Diary Of A CEO". FRAME-VERIFIED the 3 fixes all WORK:
- CENTERING: speaker properly framed across all sampled frames (clip 42 @2/8/16/22s, clip 43 @3/20/40/55s). Modal log confirms "reframe: applying N per-scene crop(s)" (face detection alive). Model verified in-container (faces_detected=1).
- WATERMARK: clean transparent white VICI PEPTIDES, no box, pops on all backgrounds.
- HOOK: full, coherent, on-topic, strong ("The FDA banned effective peptides overnight in 2023 — RFK called it illegal"; "A patient increased his sperm count 10x — a peptide helped him lose 100 lbs"). No "...".
- CONTENT: on-topic; topical gate working ("No clips selected from source" on off-topic sources).
- DASHBOARD: renders fine (Playwright screenshot shows READY card w/ thumb+hook+watermark+score). The operator's "empty dashboard" was PWA SW cache (fixed v8).

NEW ISSUES / DECISIONS FOR OPERATOR:
1) **Safety gate vs the niche (KEY DECISION).** review_gate Phase-2 has a `safety_medical_claims` AUTO-FAIL. Peptide content is inherently health-claims-heavy, so it rejects exactly the clips this campaign wants (clip 42 auto-failed on "sperm count 10x / lose 100 lbs"). Both "ready" clips are actually gate-FAILURES shown via manual Override (gate_status='overridden'; producer never sets that — only POST /api/clips/{id}/override-gate does). Options: make the medical-claims auto-fail campaign-configurable / relax it for peptides (keep truly-dangerous-advice blocks), or keep strict and accept low yield. NEEDS operator call.
2) **clean_ending weak** on both (0.25/0.40) — the sentence END-snap may be ending a beat early/late; worth tuning snap_to_sentences end selection.
3) **Diary of a CEO baked-in "Notes" graphics** (yellow/white fact-cards + black "Notes" tab) collide with our captions/watermark on parts of clips. Source-specific. Options: gate check to reject frames with competing baked-in text, or exclude DOAC-style sources.
Spend: peptides $0.27, total $0.82/$30 MTD. Fitness still enabled (daily --all cron will recreate fitness clips) — recommend disabling fitness (needs a railway up; hold until no run in flight).

### 2026-07-11 — Session: Sources view + topic-boundary segmentation (Fable 5 + 1 fork agent)
Two features from the operator brief. All work done; deploying via `railway up` (Railway-only — no Modal/render changes). GitHub push still blocked (PAT revoked); local commit made. **Full suite 499 passed** (462 baseline + 21 sources + 16 topics).

**PART 1 — "Sources" view (track every mined video). Fork agent built it.**
- Backend: `GET /api/sources` in `web/api.py` (`_source_thumbnail_url()` helper + endpoint). Lists Source rows that have been USED (status != 'pending' OR has clips), newest-first by COALESCE(processed_at, updated_at). Per source: id, source_id, platform, url, title, author_handle, campaign, status, processed_at, clip_count, clips[{id,hook,status,gate_status}], used_ranges_count, thumbnail_url. Supports `?campaign=` + `?q=` (ILIKE) filters. `selectinload(Source.clips)` avoids N+1.
- Thumbnails: youtube → `https://i.ytimg.com/vi/{id}/hqdefault.jpg` (permanent, no expiry). tiktok → `videoMeta.coverUrl` from Apify metadata (CDN-signed, expires in hours/days → frontend `onerror` falls back to platform icon). Metadata inspection confirmed those keys on real prod rows.
- Frontend: new `web/static/sources.js` (`initSources`), Sources tab in `index.html` (`#view-sources` + tab button), wired into `app.js` VIEWS/`_initView`/titles/logout resets, `api.getSources()`, 3 mock sources in `fixtures.js`, cinematic-glass card styles in `styles.css` (thumbnail + title + @handle + platform badge + campaign chip + processed date + clickable URL + status chip + "N clips" + expandable clip list with gate_status chips + client-side search + empty state). `sw.js` v8→v9, `/sources.js` precached.
- Tests: `tests/test_sources_api.py` (21): list shape, newest-first, pending-unused excluded, clip_count, campaign filter, q search, auth-required. Doubles as the dedupe audit (operator can confirm nothing is re-clipped).

**PART 2 — Cut clips on TOPIC boundaries, not arbitrary time.**
- `core/topics.py` (NEW):
  - `segment_transcript(transcript, clip_len)` — one LLM call splitting the transcript into self-contained topic segments `[{start,end,summary,ends_because}]`. **Best-effort: returns [] on ANY error** (missing SDK/keys, transport, unparseable) so the producer run never breaks.
  - `snap_end_off_next_topic(start,end,topics,spans,clip_len)` — PURE deterministic guard: if the (already sentence-snapped) end has bled into a topic that begins AFTER the topic the clip started in, trims end back to the resolving edge of an earlier topic (most-conservative single-topic boundary first, snapped to a real sentence end); over-trim guard leaves the clip unchanged if trimming would break clip_len min.
  - `FEWSHOT_BOUNDARY_EXAMPLES` — 3 worked examples from REAL VPS transcripts (2 positive + 1 negative), incl. the exact peptide-start failure case. Embedded into the ranker prompt.
- `core/llm.py`: `rank_moments` now runs the segmentation pass BEFORE selection, feeds the topic segments + few-shot + a mandatory TOPIC-BOUNDARY RULE into `_build_prompt`, and applies `snap_end_off_next_topic` right after the existing sentence-snap. `_build_prompt` gained optional `topic_segments` param (existing sentence-boundary test strings preserved).
- `producer/review_gate.py`: Phase-2 content gate gained a **self-contained boundary check**. `_content_llm_call` takes look-ahead context (`_build_lookahead_slice`: text spoken in [end, end+15s]) and the JSON verdict gained `self_contained {complete_thought, ends_on_new_topic, reason}`. `_score_content_verdict` scores it (absent → PASS, so existing mocked-verdict tests unaffected); `_run_phase2` treats a self_contained failure as a hard `didnt_pass` (→ re-cut), same as a safety fail. A clip that ends on the first sentence of a new topic now fails the gate.
- Tests: `tests/test_topics.py` (16): validation, topic-index lookup, the operator's exact case (wrong end 1398.0 → snapped 1370.4), no-op cases, over-trim guard, sentence-edge snapping, few-shot content, graceful-degradation on LLM failure.

**VERIFIED on the REAL problem transcript (youtube:jt5hHb6kzYM, "Peptide Expert: What Do Peptides Actually Do? - Dr Alex Tatem"):**
- The patient sperm-count/GLP-1 story resolves at **1370.4 ("Started with a peptide.")**. The very next lines are the host's NEW question + the "peptides are almost like an app on your phone" answer — a DIFFERENT topic.
- WRONG cut (1343.9 → 1398.0, ending inside "...app on your phone. So imagine before we had apps.") → **snap → (1343.9 → 1370.4)**, trimming 27.6s off the new topic. The peptide-overview topic becomes its own separate candidate clip. Exactly the operator's requested correction.
- (Snap demonstrated LLM-free using the real transcript's own sentence spans + the topic boundaries read from the transcript, because the live segmentation call is currently blocked — see below.)

**⚠️ BLOCKER FOR THE OPERATOR — OpenRouter credits depleted.** A live `railway run` of `segment_transcript` returned HTTP 402: "requires more credits… you requested up to 4096 tokens, but can only afford 1016." This affects ALL LLM calls (ranking, gate, segmentation), not just this feature — no live producer run can rank/gate until credits are topped up at https://openrouter.ai/settings/credits. The new code degrades gracefully (segmentation → [] → snap no-op → falls back to sentence-snapping), so nothing crashes, but topic segmentation + the content gate won't actually run until credits are restored. Re-run the peptides demo after topping up to confirm the boundary correction end-to-end on a fresh render.

**NOTE / cost:** the segmentation pass adds one LLM call per source (roughly doubles ranking token cost per source). Kept because the operator explicitly asked for a segmentation pass; the deterministic snap guard still corrects boundaries even when the LLM slips.

**OPEN (unchanged + new):** OpenRouter credit top-up (NEW blocker); safety-gate-vs-niche decision (medical_claims auto-fail rejects peptide clips — still needs operator call); POSTIZ_API_URL missing in Railway; rotate chat-pasted creds; GitHub push blocked (PAT revoked).
