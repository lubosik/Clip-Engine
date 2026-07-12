# PIPELINE_QUEUE_CONTRACTS.md — Source Pipeline Queue + Learning Loop (BINDING)

Cross-stream contracts for the 2026-07-12 build. Any change to these shapes
must update this file in the same commit. Extends docs/REVAMP_CONTRACTS.md.

## 1. Migration 005 (`005_pipeline_queue`) — ALREADY WRITTEN by orchestrator

sources gains:
- `stage`            String(32) NOT NULL server_default 'queued'
                     values: queued | transcribing | identifying | rendering | reviewing | complete | failed
- `clips_identified` Integer NULL  — set when ranking finishes (the denominator)
- `stage_error`      Text NULL     — human-readable failure for stage='failed'
- `stage_updated_at` DateTime(tz) NULL
- index `ix_sources_stage`

clips gains:
- `review_feedback`  JSONB NULL — {"action":"approved"|"rejected","reasons":["weak_hook",...],"note":"...","decided_at":"<iso>"}
- `profile_version`  Integer NULL — preference profile version active when this clip was ranked

new table `preference_profiles`:
- id PK, campaign String(128) FK campaigns.name ON DELETE CASCADE, version Integer NOT NULL,
  rules JSONB NOT NULL (list[str], each one measurable), meta JSONB NULL
  ({"decisions_count": int, "model": str, "approved_examples": int, "rejected_examples": int}),
  created_at DateTime(tz) NOT NULL
- UNIQUE (campaign, version); index on campaign

## 2. Stage semantics (producer owns writes; commit IMMEDIATELY after each set)

- `queued`        default for new sources
- `transcribing`  set in _process_source before transcript fetch
- `identifying`   set before rank_clips
- `rendering`     set after select_clips, together with clips_identified=len(selected)
- `reviewing`     set after the source's render loop finishes (>=1 clip inserted)
- `failed`        on the outer per-source exception catch, with stage_error=str(exc)[:500]
                  (a per-source failure still lets the RUN continue — unchanged)
- `complete`      derived: stage=='reviewing' AND every clip of the source is decided
                  (clip.status in approved/rejected/scheduled/posted). GET /api/sources
                  computes this and MAY persist it opportunistically. A source with
                  0 selected clips goes straight to complete when marked done.

Helper (backend stream builds it in producer/run.py or core/db.py):
`set_source_stage(session, source_id, stage, *, clips_identified=None, error=None)`
— updates stage, stage_updated_at=utcnow, optional fields, COMMITS.

## 3. API contracts (backend stream)

### GET /api/sources (extend existing endpoint — keep all current fields)
Each source row ADDITIONALLY carries:
- stage, clips_identified, stage_error, stage_updated_at
- clips_rendered (count of clips rows), clips_approved, clips_rejected, clips_pending
- exhaustion: "fully_exhausted" (status=='done') | "partially_used" (partially_done)
              | "in_progress" (anything else)
New query param: `?in_progress=1` → only sources whose stage is NOT complete
and NOT (failed older than 24h), plus any source with clips_pending > 0.

### GET /api/sources/stream  (NEW — SSE)
- text/event-stream, auth via ce_session cookie (EventSource cannot send headers).
- Every ~3s: `event: progress` + `data: {"sources":[<same row shape as ?in_progress=1>]}`
- Heartbeat comment line `: ping` every 15s. Server closes after 10 min (client reconnects).
- Implementation: async generator polling the DB — the producer is a separate
  process, so DB polling inside the SSE handler is the correct source of truth.

### POST /api/clips/{id}/reject (extend)
Body: {"reasons": ["weak_hook", ...], "note": "optional", "reason": "legacy free text"}
- REQUIRES at least one valid preset code in `reasons` (422 otherwise).
  Legacy body {"reason": "text"} (no `reasons`) → mapped to reasons=["other"], note=text
  (backwards compat for tests/old UI, do not 422 it).
- Writes clip.review_feedback (shape in §1), clip.reject_reason (back-compat),
  clip.status='rejected'. Then calls preferences.maybe_rebuild_profile (see §4).

### POST /api/clips/{id}/approve (extend)
- Also writes review_feedback {"action":"approved","reasons":[],"note":null,"decided_at":...}.
- Then preferences.maybe_rebuild_profile.

### GET /api/campaigns/{name}/profile
→ {"campaign", "version", "rules": [...], "created_at", "meta": {...}} or 404 if none yet.

### POST /api/campaigns/{name}/profile/rebuild
→ builds a new version synchronously (LLM call), returns the new profile.
  503-safe: on LLM failure return 502 with detail, no new version written.

