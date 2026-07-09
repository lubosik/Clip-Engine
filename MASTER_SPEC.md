# CLIP ENGINE — Master Build & Revamp Spec (final, 2026-07-08)

This document **supersedes and consolidates** SPEC.md (original build spec), the front-end revamp brief, and the meme-engine module. Build to this. Original SPEC.md remains for historical contracts only; where they conflict, this file wins.

**Current state:** app live at `clip-engine-production-*.up.railway.app`; login, Queue/Campaigns/Analytics/Settings all function. The `fitness` campaign shows `youtube(3)`. This pass = full visual revamp to a cinematic glass-on-charcoal aesthetic + meme engine + Modal GPU rendering + R2 storage + demo/production modes + in-app campaign builder + Modal spend tracking, without breaking what works.

---

## PART A — What exists and must keep working

Do not regress these; restyle and extend them:
- Admin-password login → Unlock (keep the auth; restyle the page).
- Bottom nav: **Queue · Campaigns · Analytics**, plus **Settings** (gear).
- Queue with **All / Today's batch** filter and a "No clips waiting / Next run: …" empty state.
- Campaigns list with per-campaign status, source summary (`youtube(3)`), pending count, and an **add-campaign (+)** action.
- Analytics with a **weeks** selector and "pulled weekly after clips are posted" empty state.
- Settings: **Mock mode** (fixture data when server offline), **Notifications** status, **Sign out** (clear token).
- Installable PWA.

## PART B — Bugs to fix during the revamp

1. **`[object Object]` on the campaign card**: the UI renders a JS object directly. Read and format the actual fields (template name, destinations, schedule) into readable text/chips.
2. **Contrast/legibility:** the new system must hit AA contrast.
3. **Empty states** must read as intentional, not broken (see Part C motion + copy).

## PART C — The visual revamp (the main ask)

### C0. Load the frontend-design skill first, then follow this brief. Where this brief pins a direction, it wins.

### C1. Aesthetic: the hero, made into an interface
Match the Clip Engine hero video/still: a **dark cinematic studio**, not flat black. Tall vertical **glass panels** rising from a **glossy reflective floor**, a luminous **light-stream** line winding across the floor, volumetric haze, cyan key + warm amber rim, jewel-tone glints on glass.

- **Base:** deep charcoal-to-near-black **gradient with depth**, faint volumetric haze and a subtle floor-reflection band — never dead flat #000.
- **Accent pair:** cyan primary + warm amber secondary. Cyan for primary actions/active states; amber sparingly for highlights/warnings.
- **Surfaces:** translucent frosted-glass panels — layered blur, soft inner glow, hairline 1px light borders, faint chromatic dispersion on edges, gentle floor reflection beneath raised elements.
- **Light-stream motif:** the winding luminous line, reused as active-nav indicator, progress accent, and section divider.
- **Type:** one characterful display face for headings, one clean high-legibility UI face for body/controls. Generous spacing, hairline dividers.
- **Motion:** elements **rise and settle** with a soft caustic shimmer. Restrained, weighty, Apple-keynote pacing. Full `prefers-reduced-motion` path (fade in place, static poster for hero).
- **Kill the boring:** no dead-black voids, no flat material cards, no default-SaaS chrome.

### C2. Login page
Full-bleed **hero loop** behind (muted autoplay, `playsinline`, poster fallback for reduced-motion/slow links) — served from R2 (`hero_loop.mp4` / `hero_poster_web.jpg`; mobile `hero_loop_vertical.mp4` / `hero_poster_mobile.jpg`). Dark gradient scrim; centered glass card with the Clip Engine wordmark, "Operator Review Console", the admin-password field, and a solid cyan **Unlock** with glow pulse + light-stream run. Keep existing auth logic.

