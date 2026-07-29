"""
producer/boundary_check.py — Pre-render boundary quality guards (spec §R2.3 + §R2.4).

Public interface:
    is_bad_start_sentence(text, prev_text)   -> bool   (§R2.3 prefilter)
    needs_end_extension(text)                -> bool   (§R2.3 prefilter)
    apply_prefilters(candidate, spans, clip_len) -> dict  (applies §R2.3 rules)
    verify_boundaries(candidate, spans, ...)  -> tuple[dict, bool]  (§R2.4 LLM gate)

All public functions are pure or near-pure (no DB, no global state).
LLM transport errors in verify_boundaries are treated as PASS so the pipeline
is never blocked by verifier unavailability.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# §R2.3 — Speaker-turn prefilter constants
# ---------------------------------------------------------------------------

# Sentence-STARTING words that mark a continuation (never a clean clip start).
# Matched case-insensitively at the start of the sentence (word-boundary aware).
_BAD_START_RE = re.compile(
    r"^(?:so|and|but|well|yeah|right|exactly|totally|i\s+mean)\b",
    re.IGNORECASE,
)

# Sentence-ending fragments that signal an unfinished comparison.
# The sentence must end with one of these patterns (followed only by punctuation).
_TRAILING_DANGLE_RE = re.compile(
    r"\b(?:like|than|as\s+if)\s*[.!?,;]*\s*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# §R.PART2 Req B1 — List/transition end-signal markers
# ---------------------------------------------------------------------------
# Canonical definition lives in core/topics.py (B1 canonical location).
# Imported here so existing code + tests that reference
# producer.boundary_check.TRANSITION_START_RE continue to work.
from core.topics import TRANSITION_START_RE  # noqa: E402 (after stdlib imports)

# Boundary-verification model preference
_DEFAULT_BOUNDARY_MODEL = "anthropic/claude-haiku-4.5"


# ---------------------------------------------------------------------------
# §R.PART2 Req B1 — Transition-trim pure function
# ---------------------------------------------------------------------------

def trim_trailing_transition(
    candidate: dict,
    sentence_spans: list[dict],
    clip_len: tuple[int, int],
) -> dict:
    """Remove a trailing list-transition sentence from the clip end (Req B1).

    If the clip's last sentence (the first span whose end >= candidate["end"])
    starts with a list or topic-transition marker (TRANSITION_START_RE), the
    clip's end is pulled back to the prior sentence boundary.

    Respects clip_len[0] (minimum duration) — returns the original candidate
    unchanged when the trim would produce a clip shorter than the minimum.

    This is a PURE function: no side-effects, no global state.

    Args:
        candidate:      Clip dict with "start" and "end" float keys.
        sentence_spans: list[{"text", "start", "end"}].
        clip_len:       (min_seconds, max_seconds).

    Returns:
        A new dict with adjusted "end", or the original candidate when no trim
        applies or the trim would violate the minimum duration.
    """
    if not sentence_spans:
        return candidate

    start = float(candidate.get("start", 0))
    end = float(candidate.get("end", 0))
    min_len = clip_len[0]
    n = len(sentence_spans)

    # Locate end sentence: first span whose end >= candidate end
    ei = n - 1
    for i, span in enumerate(sentence_spans):
        if float(span["end"]) >= end:
            ei = i
            break

    last_text = sentence_spans[ei]["text"].strip()
    if not TRANSITION_START_RE.match(last_text):
        return candidate

    # Can't trim further when at the first sentence
    if ei == 0:
        log.debug(
            "Transition trim: last sentence at index 0 — cannot trim further"
        )
        return candidate

    new_ei = ei - 1
    new_end = float(sentence_spans[new_ei]["end"])

    # Respect minimum duration
    if (new_end - start) < min_len:
        log.debug(
            "Transition trim: skipping — trim would violate min_len=%.1fs "
            "(would produce %.1fs)",
            min_len, new_end - start,
        )
        return candidate

    log.debug(
        "Transition trim: pulled end %.2f→%.2f (removed transition %r)",
        end, new_end, last_text[:60],
    )
    return {**candidate, "end": new_end}


# ---------------------------------------------------------------------------
# §R2.3 pure prefilter functions
# ---------------------------------------------------------------------------

def is_bad_start_sentence(text: str, prev_text: str = "") -> bool:  # noqa: ARG001
    """Return True when the sentence is a bad clip-start candidate.

    A sentence is a bad start when ANY of the following hold:
    - It begins with a continuation opener (So / And / But / Well / Yeah /
      Right / Exactly / Totally / I mean) — matched case-insensitively at
      word boundary.  These signal the speaker is in mid-response, not starting
      a new idea.
    - The sentence itself ends with "?" (it is an interviewer question, not an
      answer that stands on its own as a clip opener).

    Note: prev_text is accepted for future use (e.g. "previous sentence ends
    with '?'" meaning the current sentence is an answer, which is fine).  The
    argument is intentionally not used in blocking logic — answering a question
    is valid and should NOT be blocked.

    Args:
        text:      The candidate clip-start sentence.
        prev_text: The sentence immediately before (may be empty string).

    Returns:
        True  → bad start (do not start the clip here; try next sentence).
        False → acceptable start.
    """
    stripped = text.strip()
    if not stripped:
        return False

    # Continuation opener at the beginning
    if _BAD_START_RE.match(stripped):
        return True

    # The sentence itself is a question (interviewer turn used as clip start)
    if re.search(r"\?\s*$", stripped):
        return True

    return False


def needs_end_extension(text: str) -> bool:
    """Return True when the sentence ends with an unfinished comparison.

    Patterns that flag a dangling end: "like", "than", "as if" — possibly
    followed by terminal punctuation.  These indicate the speaker's thought
    was cut off mid-comparison and the clip should extend one sentence further.

    Args:
        text: The candidate clip-end sentence.

    Returns:
        True  → extend end by one sentence.
        False → acceptable ending.
    """
    return bool(_TRAILING_DANGLE_RE.search(text.strip()))


# ---------------------------------------------------------------------------
# Apply prefilters to a clip candidate
# ---------------------------------------------------------------------------

def apply_prefilters(
    candidate: dict,
    sentence_spans: list[dict],
    clip_len: tuple[int, int],
) -> dict:
    """Apply §R2.3 speaker-turn prefilters to a clip candidate.

    Uses the candidate's float start/end times to locate the corresponding
    sentence indices in sentence_spans, then:
    - Bumps the start sentence forward (max 2 bumps) when is_bad_start_sentence.
    - Extends the end sentence by 1 when needs_end_extension.
    - Enforces clip_len[1] (max duration) after extension.

    When sentence_spans is empty or the candidate cannot be aligned, returns
    the original candidate unchanged (graceful degrade).

    Args:
        candidate:      Clip dict with "start" and "end" float keys.
        sentence_spans: list[{"text", "start", "end"}] from restore_sentences
                        or build_sentence_spans.
        clip_len:       (min_seconds, max_seconds).

    Returns:
        Adjusted clip dict (new dict, original is unmodified).
    """
    if not sentence_spans:
        return candidate

    start = float(candidate.get("start", 0))
    end = float(candidate.get("end", 0))
    max_len = clip_len[1]
    n = len(sentence_spans)

    # Find start sentence: last span whose start <= candidate start
    si = 0
    for i, span in enumerate(sentence_spans):
        if float(span["start"]) <= start:
            si = i

    # Find end sentence: first span whose end >= candidate end
    ei = n - 1
    for i, span in enumerate(sentence_spans):
        if float(span["end"]) >= end:
            ei = i
            break

    # ── Start prefilter: bump forward (max 2 bumps) ──────────────────────────
    bumps = 0
    while bumps < 2 and si < n:
        prev_text = sentence_spans[si - 1]["text"] if si > 0 else ""
        if is_bad_start_sentence(sentence_spans[si]["text"], prev_text):
            new_si = si + 1
            if new_si > ei:
                break   # bumping past the end would remove the clip entirely
            si = new_si
            bumps += 1
        else:
            break

    # ── End prefilter: extend by 1 sentence when dangling ────────────────────
    if ei < n - 1 and needs_end_extension(sentence_spans[ei]["text"]):
        ei += 1

    # ── Transition trim (Req B1): pull back if last sentence is a list/topic
    #    transition marker ("Number 16", "Next up", "Moving on", etc.)
    #    Loop so a run of consecutive transition sentences at the tail (e.g.
    #    "Also, one more thing." then "Number 16, CAX.") is fully trimmed, not
    #    just the last one. Bounded to avoid any pathological spin.
    _tmp_start = float(sentence_spans[si]["start"])
    for _ in range(4):
        _tmp_end = float(sentence_spans[ei]["end"])
        _trimmed = trim_trailing_transition(
            {**candidate, "start": _tmp_start, "end": _tmp_end},
            sentence_spans,
            clip_len,
        )
        if _trimmed["end"] >= _tmp_end:
            break  # no transition at the tail (or trim would break clip_len min)
        # Find sentence index matching the trimmed end
        matched = False
        for _k in range(ei - 1, si - 1, -1):
            if abs(float(sentence_spans[_k]["end"]) - _trimmed["end"]) < 0.01:
                ei = _k
                matched = True
                break
        if not matched:
            # Trim landed on no sentence boundary — should be unreachable, but
            # discarding it silently would hide a real bug. Surface and stop.
            log.warning(
                "Transition trim: no sentence index match for end=%.3f — trim discarded",
                _trimmed["end"],
            )
            break

    # ── Enforce max duration ──────────────────────────────────────────────────
    new_start = float(sentence_spans[si]["start"])
    new_end = float(sentence_spans[ei]["end"])
    while (new_end - new_start) > max_len and ei > si:
        ei -= 1
        new_end = float(sentence_spans[ei]["end"])

    if bumps > 0 or new_start != start or new_end != end:
        log.debug(
            "Prefilter adjusted clip: start %.2f→%.2f end %.2f→%.2f (bumps=%d)",
            start, new_start, end, new_end, bumps,
        )

    return {**candidate, "start": new_start, "end": new_end}


# ---------------------------------------------------------------------------
# §R2.4 — Pre-render LLM boundary verification
# ---------------------------------------------------------------------------

def _get_boundary_client() -> tuple[Any, str] | None:
    """Build an Anthropic client for boundary verification.

    Uses BOUNDARY_CHECK_MODEL (default anthropic/claude-haiku-4.5).
    Falls back to LLM_MODEL when BOUNDARY_CHECK_MODEL is not set.
    Returns None if LLM_API_KEY is missing.
    """
    try:
        import anthropic  # type: ignore[import]
    except ImportError:
        log.warning("anthropic SDK not available; boundary verification skipped")
        return None

    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        # Try settings
        try:
            from core.settings import get_settings
            settings = get_settings()
            api_key, _ = settings.require_llm()
        except Exception:
            log.warning("LLM_API_KEY not set; boundary verification skipped")
            return None

    model = os.environ.get(
        "BOUNDARY_CHECK_MODEL",
        os.environ.get("LLM_MODEL", _DEFAULT_BOUNDARY_MODEL),
    )

    base_url = os.environ.get("LLM_BASE_URL")
    if base_url is None and api_key.startswith("sk-or-"):
        base_url = "https://openrouter.ai/api"

    client = (
        anthropic.Anthropic(api_key=api_key, base_url=base_url)
        if base_url
        else anthropic.Anthropic(api_key=api_key)
    )
    return client, model


def _build_boundary_prompt(
    before_sentences: list[str],
    clip_sentences: list[str],
    after_sentences: list[str],
) -> str:
    """Build the boundary verification prompt per the spec pattern."""
    from core.fewshot import REAL_BOUNDARY_PAIRS  # local import: only needed when building the prompt

    before_block = (
        "\n".join(f"  {s}" for s in before_sentences)
        if before_sentences
        else "  (start of transcript)"
    )
    clip_block = "\n".join(f"  [{i}] {s}" for i, s in enumerate(clip_sentences))
    after_block = (
        "\n".join(f"  {s}" for s in after_sentences)
        if after_sentences
        else "  (end of transcript)"
    )

    real_pairs_section = (
        f"\n{REAL_BOUNDARY_PAIRS}\n" if REAL_BOUNDARY_PAIRS else ""
    )

    return f"""You are a clip boundary quality reviewer for short-form social-media clips.
{real_pairs_section}
FEW-SHOT EXAMPLES OF CORRECT VERDICTS:

Example A — LIST ITEM BLEED (clip-80, Selank) — FIXABLE by end trim:
  Clip sentences:
    [0] "...some people report having a lot less daily anxiety when they use it, \
but there's really mixed results. Some people have worse anxiety."
    [1] "I'm not sure that I would try this one again."
    [2] "Number 16, CAX. This is kind of like taking Adderall..."
  Context after: "...with less jitteriness..."
  Analysis: Sentence [1] ends the Selank idea cleanly. Sentence [2] starts a NEW \
list item — "Number 16, CAX" is the next peptide in an enumerated list.
  Correct verdict: {{"verdict": "fail", "reason": "Last sentence starts a new \
list item (Number 16, CAX); trim it.", "adjusted_start_sentences": 0, \
"adjusted_end_sentences": -1}}
  NOTE: adjusted_end_sentences=-1 is NON-ZERO → the adjusted clip will be KEPT.

Example B — HOOK/BODY MISMATCH (clip-76, CJC vs retatrutide) — UNFIXABLE, whole span wrong:
  Hook: "GH secretagogues like CJC-1295 and ipamorelin are permissive anabolics."
  Clip sentences:
    [0] "...allodynia where their skin felt like it had been sunburned..."
    [1] "...glucagon receptors on sensory neurons..."
    [2] "...the risk for pancreatitis and gallstones..."
    [3] "Now again, a lot of people are getting their hands on retatrutide..."
    [4] "For retatrutide, in the trials, the doses were 2mg, 4, 6, 9, 12..."
  Analysis: The hook promises content about CJC-1295 secretagogues. The entire \
clip body is about retatrutide side effects and dosing — a completely different \
subject. No boundary trim can fix this; the whole span is semantically wrong.
  Correct verdict: {{"verdict": "fail", "reason": "Body never delivers the hook: \
hook claims CJC-1295 secretagogues but clip is entirely about retatrutide side \
effects and dosing.", "adjusted_start_sentences": 0, "adjusted_end_sentences": 0}}
  NOTE: BOTH adjustments are 0 → this clip will be DROPPED. Only use this when \
  trimming cannot fix the problem.

---

CONTEXT BEFORE THE CLIP (not part of the clip — shown for coherence only):
{before_block}

CLIP SENTENCES (these are the sentences currently selected for the clip):
{clip_block}

CONTEXT AFTER THE CLIP (not part of the clip — shown for boundary judgement):
{after_block}

Inspect the clip and answer:
1. Does the clip START on the first word of a self-contained thought? (never a \
continuation opener: So/And/But/Well/Yeah/Right/Exactly/Totally/I mean; never \
an interviewer question ending in "?")
2. Be STRICT about the END. The clip must end the MOMENT the specific idea resolves. \
If the last 1-2 sentences introduce a new list item ("Number X", "Next up", \
"Moving on"), a new named entity, a new question, a tangent, or a generic medical \
disclaimer, set verdict=fail and return adjusted_end_sentences as a NEGATIVE integer \
to trim those sentences off. Never let the clip bleed past the resolution of its \
main idea.
3. If there are adjustment improvements, express them as deltas to the current \
start/end sentence indices shown above (e.g. adjusted_start_sentences=+1 means \
"skip the first sentence shown", adjusted_end_sentences=-1 means "drop the last sentence").

Return ONLY this JSON (no prose, no code fences):
{{
  "verdict": "pass" or "fail",
  "reason": "<one line explaining the verdict>",
  "adjusted_start_sentences": <int, 0 if no adjustment>,
  "adjusted_end_sentences": <int, 0 if no adjustment>
}}

Rules:
- "pass" = the clip starts and ends cleanly on its own idea. Set both adjustments to 0.
- "fail" with NON-ZERO adjustments = the boundary is FIXABLE by trimming. The \
  adjusted clip will be KEPT — not discarded. ALWAYS prefer this path: if dropping \
  1-3 sentences from the start or end would make the clip valid, set the appropriate \
  adjustment to a non-zero value and return fail.
- "fail" with BOTH adjustments zero = the clip is UNFIXABLE by any boundary trim. \
  Reserve ONLY for hook/body mismatches where the entire clip body is semantically \
  wrong (see Example B). This causes the clip to be DROPPED. Do NOT use when a trim \
  can fix the problem.
- adjusted_start_sentences: positive int = skip N sentences from the clip start; 0 or negative = no change.
- adjusted_end_sentences: negative int = trim N sentences from the clip end; 0 or positive = no change.
- Maximum adjustment: +/-3 sentences in either direction."""


def _apply_boundary_deltas(
    candidate: dict,
    sentence_spans: list[dict],
    clip_si: int,
    clip_ei: int,
    delta_start: int,
    delta_end: int,
    clip_len: tuple[int, int],
) -> tuple[dict, int, int]:
    """Apply start/end sentence index deltas from the verifier.

    Returns (adjusted_candidate, new_si, new_ei).
    """
    n = len(sentence_spans)
    max_len = clip_len[1]
    min_len = clip_len[0]

    # Apply deltas (clamp to valid range)
    new_si = max(0, min(n - 1, clip_si + max(0, delta_start)))
    new_ei = max(new_si, min(n - 1, clip_ei + min(0, delta_end)))

    # Enforce clip duration bounds
    new_start = float(sentence_spans[new_si]["start"])
    new_end = float(sentence_spans[new_ei]["end"])
    while (new_end - new_start) > max_len and new_ei > new_si:
        new_ei -= 1
        new_end = float(sentence_spans[new_ei]["end"])
    while (new_end - new_start) < min_len and new_ei < n - 1:
        new_ei += 1
        new_end = float(sentence_spans[new_ei]["end"])

    return {**candidate, "start": new_start, "end": new_end}, new_si, new_ei


def verify_boundaries(
    candidate: dict,
    sentence_spans: list[dict],
    clip_len: tuple[int, int],
) -> tuple[dict, bool]:
    """Run pre-render LLM boundary verification for one clip candidate (spec R2.4).

    Repair-first strategy (adjust-first, drop-last):

    - verdict=pass: keep the clip (with any adjustments applied).
    - verdict=fail + NON-ZERO adjustments: REPAIR by applying the adjustment and
      keeping the clip without re-verifying.  Drop only when the repaired clip
      would be shorter than clip_len[0] (min duration) -- the repair itself
      violated the window constraint.
    - verdict=fail + BOTH adjustments zero: try a start-shift -- expand the clip
      start backward by one sentence and re-verify once (bounded, never loops).
      Keep if the re-verify passes; drop only when the re-verify also fails.

    Transport/parse errors: treat as pass (pipeline never blocked).

    Args:
        candidate:      Clip dict with "start" and "end" float keys.
        sentence_spans: Sentence spans from restore_sentences / build_sentence_spans.
        clip_len:       (min_seconds, max_seconds).

    Returns:
        (adjusted_candidate, should_keep)
        should_keep=False means caller should not render this clip (saves GPU spend).
    """
    if not sentence_spans:
        # No spans available — treat as pass (cannot verify without sentences)
        return candidate, True

    # Locate clip sentence indices
    start = float(candidate.get("start", 0))
    end = float(candidate.get("end", 0))
    n = len(sentence_spans)

    si = 0
    for i, span in enumerate(sentence_spans):
        if float(span["start"]) <= start:
            si = i
    ei = n - 1
    for i, span in enumerate(sentence_spans):
        if float(span["end"]) >= end:
            ei = i
            break

    # Build context windows
    before = [sentence_spans[i]["text"] for i in range(max(0, si - 3), si)]
    clip = [sentence_spans[i]["text"] for i in range(si, min(ei + 1, n))]
    after = [sentence_spans[i]["text"] for i in range(ei + 1, min(ei + 4, n))]

    if not clip:
        return candidate, True

    # ── First verification call ───────────────────────────────────────────────
    try:
        result = _get_boundary_client()
        if result is None:
            return candidate, True
        client, model = result

        prompt = _build_boundary_prompt(before, clip, after)
        from core.llm import create_completion, extract_text
        message = create_completion(
            client, model, 256, [{"role": "user", "content": prompt}]
        )
        raw = extract_text(message)
        verdict_obj = _parse_boundary_verdict(raw)
    except Exception as exc:
        log.warning("Boundary verify call failed (non-fatal, treating as pass): %s", exc)
        return candidate, True

    verdict = verdict_obj.get("verdict", "pass")
    delta_start = int(verdict_obj.get("adjusted_start_sentences", 0))
    delta_end = int(verdict_obj.get("adjusted_end_sentences", 0))
    reason = str(verdict_obj.get("reason", ""))

    if verdict == "pass" and delta_start == 0 and delta_end == 0:
        log.debug("Boundary verify PASS for clip start=%.2f end=%.2f", start, end)
        return candidate, True

    # ── Apply adjustments ─────────────────────────────────────────────────────
    adjusted, new_si, new_ei = _apply_boundary_deltas(
        candidate, sentence_spans, si, ei, delta_start, delta_end, clip_len
    )
    log.debug(
        "Boundary verify applied delta_start=%d delta_end=%d reason=%r: %.2f→%.2f / %.2f→%.2f",
        delta_start, delta_end, reason,
        start, adjusted["start"], end, adjusted["end"],
    )

    if verdict == "pass":
        # Pass with minor adjustments — accept
        return adjusted, True

    # ── verdict == "fail" ─────────────────────────────────────────────────────

    if delta_start != 0 or delta_end != 0:
        # Fail with non-zero adjustment → REPAIR: apply adj and keep without
        # re-verifying.  _apply_boundary_deltas already enforces both min and
        # max clip_len, so the adjusted candidate is always valid.  Only drop
        # when the verifier marks the WHOLE SPAN wrong (adj=0,0 path below).
        log.info(
            "Boundary verify REPAIRED (delta_start=%d delta_end=%d): "
            "[%.2f,%.2f]->[%.2f,%.2f] reason=%r",
            delta_start, delta_end,
            start, end, adjusted["start"], adjusted["end"], reason,
        )
        return adjusted, True

    # ── Fail + adj=0,0: start-shift repair (one round, never loops) ──────────
    # The verifier flagged the whole span as wrong (no fixable trim identified).
    # Before dropping, try expanding the clip start back by one sentence — this
    # catches the "start-mid-thought" case where the real opening sentence is just
    # before what the ranker chose.  Only possible when si > 0.
    if si <= 0:
        log.info(
            "Boundary verify DROP (fail, adj=0, no earlier sentence): "
            "clip [%.2f,%.2f] reason=%r",
            start, end, reason,
        )
        return candidate, False

    shift_si = si - 1
    shift_start = float(sentence_spans[shift_si]["start"])
    shift_candidate = {**candidate, "start": shift_start}

    before2 = [sentence_spans[i]["text"] for i in range(max(0, shift_si - 3), shift_si)]
    clip2 = [sentence_spans[i]["text"] for i in range(shift_si, min(ei + 1, n))]
    after2 = [sentence_spans[i]["text"] for i in range(ei + 1, min(ei + 4, n))]

    try:
        from core.llm import create_completion, extract_text  # already imported above; safe re-import
        prompt2 = _build_boundary_prompt(before2, clip2, after2)
        message2 = create_completion(
            client, model, 256, [{"role": "user", "content": prompt2}]
        )
        raw2 = extract_text(message2)
        verdict2_obj = _parse_boundary_verdict(raw2)
        verdict2 = verdict2_obj.get("verdict", "pass")
        delta2_start = int(verdict2_obj.get("adjusted_start_sentences", 0))
        delta2_end = int(verdict2_obj.get("adjusted_end_sentences", 0))
        reason2 = str(verdict2_obj.get("reason", ""))
    except Exception as exc:
        log.warning(
            "Boundary start-shift re-verify failed (non-fatal, keeping): %s", exc
        )
        return shift_candidate, True

    if verdict2 == "pass":
        # Apply any fine-tuning adjustments from the second call
        if delta2_start != 0 or delta2_end != 0:
            shift_adjusted, _, _ = _apply_boundary_deltas(
                shift_candidate, sentence_spans, shift_si, ei,
                delta2_start, delta2_end, clip_len,
            )
            log.info(
                "Boundary verify REPAIRED (start-shift+adj): "
                "[%.2f,%.2f]→[%.2f,%.2f] delta_start2=%d delta_end2=%d",
                start, end,
                shift_adjusted["start"], shift_adjusted["end"],
                delta2_start, delta2_end,
            )
            return shift_adjusted, True
        log.info(
            "Boundary verify REPAIRED (start-shift): [%.2f,%.2f] start %.2f→%.2f reason=%r",
            start, end, start, shift_start, reason,
        )
        return shift_candidate, True

    # Start-shift re-verify also failed → DROP
    log.info(
        "Boundary verify DROP (fail+shift also failed): "
        "clip [%.2f,%.2f] reason=%r → reason2=%r",
        start, end, reason, reason2,
    )
    return shift_candidate, False


def _parse_boundary_verdict(text: str) -> dict[str, Any]:
    """Extract the JSON verdict object from the boundary-check LLM response.

    Returns a safe default (pass, no adjustments) on parse failure.
    """
    default: dict[str, Any] = {
        "verdict": "pass",
        "reason": "parse error — treating as pass",
        "adjusted_start_sentences": 0,
        "adjusted_end_sentences": 0,
    }
    try:
        text = re.sub(r"```(?:json)?", "", text).strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return default
        obj = json.loads(match.group())
        return {**default, **obj}
    except Exception:
        return default