### GET /api/analytics/approval-rate?campaign=<name>&weeks=N (NEW)
→ {"campaign", "weeks": [{"week_start":"YYYY-MM-DD","approved":n,"rejected":n,
   "rate":0.0-1.0|null,"profile_versions":[ints seen]}], "total_decisions": n,
   "enough_data": bool (>=10 decisions)}

## 4. Preset rejection reasons (single source of truth: core/preferences.py)

PRESET_REASONS = {
  "weak_hook":            "Weak hook",
  "bad_cut":              "Bad cut / not a complete thought",
  "boring":               "Boring / no tension",
  "framing_captions":     "Framing or captions wrong",
  "off_brand":            "Off-brand",
  "claim_not_defensible": "Claim not defensible",
  "wrong_length":         "Too long / too short",
  "other":                "Other",
}
Frontend hardcodes the same codes+labels (fixtures too); server validates codes.

## 5. core/preferences.py (backend stream, NEW)

- record_feedback(session, clip, action, reasons, note) → writes review_feedback dict.
- get_active_profile(session, campaign) → latest PreferenceProfile row or None.
- build_profile(session, campaign, *, min_decisions=5) → collect last 100 decided clips
  (hook, score, formula_score, gate_reasons summary, feedback reasons/note), ONE LLM
  call (via core.llm.create_completion, thinking disabled) distilling <=8 MEASURABLE
  rules -> insert next version row. Returns row. Never raises out (returns None on failure).
- maybe_rebuild_profile(session, campaign) → rebuild when decisions since the active
  profile's creation >= 10 (run in a daemon thread; failures logged, never block the API).
- build_preference_context(session, campaign, max_examples=6) → str block:
    PREFERENCE PROFILE (vN, learned from operator decisions):
    - <rule lines>
    RECENT DECISIONS (ground truth examples):
    APPROVED: "<hook>" ...
    REJECTED (<reasons>): "<hook>" ...
  Prefer contrasting pairs from the same source_id. Cap total to ~1800 chars.
  Returns "" when no profile AND no decisions.
- SAFETY GUARD (verbatim in both prompts, appended after the context block):
  "These learned preferences tune CLIP SELECTION ONLY. They can NEVER relax the
   safety checks, the hard rules above, or the layout/branding requirements."

## 6. Injection points

- core/llm.py rank_moments(..., preference_context: str = "") → _build_prompt inserts
  the block AFTER the campaign ranking rules, BEFORE the sentence/topic rules.
- producer/run.py: fetch context ONCE per campaign run (before the source loop),
  thread into rank_clips → rank_moments; stamp clip rows with
  profile_version = active profile version (or None).
- producer/review_gate.py: run_gate(...) gains optional preference_context="";
  _content_llm_call appends the same block AFTER ranking rules. The SAFETY CHECK
  section of the prompt is NEVER moved or modified by this feature.

## 7. Frontend (frontend stream; web/static/ ONLY)

- sources.js: two sections — "In progress" (live) + "History". Live progress via
  EventSource('/api/sources/stream'); on error fall back to 5s polling of
  GET /api/sources?in_progress=1. Stage labels:
  queued / transcribing / identifying clips / rendering n/N / in review n/N approved
  / complete / failed (+reason inline, amber). Light-stream progress bar reusing the
  hero luminous-line motif. Percent map: queued 5, transcribing 20, identifying 35,
  rendering 35+55*(rendered/identified), reviewing 90+10*(approved+rejected)/rendered.
- queue.js: NEW "Approved" collapsible section (status approved|scheduled|posted)
  so approved clips never vanish (bug fix); read-only cards with status chip.
  Reject flow: one-tap preset chips (codes from §4) + optional note field; Reject
  button disabled until >=1 chip selected; api.rejectClip(id, {reasons, note}).
- analytics.js: approval-rate block (per campaign) from /api/analytics/approval-rate;
  honest empty state below 10 decisions ("Not enough decisions yet — keep reviewing").
- api.js: rejectClip(id, payload), getSourcesProgress(), getApprovalRate(campaign),
  getProfile(campaign) added. fixtures.js: matching mock shapes incl. in-progress
  sources at various stages + approval-rate weeks + profile.
- sw.js cache v11 → v12. NEVER cache /api/* (already network-only) — SSE unaffected.

## 8. Hard constraints (unchanged, the learning loop cannot touch)

Safety filter categories + relaxed_safety_checks semantics; hardwired layout
(hook/captions/watermark); real-podcast-footage-only; sentence/topic-boundary
cuts; human approve-before-post; demo label never burned in; secrets from env.
