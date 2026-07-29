"""
producer/video_pipeline.py — Add-video pipeline orchestrator.

CLI:
    python -m producer.video_pipeline <campaign> <url>
        [--mode demo|production]
        [--max-apify-spend FLOAT=2.0]
        [--max-modal-spend FLOAT=3.0]
        [--force]

Contract: docs/ADD_VIDEO_CONTRACTS.md §5.

Pipeline stages (source.stage updated at each transition):
  1. queued        → Validate URL, upsert Source, ensure_campaign.
  2. transcribing  → probe_youtube (spend guard), fetch_and_store_transcript,
                     punctuate/sentences.
  3. identifying   → rank_moments (ALL clips, exhaust intent), deterministic
                     guards (apply_prefilters, clip_within_unit, verify_boundaries).
  4. Pre-render spend guard → trim clip list.
  5. rendering     → download_source once, render_and_record per clip.
  6. Per-clip loop (bounded — max 2 corrections):
       a. run_critic → CriticReport (appended to clip.critic_reports).
       b. pass OR terminal OR attempts==2 → judge → terminal state per §4.
       c. correctable + attempts < 2 → source stage 'correcting',
          apply corrections, correction_attempts += 1, re-render, goto (a).
  7. reviewing     → when any clip pending human review.
     complete      → when all clips reached terminal state.
     mark source done + update_used_ranges.
  8. Any uncaught error → stage='failed'; every non-terminal clip gets judge(escalate).

Module-level external-effect references (monkeypatchable in tests/harness):
  _pipeline_upsert_source        → producer.dedupe.upsert_source
  _pipeline_ensure_campaign      → core.db.ensure_campaign
  _pipeline_fetch_transcript     → producer.transcripts.fetch_and_store_transcript
  _pipeline_probe_youtube        → producer.download.probe_youtube
  _pipeline_download_source      → producer.download.download_source
  _pipeline_render_and_record    → producer.render_dispatch.render_and_record
  _pipeline_run_critic           → producer.critic.run_critic
  _pipeline_rank_moments         → core.llm.rank_moments
  _pipeline_emit_event           → producer.progress_events.emit_event
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.logging import configure_logging

configure_logging()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Maximum correction iterations per clip (hard bound from contract §0)
# ---------------------------------------------------------------------------
MAX_CORRECTIONS: int = 2  # → 3 render attempts max

# ---------------------------------------------------------------------------
# YouTube URL validation
# ---------------------------------------------------------------------------

_YT_PATTERNS = [
    # Standard watch URL: https://www.youtube.com/watch?v=<id>
    re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/watch\?.*v=([A-Za-z0-9_-]{11})"),
    # Shorts: https://www.youtube.com/shorts/<id>
    re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/shorts/([A-Za-z0-9_-]{11})"),
    # Short link: https://youtu.be/<id>
    re.compile(r"(?:https?://)?youtu\.be/([A-Za-z0-9_-]{11})"),
]


def _extract_youtube_id(url: str) -> str | None:
    """Extract the 11-character YouTube video ID from a URL.

    Returns None if the URL does not match any known YouTube pattern.
    """
    for pattern in _YT_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def _validate_youtube_url(url: str) -> str:
    """Validate and normalise a YouTube URL.

    Returns the canonical video ID on success.
    Raises ValueError with a descriptive message on invalid URLs.
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")
    vid_id = _extract_youtube_id(url.strip())
    if vid_id is None:
        raise ValueError(
            f"URL does not look like a YouTube watch/shorts/youtu.be link: {url!r}"
        )
    return vid_id


# ---------------------------------------------------------------------------
# Module-level external-effect references (monkeypatched by tests/harness)
# ---------------------------------------------------------------------------

def run_modal_spend(session: Any, campaign: str, since: Any) -> float:
    """Sum of Modal render costs recorded for `campaign` since `since` (UTC).

    Per-RUN spend for the --max-modal-spend guard — NOT month-to-date, which
    conflates this run with the whole ledger (2026-07-29 bug: guard compared
    MTD to the per-run cap, so corrections would be refused forever once the
    month passed the cap).
    """
    try:
        from sqlalchemy import func

        from core.models import RenderJob

        total = (
            session.query(func.coalesce(func.sum(RenderJob.cost_estimate), 0.0))
            .filter(
                RenderJob.campaign == campaign,
                RenderJob.backend == "modal",
                RenderJob.created_at >= since,
            )
            .scalar()
        )
        return float(total or 0.0)
    except Exception:
        return 0.0


# Chunked ranking: over ~this many minutes, one giant ranking prompt dilutes
# the model's attention and yield collapses (168-min lecture → 4 raw
# candidates on 2026-07-29 while a 130-min podcast got 19). Mine long
# transcripts in windows so each section gets full attention.
_RANK_CHUNK_MINUTES = 45.0
_RANK_CHUNK_OVERLAP_MINUTES = 3.0
_RANK_CHUNK_THRESHOLD_MINUTES = 75.0


def _rank_in_chunks(
    segments: list[dict],
    ranking_cfg: Any,
    *,
    sentence_spans: list[dict] | None,
    preference_context: str = "",
    emit: Any = None,
) -> list[dict]:
    """Rank a transcript, windowing long ones into ~45-min chunks.

    Short transcripts (< _RANK_CHUNK_THRESHOLD_MINUTES) go through one call,
    exactly as before. Long ones are mined per-window (3-min overlap so a
    moment straddling a boundary is seen whole by one window), results merged
    and de-duplicated by >50% time overlap (higher score wins). Chunk-level
    RankingUnavailable degrades to skipping that chunk unless EVERY chunk
    fails, which re-raises.
    """
    duration_s = segments[-1]["end"] if segments else 0.0
    if duration_s / 60.0 < _RANK_CHUNK_THRESHOLD_MINUTES:
        return _pipeline_rank_moments(
            segments, ranking_cfg,
            sentence_spans=sentence_spans, preference_context=preference_context,
        )

    from core.llm import RankingUnavailable as _RU

    chunk_s = _RANK_CHUNK_MINUTES * 60.0
    overlap_s = _RANK_CHUNK_OVERLAP_MINUTES * 60.0
    starts: list[float] = []
    t = 0.0
    while t < duration_s:
        starts.append(t)
        t += chunk_s - overlap_s

    merged: list[dict] = []
    failures = 0
    for i, win_start in enumerate(starts, 1):
        win_end = min(win_start + chunk_s, duration_s)
        segs_w = [s for s in segments if s["end"] > win_start and s["start"] < win_end]
        spans_w = (
            [sp for sp in sentence_spans if sp["end"] > win_start and sp["start"] < win_end]
            if sentence_spans else None
        )
        if not segs_w:
            continue
        if emit is not None:
            try:
                emit(
                    f"Scanning section {i} of {len(starts)} "
                    f"({int(win_start // 60)}:{int(win_start % 60):02d}"
                    f"–{int(win_end // 60)}:{int(win_end % 60):02d})"
                )
            except Exception:
                pass
        try:
            cands = _pipeline_rank_moments(
                segs_w, ranking_cfg,
                sentence_spans=spans_w, preference_context=preference_context,
            )
        except _RU as exc:
            log.warning("chunk %d/%d ranking unavailable: %s", i, len(starts), exc)
            failures += 1
            continue
        merged.extend(cands or [])

    if failures and failures == len(starts):
        raise _RU(f"ranking unavailable for all {len(starts)} chunks")

    # De-dup overlapping candidates from window overlap: keep higher score.
    merged.sort(key=lambda c: -(c.get("score") or 0.0))
    kept: list[dict] = []
    for c in merged:
        dup = False
        for k in kept:
            ov = min(c["end"], k["end"]) - max(c["start"], k["start"])
            shorter = min(c["end"] - c["start"], k["end"] - k["start"])
            if shorter > 0 and ov / shorter > 0.5:
                dup = True
                break
        if not dup:
            kept.append(c)
    kept.sort(key=lambda c: c["start"])
    log.info(
        "chunked ranking: %d windows -> %d raw, %d after overlap de-dup",
        len(starts), len(merged), len(kept),
    )
    return kept


