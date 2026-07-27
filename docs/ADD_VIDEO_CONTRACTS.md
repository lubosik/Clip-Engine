# ADD-VIDEO PIPELINE — BINDING CONTRACTS (2026-07-27)

Extends MASTER_SPEC.md + docs/CLIP_QUALITY_FIX_SPEC.md. Any contract change must
update this file in the same commit. Campaign for launch: **peptides** (fitness is
being deleted in the same release).

**The feature:** on a campaign card, "Add video" → paste YouTube URL → "Clip it"
runs the whole video end to end: transcript → identify every clip → render →
critic review → correct-and-re-render (max 2 corrections) → judge → dashboard.

---

## 0. Architecture rules (non-negotiable)

- Sequential pipeline + orchestrator owning state. NOT a swarm.
- **CRITIC ≠ JUDGE.** Critic inspects a rendered clip and returns structured
  failures + correction instructions. It can NEVER set a terminal status.
  Judge runs ONCE per clip after the correction loop ends and returns a
  deterministic decision. Merging them is forbidden.
- **Hard loop bound: max 2 correction iterations (3 render attempts total).**
  After the bound → "Didn't pass" with reasons. Never loops forever, never
  silently disappears.
- **Safety failures are terminal.** Never corrected, never re-rendered.
  (Relaxed checks per `gate.relaxed_safety_checks` stay relaxed — peptides
  relaxes `medical_claims` only.)
- Critic and judge run **zero-context**: they get only the rendered clip
  (frames), its transcript span, and the guidelines/campaign rules. Never the
  clipper's reasoning or prior attempts (except the specific failure being
  corrected, passed as `prior_failures` so the critic can verify the fix).
- Typed schema validation (Pydantic) at every handoff. A stage that receives
  an invalid payload marks the clip/video `failed` with the validation error —
  it never passes malformed data downstream.
- Human approve-before-post stays. Judge only decides what reaches the
  dashboard review queue, never what posts.
- No niche/brand hardcoded. Everything reads campaign config.
- Observability: log every stage input/output summary, timing, spend,
  correction iteration to the video pipeline log file.

## 1. Existing building blocks (REUSE, do not fork)

| Piece | Where | Contract |
|---|---|---|
| Source upsert | `producer/dedupe.py: upsert_source(session, candidate, campaign)` | candidate = {source_id, platform, url, title, author_handle, raw} |
| Campaign row seed | `core/db.py: ensure_campaign(session, name, enabled, config_snapshot)` | call before any FK write |
| Transcript | `producer/transcripts.py: fetch_and_store_transcript(...)` | raises TranscriptFetchError on actor failure |
| Punctuation/sentences | `core/punctuate.py`, Transcript.sentences cache | reuse as in `_process_source` |
| Ranking/segmentation | `core/llm.py: rank_moments(...)` | combined topics+clips call, sentence-index mode |
| Deterministic guards | `producer/boundary_check.py: apply_prefilters, verify_boundaries`, `core/topics.py: clip_within_unit, snap_end_off_next_topic` | pre-render, fail = re-cut before GPU spend |
| Probe (spend guard) | `producer/download.py: probe_youtube(url)` | raises on DRM/unavailable BEFORE LLM spend |
| Download | `producer/download.py: download_source(...)` | |
| Render | `producer/render_dispatch.py: render_and_record(cfg, source_meta, clip_candidate, source_video, words, workdir, *, campaign_name, campaign_mode, session)` | re-render = call again with modified clip_candidate + NEW output keys |
| Gate checks (machinery) | `producer/review_gate.py` phase1/phase2 helpers | critic reuses check machinery; see §3 |
| Stage machine | `producer/run.py: set_source_stage(...)` | stages: queued/transcribing/identifying/rendering/reviewing/correcting/complete/failed (**'correcting' is NEW**, added to allowed values) |
| SSE | `web/api.py: GET /api/sources/stream` | extend payload per §6 |
| Spend ledger | RenderJob rows + `/api/spend` | correction re-renders MUST insert RenderJob rows too |

## 2. Typed handoff contracts — `producer/pipeline_contracts.py` (NEW)

Pydantic v2 models. Every orchestrator stage validates its input/output with
these. `model_validate` errors → clip/video failed state, logged.