### C3. Queue = the panel stage (signature interaction)
- Pending items render as **glass panels rising from the reflective floor**, staggered, thumbnail/first frame inside the glass with glow + floor reflection.
- **Clips are 9:16; memes are 1:1 or 4:5 — the stage handles mixed aspect gracefully.**
- Score as a **luminous edge meter** (not a number badge); source credit etched at the base; destination platforms as small glowing glyphs; a small **Clip / Meme** tag.
- Tap a panel → it **steps forward and opens the review view** (C4).
- Empty state: calm lit floor with the light-stream line and "No clips waiting · Next run: Today at 20:00".
- Keep **All / Today's batch**; add **Clips / Memes / All** type filter.

### C4. Review view (opened panel)
- Item plays/shows large in its glass panel, centered, floor reflection, haze behind. Clips: real rendered 9:16 with burned-in captions/hook/watermark/outro from R2. Memes: full image + caption.
- Quiet glass controls: **Approve** (primary cyan; panel "sends" — rises and dissolves into light, next advances), **Reject** (ghost; optional reason; sinks into floor), **Edit caption** (inline glass field, live-updates what posts).
- Metadata row: hook/caption, score, source credit, destinations, proposed schedule slot.

### C5. Campaigns
- Each campaign a glass panel: name + status light, source summary chips (`YouTube · 3 terms`), pending count, per-engine toggles (**Clips / Memes**). Fix the `[object Object]` — render real fields.
- **(+)** opens the create flow (Part L).
- Selecting a campaign can retint the accent to that campaign's color.

### C6. Analytics
- Same glass system; calm cyan/amber dark-native charts. Keep weeks selector.
- Per-clip and per-channel weekly performance. Best performers can **stand taller** (performance as height). Honest empty state.
- **Modal spend tracker** (Part M) surfaces here + Settings.

### C7. Settings
- Glass rows for Mock mode, Notifications, Sign out — all functions intact, AA contrast.

### C8. Mobile + desktop, both first-class
- **Mobile:** panels stack front-to-back; bottom nav with light-stream active indicator; touch targets ≥44px; test at 380px.
- **Desktop:** wider lit floor, more panels in depth.
- **Performance budget (critical):** CSS 3D transforms + layered blur/gradients first; WebGL only if it holds ~60fps on a mid phone and degrades gracefully. Lazy-load panel textures; only the open clip + a few visible thumbnails play video. Reduced-motion first-class.

## PART D — Clip engine (unchanged core)

Config-driven campaigns (`campaigns/<name>.yaml`), fitness seeded. Pipeline: **discover → dedupe → transcript → rank → render → review → schedule → weekly analytics.** Apify actors: `streamers/youtube-scraper`, `pintostudio/youtube-transcript-scraper`, `clockworks/free-tiktok-scraper`, `clockworks/tiktok-comments-scraper`, `agentx/tiktok-transcript`, `apify/instagram-reel-scraper`, `apify/instagram-scraper`. Dedupe + per-source used-range tracking; `exhaust_source` toggle; default per-source cap. Ranking prompt carries the content filter (exclude unsafe dieting / disordered-eating / medical claims; credit the source). Scheduling via Postiz (drafts unless `autopost`).

## PART E — Meme engine (same app)

Sibling producer (`meme/`), shares core/storage/review/scheduler/analytics.
- **Reference-based, not fine-tuned.** Extract structured `meme_style_profile.json` from reference memes in `campaigns/fitness/meme_refs/` (visual format + caption voice, measurable rules + confidence). Generate concept+caption from the profile, render via image model (Higgsfield/Seedream/Nano-Banana) using reference memes as visual style references.
- **On-brand classifier** (LLM-as-judge) scores each meme vs the profile (on-format, on-voice, on-brand, legibility, compliance); auto-rejects misses before review. Compliance failures always rejected.
- **Feedback loop:** weekly, top performers promoted into the reference set; re-extract profile → new version. This is the only "training."
- **Text/X posts:** same voice profile, no image, same classifier + review.
- Hard rules per campaign: no em-dashes; no medical/health claims; nothing promoting unsafe dieting/disordered eating.
- **Renders in the same queue** as clips, flexed to meme aspect, Clips/Memes/All filter.

## PART F — Storage AND render (both required)