def _pipeline_upsert_source(session: Any, candidate: dict, campaign_name: str) -> Any:
    from producer.dedupe import upsert_source
    return upsert_source(session, candidate, campaign_name)


def _pipeline_ensure_campaign(session: Any, name: str, enabled: bool, config_snapshot: Any) -> Any:
    from core.db import ensure_campaign
    # ensure_campaign's enabled/config_snapshot are keyword-only.
    return ensure_campaign(session, name, enabled=enabled, config_snapshot=config_snapshot)


def _pipeline_fetch_transcript(
    session: Any, source_id: str, platform: str, url: str, apify: Any, campaign: str
) -> list[dict]:
    from producer.transcripts import fetch_and_store_transcript
    return fetch_and_store_transcript(
        session=session,
        source_id=source_id,
        platform=platform,
        url=url,
        apify=apify,
        campaign=campaign,
    )


def _pipeline_probe_youtube(url: str) -> None:
    from producer.download import probe_youtube
    probe_youtube(url)


def _pipeline_download_source(
    source_id: str,
    platform: str,
    url: str,
    raw: dict,
    campaign: str | None = None,
    on_event: Any = None,
) -> str:
    from producer.download import download_source
    return download_source(
        source_id=source_id, platform=platform, url=url, raw=raw,
        campaign=campaign, on_event=on_event,
    )


def _pipeline_render_and_record(
    cfg: Any,
    source_meta: dict,
    clip_candidate: dict,
    source_video: Path,
    workdir: Path,
    *,
    campaign_name: str,
    campaign_mode: str,
    session: Any,
) -> Any:
    from producer.render_dispatch import render_and_record
    return render_and_record(
        cfg=cfg,
        source_meta=source_meta,
        clip_candidate=clip_candidate,
        source_video=source_video,
        words=None,
        workdir=workdir,
        campaign_name=campaign_name,
        campaign_mode=campaign_mode,
        session=session,
    )


def _pipeline_run_critic(
    clip_row: Any,
    video_path_or_r2: str,
    transcript_segments: list[dict] | None,
    campaign_cfg: Any,
    session: Any,
    prior_failures: list | None = None,
) -> Any:
    from producer.critic import run_critic
    return run_critic(
        clip_row=clip_row,
        video_path_or_r2=video_path_or_r2,
        transcript_segments=transcript_segments,
        campaign_cfg=campaign_cfg,
        session=session,
        prior_failures=prior_failures,
    )


def _pipeline_rank_moments(
    transcript: list[dict],
    ranking_cfg: Any,
    *,
    sentence_spans: list[dict] | None = None,
    preference_context: str = "",
) -> list[dict]:
    # Route through ranker.rank_clips — the same wrapper the campaign producer
    # uses — so stance + §R2.3 speaker-turn prefilters apply here too.
    from producer.ranker import rank_clips
    return rank_clips(
        transcript,
        None,
        ranking_cfg,
        preference_context=preference_context,
        sentence_spans=sentence_spans,
    )


def _pipeline_emit_event(
    session: Any,
    source_id: str,
    stage: str,
    *,
    status: str = "running",
    clip_id: int | None = None,
    n: int | None = None,
    total: int | None = None,
    detail: str = "",
    reason: str | None = None,
) -> None:
    """Module-level wrapper for producer.progress_events.emit_event.

    Monkeypatchable by tests so emission points can be intercepted without
    touching the real DB.  Never raises (emitter contract).
    """
    from producer.progress_events import emit_event
    emit_event(
        session, source_id, stage,
        status=status, clip_id=clip_id, n=n, total=total,
        detail=detail, reason=reason,
    )


# ---------------------------------------------------------------------------
# Sentence-index math for corrections
# ---------------------------------------------------------------------------

def _apply_correction_to_candidate(
    candidate: dict,
    correction: Any,
    sentence_spans: list[dict],
    clip_len: tuple[float, float],
) -> dict:
    """Apply a single Correction to a ClipCandidate dict.

    Uses sentence-index math: finds the sentence index of start/end, adds the
    delta, then maps back to float times.

    Returns a new dict (original unmodified).
    """
    from core.hook_style import enforce_hook_style

    updated = dict(candidate)

    if correction is None:
        return updated

    kind = (
        correction.get("kind") if isinstance(correction, dict)
        else getattr(correction, "kind", None)
    )

    if kind in ("adjust_start", "adjust_end") and sentence_spans:
        delta = (
            getattr(correction, "delta_sentences", 0)
            or (correction.get("delta_sentences", 0) if isinstance(correction, dict) else 0)
        ) or 0

        start_t = float(updated.get("start", 0))
        end_t = float(updated.get("end", 0))

        # Find sentence indices
        n = len(sentence_spans)

        if kind == "adjust_start" and delta != 0:
            # Find current start sentence index
            si = 0
            for i, span in enumerate(sentence_spans):
                if float(span["start"]) <= start_t:
                    si = i
            # Apply delta (negative = extend back, positive = skip forward)
            new_si = max(0, min(n - 1, si + delta))
            updated["start"] = float(sentence_spans[new_si]["start"])

        elif kind == "adjust_end" and delta != 0:
            # Find current end sentence index
            ei = n - 1
            for i, span in enumerate(sentence_spans):
                if float(span["end"]) >= end_t:
                    ei = i
                    break
            new_ei = max(0, min(n - 1, ei + delta))
            updated["end"] = float(sentence_spans[new_ei]["end"])

        # Enforce clip_len constraints
        dur = updated["end"] - updated["start"]
        if dur < clip_len[0] and len(sentence_spans) > 0:
            # Need to extend end
            ei = n - 1
            for i, span in enumerate(sentence_spans):
                if float(span["end"]) >= updated["end"]:
                    ei = i
                    break
            while dur < clip_len[0] and ei < n - 1:
                ei += 1
                updated["end"] = float(sentence_spans[ei]["end"])
                dur = updated["end"] - updated["start"]

        if dur > clip_len[1]:
            # Trim end
            target_end = updated["start"] + clip_len[1]
            ei = n - 1
            for i, span in enumerate(sentence_spans):
                if float(span["end"]) <= target_end:
                    ei = i
            updated["end"] = float(sentence_spans[ei]["end"])

    elif kind == "rewrite_hook":
        new_hook = (
            getattr(correction, "new_hook", None)
            or (correction.get("new_hook") if isinstance(correction, dict) else None)
        )
        if new_hook:
            try:
                new_hook = enforce_hook_style(new_hook)
            except Exception:
                pass
            updated["hook"] = new_hook

    # kind == "rerender": no structural change, just re-render with same candidate
    return updated