```python
class TranscriptPayload(BaseModel):
    source_id: str
    segments: list[Segment]          # Segment: start float, end float, text str (non-empty)
    sentences: list[Sentence] | None # Sentence: text, start, end

class ClipCandidate(BaseModel):
    start: float; end: float         # end > start
    score: float                     # 0..1
    hook: str                        # non-empty
    reason: str = ""
    # carried through corrections:
    attempt: int = 0                 # 0 = first render

class RenderOutcome(BaseModel):
    clip_id: int
    file_path: str                   # r2:// or local path, non-empty when ok
    thumb_path: str
    backend: str; gpu: str | None
    status: Literal["ok", "error"]; error: str | None = None

class CriticFailure(BaseModel):
    phase: Literal["1", "2"]
    check: str
    reason: str                      # plain language, shown to the human
    severity: Literal["correctable", "terminal"]
    correction: Correction | None    # None when terminal / not correctable

class Correction(BaseModel):
    kind: Literal["adjust_start", "adjust_end", "rewrite_hook", "rerender"]
    # adjust_*: delta in whole sentences (int, ±1..3) relative to current bounds
    delta_sentences: int | None = None
    new_hook: str | None = None      # rewrite_hook only
    note: str = ""                   # free-text instruction for the log

class CriticReport(BaseModel):
    clip_id: int
    attempt: int                     # which render attempt was inspected
    failures: list[CriticFailure]    # empty = clean
    formula_score: float | None
    passed: bool                     # convenience: not failures
    # critic NEVER emits a terminal clip status — only this report

class JudgeDecision(BaseModel):
    clip_id: int
    decision: Literal["approved", "rejected", "escalate_to_human"]
    reasons: list[str]               # plain language
    decided_at: str                  # iso8601
```

## 3. Critic — `producer/critic.py` (NEW, wraps existing gate machinery)

`run_critic(clip_row, video_path_or_r2, transcript_segments, campaign_cfg,
session, prior_failures: list[CriticFailure] | None = None) -> CriticReport`

- Internally reuses review_gate's phase-1 (frame extraction + vision LLM) and
  phase-2 (content LLM) helpers. Same checks, same relaxation rules.
