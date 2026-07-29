"""
producer/critic.py — AI critic for rendered clips (add-video pipeline).

Wraps the existing review_gate phase-1 and phase-2 machinery.  Does NOT
duplicate any prompt text — refactors review_gate to expose reusable helpers.

Contract (docs/ADD_VIDEO_CONTRACTS.md §3):

  run_critic(clip_row, video_path_or_r2, transcript_segments, campaign_cfg,
             session, prior_failures=None) -> CriticReport

Phase 1 (design): reuses review_gate._run_phase1 (frame extraction + vision LLM)
Phase 2 (content): reuses review_gate._run_phase2 with an extended prompt that
  asks, for each failing check, for a machine-usable Correction JSON.

Phase-1 failures map to Corrections deterministically in code (no extra LLM):
  hook_present_in_hook_frame false  → rerender
  hook_absent_in_mid_frame false    → rerender
  captions_present false            → rerender
  self_contained.ends_on_new_topic  → adjust_end (default delta -1)
  starts mid-thought                → adjust_start (default ±1 per critic)
  hook_body_match false             → rewrite_hook with new_hook
  watermark_visible / resolution / real_humans / animation_detected /
  footage_in_focus / speaker_centered false → terminal

Safety checks (unrelaxed) → terminal ALWAYS.

Transport/LLM errors → raise CriticUnavailable (orchestrator escalates via judge).

Zero-context discipline: prompt contains ONLY frames, transcript span, campaign
rules, and prior_failures (for fix-verification). No ranker reasoning.

Test isolation: all external-effect calls are module-level names so tests can
monkeypatch: _critic_run_phase1, _critic_run_phase2.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class CriticUnavailable(Exception):
    """Raised when the critic cannot run due to a transport / LLM error.

    The orchestrator catches this and calls judge(escalate) instead of
    crashing or looping.
    """


# ---------------------------------------------------------------------------
# SAFETY set — checks that are ALWAYS terminal regardless of campaign relaxation.
# These are the exact four safety check keys from the gate.
# "Relaxed checks never reach judge as failures" — when a check is campaign-
# relaxed it is already counted as a PASS by _score_content_verdict, so it
# cannot appear as a failure in a CriticReport.  The SAFETY set below therefore
# includes all four; only unrelaxed triggers will ever be CriticFailures.
# ---------------------------------------------------------------------------

SAFETY_CHECKS: frozenset[str] = frozenset({
    "safety_unsafe_diet_content",
    "safety_medical_claims",
    "safety_harmful_content",
    "safety_guideline_violation",
})

# Phase-1 checks that are ALWAYS terminal (can't be fixed by re-cutting or
# re-rendering with the same source footage).
TERMINAL_VISION_CHECKS: frozenset[str] = frozenset({
    "watermark_visible",
    "resolution",
    "real_humans",
    "animation_detected",
    "footage_in_focus",
    "speaker_centered",
})

# ---------------------------------------------------------------------------
# Module-level references — monkeypatched in tests
# ---------------------------------------------------------------------------

def _critic_run_phase1(
    clip_row: Any,
    video_path_or_r2: str,
    transcript_segments: list[dict] | None,
    campaign_name: str,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """Delegate to review_gate._run_phase1."""
    from producer.review_gate import _run_phase1
    return _run_phase1(clip_row, video_path_or_r2, transcript_segments, campaign_name)


def _critic_run_phase2(
    clip_row: Any,
    transcript_segments: list[dict] | None,
    campaign_cfg: Any,
    phase1_reasons: list[dict[str, Any]],
    preference_context: str = "",
) -> tuple[str, float, list[dict[str, Any]]]:
    """Delegate to review_gate._run_phase2."""
    from producer.review_gate import _run_phase2
    return _run_phase2(
        clip_row, transcript_segments, campaign_cfg, phase1_reasons,
        preference_context=preference_context,
    )


# ---------------------------------------------------------------------------
# Deterministic phase-1 → Correction mapping
# ---------------------------------------------------------------------------

def _correction_for_phase1_failure(
    check: str,
    reason: str,
) -> tuple[str, "Correction | None"]:  # noqa: F821  (forward ref in string)
    """Return (severity, Correction|None) for a phase-1 check failure.

    All phase-1 terminal failures return severity='terminal', correction=None.
    Correctable phase-1 failures return severity='correctable' with an action.
    """
    from producer.pipeline_contracts import Correction

    # These are always terminal — re-rendering the same footage cannot fix them.
    if check in TERMINAL_VISION_CHECKS or check in SAFETY_CHECKS:
        return "terminal", None

    if check in ("hook_present_in_hook_frame", "hook_absent_in_mid_frame", "captions_present"):
        return "correctable", Correction(
            kind="rerender",
            note=f"Phase-1 check '{check}' failed — re-render to fix overlay issue",
        )

    # duration_sanity or resolution already covered above, anything else is terminal
    return "terminal", None


# ---------------------------------------------------------------------------
# Phase-2 → CriticFailure conversion
# ---------------------------------------------------------------------------

def _phase2_reason_to_critic_failure(
    reason: dict[str, Any],
    phase2_extended_verdict: dict[str, Any],
) -> "CriticFailure | None":  # noqa: F821
    """Convert a single phase-2 reason dict to a CriticFailure (or None if passed).

    phase2_extended_verdict: the raw LLM verdict dict, used to extract the
    machine-usable correction for content failures (hook_body_match → new_hook,
    self_contained → adjust_end, etc.).
    """
    from producer.pipeline_contracts import Correction, CriticFailure

    if reason.get("pass") is not False:
        # Not a failure — skip
        return None

    check = reason.get("check", "")
    plain_reason = reason.get("reason", "")
    phase = str(reason.get("phase", "2"))

    # Safety checks → terminal
    if check in SAFETY_CHECKS:
        return CriticFailure(
            phase=phase,  # type: ignore[arg-type]
            check=check,
            reason=plain_reason,
            severity="terminal",
            correction=None,
        )

    # campaign_alignment, topical_relevance, hook_body_match, formula_score_threshold
    # → determine severity and correction from the specific check

    if check == "campaign_alignment":
        return CriticFailure(
            phase="2",
            check=check,
            reason=plain_reason,
            severity="terminal",
            correction=None,
        )

    if check == "topical_relevance":
        return CriticFailure(
            phase="2",
            check=check,
            reason=plain_reason,
            severity="terminal",
            correction=None,
        )

    if check == "hook_body_match":
        # Correctable: rewrite the hook to match the body content.
        # Try to get a suggested hook from the extended verdict; fall back to empty.
        suggested_hook: str | None = None
        hbm_raw = phase2_extended_verdict.get("hook_body_match") or {}
        if isinstance(hbm_raw, dict):
            suggested_hook = hbm_raw.get("suggested_hook") or None
        return CriticFailure(
            phase="2",
            check=check,
            reason=plain_reason,
            severity="correctable",
            correction=Correction(
                kind="rewrite_hook",
                new_hook=suggested_hook,
                note="Hook does not match clip body — rewrite to match opening claim",
            ),
        )

    if check == "self_contained":
        # Correctable: trim the end back (adjust_end -1) when ends_on_new_topic.
        sc_raw = phase2_extended_verdict.get("self_contained") or {}
        ends_on_new = isinstance(sc_raw, dict) and bool(sc_raw.get("ends_on_new_topic"))
        if ends_on_new:
            return CriticFailure(
                phase="2",
                check=check,
                reason=plain_reason,
                severity="correctable",
                correction=Correction(
                    kind="adjust_end",
                    delta_sentences=-1,
                    note="Clip ends on first sentence of new topic — trim by 1 sentence",
                ),
            )
        else:
            # starts mid-thought — correctable by extending start back
            return CriticFailure(
                phase="2",
                check=check,
                reason=plain_reason,
                severity="correctable",
                correction=Correction(
                    kind="adjust_start",
                    delta_sentences=-1,
                    note="Clip starts mid-thought — extend start back by 1 sentence",
                ),
            )

    if check == "formula_score_threshold":
        # Low rubric score — escalate for human review
        return CriticFailure(
            phase="2",
            check=check,
            reason=plain_reason,
            severity="terminal",
            correction=None,
        )

    # Per-rubric score failures (individual pacing/hook_quality etc.) are informational;
    # the formula_score_threshold check captures the aggregate. Skip individual ones.
    if check in (
        "hook_quality", "promise_delivery", "novelty", "pacing",
        "standalone_value", "speaker_engagement", "clean_ending",
        "shareability", "comprehension", "completion_likelihood",
    ):
        return None  # Handled by the aggregate formula_score_threshold failure

    # Unknown check — treat as terminal to be safe
    return CriticFailure(
        phase=phase,  # type: ignore[arg-type]
        check=check,
        reason=plain_reason,
        severity="terminal",
        correction=None,
    )


# ---------------------------------------------------------------------------
# Phase-2 content LLM call with correction JSON request
# ---------------------------------------------------------------------------

def _content_llm_call_with_corrections(
    hook: str,
    transcript_text: str,
    ranking_rules: str,
    next_context: str = "",
    preference_context: str = "",
    stance: str = "",
    prior_failures: list[Any] | None = None,
) -> dict[str, Any]:
    """Call the content LLM with the correction-augmented prompt.

    This is the critic's version of _content_llm_call.  It is identical to the
    gate's version except for two additions:
    1. prior_failures is included so the critic can verify that a previous
       correction actually fixed the problem (zero-context: no ranker reasoning).
    2. The model is asked to emit a machine-usable correction for each failing
       check (JSON matching Correction schema).

    Zero-context discipline: prior_failures contains ONLY the structured failure
    objects from the previous attempt — no ranker reasoning, no clip history.
    """
    try:
        import anthropic  # type: ignore[import]
    except ImportError as exc:
        raise CriticUnavailable("anthropic SDK not available") from exc

    from core.settings import get_settings
    settings = get_settings()
    try:
        api_key, model = settings.require_llm()
    except Exception as exc:
        raise CriticUnavailable(f"LLM not configured: {exc}") from exc

    base_url = settings.llm_base_url
    if base_url is None and api_key.startswith("sk-or-"):
        base_url = "https://openrouter.ai/api"

    client = (
        anthropic.Anthropic(api_key=api_key, base_url=base_url)
        if base_url
        else anthropic.Anthropic(api_key=api_key)
    )

    next_section = (
        f"\n\nWHAT IS SAID IMMEDIATELY AFTER THE CLIP ENDS:\n{next_context}"
        if next_context
        else ""
    )

    pref_section = ""
    if preference_context and preference_context.strip():
        pref_section = f"\n\n{preference_context.strip()}"

    stance_section = ""
    stance_json_field = ""
    if stance and stance.strip():
        stance_section = f"\n\nCAMPAIGN STANCE: {stance.strip()}"
        stance_json_field = ',\n  "campaign_alignment": {"aligned": true, "reason": ""}'

    # Prior failures section — only when this is a correction attempt (attempt >= 1)
    prior_section = ""
    if prior_failures:
        try:
            prior_list = [
                {
                    "check": f.check if hasattr(f, "check") else f.get("check", ""),
                    "reason": f.reason if hasattr(f, "reason") else f.get("reason", ""),
                    "correction_applied": (
                        f.correction.kind if hasattr(f, "correction") and f.correction
                        else (f.get("correction") or {}).get("kind", "none")
                    ),
                }
                for f in prior_failures
            ]
            prior_json = json.dumps(prior_list, indent=2)
            prior_section = (
                f"\n\nPRIOR FAILURES FROM PREVIOUS RENDER (verify these are FIXED):\n"
                f"{prior_json}\n"
                "For each prior failure, confirm whether the correction resolved it."
            )
        except Exception:
            pass  # Non-fatal — proceed without prior context

    prompt = f"""You are a viral short-form content quality analyst and critic.

