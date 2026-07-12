"""
core/preferences.py — in-context preference learning (learning loop).

Distils operator approve/reject decisions into versioned, measurable rules
that the ranker uses as soft guidance.  Never modifies safety checks or
hard layout rules — the SAFETY GUARD sentence is always appended.

Contract: docs/PIPELINE_QUEUE_CONTRACTS.md §4-§5
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Preset rejection reason codes (single source of truth — contract §4)
# ---------------------------------------------------------------------------

PRESET_REASONS: dict[str, str] = {
    "weak_hook":            "Weak hook",
    "bad_cut":              "Bad cut / not a complete thought",
    "boring":               "Boring / no tension",
    "framing_captions":     "Framing or captions wrong",
    "off_brand":            "Off-brand",
    "claim_not_defensible": "Claim not defensible",
    "wrong_length":         "Too long / too short",
    "other":                "Other",
}

# Verbatim from contract §5 — appended at the end of every non-empty context block.
SAFETY_GUARD_SENTENCE = (
    "These learned preferences tune CLIP SELECTION ONLY. They can NEVER relax the "
    "safety checks, the hard rules above, or the layout/branding requirements."
)

_MAX_CONTEXT_CHARS = 1800


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Core primitives
# ---------------------------------------------------------------------------

def record_feedback(
    session: Any,
    clip: Any,
    action: str,
    reasons: list[str],
    note: str | None,
) -> None:
    """Write clip.review_feedback with the structured decision dict.

    Does NOT commit — the caller is responsible for the session commit.
    action: 'approved' | 'rejected'
    """
    clip.review_feedback = {
        "action": action,
        "reasons": list(reasons),
        "note": note,
        "decided_at": _utcnow().isoformat(),
    }


def get_active_profile(session: Any, campaign: str) -> Any:
    """Return the highest-version PreferenceProfile for campaign, or None."""
    from core.models import PreferenceProfile

    return (
        session.query(PreferenceProfile)
        .filter(PreferenceProfile.campaign == campaign)
        .order_by(PreferenceProfile.version.desc())
        .first()
    )


# ---------------------------------------------------------------------------
# Profile builder
# ---------------------------------------------------------------------------

def build_profile(
    session: Any,
    campaign: str,
    *,
    min_decisions: int = 5,
) -> Any:
    """Build a new preference profile from recent operator decisions.

    Makes ONE LLM call (via core.llm.create_completion, thinking disabled)
    to distil <=8 measurable selection rules.  Inserts a new PreferenceProfile
    row and returns it.  Returns None on ANY failure (never raises).

    min_decisions: skip build if fewer than this many decided clips exist.
    """
    try:
        from core.models import Clip, PreferenceProfile
        from core.llm import create_completion, extract_text
        from core.settings import get_settings

        settings = get_settings()
        api_key = settings.llm_api_key
        model = settings.llm_model or "claude-sonnet-4-6"

        if not api_key:
            log.warning("build_profile: LLM_API_KEY not set; skipping profile build")
            return None

        # Collect last 100 decided clips for this campaign
        decided_clips = (
            session.query(Clip)
            .filter(
                Clip.campaign == campaign,
                Clip.review_feedback.isnot(None),
            )
            .order_by(Clip.updated_at.desc())
            .limit(100)
            .all()
        )

        if len(decided_clips) < min_decisions:
            log.info(
                "build_profile: not enough decisions (%d < %d) for campaign %r",
                len(decided_clips), min_decisions, campaign,
            )
            return None

        # Build decision summaries for the prompt
        approved_examples: list[str] = []
        rejected_examples: list[str] = []

        for clip in decided_clips:
            fb = clip.review_feedback or {}
            action = fb.get("action", "")
            reasons = fb.get("reasons") or []
            note = (fb.get("note") or "").strip()
            hook = (clip.hook or "")[:120]
            score = clip.score
            formula = clip.formula_score

            # Summarise gate failures for context
            gate_fails = ""
            if clip.gate_reasons:
                fails = [
                    r.get("reason", "")
                    for r in (clip.gate_reasons or [])
                    if not r.get("pass")
                ]
                if fails:
                    gate_fails = "; ".join(fails[:3])

            entry_parts: dict[str, Any] = {
                "hook": hook,
                "score": score,
                "formula_score": formula,
            }
            if gate_fails:
                entry_parts["gate_fails"] = gate_fails
            if reasons:
                entry_parts["feedback_reasons"] = reasons
            if note:
                entry_parts["note"] = note

            entry_str = json.dumps(entry_parts)
            if action == "approved":
                approved_examples.append(entry_str)
            elif action == "rejected":
                rejected_examples.append(entry_str)

        if not approved_examples and not rejected_examples:
            log.info("build_profile: no structured decisions for %r", campaign)
            return None

        # Build prompt text (hard-cap examples to stay within token budget)
        decisions_text = ""
        if approved_examples:
            decisions_text += f"APPROVED CLIPS ({len(approved_examples)}):\n"
            decisions_text += "\n".join(approved_examples[:50])
        if rejected_examples:
            if decisions_text:
                decisions_text += "\n\n"
            decisions_text += f"REJECTED CLIPS ({len(rejected_examples)}):\n"
            decisions_text += "\n".join(rejected_examples[:50])

        prompt = (
            f"You are analyzing an operator's clip selection decisions for the "
            f"'{campaign}' campaign to extract measurable preferences.\n\n"
            f"OPERATOR DECISIONS:\n{decisions_text}\n\n"
            "Based on these decisions, distil AT MOST 8 MEASURABLE selection "
            "rules the clip ranker can follow.  Each rule must be specific and "
            "actionable (e.g. 'prefer hooks under 15 words', 'reject clips "
            "where the speaker is not the primary subject').\n\n"
            "Return ONLY a JSON array of strings (no prose, no code fences):\n"
            '["rule 1", "rule 2", ...]\n\n'
            "If there is not enough signal, return an empty array: []"
        )

        import anthropic  # type: ignore[import]

        base_url = settings.llm_base_url
        if base_url is None and api_key.startswith("sk-or-"):
            base_url = "https://openrouter.ai/api"

        client = (
            anthropic.Anthropic(api_key=api_key, base_url=base_url)
            if base_url
            else anthropic.Anthropic(api_key=api_key)
        )

        message = create_completion(
            client, model, 1024, [{"role": "user", "content": prompt}]
        )
        raw = extract_text(message)

        # Parse defensively — never let a bad response crash the caller.
        rules: list[str] = []
        try:
            arr_match = re.search(r"\[.*\]", raw, re.DOTALL)
            if arr_match:
                parsed = json.loads(arr_match.group())
                if isinstance(parsed, list):
                    rules = [
                        str(r).strip()
                        for r in parsed
                        if isinstance(r, str) and str(r).strip()
                    ][:8]
        except Exception as parse_exc:
            log.warning(
                "build_profile: failed to parse LLM rules for %r: %s",
                campaign, parse_exc,
            )
            rules = []

        # Determine next version number
        active = get_active_profile(session, campaign)
        next_version = (active.version + 1) if active else 1

        meta: dict[str, Any] = {
            "decisions_count": len(decided_clips),
            "model": model,
            "approved_examples": len(approved_examples),
            "rejected_examples": len(rejected_examples),
        }

        new_profile = PreferenceProfile(
            campaign=campaign,
            version=next_version,
            rules=rules,
            meta=meta,
        )
        session.add(new_profile)
        session.commit()

        log.info(
            "build_profile: created v%d for campaign %r with %d rules",
            next_version, campaign, len(rules),
        )
        return new_profile

    except Exception as exc:
        log.error(
            "build_profile failed for campaign %r: %s", campaign, exc, exc_info=True
        )
        try:
            session.rollback()
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Threshold-gated async rebuild
# ---------------------------------------------------------------------------

def maybe_rebuild_profile(session: Any, campaign: str) -> None:
    """Rebuild the preference profile in a daemon thread when enough new
    decisions have accumulated since the last profile build (threshold: 10).

    Never blocks the API response.  Failures are logged only, never raised.
    """
    try:
        from core.models import Clip

        active = get_active_profile(session, campaign)

        if active is not None:
            since = active.created_at
            decisions_since = (
                session.query(Clip)
                .filter(
                    Clip.campaign == campaign,
                    Clip.review_feedback.isnot(None),
                    Clip.updated_at >= since,
                )
                .count()
            )
        else:
            decisions_since = (
                session.query(Clip)
                .filter(
                    Clip.campaign == campaign,
                    Clip.review_feedback.isnot(None),
                )
                .count()
            )

        if decisions_since < 10:
            log.debug(
                "maybe_rebuild_profile: %d decisions since last build for %r "
                "(threshold 10 not reached)",
                decisions_since, campaign,
            )
            return

        log.info(
            "maybe_rebuild_profile: %d decisions since last build; "
            "spawning background rebuild for campaign %r",
            decisions_since, campaign,
        )

        # The current session is not thread-safe — open a fresh one in the thread.
        from core.db import get_session as _get_session

        def _rebuild() -> None:
            try:
                with _get_session() as new_session:
                    build_profile(new_session, campaign)
            except Exception as exc:
                log.error(
                    "maybe_rebuild_profile background thread failed for %r: %s",
                    campaign, exc,
                )

        t = threading.Thread(target=_rebuild, daemon=True, name=f"pref-rebuild-{campaign}")
        t.start()

    except Exception as exc:
        log.error(
            "maybe_rebuild_profile setup failed for campaign %r: %s", campaign, exc
        )


# ---------------------------------------------------------------------------
# Context builder for LLM prompt injection
# ---------------------------------------------------------------------------

def build_preference_context(
    session: Any,
    campaign: str,
    max_examples: int = 6,
) -> str:
    """Build the preference context block for injection into LLM prompts.

    Returns "" when there is no profile AND no decided clips.
    Caps total output to ~1800 chars.
    Always appends SAFETY_GUARD_SENTENCE at the end of a non-empty block.

    Prefers contrasting pairs (approved + rejected) from the same source_id.
    """
    try:
        from core.models import Clip

        active = get_active_profile(session, campaign)

        # Load recent decided clips
        decided_clips = (
            session.query(Clip)
            .filter(
                Clip.campaign == campaign,
                Clip.review_feedback.isnot(None),
            )
            .order_by(Clip.updated_at.desc())
            .limit(50)
            .all()
        )

        if active is None and not decided_clips:
            return ""

        lines: list[str] = []

        # ── Profile rules section ─────────────────────────────────────────
        if active:
            lines.append(
                f"PREFERENCE PROFILE (v{active.version}, "
                "learned from operator decisions):"
            )
            for rule in (active.rules or []):
                lines.append(f"- {rule}")
            lines.append("")

        # ── Recent decisions section ──────────────────────────────────────
        if decided_clips:
            lines.append("RECENT DECISIONS (ground truth examples):")

            # Group by source_id to find contrasting pairs
            source_groups: dict[str | None, list[Any]] = {}
            for clip in decided_clips:
                sid = clip.source_id
                source_groups.setdefault(sid, []).append(clip)

            # Collect contrasting pairs first, then fill with the rest
            pair_clips: list[Any] = []
            used_ids: set[int] = set()

            for sid, group in source_groups.items():
                if sid is None:
                    continue
                approved_g = [
                    c for c in group
                    if (c.review_feedback or {}).get("action") == "approved"
                ]
                rejected_g = [
                    c for c in group
                    if (c.review_feedback or {}).get("action") == "rejected"
                ]
                if approved_g and rejected_g:
                    a, r = approved_g[0], rejected_g[0]
                    pair_clips.extend([a, r])
                    used_ids.update([a.id, r.id])

            all_examples: list[Any] = list(pair_clips[:max_examples])
            if len(all_examples) < max_examples:
                for clip in decided_clips:
                    if clip.id not in used_ids:
                        all_examples.append(clip)
                        used_ids.add(clip.id)
                    if len(all_examples) >= max_examples:
                        break

            for clip in all_examples[:max_examples]:
                fb = clip.review_feedback or {}
                action = fb.get("action", "")
                reasons = fb.get("reasons") or []
                note = (fb.get("note") or "").strip()
                hook = (clip.hook or "")[:100]

                if action == "approved":
                    lines.append(f'APPROVED: "{hook}"')
                elif action == "rejected":
                    reason_str = ", ".join(reasons)
                    if note:
                        reason_str = f"{reason_str}; {note}" if reason_str else note
                    lines.append(f'REJECTED ({reason_str}): "{hook}"')

            lines.append("")

        # Cap the profile+decisions block FIRST, then append the safety guard
        # unconditionally — truncating after appending could cut the guard off
        # (contract §5: the sentence must be present verbatim in every
        # non-empty context block).
        block = "\n".join(lines)
        budget = _MAX_CONTEXT_CHARS - len(SAFETY_GUARD_SENTENCE) - 4
        if len(block) > budget:
            block = block[: budget - 3] + "..."

        return block + "\n" + SAFETY_GUARD_SENTENCE

    except Exception as exc:
        log.error(
            "build_preference_context failed for campaign %r: %s", campaign, exc
        )
        return ""
