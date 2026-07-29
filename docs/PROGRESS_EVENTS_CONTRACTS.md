# LIVE PIPELINE PROGRESS EVENTS — BINDING CONTRACTS (2026-07-29)

Extends docs/ADD_VIDEO_CONTRACTS.md. The Sources tab renders pipeline progress
ONLY from these events + DB state. The bar must never lie: events are emitted
at the moment the state transition COMMITS — no optimistic/fake progress.

## 1. Storage: `pipeline_events` table (migration 008)

```
id          BIGSERIAL PK          -- global monotonic; used as SSE Last-Event-ID
source_id   String(512) NOT NULL  -- indexed; NOT an FK (events must survive source deletion cleanly? NO —
                                  -- FK ON DELETE CASCADE so cleared sources drop their events)
clip_id     Integer NULL
stage       String(24) NOT NULL   -- see §2 stage vocabulary
status      String(12) NOT NULL   -- running | done | failed | corrected
progress_n  Integer NULL
progress_total Integer NULL
detail      Text NULL             -- human-readable line (UI shows verbatim)
reason      Text NULL             -- failure/correction reason
created_at  timestamptz NOT NULL
Index: (source_id, id)
```

State lives in sources/clips rows (as today) — events are the notification log.
Emitter writes the state change AND the event row in the SAME commit.

## 2. Event wire schema (v1) — UI renders ONLY from this

SSE `event: progress`, `id: <pipeline_events.id>`, data:
```json
{ "v": 1, "source_id": "youtube:abc", "ts": "iso8601", "stage": "rendering",
  "clip_id": 141, "progress": {"n": 3, "total": 10},
  "status": "running", "detail": "Creating clip 3 of 10 — rendering on Modal",
  "reason": null }
```

Stage vocabulary (exact strings; emitted in pipeline order):
`queued`, `transcribing`, `downloading`, `identifying`, `identified`,
`pre_verify`, `rendering`, `reviewing`, `correction`, `judging`,
`ready`, `didnt_pass`, `complete`, plus any-stage `status:"failed"`.

Emission points (orchestrator producer/video_pipeline.py + emit helper):
- queued: source upserted.
- transcribing: start (running) / done. Cached transcript → done immediately
  with detail "Using cached transcript (N segments)".
- downloading: start; done with file size; yt-dlp→Apify fallback emits
  detail "yt-dlp blocked — downloading via Apify".
- identifying: running with detail "Reading transcript / selecting moments";
  identified: done with progress.total = N candidates surviving guards +
  detail "Found N clips". (Per-candidate identifying events are emitted when
  the ranker output is parsed — one event per accepted candidate.)
- pre_verify: per clip; drop → status failed + reason (clip never renders).
- rendering: per clip start/done, progress {n, total}, detail includes attempt
  suffix "(fix 2/2)" on correction re-renders.
- reviewing: per clip start; pass → done; fail-with-correctable → `correction`
  event with reason + attempt count, then rendering again for THAT clip.
- judging → terminal per clip: `ready` or `didnt_pass` with reason.
- complete: summary detail "X ready · Y didn't pass · source exhausted".
- ANY exception → status failed + real error string on the current stage.

Emitter contract: `emit_event(session, source_id, stage, *, status="running",
clip_id=None, n=None, total=None, detail="", reason=None)` in
`producer/progress_events.py`. Inserts the row; caller owns the commit (emit
inside the same transaction as the state change). NEVER raises (log+swallow).
A module-level reference in video_pipeline (`_pipeline_emit_event`) so
tests/sim can intercept.

The CAMPAIGN producer (producer/run.py) is out of scope this pass — Add-video
flow only (the Sources live view already covers cron runs via stage polling).

## 3. SSE endpoint

`GET /api/sources/{source_id}/events` (auth: ce_session cookie OR Bearer):
- content-type text/event-stream; headers Cache-Control: no-cache,
  X-Accel-Buffering: no.
- On connect: reads `Last-Event-ID` header (or `?last_event_id=`); replays all
  pipeline_events rows with id > last for this source (from DB), then tails:
  poll DB every 2s for new rows (anyio.to_thread like the existing stream),
  `: ping` heartbeat every 15s, hard cap 30 min per connection, `retry: 3000`.
- Event id field = pipeline_events.id → EventSource resumes natively.
- The EXISTING `/api/sources/stream` stays untouched (history/list view).
- `GET /api/sources/{source_id}/events/state` → JSON snapshot for initial render
  + polling fallback: source row fields + clips_detail + last_event_id +
  per-stage elapsed (derived from event rows). Page load renders from THIS,
  then subscribes for deltas.

## 4. UI contract (web/static/sources.js + styles)

- In-progress panel: overall stage line + light-stream bar. Bar percent =
  stage-weighted map (queued 2, transcribing 10, downloading 25, identifying 35,
  identified 40, then 40→95 proportional to terminal clips/total, complete 100).
  CSS transition ~500ms ease-out; reduced-motion: no transition.
- Per-clip chip rail appears at `identified` (total known): chips 1..N,
  micro-states waiting → rendering → reviewing → correcting (attempt n) →
  ready | didn't pass. Correction glyph on corrected chips; amber failed chips
  tap-to-reveal reason. Chips wrap at 380px.
- Live caption line = latest event.detail verbatim. Elapsed time per stage from
  event timestamps; stalled stage shows elapsed since last event/heartbeat.
- Terminal: panel settles into history list with "X ready / Y didn't pass"
  linking to queue sections.
- EventSource with native retry + Last-Event-ID; on error ×3 → fall back to
  polling the state endpoint every 4s (same rendering path).
- sw.js bump (v14→v15); new file additions precached; node --check all.

## 5. Simulation & demo

- simulate-pipeline: the orchestrator's emit reference is intercepted to a
  collector; scenarios assert the EXACT event sequences (incl. correction and
  didnt_pass paths) and that every clip reaches a terminal event; also asserts
  events are written through the real emitter into the sim SQLite DB (so
  Last-Event-ID replay is testable offline).
- `make demo-progress`: scripts/demo_progress.py replays a recorded event
  sequence (fixture JSON) into a local SQLite DB at realistic speed while the
  local API serves it — lets the UI be demoed with zero spend.
- Asserts: mid-run state endpoint reconstructs current stage; replay-from-id
  returns exactly the missed events; UI never shows a stage absent from the
  vocabulary.

## 6. Ranking yield fix (ships in the same release)

Operator's clip definition becomes the mining mandate (config-driven):
- peptides.yaml ranking_rules gains the definition + categories (what-it-does,
  effects, experiences, misconceptions, myths) and "identify EVERY qualifying
  moment — a long podcast typically holds 10-30".
- Add-video path: max_clips scales with duration —
  `min(30, max(cfg.max_clips_per_source, int(duration_min // 4)))`.
- Ranking call max_tokens 4096 → 8000; prompt instructs topics list to stay
  coarse (major segments only).
- LLM-parse failure NO LONGER exhausts the source: rank_moments raises
  `RankingUnavailable` (new) on unparseable-after-retry; orchestrator marks
  stage failed + source retryable (status untouched) — mirrors
  TranscriptFetchError semantics. JSON hardening: retry prompt prefixes
  "Return ONLY the JSON object".
- min_score for peptides: 0.62 → 0.55 (critic still gates quality).