CLIP HOOK:
{hook or '(no hook)'}

TRANSCRIPT EXCERPT (full spoken content of the clip):
{transcript_text or '(no transcript)'}

CAMPAIGN RANKING RULES:
{ranking_rules or 'Default: prefer useful, interesting, standalone moments.'}{next_section}{pref_section}{stance_section}{prior_section}

HOOK/BODY MATCH CHECK (hard fail): The hook names a SPECIFIC subject or claim. \
Verify the transcript ACTUALLY delivers that subject. Set matches=false and \
provide a suggested_hook if the body doesn't match the hook's promise.

TOPICAL RELEVANCE CHECK (hard fail): The clip must SUBSTANTIVELY discuss a \
campaign-specific topic. Generic advice with a passing mention is not enough.

SELF-CONTAINED BOUNDARY CHECK: ONE complete idea, start to finish. Fail \
(ends_on_new_topic=true) if the clip bleeds into a new topic at the tail.

Score each criterion from 0.0 to 1.0:
1. hook_quality 2. promise_delivery 3. novelty 4. pacing 5. standalone_value
6. speaker_engagement 7. clean_ending 8. shareability 9. comprehension
10. completion_likelihood

SAFETY CHECK — auto-fail if ANY are true:
- unsafe_diet_content: promotes unsafe dieting or disordered eating
- medical_claims: unqualified medical claims (diagnoses, treatments, dosages)
- harmful_content: self-harm, dangerous activities, or hate speech
- guideline_violation: would clearly violate platform community guidelines