def _r2_key_with_attempt_suffix(original_key: str, attempt: int) -> str:
    """Append _r{attempt} suffix to an R2 key before the extension."""
    if "." in original_key.split("/")[-1]:
        # Has extension — insert suffix before extension
        base, ext = original_key.rsplit(".", 1)
        return f"{base}_r{attempt}.{ext}"
    return f"{original_key}_r{attempt}"


# ---------------------------------------------------------------------------
# Per-clip correction loop
# ---------------------------------------------------------------------------

def _run_clip_loop(
    clip_row: Any,
    initial_file_path: str,
    initial_thumb_path: str,
    candidate: dict,
    sentence_spans: list[dict],
    transcript_segments: list[dict] | None,
    campaign_cfg: Any,
    source_meta: dict,
    source_video_path: str,
    workdir: Path,
    run_mode: str,
    session: Any,
    max_modal_spend: float | None,
    run_start: Any = None,
    # Progress event support — passed through from run_video
    _source_id: str = "",
    _clip_idx: int = 0,
    _total_clips: int = 1,
) -> None:
    """Run the critic→correct→re-render loop for a single clip.

    Updates clip_row in-place (correction_attempts, critic_reports, judge_decision,
    gate_status, gate_reasons, file_path, thumb_path, start, end, hook).
    Does NOT commit — caller commits after each clip.

    Contract:
    - max 2 corrections (3 render attempts total)
    - judge called exactly once per clip
    - every clip reaches a terminal gate_status on return
    - safety failures are immediately terminal (no correction)
    """
    from producer.critic import CriticUnavailable
    from producer.judge import judge, apply_judge_to_clip, SAFETY_SET
    from producer.pipeline_contracts import CriticReport, CriticFailure

    clip_id = clip_row.id
    campaign_name = campaign_cfg.name
    clip_len = (
        float(campaign_cfg.ranking.clip_length[0]),
        float(campaign_cfg.ranking.clip_length[1]),
    )

    current_file_path = initial_file_path
    current_thumb_path = initial_thumb_path
    current_candidate = dict(candidate)

    prior_failures: list[CriticFailure] = []
    critic_reports_list: list[dict] = list(
        getattr(clip_row, "critic_reports") or []
    )

    while True:
        attempt = getattr(clip_row, "correction_attempts", 0) or 0

        # Pre-loop spend guard (before correction re-render, attempt >= 1)
        if attempt > 0 and max_modal_spend is not None:
            try:
                from producer.render_dispatch import estimate_modal_batch_cost
                correction_cost = estimate_modal_batch_cost(1, session)
                campaign_name = getattr(clip_row, "campaign", "") or ""
                spent = (
                    run_modal_spend(session, campaign_name, run_start)
                    if run_start is not None else 0.0
                )
                if spent + correction_cost > max_modal_spend:
                    log.warning(
                        "_run_clip_loop: spend guard hit before correction re-render "
                        "clip_id=%s attempt=%d run_spend=%.4f correction_cost=%.4f "
                        "max=%.2f -> escalating",
                        clip_id, attempt, spent, correction_cost, max_modal_spend,
                    )
                    # Force judge(escalate) via a synthetic terminal failure
                    synthetic_report = CriticReport(
                        clip_id=clip_id,
                        attempt=attempt,
                        failures=[
                            CriticFailure(
                                phase="2",
                                check="spend_guard",
                                reason="Modal spend cap reached before correction re-render",
                                severity="terminal",
                                correction=None,
                            )
                        ],
                        formula_score=None,
                        passed=False,
                    )
                    decision = judge(synthetic_report, attempt, MAX_CORRECTIONS)
                    apply_judge_to_clip(clip_row, decision, session)
                    return
            except Exception as guard_exc:
                log.warning("Spend guard check failed (non-fatal): %s", guard_exc)

        # ── Run critic ────────────────────────────────────────────────────────
        try:
            report = _pipeline_run_critic(
                clip_row,
                current_file_path,
                transcript_segments,
                campaign_cfg,
                session,
                prior_failures=prior_failures if prior_failures else None,
            )
        except Exception as exc:
            # Catches CriticUnavailable (transport/LLM errors) AND any other
            # unexpected exception from the critic (e.g. pydantic.ValidationError
            # from malformed output — §8 scenario 5).  Per contract: validation
            # failures → clip escalated, never left in a non-terminal state.
            if isinstance(exc, CriticUnavailable):
                log.warning(
                    "_run_clip_loop: CriticUnavailable clip_id=%s attempt=%d: %s "
                    "-> judge(escalate)",
                    clip_id, attempt, exc,
                )
            else:
                log.warning(
                    "_run_clip_loop: unexpected critic exception clip_id=%s "
                    "attempt=%d %s: %s — treating as CriticUnavailable",
                    clip_id, attempt, type(exc).__name__, exc,
                )
            # Synthesise a terminal report so judge runs once
            report = CriticReport(
                clip_id=clip_id,
                attempt=attempt,
                failures=[
                    CriticFailure(
                        phase="2",
                        check="critic_transport",
                        reason=f"Critic unavailable: {exc}",
                        severity="terminal",
                        correction=None,
                    )
                ],
                formula_score=None,
                passed=False,
            )

        # Append report to the list and persist
        critic_reports_list.append(report.model_dump())
        clip_row.critic_reports = list(critic_reports_list)

        log.info(
            "_run_clip_loop: clip_id=%s attempt=%d passed=%s failures=%d",
            clip_id, attempt, report.passed, len(report.failures),
        )

        # ── Determine loop exit conditions ────────────────────────────────────

        has_terminal = any(f.severity == "terminal" for f in report.failures)
        has_safety_terminal = any(
            f.severity == "terminal" and f.check in SAFETY_SET
            for f in report.failures
        )
        loop_bound_hit = attempt >= MAX_CORRECTIONS

        should_exit = report.passed or has_terminal or loop_bound_hit

        if should_exit:
            # Judge runs exactly once per clip
            decision = judge(report, attempt, MAX_CORRECTIONS)
            apply_judge_to_clip(clip_row, decision, session)
            log.info(
                "_run_clip_loop: clip_id=%s judge decision=%s attempt=%d",
                clip_id, decision.decision, attempt,
            )
            return

        # ── Apply corrections and re-render ───────────────────────────────────
        # Only correctable failures remain; attempt < MAX_CORRECTIONS.
        correctable = [f for f in report.failures if f.severity == "correctable"]
        if not correctable:
            # Nothing to correct (shouldn't happen given should_exit logic above)
            decision = judge(report, attempt, MAX_CORRECTIONS)
            apply_judge_to_clip(clip_row, decision, session)
            return

        log.info(
            "_run_clip_loop: clip_id=%s applying %d correction(s) before re-render",
            clip_id, len(correctable),
        )

        # Apply all corrections in order (only the first applicable one per kind)
        kinds_applied: set[str] = set()
        for failure in correctable:
            if failure.correction is None:
                continue
            kind = failure.correction.kind
            if kind in kinds_applied:
                continue
            kinds_applied.add(kind)
            current_candidate = _apply_correction_to_candidate(
                current_candidate,
                failure.correction,
                sentence_spans,
                clip_len,
            )

        # Validate the adjusted candidate
        try:
            from producer.pipeline_contracts import ClipCandidate as _CC
            _CC.model_validate(current_candidate)
        except Exception as val_exc:
            log.warning(
                "_run_clip_loop: corrected candidate failed validation clip_id=%s: %s "
                "-> escalating",
                clip_id, val_exc,
            )
            synthetic = CriticReport(
                clip_id=clip_id,
                attempt=attempt + 1,
                failures=[
                    CriticFailure(
                        phase="2",
                        check="correction_validation",
                        reason=f"Corrected candidate failed validation: {val_exc}",
                        severity="terminal",
                        correction=None,
                    )
                ],
                formula_score=None,
                passed=False,
            )
            decision = judge(synthetic, attempt + 1, MAX_CORRECTIONS)
            apply_judge_to_clip(clip_row, decision, session)
            return

        # Stage 'correcting'
        from producer.run import set_source_stage
        source_id = getattr(clip_row, "source_id", "") or _source_id
        set_source_stage(session, source_id, "correcting")

        # New R2 output keys with _r{attempt+1} suffix
        new_attempt = attempt + 1

        # Emit correction event (critic passed correctable failures, re-rendering)
        correctable_reasons = [f.reason for f in correctable if f.reason]
        _pipeline_emit_event(
            session, source_id, "correction", status="running",
            clip_id=clip_id,
            detail=(
                f"Clip {_clip_idx + 1}: applying correction (fix {new_attempt}/{MAX_CORRECTIONS})"
                + (f" — {correctable_reasons[0]}" if correctable_reasons else "")
            ),
            reason="; ".join(correctable_reasons) if correctable_reasons else None,
        )

        new_candidate = dict(current_candidate)
        new_candidate["attempt"] = new_attempt

        # Emit rendering start for the correction re-render
        _pipeline_emit_event(
            session, source_id, "rendering", status="running",
            clip_id=clip_id, n=_clip_idx + 1, total=_total_clips,
            detail=(
                f"Creating clip {_clip_idx + 1} of {_total_clips} — rendering on Modal "
                f"(fix {new_attempt}/{MAX_CORRECTIONS})"
            ),
        )
        session.commit()

        # Re-render: call render_and_record with the corrected candidate
        # and new output keys suffixed _r{new_attempt}
        try:
            dispatch = _pipeline_render_and_record(
                campaign_cfg,
                source_meta,
                new_candidate,
                Path(source_video_path),
                workdir,
                campaign_name=campaign_name,
                campaign_mode=run_mode,
                session=session,
            )
        except Exception as render_exc:
            log.error(
                "_run_clip_loop: re-render failed clip_id=%s attempt=%d: %s",
                clip_id, new_attempt, render_exc,
            )
            synthetic = CriticReport(
                clip_id=clip_id,
                attempt=new_attempt,
                failures=[
                    CriticFailure(
                        phase="2",
                        check="render_error",
                        reason=f"Re-render failed: {render_exc}",
                        severity="terminal",
                        correction=None,
                    )
                ],
                formula_score=None,
                passed=False,
            )
            decision = judge(synthetic, new_attempt, MAX_CORRECTIONS)
            apply_judge_to_clip(clip_row, decision, session)
            return

        if dispatch.status == "error":
            log.error(
                "_run_clip_loop: re-render error clip_id=%s attempt=%d: %s",
                clip_id, new_attempt, dispatch.error,
            )
            synthetic = CriticReport(
                clip_id=clip_id,
                attempt=new_attempt,
                failures=[
                    CriticFailure(
                        phase="2",
                        check="render_error",
                        reason=f"Re-render error: {dispatch.error}",
                        severity="terminal",
                        correction=None,
                    )
                ],
                formula_score=None,
                passed=False,
            )
            decision = judge(synthetic, new_attempt, MAX_CORRECTIONS)
            apply_judge_to_clip(clip_row, decision, session)
            return

        # Update clip row with new render output
        clip_row.file_path = dispatch.file_path
        clip_row.thumb_path = dispatch.thumb_path
        clip_row.start = float(current_candidate.get("start", clip_row.start))
        clip_row.end = float(current_candidate.get("end", clip_row.end))
        if "hook" in current_candidate:
            clip_row.hook = current_candidate["hook"]
        clip_row.correction_attempts = new_attempt

        current_file_path = dispatch.file_path
        current_thumb_path = dispatch.thumb_path

        # §2 fix: emit rendering:done for the correction re-render (per-render start/done)
        _pipeline_emit_event(
            session, source_id, "rendering", status="done",
            clip_id=clip_id, n=_clip_idx + 1, total=_total_clips,
            detail=(
                f"Clip {_clip_idx + 1} of {_total_clips} rendered "
                f"(fix {new_attempt}/{MAX_CORRECTIONS})"
            ),
        )

        # Update prior_failures for next critic run (zero-context: only the failures)
        prior_failures = list(report.failures)

        log.info(
            "_run_clip_loop: re-render complete clip_id=%s attempt=%d "
            "start=%.2f end=%.2f",
            clip_id, new_attempt,
            current_candidate.get("start", 0),
            current_candidate.get("end", 0),
        )
        # Loop back to run_critic on the new render


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_video(
    campaign_name: str,
    url: str,
    *,
    run_mode: str | None = None,
    max_apify_spend: float = 2.0,
    max_modal_spend: float = 3.0,
    force: bool = False,
) -> "VideoRunResult":  # noqa: F821
    """Run the full add-video pipeline for a single YouTube URL.

    Returns VideoRunResult summarising what happened.
    """
    from producer.pipeline_contracts import VideoRunResult

    run_start = datetime.now(tz=timezone.utc)

    # ── Step 1: Validate URL ─────────────────────────────────────────────────
    try:
        vid_id = _validate_youtube_url(url)
    except ValueError as exc:
        return VideoRunResult(
            campaign=campaign_name,
            source_id="",
            clips_identified=0,
            clips_rendered=0,
            clips_approved=0,
            clips_rejected=0,
            clips_escalated=0,
            total_corrections=0,
            apify_spend_usd=0.0,
            modal_spend_usd=0.0,
            status="failed",
            error=str(exc),
        )

    # Deferred imports — only needed after URL is validated
    from producer.transcripts import TranscriptFetchError
    from producer.dedupe import mark_source_status, update_used_ranges
    from core.config import load_campaign
    from core.db import get_session
    from core.models import Clip, Source as SourceModel
    from core.storage import work_dir

    source_id = f"youtube:{vid_id}"
    log.info(
        "run_video: campaign=%s source_id=%s url=%s mode=%s",
        campaign_name, source_id, url, run_mode,
    )

    # ── Load campaign ────────────────────────────────────────────────────────
    try:
        campaign_path = Path("campaigns") / f"{campaign_name}.yaml"
        campaign_cfg = load_campaign(campaign_path, strict_assets=False)
        if run_mode is None:
            # Contract §7: default mode = the campaign's own mode.
            run_mode = getattr(campaign_cfg, "mode", "demo") or "demo"
            log.info("run_video: mode not given; using campaign mode %r", run_mode)
    except Exception as exc:
        return VideoRunResult(
            campaign=campaign_name,
            source_id=source_id,
            clips_identified=0,
            clips_rendered=0,
            clips_approved=0,
            clips_rejected=0,
            clips_escalated=0,
            total_corrections=0,
            apify_spend_usd=0.0,
            modal_spend_usd=0.0,
            status="failed",
            error=f"Campaign load failed: {exc}",
        )

    source_meta = {
        "source_id": source_id,
        "platform": "youtube",
        "url": url,
        "title": "",
        "author_handle": "",
        "raw": {},
    }

    # ── Step 1 (cont): Upsert source, ensure campaign ────────────────────────
    with get_session() as session:
        try:
            snapshot = campaign_cfg.model_dump(mode="json")
        except Exception:
            snapshot = None

        _pipeline_ensure_campaign(
            session, campaign_name, campaign_cfg.enabled, snapshot
        )

        # Check if source already done (exhausted) — refuse unless --force
        existing = session.query(SourceModel).filter_by(source_id=source_id).first()
        if existing and existing.status == "done" and not force:
            log.info(
                "run_video: source %s is already done/exhausted; use --force to re-run",
                source_id,
            )
            return VideoRunResult(
                campaign=campaign_name,
                source_id=source_id,
                clips_identified=0,
                clips_rendered=0,
                clips_approved=0,
                clips_rejected=0,
                clips_escalated=0,
                total_corrections=0,
                apify_spend_usd=0.0,
                modal_spend_usd=0.0,
                status="failed",
                error=f"Source {source_id!r} is already exhausted. Use --force to re-clip.",
            )

        _pipeline_upsert_source(session, source_meta, campaign_name)
        session.commit()

        # Set stage to queued
        from producer.run import set_source_stage
        set_source_stage(session, source_id, "queued")
        _pipeline_emit_event(
            session, source_id, "queued", status="running",
            detail="Source queued for processing",
        )
        session.commit()

    # ── Step 2: Transcribing ─────────────────────────────────────────────────
    try:
        from core.apify import Apify
        apify = Apify()

        with get_session() as session:
            set_source_stage(session, source_id, "transcribing")
            _pipeline_emit_event(
                session, source_id, "transcribing", status="running",
                detail="Starting transcript fetch",
            )
            session.commit()

            # Spend guard: probe before LLM spend
            try:
                _pipeline_probe_youtube(url)
            except Exception as probe_exc:
                log.warning(
                    "run_video: probe failed for %s: %s — aborting",
                    source_id, probe_exc,
                )
                set_source_stage(session, source_id, "failed", error=str(probe_exc)[:500])
                _pipeline_emit_event(
                    session, source_id, "transcribing", status="failed",
                    detail="YouTube probe failed",
                    reason=str(probe_exc)[:500],
                )
                session.commit()
                return VideoRunResult(
                    campaign=campaign_name,
                    source_id=source_id,
                    clips_identified=0,
                    clips_rendered=0,
                    clips_approved=0,
                    clips_rejected=0,
                    clips_escalated=0,
                    total_corrections=0,
                    apify_spend_usd=apify.total_cost_usd,
                    modal_spend_usd=0.0,
                    status="failed",
                    error=f"YouTube probe failed: {probe_exc}",
                )

            try:
                segments = _pipeline_fetch_transcript(
                    session, source_id, "youtube", url, apify, campaign_name
                )
            except TranscriptFetchError as exc:
                log.warning(
                    "run_video: transcript fetch failed for %s: %s — NOT marking done",
                    source_id, exc,
                )
                set_source_stage(session, source_id, "failed", error=str(exc)[:500])
                _pipeline_emit_event(
                    session, source_id, "transcribing", status="failed",
                    detail="Transcript fetch failed (transient — retryable)",
                    reason=str(exc)[:500],
                )
                session.commit()
                return VideoRunResult(
                    campaign=campaign_name,
                    source_id=source_id,
                    clips_identified=0,
                    clips_rendered=0,
                    clips_approved=0,
                    clips_rejected=0,
                    clips_escalated=0,
                    total_corrections=0,
                    apify_spend_usd=apify.total_cost_usd,
                    modal_spend_usd=0.0,
                    status="failed",
                    error=f"TranscriptFetchError: {exc}",
                )

        if not segments:
            with get_session() as session:
                mark_source_status(session, source_id, "done")
                set_source_stage(session, source_id, "complete")
                _pipeline_emit_event(
                    session, source_id, "transcribing", status="done",
                    detail="No transcript segments found — source marked complete",
                )
                session.commit()
            return VideoRunResult(
                campaign=campaign_name,
                source_id=source_id,
                clips_identified=0,
                clips_rendered=0,
                clips_approved=0,
                clips_rejected=0,
                clips_escalated=0,
                total_corrections=0,
                apify_spend_usd=apify.total_cost_usd,
                modal_spend_usd=0.0,
                status="complete",
            )

        # Punctuation/sentence cache (as in _process_source)
        sentence_spans: list[dict] | None = None
        _transcript_was_cached: bool = False
        with get_session() as session:
            try:
                from core.models import Transcript as _Tr
                from core.punctuate import restore_sentences as _restore
                tr_row = session.query(_Tr).filter_by(source_id=source_id).first()
                if tr_row is not None:
                    if tr_row.sentences is not None:
                        sentence_spans = tr_row.sentences
                        _transcript_was_cached = True
                    else:
                        sentence_spans = _restore(segments)
                        if sentence_spans is not None:
                            tr_row.sentences = sentence_spans
                            session.commit()
            except Exception as span_exc:
                log.warning("run_video: sentence spans failed (non-fatal): %s", span_exc)

        # Emit transcribing done — distinguish cached vs freshly fetched.
        with get_session() as session:
            n_segs = len(segments)
            if _transcript_was_cached:
                _pipeline_emit_event(
                    session, source_id, "transcribing", status="done",
                    detail=f"Using cached transcript ({n_segs} segments)",
                )
            else:
                _pipeline_emit_event(
                    session, source_id, "transcribing", status="done",
                    detail=f"Transcript fetched ({n_segs} segments)",
                )
            session.commit()

    except Exception as exc:
        log.error("run_video: stage 2 failed for %s: %s", source_id, exc, exc_info=True)
        with get_session() as session:
            set_source_stage(session, source_id, "failed", error=str(exc)[:500])
        return VideoRunResult(
            campaign=campaign_name,
            source_id=source_id,
            clips_identified=0,
            clips_rendered=0,
            clips_approved=0,
            clips_rejected=0,
            clips_escalated=0,
            total_corrections=0,
            apify_spend_usd=0.0,
            modal_spend_usd=0.0,
            status="failed",
            error=str(exc),
        )

    # ── Step 3: Identifying (rank_moments + deterministic guards) ────────────
    try:
        from core.llm import RankingUnavailable

        with get_session() as session:
            set_source_stage(session, source_id, "identifying")
            _pipeline_emit_event(
                session, source_id, "identifying", status="running",
                detail="Reading transcript / selecting moments",
            )
            session.commit()

        ranking_cfg = campaign_cfg.ranking

        # §6 max_clips: scale with duration — min(30, max(cfg, duration_min // 4))
        base_max_clips = getattr(ranking_cfg, "max_clips_per_source", 8) or 8
        try:
            # Use last transcript segment end time as video duration estimate
            duration_secs = float(segments[-1]["end"]) if segments else 0.0
            duration_min = duration_secs / 60.0
            duration_based = int(duration_min // 4)
            max_clips = min(30, max(base_max_clips, duration_based))
            log.info(
                "run_video: %s max_clips=%d (cfg=%d, duration_min=%.1f, duration_based=%d)",
                source_id, max_clips, base_max_clips, duration_min, duration_based,
            )
        except Exception:
            max_clips = base_max_clips

        # Temporarily override max_clips_per_source on ranking_cfg for this call
        # without mutating the original cfg object.  rank_clips reads the field
        # directly from cfg, so we wrap it.
        class _RankingCfgOverride:
            """Thin wrapper that overrides max_clips_per_source."""
            def __init__(self, inner: Any, max_clips_override: int) -> None:
                self._inner = inner
                self.max_clips_per_source = max_clips_override
            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

        ranking_cfg_for_call = _RankingCfgOverride(ranking_cfg, max_clips)

        try:
            from core.preferences import build_preference_context
            with get_session() as _pref_session:
                preference_context = build_preference_context(_pref_session, campaign_name)
        except Exception:
            preference_context = ""

        def _emit_identifying(detail: str) -> None:
            try:
                with get_session() as _ev_session:
                    _pipeline_emit_event(
                        _ev_session, source_id, "identifying",
                        status="running", detail=detail,
                    )
                    _ev_session.commit()
            except Exception:
                pass

        try:
            raw_candidates = _rank_in_chunks(
                segments,
                ranking_cfg_for_call,
                sentence_spans=sentence_spans,
                preference_context=preference_context,
                emit=_emit_identifying,
            )
        except RankingUnavailable as ru_exc:
            # Mirror TranscriptFetchError semantics: stage failed, source UNTOUCHED
            log.warning(
                "run_video: RankingUnavailable for %s: %s — NOT marking done",
                source_id, ru_exc,
            )
            with get_session() as session:
                set_source_stage(session, source_id, "failed", error=str(ru_exc)[:500])
                _pipeline_emit_event(
                    session, source_id, "identifying", status="failed",
                    detail="LLM ranking unavailable (retryable)",
                    reason=str(ru_exc)[:500],
                )
                session.commit()
            return VideoRunResult(
                campaign=campaign_name,
                source_id=source_id,
                clips_identified=0,
                clips_rendered=0,
                clips_approved=0,
                clips_rejected=0,
                clips_escalated=0,
                total_corrections=0,
                apify_spend_usd=getattr(apify, "total_cost_usd", 0.0),
                modal_spend_usd=0.0,
                status="failed",
                error=f"RankingUnavailable: {ru_exc}",
            )

        log.info(
            "run_video: %s rank_moments returned %d candidates",
            source_id, len(raw_candidates),
        )

        # Deterministic guards (apply_prefilters, clip_within_unit, verify_boundaries)
        selected: list[dict] = []
        if raw_candidates and sentence_spans:
            from core.topics import (
                clip_within_unit,
                detect_unit_boundaries,
                build_units_from_boundaries,
            )
            from producer.boundary_check import apply_prefilters, verify_boundaries

            try:
                boundaries = detect_unit_boundaries(sentence_spans)
                units = build_units_from_boundaries(sentence_spans, boundaries)
                clip_len = (
                    float(ranking_cfg.clip_length[0]),
                    float(ranking_cfg.clip_length[1]),
                )

                n_repaired = 0
                n_dropped = 0
                for cand in raw_candidates[:max_clips]:
                    try:
                        # apply_prefilters
                        cand = apply_prefilters(cand, sentence_spans, clip_len)  # type: ignore[assignment]
                        # clip_within_unit
                        cand = clip_within_unit(cand, units, sentence_spans, clip_len=clip_len)
                        # verify_boundaries (repair-first: adjusted may differ)
                        adjusted, keep = verify_boundaries(cand, sentence_spans, clip_len=clip_len)
                        if keep:
                            if (
                                adjusted.get("start") != cand.get("start")
                                or adjusted.get("end") != cand.get("end")
                            ):
                                n_repaired += 1
                            selected.append(adjusted)
                        else:
                            n_dropped += 1
                            log.info(
                                "run_video: boundary guard dropped candidate start=%.2f end=%.2f",
                                cand.get("start", 0), cand.get("end", 0),
                            )
                    except Exception as guard_exc:
                        log.warning(
                            "run_video: guard error (non-fatal, keeping): %s", guard_exc
                        )
                        selected.append(cand)
                log.info(
                    "run_video: boundary verifier: %d repaired, %d dropped, %d clean",
                    n_repaired, n_dropped, len(selected) - n_repaired,
                )
            except Exception as det_exc:
                log.warning(
                    "run_video: deterministic guard setup failed (non-fatal): %s", det_exc
                )
                selected = list(raw_candidates[:max_clips])
        else:
            selected = list(raw_candidates[:max_clips])

        clips_identified = len(selected)
        log.info(
            "run_video: %s clips identified after guards: %d", source_id, clips_identified
        )

        # Emit per-candidate identified events, then the identified summary.
        with get_session() as session:
            for idx, cand in enumerate(selected):
                _pipeline_emit_event(
                    session, source_id, "identified", status="running",
                    n=idx + 1, total=clips_identified,
                    detail=(
                        f"Candidate {idx + 1}/{clips_identified} "
                        f"[{cand.get('start', 0):.1f}s–{cand.get('end', 0):.1f}s] "
                        f"score={cand.get('score', 0):.2f}"
                    ),
                )
            # Summary identified event
            _pipeline_emit_event(
                session, source_id, "identified", status="done",
                total=clips_identified,
                detail=f"Found {clips_identified} clip{'s' if clips_identified != 1 else ''}",
            )
            session.commit()

        if not selected:
            with get_session() as session:
                mark_source_status(session, source_id, "done")
                set_source_stage(session, source_id, "complete")
                _pipeline_emit_event(
                    session, source_id, "complete", status="done",
                    detail="0 clips identified — source exhausted",
                )
                session.commit()
            return VideoRunResult(
                campaign=campaign_name,
                source_id=source_id,
                clips_identified=0,
                clips_rendered=0,
                clips_approved=0,
                clips_rejected=0,
                clips_escalated=0,
                total_corrections=0,
                apify_spend_usd=apify.total_cost_usd,
                modal_spend_usd=0.0,
                status="complete",
            )

        # ── Step 4: Pre-render spend guard ───────────────────────────────────
        with get_session() as session:
            from producer.render_dispatch import estimate_modal_batch_cost
            estimated_cost = estimate_modal_batch_cost(len(selected), session)
            if estimated_cost > max_modal_spend:
                # Trim to fit
                max_clips_in_budget = max(
                    1, int(max_modal_spend / (estimated_cost / len(selected)))
                )
                dropped = len(selected) - max_clips_in_budget
                if dropped > 0:
                    log.info(
                        "run_video: trimming from %d to %d clips to fit modal spend cap "
                        "(est %.4f > max %.2f); %d dropped",
                        len(selected), max_clips_in_budget,
                        estimated_cost, max_modal_spend, dropped,
                    )
                selected = selected[:max_clips_in_budget]

        with get_session() as session:
            set_source_stage(
                session, source_id, "rendering",
                clips_identified=clips_identified,
            )
            session.commit()

    except RankingUnavailable:
        # Already handled above — re-raise would bypass the error return, so just pass.
        raise  # pragma: no cover — the inner except already returns
    except Exception as exc:
        log.error("run_video: stage 3/4 failed for %s: %s", source_id, exc, exc_info=True)
        with get_session() as session:
            set_source_stage(session, source_id, "failed", error=str(exc)[:500])
            _pipeline_emit_event(
                session, source_id, "identifying", status="failed",
                detail="Identification stage error",
                reason=str(exc)[:500],
            )
            session.commit()
        return VideoRunResult(
            campaign=campaign_name,
            source_id=source_id,
            clips_identified=0,
            clips_rendered=0,
            clips_approved=0,
            clips_rejected=0,
            clips_escalated=0,
            total_corrections=0,
            apify_spend_usd=getattr(apify, "total_cost_usd", 0.0),
            modal_spend_usd=0.0,
            status="failed",
            error=str(exc),
        )

    # ── Step 5: Download + render all clips ──────────────────────────────────

    # Emit downloading start and wire up on_event callback for yt-dlp/Apify events.
    with get_session() as _dl_session_start:
        _pipeline_emit_event(
            _dl_session_start, source_id, "downloading", status="running",
            detail="Starting video download",
        )
        _dl_session_start.commit()

    def _download_on_event(stage: str, detail: str) -> None:
        """Forward download lifecycle events to the pipeline_events table."""
        try:
            with get_session() as _ev_session:
                _pipeline_emit_event(
                    _ev_session, source_id, "downloading", status="running",
                    detail=detail,
                )
                _ev_session.commit()
        except Exception as _ev_exc:
            log.debug("_download_on_event failed (ignored): %s", _ev_exc)

    try:
        source_video_path = _pipeline_download_source(
            source_id, "youtube", url, {}, campaign=campaign_name,
            on_event=_download_on_event,
        )
    except Exception as dl_exc:
        log.error("run_video: download failed for %s: %s", source_id, dl_exc)
        with get_session() as session:
            set_source_stage(
                session, source_id, "failed", error=str(dl_exc)[:500]
            )
            _pipeline_emit_event(
                session, source_id, "downloading", status="failed",
                detail="Download failed",
                reason=str(dl_exc)[:500],
            )
            session.commit()
        return VideoRunResult(
            campaign=campaign_name,
            source_id=source_id,
            clips_identified=clips_identified,
            clips_rendered=0,
            clips_approved=0,
            clips_rejected=0,
            clips_escalated=0,
            total_corrections=0,
            apify_spend_usd=apify.total_cost_usd,
            modal_spend_usd=0.0,
            status="failed",
            error=f"Download failed: {dl_exc}",
        )

    # Emit downloading done event (the on_event callback already emits intermediate
    # steps; this is the final confirmation after the file is on disk).
    with get_session() as _dl_session_done:
        _pipeline_emit_event(
            _dl_session_done, source_id, "downloading", status="done",
            detail="Video download complete",
        )
        _dl_session_done.commit()

    wdir = work_dir(source_id)
    inserted_clips: list[Any] = []
    new_ranges: list[list[float]] = []

    # Track transcript segments for critic
    with get_session() as session:
        from core.models import Transcript as _Tr2
        tr_row = session.query(_Tr2).filter_by(source_id=source_id).first()
        transcript_segments = tr_row.segments if tr_row else segments

    total_clips = len(selected)
    for clip_idx, candidate in enumerate(selected):
        with get_session() as session:
            try:
                # Emit rendering start for this clip (clip_id unknown yet — use None)
                _pipeline_emit_event(
                    session, source_id, "rendering", status="running",
                    n=clip_idx + 1, total=total_clips,
                    detail=f"Creating clip {clip_idx + 1} of {total_clips} — rendering on Modal",
                )
                session.commit()

                dispatch = _pipeline_render_and_record(
                    campaign_cfg,
                    source_meta,
                    candidate,
                    Path(source_video_path),
                    wdir,
                    campaign_name=campaign_name,
                    campaign_mode=run_mode,
                    session=session,
                )

                if dispatch.status == "error":
                    log.error(
                        "run_video: initial render error for clip start=%.2f end=%.2f: %s",
                        candidate.get("start", 0), candidate.get("end", 0), dispatch.error,
                    )
                    # Retry once (existing dispatch behavior) — no, dispatch already does
                    # one retry. A 'error' status here means both attempts failed.
                    # Escalate via judge.
                    from producer.pipeline_contracts import CriticReport, CriticFailure
                    from producer.judge import judge, apply_judge_to_clip, SAFETY_SET

                    # Insert a stub clip row so judge can write to it
                    from producer.run import _build_caption
                    clip_row = Clip(
                        campaign=campaign_name,
                        source_id=source_id,
                        start=candidate.get("start"),
                        end=candidate.get("end"),
                        kind="clip",
                        mode=run_mode,
                        aspect="9:16",
                        hook=candidate.get("hook"),
                        score=candidate.get("score"),
                        file_path="",
                        thumb_path="",
                        caption="",
                        destination_channels=campaign_cfg.destinations.postiz_channels,
                        status="pending_review",
                        correction_attempts=0,
                        critic_reports=[],
                        judge_decision=None,
                    )
                    session.add(clip_row)
                    session.flush()

                    _pipeline_emit_event(
                        session, source_id, "rendering", status="failed",
                        clip_id=clip_row.id, n=clip_idx + 1, total=total_clips,
                        detail=f"Render failed for clip {clip_idx + 1} of {total_clips}",
                        reason=str(dispatch.error)[:300] if dispatch.error else None,
                    )

                    synthetic = CriticReport(
                        clip_id=clip_row.id,
                        attempt=0,
                        failures=[
                            CriticFailure(
                                phase="2",
                                check="render_error",
                                reason=f"Render failed: {dispatch.error}",
                                severity="terminal",
                                correction=None,
                            )
                        ],
                        formula_score=None,
                        passed=False,
                    )
                    decision = judge(synthetic, 0, MAX_CORRECTIONS)
                    apply_judge_to_clip(clip_row, decision, session)

                    # Terminal event: judging → didnt_pass or ready
                    terminal_stage = "didnt_pass" if decision.decision != "approved" else "ready"
                    _pipeline_emit_event(
                        session, source_id, terminal_stage, status="done",
                        clip_id=clip_row.id,
                        detail=f"Clip {clip_idx + 1}: {decision.decision}",
                        reason=("; ".join(decision.reasons) if decision.reasons else None),
                    )
                    session.commit()
                    inserted_clips.append(clip_row)
                    continue

                # Build caption
                from producer.run import _build_caption
                caption = _build_caption(
                    template=campaign_cfg.destinations.caption_template,
                    hook=candidate.get("hook", ""),
                    source_handle=source_meta.get("author_handle") or "",
                    hashtags=campaign_cfg.destinations.hashtags,
                )

                clip_row = Clip(
                    campaign=campaign_name,
                    source_id=source_id,
                    start=candidate.get("start"),
                    end=candidate.get("end"),
                    kind="clip",
                    mode=run_mode,
                    aspect="9:16",
                    hook=candidate.get("hook"),
                    score=candidate.get("score"),
                    reason=candidate.get("reason"),
                    file_path=dispatch.file_path,
                    thumb_path=dispatch.thumb_path,
                    caption=caption,
                    destination_channels=campaign_cfg.destinations.postiz_channels,
                    status="pending_review",
                    gate_status="pending",
                    correction_attempts=0,
                    critic_reports=[],
                    judge_decision=None,
                )
                session.add(clip_row)
                session.flush()  # get clip_row.id

                # Emit rendering done for this clip now that we have clip_id
                _pipeline_emit_event(
                    session, source_id, "rendering", status="done",
                    clip_id=clip_row.id, n=clip_idx + 1, total=total_clips,
                    detail=f"Clip {clip_idx + 1} of {total_clips} rendered",
                )

                log.info(
                    "run_video: initial render OK clip_id=%s start=%.2f end=%.2f",
                    clip_row.id, candidate.get("start", 0), candidate.get("end", 0),
                )

                # Emit reviewing start
                _pipeline_emit_event(
                    session, source_id, "reviewing", status="running",
                    clip_id=clip_row.id,
                    detail=f"Reviewing clip {clip_idx + 1}",
                )
                session.commit()

                # ── Per-clip critic/judge loop ────────────────────────────────
                _run_clip_loop(
                    clip_row=clip_row,
                    initial_file_path=dispatch.file_path,
                    initial_thumb_path=dispatch.thumb_path,
                    candidate=candidate,
                    sentence_spans=sentence_spans or [],
                    transcript_segments=transcript_segments,
                    campaign_cfg=campaign_cfg,
                    source_meta=source_meta,
                    source_video_path=source_video_path,
                    workdir=wdir,
                    run_mode=run_mode,
                    session=session,
                    max_modal_spend=max_modal_spend,
                    run_start=run_start,
                    _source_id=source_id,
                    _clip_idx=clip_idx,
                    _total_clips=total_clips,
                )

                # Emit terminal event based on judge decision
                judge_dec = getattr(clip_row, "judge_decision", None)
                if judge_dec:
                    dec_name = (judge_dec or {}).get("decision", "")
                    terminal_stage = "ready" if dec_name == "approved" else "didnt_pass"
                    correction_atts = getattr(clip_row, "correction_attempts", 0) or 0
                    if correction_atts > 0:
                        # Also emit corrected event to record that correction happened
                        _pipeline_emit_event(
                            session, source_id, "correction", status="corrected",
                            clip_id=clip_row.id,
                            detail=f"Clip {clip_idx + 1}: {correction_atts} correction(s) applied",
                        )
                    # §2 fix: emit judging stage before terminal (judging → ready/didnt_pass)
                    _pipeline_emit_event(
                        session, source_id, "judging", status="running",
                        clip_id=clip_row.id,
                        detail=f"Clip {clip_idx + 1}: judging quality",
                    )
                    _pipeline_emit_event(
                        session, source_id, terminal_stage, status="done",
                        clip_id=clip_row.id,
                        detail=f"Clip {clip_idx + 1}: {dec_name or terminal_stage}",
                        reason=("; ".join((judge_dec or {}).get("reasons", [])) or None),
                    )

                new_ranges.append([candidate["start"], candidate["end"]])
                inserted_clips.append(clip_row)
                session.commit()

            except Exception as clip_exc:
                log.error(
                    "run_video: clip processing failed start=%.2f end=%.2f: %s",
                    candidate.get("start", 0), candidate.get("end", 0), clip_exc,
                    exc_info=True,
                )
                try:
                    # Best-effort: emit a failed event so the UI doesn't hang
                    _pipeline_emit_event(
                        session, source_id, "rendering", status="failed",
                        n=clip_idx + 1, total=total_clips,
                        detail=f"Clip {clip_idx + 1} processing error",
                        reason=str(clip_exc)[:300],
                    )
                    session.commit()
                except Exception:
                    pass
                try:
                    session.rollback()
                except Exception:
                    pass

    # ── Step 7: Mark source done + update_used_ranges ────────────────────────
    try:
        with get_session() as session:
            if new_ranges:
                update_used_ranges(session, source_id, new_ranges)
            mark_source_status(session, source_id, "done")

            # The PIPELINE is done here — every clip has a judge decision.
            # Human review happens in the Queue; leaving the source at
            # 'reviewing' kept it in the live-progress panel forever with a
            # growing elapsed timer ("Reviewing clip · 2h 17m — stalled?",
            # operator report 2026-07-30). Terminal stage is 'complete'.
            set_source_stage(session, source_id, "complete")

            # Compute terminal summary for complete event
            n_ready = sum(
                1 for c in inserted_clips
                if (getattr(c, "judge_decision", None) or {}).get("decision") == "approved"
            )
            n_didnt_pass = sum(
                1 for c in inserted_clips
                if (getattr(c, "judge_decision", None) or {}).get("decision") != "approved"
                and getattr(c, "judge_decision", None) is not None
            )
            _pipeline_emit_event(
                session, source_id, "complete", status="done",
                detail=(
                    f"{n_ready} ready · {n_didnt_pass} didn't pass · source exhausted"
                ),
            )
            session.commit()
    except Exception as fin_exc:
        log.error("run_video: finalization failed for %s: %s", source_id, fin_exc)

    # ── Cleanup source video ─────────────────────────────────────────────────
    try:
        from producer.download import cleanup_source
        cleanup_source(source_id)
    except Exception:
        pass

    # ── Compute result stats ─────────────────────────────────────────────────
    clips_approved = sum(
        1 for c in inserted_clips
        if getattr(c, "judge_decision", None)
        and (c.judge_decision or {}).get("decision") == "approved"
    )
    clips_rejected = sum(
        1 for c in inserted_clips
        if getattr(c, "judge_decision", None)
        and (c.judge_decision or {}).get("decision") == "rejected"
    )
    clips_escalated = sum(
        1 for c in inserted_clips
        if getattr(c, "judge_decision", None)
        and (c.judge_decision or {}).get("decision") == "escalate_to_human"
    )
    total_corrections = sum(
        getattr(c, "correction_attempts", 0) or 0
        for c in inserted_clips
    )

    apify_ledger_spend = 0.0
    try:
        with get_session() as session:
            modal_spend = run_modal_spend(session, campaign_name, run_start)
            # Fallback downloader runs use their own Apify client; pick their
            # spend up from the ledger so the run result doesn't under-report.
            from sqlalchemy import func

            from core.models import ApifyRun

            apify_ledger_spend = float(
                session.query(func.coalesce(func.sum(ApifyRun.cost_usd), 0.0))
                .filter(
                    ApifyRun.campaign == campaign_name,
                    ApifyRun.created_at >= run_start,
                )
                .scalar() or 0.0
            )
    except Exception:
        modal_spend = 0.0

    run_end = datetime.now(tz=timezone.utc)
    elapsed = (run_end - run_start).total_seconds()
    log.info(
        "run_video: complete campaign=%s source_id=%s clips_rendered=%d "
        "approved=%d rejected=%d escalated=%d corrections=%d elapsed=%.1fs",
        campaign_name, source_id, len(inserted_clips),
        clips_approved, clips_rejected, clips_escalated,
        total_corrections, elapsed,
    )

    return VideoRunResult(
        campaign=campaign_name,
        source_id=source_id,
        clips_identified=clips_identified,
        clips_rendered=len(inserted_clips),
        clips_approved=clips_approved,
        clips_rejected=clips_rejected,
        clips_escalated=clips_escalated,
        total_corrections=total_corrections,
        apify_spend_usd=max(apify.total_cost_usd, apify_ledger_spend),
        modal_spend_usd=modal_spend,
        status="complete",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clip Engine — add-video pipeline"
    )
    parser.add_argument("campaign", help="Campaign name (YAML filename without .yaml)")
    parser.add_argument("url", help="YouTube video URL to clip")
    parser.add_argument(
        "--mode",
        choices=["demo", "production"],
        default=None,
        help="Run mode (default: the campaign's own mode from its YAML)",
    )
    parser.add_argument(
        "--max-apify-spend",
        type=float,
        default=2.0,
        metavar="USD",
        help="Apify spend cap (default $2.0)",
    )
    parser.add_argument(
        "--max-modal-spend",
        type=float,
        default=3.0,
        metavar="USD",
        help="Modal spend cap (default $3.0)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-clip a source that is already exhausted (status=done)",
    )
    args = parser.parse_args()

    result = run_video(
        args.campaign,
        args.url,
        run_mode=args.mode,
        max_apify_spend=args.max_apify_spend,
        max_modal_spend=args.max_modal_spend,
        force=args.force,
    )

    print(json.dumps(result.model_dump(), indent=2))

    if result.status == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