- NEW: the phase-2 prompt additionally asks, for each failing check, for a
  machine-usable correction (JSON matching `Correction`). Phase-1 failures map
  to corrections deterministically in code (no extra LLM):
  - `hook_present_in_hook_frame` false → `rerender` (hook drawtext retry)
  - `hook_absent_in_mid_frame` false → `rerender`
  - `captions_present` false → `rerender`
  - `self_contained.ends_on_new_topic` → `adjust_end` (negative delta from the
    critic's reason, default -1 sentence)
  - starts mid-thought → `adjust_start` (default -1 sentence, i.e. extend back)
    or +1 (skip fragment) per critic judgment
  - `hook_body_match` false → `rewrite_hook` with `new_hook`
  - `watermark_visible`/`resolution`/`real_humans`/`animation_detected`/
    `footage_in_focus`/`speaker_centered` false → severity `terminal`
    (re-rendering the same footage cannot fix these) — goes to Didn't pass.
  - safety checks (unrelaxed) → severity `terminal`, ALWAYS.
- Transport/LLM errors → raise `CriticUnavailable` (orchestrator marks clip
  `escalate_to_human` via judge, gate_status 'pending'-equivalent; never crash).
- Zero-context: prompt contains ONLY frames, transcript span, campaign rules,
  and `prior_failures` (so it can confirm a fix). No ranker reasoning.

## 4. Judge — `producer/judge.py` (NEW, pure function, NO LLM)

`judge(report: CriticReport, attempts_used: int, max_corrections: int = 2) -> JudgeDecision`

Deterministic mapping — same input always gives same output:
- report.passed → `approved`
- any failure with severity `terminal` and check in SAFETY set → `rejected`
- any failure with severity `terminal` (non-safety) → `escalate_to_human`
- failures remain and attempts_used > max_corrections → `escalate_to_human`
- (the orchestrator only calls judge when the loop is over: pass, terminal, or
  bound hit — judge runs ONCE per clip)

DB mapping (reuses existing UI):
- `approved` → clip.gate_status='ready', status stays 'pending_review' → Ready queue
- `rejected` → gate_status='didnt_pass', judge reasons prefixed "SAFETY" → Didn't pass (terminal; override button still exists but reasons say safety)
- `escalate_to_human` → gate_status='didnt_pass' → Didn't pass section w/ reasons + attempt count

## 5. Orchestrator — `producer/video_pipeline.py` (NEW)

CLI: `python -m producer.video_pipeline <campaign> <url> [--mode demo|production]
[--max-apify-spend F=2.0] [--max-modal-spend F=3.0]`

`run_video(campaign_name, url, *, run_mode, max_apify_spend, max_modal_spend) -> VideoRunResult`

Stages (source.stage updated at every transition; per-clip state on clip rows):
1. Validate URL (youtube watch/shorts/youtu.be; extract video id) → upsert
   Source (source_id `youtube:<id>`), ensure_campaign, stage `queued`.
   If source exists and status=='done' → refuse ("already exhausted") unless
   `--force`.
2. `transcribing` → probe_youtube (spend guard) → fetch_and_store_transcript →
   punctuate/sentences → TranscriptPayload.
3. `identifying` → rank_moments (ALL clips in the video: max_clips = campaign
   max_clips_per_source, exhaust intent) → deterministic guards
   (apply_prefilters, clip_within_unit, verify_boundaries) → list[ClipCandidate].
   set clips_identified=N.
4. Pre-render spend guard: estimate (N + possible corrections) × avg cost;
   trim N to fit --max-modal-spend. Log what was dropped.
5. `rendering` → download once → per clip: render_and_record → RenderOutcome
   (insert Clip row first w/ gate_status 'pending', correction_attempts=0).
   Render failures: retry once (existing dispatch behavior), else clip failed →
   judge(escalate).
6. Per clip loop (bounded):
   a. `run_critic` → CriticReport (persist to clip.critic_reports append).
   b. If passed OR only-terminal failures OR attempts==2 → judge → persist
      JudgeDecision → terminal state per §4.
   c. Else (correctable failures, attempts < 2): source stage `correcting`;
      apply corrections to ClipCandidate (sentence-index math against the
      sentence spans; re-validate bounds with the deterministic guards);
      correction_attempts += 1; re-render SAME clip row (new R2 keys suffixed
      `_r{attempt}`); goto (a) with prior_failures.
7. `reviewing` while any clip pending human review; when all clips terminal:
   mark source status per exhaust rules (`mark_source_status` — 'done', i.e.
   exhausted, never re-clipped) + stage `complete`. update_used_ranges as in
   _process_source.
8. Any uncaught stage error → stage `failed` + stage_error; every clip left
   non-terminal gets judge(escalate) — NOTHING is left stuck.

Persistence on Clip (migration 007):
```
correction_attempts INTEGER NOT NULL DEFAULT 0
critic_reports      JSONB NULL   -- list[CriticReport.model_dump()], one per attempt
judge_decision      JSONB NULL   -- JudgeDecision.model_dump()
```
Also: extend allowed `sources.stage` values with `correcting` (no column change;
update validators/labels).

Timeout/retry discipline: every LLM call already routes create_completion;
critic/judge stage wraps each external call in try/except with one retry, then
degrades per §3/§4. The subprocess is detached (same pattern as trigger_run);
log file `STORAGE_DIR/logs/video-<campaign>-<video_id>.log`.

## 6. API — `web/api.py`

- `POST /api/campaigns/{name}/videos` (auth) body `{url: str, mode?: str,
  max_apify_spend?: float, max_modal_spend?: float, force?: bool}` →
  validates campaign + URL shape, 409 if source exists with status='done' and
  not force, 422 on bad input; spawns detached
  `python -m producer.video_pipeline ...` (Popen pattern copied from
  trigger_run, caps ALWAYS passed — never uncapped); returns
  `{started: true, source_id, pid, log, caps...}`.
- `GET /api/videos/{source_id}/log` (auth) — tail like runs log (or reuse).
- SSE: `_source_row_to_dict` gains `clips_detail`: for in-progress sources,
  `[{id, gate_status, status, correction_attempts, last_failure_reasons:
  list[str] (plain language, from judge_decision.reasons or latest
  critic_reports failures), judge: str|null}]`. Existing counts stay.
- Existing `/api/sources/stream` cadence/auth unchanged.

## 7. UI — web/static (frontend stream)

- `campaigns.js` `_buildCampaignCard`: new glass button **"Add video"** →
  bottom sheet: URL field + campaign name + one primary button **"Clip it"**
  (+ advanced collapsible: mode/caps; defaults demo? NO — default mode =
  campaign's own mode; peptides is production. Caps default from server).
  POST → toast + jump to Sources view where the live card appears.
- `sources.js`: in-progress card gains per-clip rows when `clips_detail`
  present: stage chips `transcribing → identifying → found N clips →
  rendering (n/N) → reviewing (n/N) → correcting (n) → ready (n/N)`;
  per clip: attempt count ("fix 1/2"), failure reasons in plain language.
  Light-stream progress bar keyed on stage percent (extend `_stagePercent`
  with `correcting`).
- `queue.js`: Didn't-pass cards show judge reasons + correction attempts
  ("2 corrections attempted"). No layout rework.
- `fixtures.js`: REMOVE the fitness mock campaign entirely; add mock
  `clips_detail` on one in-progress source + one didnt_pass clip with
  judge_decision so mock mode exercises the new UI.
- `sw.js` cache v12 → v13. `node --check` on every touched file.
- Mobile-first; 44px targets; reduced-motion respected; AA contrast; same
  cinematic glass system.

## 8. Simulation — `make simulate-pipeline` (scripts/simulate_pipeline.py)

FULLY OFFLINE. Zero network, zero GPU, zero LLM. Monkeypatch-style injection
(same pattern as tests): fake Apify (fixture transcripts from
tests/fixtures/segmentation/*.json), fake probe/download (writes a tiny ffmpeg
testsrc mp4 into workdir — generated locally, no network), fake
render dispatch (copies the tiny mp4, returns RenderOutcome ok), scripted
critic LLM responses per scenario, REAL: contracts validation, deterministic
guards, orchestrator state machine, judge (pure), DB writes to a temp SQLite.

Scenarios (all must pass):
1. Happy path: N clips → all critic-clean → judge approved → gate_status ready.
2. Correctable failure: critic fails `self_contained` w/ adjust_end(-1) →
   re-render → critic passes → approved. Assert correction_attempts==1, bounds
   actually moved to an earlier sentence end, new render happened.
3. Fail twice → third critic still fails → judge escalate_to_human →
   didnt_pass w/ reasons. Assert exactly 2 corrections / 3 renders, no more.
4. Safety terminal: critic returns unrelaxed safety failure on attempt 0 →
   judge rejected IMMEDIATELY (0 corrections, 1 render).
5. Malformed critic output (missing field) → schema validation catches →
   clip escalated, pipeline continues, run terminates cleanly.
6. Stage timeout/exception mid-pipeline → source stage failed, all non-terminal
   clips escalated — nothing stuck in a non-terminal state.
7. Regression: the 4 boundary_failure_pairs cases fed as ranker output (wrong
   bounds) → deterministic guards produce sentence-aligned, non-straddling
   bounds (reuse eval_segmentation assertions a–d).

Global asserts across scenarios: no clip straddles a unit boundary; all bounds
sentence-aligned; correction loop ≤ 2; judge called exactly once per clip and
deterministic (call twice, same output); every clip reaches terminal state;
RenderJob rows recorded for every simulated render. Prints per-scenario PASS/
FAIL + pass rate; exit 0 only when 100%.

## 9. Fitness removal (same release)

- Delete `campaigns/fitness.yaml`, `campaigns/fitness/` dir, `assets/fitness/`,
  fitness entries in `web/static/fixtures.js`.
- Makefile `demo` target → `peptides`.
- Tests: test_config.py fitness tests → rewrite against NEW neutral fixture
  `tests/fixtures/test_campaign.yaml` (copy of old fitness.yaml with name
  `testcamp`, strict_assets=False paths); test_apify_outage_resilience.py:75 and
  test_gate_relaxation.py::test_fitness_yaml_stays_strict → same fixture.
  String-literal "fitness" campaign names in DB-only unit tests may stay
  (arbitrary strings) but rename to "testcamp" where trivial.
- Prod data purge is an OPERATOR-side action (orchestrator runs it, not agents).

## 10. Spend & cost discipline

- Every simulated/real render inserts RenderJob (backend 'local' rate 0 in sim).
- video_pipeline: `--max-modal-spend` checked BEFORE first render batch AND
  before every correction re-render (real accumulated ledger).
- Apify: transcript fetch is the only paid call (~$0.01/video) — guard with
  --max-apify-spend.
- Log per-run totals at completion.