1. **Storage → Cloudflare R2.** All media (raw, intermediate, finished clips/memes, thumbnails, hero assets) in R2. S3-compatible, zero egress. VPS holds **no** video; local temp deleted after upload. PWA + Postiz read via presigned URLs. R2 alone does NOT speed renders.
2. **Render speed → Modal serverless GPU, scale-to-zero.** ffmpeg with **NVENC**, pulls source, renders, uploads to R2, exits. The CPU VPS never renders.

**Modal wiring:**
- `render/modal_app.py`: `modal.Image.debian_slim(python_version="3.11").apt_install("ffmpeg").pip_install("boto3", "faster-whisper")`; `app = modal.App("clip-engine-render", image=image)`; `@app.function(gpu=["l4","t4","any"], timeout=1800, secrets=[modal.Secret.from_name("clip-engine")]) def render_clip(job: dict) -> dict:` — pull source segment from R2 → ffmpeg cut → 9:16 reframe → ASS word-by-word captions → hook overlay → watermark/badge/source-credit → outro concat → `-c:v h264_nvenc` → upload mp4 + thumbnail to R2, return keys + status.
- **GPU:** L4 with t4/any fallback. NOT A100/H100.
- **Deploy:** `modal deploy render/modal_app.py`. Producer invokes via `modal.Function.from_name("clip-engine-render", "render_clip").remote(job)`; batch a campaign's clips via `.map()`/concurrent calls.
- **Secrets on Modal:** R2 keys + `DATABASE_URL` in Modal Secret `clip-engine`. VPS authenticates SDK with `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` from Railway variables. No secret hardcoded anywhere.
- **Billing:** per-second, scale-to-zero; Starter tier free credits cover demo + early production.
- `faster-whisper` (small/base) runs **inside the Modal container** only as caption-timing fallback; never on the VPS.