Return ONLY this JSON (no prose, no code fences):
{{
  "scores": {{
    "hook_quality": 0.0, "promise_delivery": 0.0, "novelty": 0.0,
    "pacing": 0.0, "standalone_value": 0.0, "speaker_engagement": 0.0,
    "clean_ending": 0.0, "shareability": 0.0, "comprehension": 0.0,
    "completion_likelihood": 0.0
  }},
  "safety": {{
    "unsafe_diet_content": false, "medical_claims": false,
    "harmful_content": false, "guideline_violation": false
  }},
  "self_contained": {{
    "complete_thought": true,
    "ends_on_new_topic": false,
    "reason": "<one line>"
  }},
  "hook_body_match": {{
    "matches": true,
    "reason": "<one line>",
    "suggested_hook": null
  }},
  "topical_relevance": {{
    "on_topic": true,
    "reason": "<one line>"
  }}{stance_json_field}
}}"""

    from core.llm import create_completion, extract_text
    from producer.review_gate import _parse_json_object

    # 512 tokens truncated the critic's extended verdict JSON mid-object on
    # every real clip (2026-07-29 first live run) — the correction fields make
    # this payload much larger than the original gate verdict. 2000 gives
    # ample headroom; one retry covers transient malformed output.
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            message = create_completion(
                client, model, 2000, [{"role": "user", "content": prompt}]
            )
            raw = extract_text(message)
            log.debug(
                "Critic content LLM raw response (len=%d): %s", len(raw), raw[:300]
            )
            return _parse_json_object(raw)
        except Exception as exc:
            last_exc = exc
            log.warning(
                "Critic content LLM attempt %d failed: %s", attempt + 1, exc
            )
    raise CriticUnavailable(f"Content LLM transport error: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def run_critic(
    clip_row: Any,
    video_path_or_r2: str,
    transcript_segments: list[dict] | None,
    campaign_cfg: Any,
    session: Any,
    prior_failures: list[Any] | None = None,
) -> "CriticReport":  # noqa: F821
    """Run the two-phase AI critic on a rendered clip.

    Args:
        clip_row:             SQLAlchemy Clip ORM row (must have .id, .hook,
                              .start, .end, .campaign, .correction_attempts).
        video_path_or_r2:     Local path or 'r2://...' key to the rendered mp4.
        transcript_segments:  Segment list from Transcript.segments (may be None).
        campaign_cfg:         CampaignConfig for ranking_rules and campaign name.
        session:              Active SQLAlchemy session (read-only in this call).
        prior_failures:       list[CriticFailure] from the previous attempt so
                              the critic can verify the correction was applied.
                              Zero-context: this is the ONLY prior-attempt data
                              passed — no ranker reasoning.

    Returns:
        CriticReport

    Raises:
        CriticUnavailable: on any transport / LLM error.
    """
    from producer.pipeline_contracts import (
        CriticFailure as _CriticFailure,
        CriticReport as _CriticReport,
        Correction as _Correction,
    )

    clip_id = getattr(clip_row, "id", 0) or 0
    attempt = getattr(clip_row, "correction_attempts", 0) or 0
    campaign_name = getattr(clip_row, "campaign", "") or ""

    log.info(
        "run_critic: clip_id=%s attempt=%d campaign=%s",
        clip_id, attempt, campaign_name,
    )

    failures: list[_CriticFailure] = []
    formula_score: float | None = None

    # ── Phase 1: design ────────────────────────────────────────────────────────
    try:
        phase1_reasons, phase1_passed, probe_data = _critic_run_phase1(
            clip_row, video_path_or_r2, transcript_segments, campaign_name
        )
    except Exception as exc:
        raise CriticUnavailable(f"Phase 1 transport error: {exc}") from exc

    # Convert phase-1 failures to CriticFailures with deterministic corrections
    for reason in phase1_reasons:
        if reason.get("pass") is False:
            check = reason.get("check", "")
            severity, correction = _correction_for_phase1_failure(
                check, reason.get("reason", "")
            )
            failures.append(
                _CriticFailure(
                    phase="1",
                    check=check,
                    reason=reason.get("reason", check),
                    severity=severity,
                    correction=correction,
                )
            )

    if not phase1_passed:
        log.info(
            "run_critic: clip_id=%s phase 1 failed (%d failures); skipping phase 2",
            clip_id, len(failures),
        )
        return _CriticReport(
            clip_id=clip_id,
            attempt=attempt,
            failures=failures,
            formula_score=None,
            passed=len(failures) == 0,
        )

    # ── Phase 2: content (with correction-augmented prompt) ───────────────────
    hook = getattr(clip_row, "hook", "") or ""
    start = getattr(clip_row, "start", None)
    end = getattr(clip_row, "end", None)
    ranking_rules = ""
    try:
        ranking_rules = campaign_cfg.ranking.ranking_rules or ""
    except Exception:
        pass

    relaxed_checks: list[str] = []
    try:
        relaxed_checks = list(campaign_cfg.gate.relaxed_safety_checks or [])
    except Exception:
        pass

    stance = ""
    try:
        stance = str(campaign_cfg.ranking.stance or "")
    except Exception:
        pass

    from producer.review_gate import (
        _build_transcript_slice,
        _build_lookahead_slice,
        _score_content_verdict,
    )
    transcript_text = _build_transcript_slice(transcript_segments, start, end)
    next_context = _build_lookahead_slice(transcript_segments, end)

    preference_context = ""  # zero-context: never inject ranker preference here

    try:
        verdict = _content_llm_call_with_corrections(
            hook,
            transcript_text,
            ranking_rules,
            next_context=next_context,
            preference_context=preference_context,
            stance=stance,
            prior_failures=prior_failures,
        )
    except CriticUnavailable:
        raise
    except Exception as exc:
        raise CriticUnavailable(f"Phase 2 content call error: {exc}") from exc

    formula_score, content_reasons = _score_content_verdict(
        verdict, relaxed_safety_checks=relaxed_checks
    )

    # Convert content failures to CriticFailures
    for reason in content_reasons:
        cf = _phase2_reason_to_critic_failure(reason, verdict)
        if cf is not None:
            failures.append(cf)

    log.info(
        "run_critic: clip_id=%s attempt=%d formula_score=%.3f failures=%d",
        clip_id, attempt, formula_score or 0.0, len(failures),
    )

    return _CriticReport(
        clip_id=clip_id,
        attempt=attempt,
        failures=failures,
        formula_score=formula_score,
        passed=len(failures) == 0,
    )