R2 config (non-secret):
```
Account ID: ff595249c8042ae47c68bafe4be405dc
S3 endpoint: https://ff595249c8042ae47c68bafe4be405dc.r2.cloudflarestorage.com
```
Credentials via env only: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_ENDPOINT`; `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET`; plus `APIFY_TOKEN`, `POSTIZ_API_KEY`, `LLM_API_KEY`, `WEB_ADMIN_PASSWORD`. The API issues presigned URLs; the browser never holds keys. **Rotate any credential ever pasted outside the variables tab.**

## PART G — Sub-agents / work streams

1. **Core** — config loader/validation, Postgres models, Apify + R2 clients, env/secrets.
2. **Discovery** — Apify discovery/transcript, dedupe, comment attribution.
3. **Ranking** — transcript normalization, LLM ranking + content filter, non-overlap/exhaustion, render dispatch.
4. **Render** — Modal GPU worker (Part F).
5. **Web** — cinematic PWA revamp (Part C), all sections + login + review; fixes `[object Object]`; mobile + desktop.
6. **Meme** — profile extraction, generation, classifier, feedback loop (Part E).
7. **Scheduler/Analytics** — Postiz posting + weekly pull-back + matching.

Shared Postgres/R2/API contracts so streams run in parallel.

## PART H — Assets (in R2 / on server)

- Hero: `hero_loop.mp4` (16:9), `hero_loop_vertical.mp4` / `hero_poster_mobile.jpg` (9:16), `hero_poster_web.jpg` (fallback).
- Per-campaign (fitness): `logo.png` (Vici watermark), `logo_circle.png` (corner badge), CTA outro, caption font, `meme_refs/`.

## PART K — Demo vs Production mode (+ demo suite)

Every campaign, run, clip, and meme carries **`mode`: `demo` | `production`**. Separate from Settings "Mock mode" (fixture-data-when-offline). Mode governs real output and labelling.

- **Demo:** full real pipeline — real discovery, real render to R2, can post — everything **tagged `demo`** (amber glass pill) on panel, campaign card, posts list. Demo posting can target a designated **test destination** or the real one; dashboard always shows it was demo. **The `demo` label is dashboard-only, NEVER burned into the video.**
- **Production:** cyan/green pill; posts to live channels. Campaign flips to production when satisfied; existing demo items keep their label.
- Badge everywhere an item appears (queue panel corner, review view, campaign card, analytics rows). Campaign-level toggle sets the default; runs inherit.

**Demo suite (`make demo`):** full pipeline end-to-end in demo mode on a known-good source → real clips to R2 → queue tagged `demo` → review → optionally post to test destination. Reuses §I harness but exercises real render + post path, capped by `--max-apify-spend` and `--max-modal-spend`.

## PART L — In-app campaign builder (the (+))

Glass, mobile-friendly, multi-step form → uploads assets to R2 → writes/validates `campaigns/<name>.yaml` server-side. No hand YAML. Fields:

- **Basics:** name, niche, `mode` (demo/production).
- **Sources:** YouTube terms/channels, TikTok profiles/hashtags, IG profiles; min views; recency window.
- **Asset uploads (→ R2):** logo/watermark, corner badge, outro video, caption font, visual reference images of desired look (passed to render/ranking as guidance). Preview per upload.
- **Creative direction (free text):** natural-language brief fed into ranking + render config.
- **Captions:** placement, style (word-by-word CapCut), font, base + highlight colors, max words/line.
- **Hook:** placement, seconds on screen (e.g. 0–8s), text source (ranker hook or custom).
- **Clip rules:** length range, `ranking_rules` text (content filter + what to look for), max clips per source, `exhaust_source`.
- **Watermark & outro:** on/off, placement, opacity.
- **Engines:** Clips on/off, Memes on/off (+ meme reference upload if on).
- **Destinations:** Postiz channels, schedule (posts/day, times, timezone), hashtags, autopost.

On **Save:** validate, upload assets to R2 under the campaign prefix, write YAML, show in list. Campaign then runs by itself on its cron. Editing re-opens the form pre-filled.

## PART M — Modal spend tracking (in-dashboard)

The operator must see Modal spend inside the dashboard — no billing surprises before an RFP or a batch run.
- Record every Modal render job in Postgres: GPU type actually used, wall-clock duration, per-second rate, computed cost estimate; roll up per run / per campaign / current month.
- Dashboard widget (Analytics + Settings): month-to-date estimated Modal spend, free-credit remaining estimate, per-campaign breakdown, cost of last batch; amber warning when nearing a configurable monthly cap (`MODAL_MONTHLY_BUDGET`, default $30).
- `--max-modal-spend` guard on producer runs: estimate before dispatch (clip count × avg cost), refuse to exceed.
- If Modal exposes an official usage/billing API, reconcile estimates against it; otherwise estimates from recorded durations × published rates, labelled "estimated".

## PART I — Trial run harness

- `make healthcheck`: verify Postgres, R2 (write+read test object), Apify token, Postiz, Modal — PASS/FAIL table.
- `make smoke`: one known YouTube URL → finished clip in R2 → visible in Queue. Under a couple minutes.
- Offline fixtures for rank+render (no Apify spend); `--dry-run` (no posting); `--max-apify-spend` guard.
- Debug loop until green: healthcheck clean → smoke green → dry-run batch reviewed → one real post to test channel → enable cron. Fix causes, not symptoms.

## PART J — Definition of done

- Login is a cinematic hero page; installs to phone, opens standalone, glass-on-charcoal.
- Queue shows rising glass panels; tap opens real rendered clip from R2; Approve schedules via Postiz + animates send.
- Memes generate via profile+classifier, same queue, Clips/Memes/All filter, meme aspect.
- Campaigns render real fields (no `[object Object]`), add-campaign works, per-engine toggles work.
- Analytics dark-native with weekly data + Modal spend; Settings restyled, functions intact.
- Renders on Modal (L4/NVENC, scale-to-zero) to R2; VPS holds no video.
- Demo vs production labelling everywhere (dashboard-only, never burned in); `make demo` works.
- Campaign builder creates a campaign end-to-end.
- ~60fps on a mid phone; reduced-motion + AA contrast; no credential in the browser.

*One app, two engines (clips + memes), one cinematic review console.*
