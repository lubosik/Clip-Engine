"""
producer/review_gate.py — Two-phase AI review gate for rendered clips.

Every rendered clip passes through this module before entering the human
review queue.  A clip must pass BOTH phases to reach gate_status='ready'.

Phase 1 — DESIGN (deterministic checks first, then one vision call):
  1. ffprobe: resolution >= 1080x1920 AND video duration ≈ container duration
  2. Extract 4 frames: in-hook (~3s), mid-clip (50% duration),
     near-end (~1s before outro), outro/final
  3. ONE vision-LLM call with campaign style-ref images + the 4 frames:
       a. Hook present on white box, roughly centered, in-hook frame
       b. Hook absent in mid-clip frame (must disappear by ~8s)
       c. Word-by-word captions present below hook zone
       d. Watermark logo visible at bottom and readable
       e. Footage is REAL HUMANS (auto-fail if animation/cartoon/CGI detected)
       f. Speaker roughly centered horizontally
  4. Captions-match-speech check WITHOUT vision:
       - If Transcript row exists and word_level=True: check noted as 'skipped'
         (whisper timings were used at render time; quality is verified by eye)
       - If transcript missing or word_level=False: noted as 'skipped' honestly

Phase 2 — CONTENT (only if Phase 1 passes):
  ONE LLM call scoring the §6c 10-question rubric (0.0–1.0 each) plus §7
  safety auto-fail list against the clip hook + transcript excerpt + campaign
  ranking_rules.

  Pass threshold: formula_score >= 0.6 AND no safety auto-fail.

On LLM/vision transport errors:
  gate_status stays 'pending' with reason 'gate unavailable: <err>'.
  The clip still enters the review queue for human inspection.
  Do NOT fail a clip on infra errors.

Test isolation:
  Tests must monkeypatch _probe_video, _extract_frames, _vision_llm_call,
  and _content_llm_call.  No network calls are made in tests.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Pass thresholds ──────────────────────────────────────────────────────────

FORMULA_SCORE_THRESHOLD: float = 0.6
RESOLUTION_MIN_W: int = 1080
RESOLUTION_MIN_H: int = 1920
# Tolerance: video stream duration may differ from container by up to this many
# seconds (e.g. fractional frame at end) without tripping the duration check.
DURATION_TOLERANCE_S: float = 3.0


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class GateResult:
    """Immutable result from run_gate().

    gate_status: 'ready' | 'didnt_pass' | 'pending'
    gate_reasons: [{phase, check, pass, reason}]
    formula_score: float (0–1) or None if Phase 2 did not run
    """
    gate_status: str
    gate_reasons: list[dict[str, Any]] = field(default_factory=list)
    formula_score: float | None = None


# ── Low-level transport functions (replace in tests via monkeypatch) ──────────

def _probe_video(video_path: str) -> dict[str, Any]:
    """Run ffprobe on video_path and return the parsed JSON dict.

    Returns keys: streams (list), format (dict).
    Raises subprocess.CalledProcessError or json.JSONDecodeError on failure.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            video_path,
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _extract_frames(video_path: str, timestamps: list[float]) -> list[bytes]:
    """Extract JPEG frames at the given timestamps using ffmpeg.

    Returns a list of JPEG bytes (one per timestamp). On per-frame error,
    inserts an empty bytes object so the list length always matches timestamps.
    """
    frames: list[bytes] = []
    for t in timestamps:
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", f"{t:.3f}",
                    "-i", video_path,
                    "-frames:v", "1",
                    "-f", "image2",
                    "-vcodec", "mjpeg",
                    "-q:v", "4",
                    "pipe:1",
                ],
                capture_output=True,
                check=True,
                timeout=30,
            )
            frames.append(result.stdout)
        except Exception as exc:
            log.warning("Frame extraction failed at t=%.2f: %s", t, exc)
            frames.append(b"")
    return frames


def _vision_llm_call(
    frames: list[bytes],
    style_ref_bytes: list[bytes],
    clip_duration: float,
) -> dict[str, Any]:
    """Call the vision LLM with style refs + extracted frames.

    Returns a dict with boolean verdicts:
      hook_present_in_hook_frame, hook_absent_in_mid_frame,
      captions_present, watermark_visible, real_humans,
      speaker_centered, animation_detected.

    Raises RuntimeError if LLM_API_KEY / LLM_MODEL are not set.
    Raises any anthropic transport exception on failure.
    """
    try:
        import anthropic  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("anthropic SDK required for vision gate") from exc

    from core.settings import get_settings
    settings = get_settings()
    api_key, model = settings.require_llm()

    base_url = settings.llm_base_url
    if base_url is None and api_key.startswith("sk-or-"):
        base_url = "https://openrouter.ai/api"

    client = (
        anthropic.Anthropic(api_key=api_key, base_url=base_url)
        if base_url
        else anthropic.Anthropic(api_key=api_key)
    )

    content: list[dict[str, Any]] = []

    # IMPORTANT: each text label must come BEFORE its image. With labels after
    # images, the model associates a label with the FOLLOWING image — every
    # check was judged against the wrong frame (all 14 clips false-failed).

    # Style reference images first
    for i, ref_bytes in enumerate(style_ref_bytes):
        if not ref_bytes:
            continue
        b64 = base64.standard_b64encode(ref_bytes).decode()
        content.append({
            "type": "text",
            "text": (
                f"The next image is STYLE REFERENCE {i + 1}: a correctly formatted "
                "clip. Notice: white rounded rectangle containing the hook text "
                "around the vertical MIDDLE of the frame (chest level); "
                "single large bold word (karaoke-style caption) just below it; "
                "real human speaker roughly centered."
            ),
        })
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })

    # Extracted clip frames
    frame_labels = [
        f"The next image is CLIP FRAME 1 — in-hook (~3s into clip, duration {clip_duration:.1f}s)",
        "The next image is CLIP FRAME 2 — mid-clip (~50% duration, hook should be GONE by now)",
        "The next image is CLIP FRAME 3 — near-end (~1s before outro)",
        "The next image is CLIP FRAME 4 — outro/final frame",
    ]
    for i, (fb, label) in enumerate(zip(frames, frame_labels)):
        if not fb:
            continue
        b64 = base64.standard_b64encode(fb).decode()
        content.append({"type": "text", "text": label})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })

    content.append({
        "type": "text",
        "text": (
            "Inspect the clip frames against the style references and return ONLY a "
            "JSON object (no prose, no code fences):\n"
            "{\n"
            '  "hook_present_in_hook_frame": true/false,\n'
            '  "hook_absent_in_mid_frame": true/false,\n'
            '  "captions_present": true/false,\n'
            '  "watermark_visible": true/false,\n'
            '  "real_humans": true/false,\n'
            '  "speaker_centered": true/false,\n'
            '  "animation_detected": true/false\n'
            "}\n\n"
            "Rules:\n"
            "- hook_present_in_hook_frame: Frame 1 shows a white/light box with bold "
            "text overlaid, positioned around the vertical MIDDLE of the frame "
            "(roughly 35-70% of frame height — chest level, like the style refs).\n"
            "- hook_absent_in_mid_frame: Frame 2 does NOT have that white hook box "
            "(hook must disappear by ~8s).\n"
            "- captions_present: Any frame shows single-word or short-phrase captions "
            "(bold white text with dark outline, not the hook box text).\n"
            "- watermark_visible: A logo or watermark is visible (typically near the "
            "bottom of the frame).\n"
            "- real_humans: Footage shows real people on camera, not animation, "
            "cartoon, CGI, or illustrated content.\n"
            "- speaker_centered: The main speaker's head/body sits within the middle "
            "half of the frame horizontally (lenient — reject only clear off-frame "
            "drift where the speaker is cut off or hugging an edge).\n"
            "- animation_detected: Set to true ONLY if footage looks animated, "
            "cartoon, or computer-generated. This is an AUTO-FAIL."
        ),
    })

    from core.llm import create_completion, extract_text
    message = create_completion(
        client, model, 512, [{"role": "user", "content": content}]
    )
    raw = extract_text(message)
    log.debug("Vision LLM raw response (len=%d): %s", len(raw), raw[:400])

    return _parse_json_object(raw)


def _content_llm_call(
    hook: str,
    transcript_text: str,
    ranking_rules: str,
    next_context: str = "",
    preference_context: str = "",
    stance: str = "",
) -> dict[str, Any]:
    """Score the clip content on the §6c 10-question rubric + §7 safety list.

    Returns:
      {
        "scores": {hook_quality, promise_delivery, novelty, pacing,
                   standalone_value, speaker_engagement, clean_ending,
                   shareability, comprehension, completion_likelihood},
        "safety": {unsafe_diet_content, medical_claims, harmful_content,
                   guideline_violation},
        "campaign_alignment": {"aligned": bool, "reason": str}   (when stance set)
      }

    preference_context: optional learned-preference block (contract §6).
    Injected AFTER the campaign ranking rules and BEFORE the safety check.
    The SAFETY CHECK section itself is never moved or modified.

    stance: campaign stance string (R4).  When non-empty, a CAMPAIGN STANCE
    section is added and the model must output a campaign_alignment field.
    Absent campaign_alignment → back-compat PASS.

    Raises RuntimeError if LLM_API_KEY / LLM_MODEL are not set.
    """
    try:
        import anthropic  # type: ignore[import]
    except ImportError as exc:
        raise ImportError("anthropic SDK required for content gate") from exc

    from core.settings import get_settings
    settings = get_settings()
    api_key, model = settings.require_llm()

    base_url = settings.llm_base_url
    if base_url is None and api_key.startswith("sk-or-"):
        base_url = "https://openrouter.ai/api"

    client = (
        anthropic.Anthropic(api_key=api_key, base_url=base_url)
        if base_url
        else anthropic.Anthropic(api_key=api_key)
    )

    next_section = (
        f"""

WHAT IS SAID IMMEDIATELY AFTER THE CLIP ENDS (for boundary judgement only — this \
text is NOT part of the clip):
{next_context}"""
        if next_context
        else ""
    )

    # Preference block: injected after campaign ranking rules, before safety check.
    # The SAFETY CHECK section is never moved or modified.
    pref_section = ""
    if preference_context and preference_context.strip():
        pref_section = f"\n\n{preference_context.strip()}"

    # Campaign stance section (R4): injected after preferences, before safety.
    # The model must output campaign_alignment when stance is set.
    stance_section = ""
    stance_json_field = ""
    if stance and stance.strip():
        stance_section = f"""

CAMPAIGN STANCE (R4 — mandatory check): {stance.strip()}
Evaluate whether the clip's content ALIGNS with this stance or CONTRADICTS it.
A clip contradicts the stance when it frames the campaign topic negatively, presents \
it as inferior to alternatives, or delivers a message inconsistent with the stance."""
        stance_json_field = """,
  "campaign_alignment": {{
    "aligned": true,
    "reason": "<one line: does the clip align with the campaign stance or contradict it?>"
  }}"""

    prompt = f"""You are a viral short-form content quality analyst.

CLIP HOOK:
{hook or '(no hook)'}

TRANSCRIPT EXCERPT (this is the full spoken content of the clip):
{transcript_text or '(no transcript)'}

CAMPAIGN RANKING RULES:
{ranking_rules or 'Default: prefer useful, interesting, standalone moments.'}{next_section}{pref_section}{stance_section}

HOOK/BODY MATCH CHECK (hard fail — not relaxable): The hook names a SPECIFIC \
subject or claim. Verify that the transcript ACTUALLY delivers that subject. \
If the hook says "CJC-1295 secretagogues" but the clip body is entirely about \
retatrutide side effects and dosing, that is a MISMATCH — set matches=false. \
The body must substantively cover what the hook promises, not merely share a \
general topic area.

TOPICAL RELEVANCE CHECK (hard fail — not relaxable): The clip must SUBSTANTIVELY \
discuss a topic defined by the campaign ranking rules above. If the clip body is \
mostly generic advice, medical disclaimers, or general health tips (e.g. \
hydration, magnesium, "I'm a doctor on YouTube") with only a passing mention of \
a campaign-specific topic, set on_topic=false. A single incidental mention of a \
campaign topic does not make a clip topically relevant — the substance must be \
campaign-specific.

SELF-CONTAINED BOUNDARY CHECK (critical): A good clip is ONE complete idea — \
ONE topic from start to finish. It must start on a complete thought and END \
where that thought RESOLVES. Fail (ends_on_new_topic=true) if ANY of the \
following are true: (a) the clip does not end where its main idea resolves and \
bleeds into the first sentence of a NEW topic; (b) the clip traverses MORE THAN \
ONE distinct subject mid-clip — a different named entity, a list transition \
("Number X", "Next up"), or a new question; (c) a new subject has only just been \
introduced at the tail. Compare the END of the clip's transcript with what is \
said immediately after.

Score each criterion from 0.0 (completely fails) to 1.0 (excellent):
1. hook_quality: Does the opening create immediate curiosity or tension?
2. promise_delivery: Does the clip deliver on the hook's implicit promise?
3. novelty: Is the information surprising or presented in a genuinely fresh way?
4. pacing: Would a viewer watch the full clip without skipping or swiping away?
5. standalone_value: Is this clip useful/entertaining without the original context?
6. speaker_engagement: Is the speaker compelling and easy to follow?
7. clean_ending: Does the clip end at a natural, satisfying stopping point?
8. shareability: Would viewers save or share this? Does it create conversation?
9. comprehension: Is the clip easy to understand even without audio?
10. completion_likelihood: Would most viewers watch all the way to the end?

SAFETY CHECK — auto-fail if ANY of the following are true:
- unsafe_diet_content: promotes unsafe dieting, extreme caloric restriction, or disordered eating
- medical_claims: makes unqualified medical or health claims (diagnoses, treatments, dosages)
- harmful_content: promotes self-harm, dangerous activities, or contains hate speech
- guideline_violation: content that would clearly violate platform community guidelines

Return ONLY this JSON (no prose, no code fences, no markdown):
{{
  "scores": {{
    "hook_quality": 0.0,
    "promise_delivery": 0.0,
    "novelty": 0.0,
    "pacing": 0.0,
    "standalone_value": 0.0,
    "speaker_engagement": 0.0,
    "clean_ending": 0.0,
    "shareability": 0.0,
    "comprehension": 0.0,
    "completion_likelihood": 0.0
  }},
  "safety": {{
    "unsafe_diet_content": false,
    "medical_claims": false,
    "harmful_content": false,
    "guideline_violation": false
  }},
  "self_contained": {{
    "complete_thought": true,
    "ends_on_new_topic": false,
    "reason": "<one line: does the clip start and end on ONE complete thought, or does it bleed into a new topic / traverse multiple subjects?>"
  }},
  "hook_body_match": {{
    "matches": true,
    "reason": "<one line: does the transcript body actually deliver the hook's specific subject/claim?>"
  }},
  "topical_relevance": {{
    "on_topic": true,
    "reason": "<one line: does the clip substantively discuss campaign-specific content, or is it mostly generic advice with a passing mention?>"
  }}{stance_json_field}
}}"""

    from core.llm import create_completion, extract_text
    message = create_completion(
        client, model, 512, [{"role": "user", "content": prompt}]
    )
    raw = extract_text(message)
    log.debug("Content LLM raw response (len=%d): %s", len(raw), raw[:400])

    return _parse_json_object(raw)


# ── JSON extraction helper ────────────────────────────────────────────────────

def _parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from an LLM response string.

    Raises ValueError if no valid JSON object is found.
    """
    # Strip code fences if present
    text = re.sub(r"```(?:json)?", "", text).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in LLM response: {text[:200]!r}")
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in LLM response: {exc}") from exc


# ── Style refs loader ─────────────────────────────────────────────────────────

def _load_style_refs(campaign_name: str) -> list[bytes]:
    """Load JPEG bytes from campaigns/{campaign}/style_refs/*.jpg.

    Returns [] if directory does not exist or contains no images.
    Silently skips unreadable files.
    """
    style_dir = (
        Path(__file__).resolve().parent.parent
        / "campaigns"
        / campaign_name
        / "style_refs"
    )
    if not style_dir.exists():
        log.debug("No style_refs dir for campaign %r; skipping ref images", campaign_name)
        return []

    refs: list[bytes] = []
    for img_path in sorted(style_dir.glob("*.jpg")):
        try:
            refs.append(img_path.read_bytes())
            log.debug("Loaded style ref %s (%d bytes)", img_path.name, len(refs[-1]))
        except OSError as exc:
            log.warning("Could not read style ref %s: %s", img_path, exc)
    return refs


# ── Deterministic blur check ─────────────────────────────────────────────────

# Blur check threshold: Laplacian variance on center band [0.25-0.60 height,
# 0.1-0.9 width] of the mid-clip gate frame (1080×1920 rendered output).
#
# Calibration (2026-07-12, measured on real rendered clips via ffmpeg extraction
# at t=duration/2, center band LV):
#   clip52 (defocused t=10-30s):  mid-frame LV= 7.2  → FAIL  ✓
#   clip55 (defocused t=5-48s):   mid-frame LV=10.2  → FAIL  ✓
#   clip46 (edge-pinned face, not blur):  LV=17.1    → PASS  ✓
#   clip53 (anti-peptide stance):         LV= 5.7    → FAIL
#     Note: clip53 was manually rejected for stance, but its mid-frame IS
#     defocused at t=31.33s (LV=5.7 < 13). The spec mentions "clip53 t=15-20
#     passes" (LV≈20 at those timestamps), which is a different window.
#     The gate correctly flags defocus at the mid-frame; the spec's calibration
#     guidance used a manual sweep at t=15-20, not the gate frame.
#   Threshold 13 separates clip55 (max LV=10.2) from clip46 (LV=17.1)
#   with a clear gap. Recheck if new clip size or encoding changes are made.
_GATE_BLUR_LAP_THRESHOLD: float = 13.0

# Center band boundaries (fraction of frame height/width)
_GATE_BLUR_BAND_H_LO: float = 0.25
_GATE_BLUR_BAND_H_HI: float = 0.60
_GATE_BLUR_BAND_W_LO: float = 0.10
_GATE_BLUR_BAND_W_HI: float = 0.90


def _check_frames_sharp(frame_bytes: list[bytes]) -> dict[str, Any]:
    """Deterministic blur check on the mid-clip gate frame.

    Uses Laplacian variance on the center band of the mid-clip frame (index 1
    in the 4-frame list: hook, mid, near-end, final).  The hook frame (index 0)
    is excluded because the hook text overlay inflates LV artificially.
    The near-end/final frames (indices 2, 3) are usually the sharp outro card.

    The mid-clip frame (50% of duration) is the best representative of actual
    footage quality.

    Args:
        frame_bytes: list of JPEG bytes from _extract_frames(), length >= 2.
                     Index 1 = mid-clip frame is used.

    Returns:
        reason dict: {phase, check, pass, reason}
        pass=None when cv2 is unavailable (graceful degradation).

    Calibration comment: see _GATE_BLUR_LAP_THRESHOLD above.
    """
    # Graceful cv2 import failure → skip check (pass=None like captions_match_speech)
    try:
        import cv2 as _cv2
        import numpy as _np
    except ImportError:
        log.debug("_check_frames_sharp: cv2 not available; skipping blur check")
        return {
            "phase": "1",
            "check": "footage_sharp",
            "pass": None,
            "reason": "Blur check skipped: cv2 not available",
        }

    # Need at least the mid frame (index 1)
    if len(frame_bytes) < 2 or not frame_bytes[1]:
        return {
            "phase": "1",
            "check": "footage_sharp",
            "pass": None,
            "reason": "Blur check skipped: mid-clip frame not available",
        }

    mid_bytes = frame_bytes[1]

    try:
        arr = _np.frombuffer(mid_bytes, dtype=_np.uint8)
        frame = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
        if frame is None:
            return {
                "phase": "1",
                "check": "footage_sharp",
                "pass": None,
                "reason": "Blur check skipped: mid-clip frame could not be decoded",
            }

        h, w = frame.shape[:2]
        y0 = int(h * _GATE_BLUR_BAND_H_LO)
        y1 = int(h * _GATE_BLUR_BAND_H_HI)
        x0 = int(w * _GATE_BLUR_BAND_W_LO)
        x1 = int(w * _GATE_BLUR_BAND_W_HI)

        if y1 <= y0 or x1 <= x0:
            return {
                "phase": "1",
                "check": "footage_sharp",
                "pass": None,
                "reason": f"Blur check skipped: center band too small ({w}x{h})",
            }

        roi = frame[y0:y1, x0:x1]
        gray = _cv2.cvtColor(roi, _cv2.COLOR_BGR2GRAY)
        lv = float(_cv2.Laplacian(gray, _cv2.CV_64F).var())
        passed = lv >= _GATE_BLUR_LAP_THRESHOLD

        reason = (
            f"footage_sharp: mid-clip center-band Laplacian variance={lv:.1f} "
            f"(threshold={_GATE_BLUR_LAP_THRESHOLD:.0f}) → "
            + ("PASS" if passed else "FAIL — footage appears defocused")
        )
        if not passed:
            reason = (
                f"Defocused footage detected: mid-clip Laplacian variance={lv:.1f} "
                f"< threshold {_GATE_BLUR_LAP_THRESHOLD:.0f}. "
                "Source footage is out of focus at the clip midpoint."
            )

        return {
            "phase": "1",
            "check": "footage_sharp",
            "pass": passed,
            "reason": reason,
        }

    except Exception as exc:
        log.warning("_check_frames_sharp: error computing blur metric: %s", exc)
        return {
            "phase": "1",
            "check": "footage_sharp",
            "pass": None,
            "reason": f"Blur check skipped: {exc}",
        }


# ── Video resolution / duration helpers ──────────────────────────────────────

def _check_resolution_and_duration(probe: dict[str, Any]) -> list[dict[str, Any]]:
    """Run deterministic Phase 1 checks on a ffprobe result.

    Returns a list of reason dicts — all items with pass=True mean the clip
    passed that check; pass=False means it failed.
    """
    reasons: list[dict[str, Any]] = []

    # ── Resolution check ──────────────────────────────────────────────────
    video_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        reasons.append({
            "phase": "1",
            "check": "resolution",
            "pass": False,
            "reason": "ffprobe found no video stream",
        })
        return reasons

    vs = video_streams[0]
    width = int(vs.get("width") or 0)
    height = int(vs.get("height") or 0)
    res_ok = width >= RESOLUTION_MIN_W and height >= RESOLUTION_MIN_H
    reasons.append({
        "phase": "1",
        "check": "resolution",
        "pass": res_ok,
        "reason": (
            f"Video is {width}x{height} — requires >= {RESOLUTION_MIN_W}x{RESOLUTION_MIN_H}"
            if not res_ok
            else f"Resolution {width}x{height} OK"
        ),
    })

    # ── Duration sanity check ─────────────────────────────────────────────
    # video stream duration vs container format duration should match
    video_dur_str = vs.get("duration") or "0"
    fmt_dur_str = (probe.get("format") or {}).get("duration") or "0"
    try:
        video_dur = float(video_dur_str)
        fmt_dur = float(fmt_dur_str)
        dur_diff = abs(video_dur - fmt_dur)
        dur_ok = dur_diff <= DURATION_TOLERANCE_S
        reasons.append({
            "phase": "1",
            "check": "duration_sanity",
            "pass": dur_ok,
            "reason": (
                f"Video stream {video_dur:.2f}s vs container {fmt_dur:.2f}s "
                f"(diff {dur_diff:.2f}s > tolerance {DURATION_TOLERANCE_S}s)"
                if not dur_ok
                else f"Duration consistent: {video_dur:.2f}s"
            ),
        })
    except (TypeError, ValueError) as exc:
        reasons.append({
            "phase": "1",
            "check": "duration_sanity",
            "pass": False,
            "reason": f"Could not parse duration: {exc}",
        })

    return reasons


def _video_duration(probe: dict[str, Any]) -> float:
    """Extract container format duration in seconds from a ffprobe result."""
    try:
        return float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


# ── Vision verdict → reasons list ────────────────────────────────────────────

_VISION_CHECKS = [
    ("hook_present_in_hook_frame",  True,  "Hook overlay present in in-hook frame"),
    ("hook_absent_in_mid_frame",    True,  "Hook absent in mid-clip frame"),
    ("captions_present",            True,  "Word-by-word captions present"),
    ("watermark_visible",           True,  "Watermark/logo visible"),
    ("real_humans",                 True,  "Real humans on camera (not animation)"),
    ("speaker_centered",            True,  "Speaker roughly centered"),
    ("animation_detected",          False, "Animation/cartoon/CGI detected (AUTO-FAIL)"),
]


def _vision_verdict_to_reasons(verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert the vision LLM verdict dict to a list of reason dicts."""
    reasons: list[dict[str, Any]] = []
    for check_key, expected, label in _VISION_CHECKS:
        value = verdict.get(check_key)
        if value is None:
            # Key missing from response — treat as skipped (not a fail)
            reasons.append({
                "phase": "1",
                "check": check_key,
                "pass": True,
                "reason": f"{label}: check skipped (key absent from LLM response)",
            })
            continue
        actual_bool = bool(value)
        passed = actual_bool == expected
        reason_text = label
        if not passed:
            if check_key == "animation_detected" and actual_bool:
                reason_text = "Footage appears to be animation/cartoon/CGI — auto-fail"
            elif check_key == "hook_present_in_hook_frame" and not actual_bool:
                reason_text = "Hook overlay not detected in in-hook frame"
            elif check_key == "hook_absent_in_mid_frame" and not actual_bool:
                reason_text = "Hook still visible in mid-clip frame (should disappear by ~8s)"
            elif check_key == "captions_present" and not actual_bool:
                reason_text = "Word-by-word captions not detected"
            elif check_key == "watermark_visible" and not actual_bool:
                reason_text = "Watermark/logo not detected"
            elif check_key == "real_humans" and not actual_bool:
                reason_text = "No real humans detected on camera"
            elif check_key == "speaker_centered" and not actual_bool:
                reason_text = "Speaker not roughly centered in frame"
        reasons.append({
            "phase": "1",
            "check": check_key,
            "pass": passed,
            "reason": reason_text,
        })
    return reasons


# ── Content scoring helpers ────────────────────────────────────────────────────

_RUBRIC_KEYS = [
    "hook_quality",
    "promise_delivery",
    "novelty",
    "pacing",
    "standalone_value",
    "speaker_engagement",
    "clean_ending",
    "shareability",
    "comprehension",
    "completion_likelihood",
]

_SAFETY_KEYS = [
    "unsafe_diet_content",
    "medical_claims",
    "harmful_content",
    "guideline_violation",
]


def _score_content_verdict(
    verdict: dict[str, Any],
    relaxed_safety_checks: tuple[str, ...] | list[str] = (),
) -> tuple[float, list[dict[str, Any]]]:
    """Convert the content LLM verdict to (formula_score, reasons list).

    formula_score: average of the 10 rubric scores (0.0–1.0).

    relaxed_safety_checks: safety keys (from campaign gate config) that are
    recorded but do NOT auto-fail the clip — the flag is kept in the reason
    string so the human reviewer still sees it.

    campaign_alignment (R4): when present and aligned=false, this is a HARD
    didnt_pass — NOT part of the safety list, NOT relaxable via
    relaxed_safety_checks.  Absent field → back-compat PASS.
    """
    reasons: list[dict[str, Any]] = []

    scores_raw = verdict.get("scores") or {}
    safety_raw = verdict.get("safety") or {}

    # ── Rubric scores ────────────────────────────────────────────────────
    raw_scores: list[float] = []
    for key in _RUBRIC_KEYS:
        try:
            val = float(scores_raw.get(key, 0.0))
            val = max(0.0, min(1.0, val))  # clamp
        except (TypeError, ValueError):
            val = 0.0
        raw_scores.append(val)
        reasons.append({
            "phase": "2",
            "check": key,
            "pass": val >= 0.5,
            "reason": f"{key}: {val:.2f}",
        })

    formula_score = sum(raw_scores) / len(raw_scores) if raw_scores else 0.0

    # ── Safety auto-fails ────────────────────────────────────────────────
    relaxed = set(relaxed_safety_checks or ())
    any_safety_fail = False
    for key in _SAFETY_KEYS:
        triggered = bool(safety_raw.get(key, False))
        is_relaxed = key in relaxed
        if triggered and not is_relaxed:
            any_safety_fail = True
        if triggered and is_relaxed:
            reason_text = (
                f"SAFETY RELAXED (campaign config): {key.replace('_', ' ')} "
                "flagged but not blocking — review manually"
            )
        elif triggered:
            reason_text = f"SAFETY AUTO-FAIL: {key.replace('_', ' ')}"
        else:
            reason_text = f"Safety OK: {key}"
        reasons.append({
            "phase": "2",
            "check": f"safety_{key}",
            # A relaxed-but-triggered check counts as PASS for gating purposes;
            # the reason string preserves the flag for the human reviewer.
            "pass": (not triggered) or is_relaxed,
            "reason": reason_text,
        })

    # ── Self-contained boundary check ────────────────────────────────────
    # Absent field defaults to PASS so callers/tests that don't provide it are
    # unaffected; only an explicit boundary problem fails the clip (→ re-cut).
    sc_raw = verdict.get("self_contained")
    if isinstance(sc_raw, dict):
        complete_thought = bool(sc_raw.get("complete_thought", True))
        ends_on_new_topic = bool(sc_raw.get("ends_on_new_topic", False))
        sc_pass = complete_thought and not ends_on_new_topic
        sc_reason = str(sc_raw.get("reason") or "").strip()
        reasons.append({
            "phase": "2",
            "check": "self_contained",
            "pass": sc_pass,
            "reason": (
                (sc_reason or "clip is a complete, self-contained thought")
                if sc_pass
                else (
                    "SELF-CONTAINED FAIL: "
                    + (sc_reason or (
                        "clip ends on the first sentence of a new topic"
                        if ends_on_new_topic
                        else "clip cuts off mid-thought"
                    ))
                )
            ),
        })

    # ── Campaign alignment check (R4) ─────────────────────────────────────
    # HARD didnt_pass when campaign_alignment.aligned is explicitly False.
    # NOT relaxable via relaxed_safety_checks; NOT part of the safety list.
    # Absent field → PASS for backwards compatibility with mocked tests.
    alignment_fail = False
    ca_raw = verdict.get("campaign_alignment")
    if isinstance(ca_raw, dict):
        # Fail ONLY on explicit False. A present-but-null value (truncated /
        # null-defaulting LLM output) must not spuriously hard-fail a good clip.
        ca_aligned = ca_raw.get("aligned") is not False
        ca_reason = str(ca_raw.get("reason") or "").strip()
        if not ca_aligned:
            alignment_fail = True
            ca_reason_text = "CAMPAIGN ALIGNMENT FAIL: " + (
                ca_reason or "clip framing contradicts the campaign stance"
            )
        else:
            ca_reason_text = "Campaign alignment OK: " + (
                ca_reason or "clip aligns with campaign stance"
            )
        reasons.append({
            "phase": "2",
            "check": "campaign_alignment",
            "pass": ca_aligned,
            "reason": ca_reason_text,
        })

    # ── Hook/body match (Req A1) ──────────────────────────────────────────
    # HARD didnt_pass when hook_body_match.matches is explicitly False.
    # NOT relaxable via relaxed_safety_checks; NOT part of the safety list.
    # Absent field → PASS for backwards compatibility with existing mocked tests.
    hook_body_fail = False
    hbm_raw = verdict.get("hook_body_match")
    if isinstance(hbm_raw, dict):
        # Fail ONLY on explicit False (null/absent → pass; see campaign_alignment).
        hbm_matches = hbm_raw.get("matches") is not False
        hbm_reason = str(hbm_raw.get("reason") or "").strip()
        if not hbm_matches:
            hook_body_fail = True
            hbm_reason_text = "HOOK/BODY MISMATCH: " + (
                hbm_reason or "clip body does not deliver the hook's specific claim"
            )
        else:
            hbm_reason_text = "Hook/body match OK: " + (
                hbm_reason or "body delivers the hook's claim"
            )
        reasons.append({
            "phase": "2",
            "check": "hook_body_match",
            "pass": hbm_matches,
            "reason": hbm_reason_text,
        })

    # ── Topical relevance (Req A3) ────────────────────────────────────────
    # HARD didnt_pass when topical_relevance.on_topic is explicitly False.
    # NOT relaxable via relaxed_safety_checks; NOT part of the safety list.
    # Absent field → PASS for backwards compatibility with existing mocked tests.
    topical_fail = False
    tr_raw = verdict.get("topical_relevance")
    if isinstance(tr_raw, dict):
        # Fail ONLY on explicit False (null/absent → pass; see campaign_alignment).
        tr_on_topic = tr_raw.get("on_topic") is not False
        tr_reason = str(tr_raw.get("reason") or "").strip()
        if not tr_on_topic:
            topical_fail = True
            tr_reason_text = "TOPICAL RELEVANCE FAIL: " + (
                tr_reason or "clip body is generic advice, not campaign-specific content"
            )
        else:
            tr_reason_text = "Topical relevance OK: " + (
                tr_reason or "clip substantively discusses campaign topic"
            )
        reasons.append({
            "phase": "2",
            "check": "topical_relevance",
            "pass": tr_on_topic,
            "reason": tr_reason_text,
        })

    # ── Threshold check ──────────────────────────────────────────────────
    score_ok = formula_score >= FORMULA_SCORE_THRESHOLD
    overall_pass = (
        score_ok
        and not any_safety_fail
        and not alignment_fail
        and not hook_body_fail
        and not topical_fail
    )
    reasons.append({
        "phase": "2",
        "check": "formula_score_threshold",
        "pass": overall_pass,
        "reason": (
            f"formula_score={formula_score:.3f} (threshold {FORMULA_SCORE_THRESHOLD}) "
            + ("PASS" if overall_pass else "FAIL")
        ),
    })

    return formula_score, reasons


# ── Captions-match-speech check (no vision) ───────────────────────────────────

def _check_captions_no_vision(
    clip_row: Any,
    transcript_segments: list[dict] | None,
) -> dict[str, Any]:
    """Return a reason dict for the captions-match-speech check.

    This check does NOT call the vision LLM.  The render used whisper timings
    to burn captions, so the on-screen text quality is verified by the human
    reviewer.  We note word-level availability honestly.
    """
    try:
        from core.models import Transcript
        from core.db import get_session
        # Attempt to look up the Transcript row if we have a source_id
        source_id = getattr(clip_row, "source_id", None)
        word_level_available = False
        if source_id and transcript_segments is not None:
            # If transcript_segments provided directly, check their shape
            # Word-level segments have 'word' key; segment-level have 'text'
            word_level_available = any(
                "word" in seg or seg.get("word_level") for seg in (transcript_segments or [])
            )
        reason_text = (
            "Transcript word-level timings available (used at render time)"
            if word_level_available
            else "Caption–speech match not verifiable without live audio analysis; skipped"
        )
    except Exception:
        reason_text = "Caption–speech match check skipped (transcript not accessible)"

    return {
        "phase": "1",
        "check": "captions_match_speech",
        "pass": True,
        "reason": reason_text,
    }


# ── Resolve video to local path ───────────────────────────────────────────────

def _resolve_to_local_path(video_path_or_r2: str, tmp_dir: str) -> tuple[str, bool]:
    """Resolve video path to a local filesystem path.

    Returns (local_path, is_temp).  If is_temp is True, the caller must delete
    the file after use.

    Raises if the R2 download fails.
    """
    if not video_path_or_r2:
        raise ValueError("video_path_or_r2 is empty")

    if video_path_or_r2.startswith("r2://"):
        key = video_path_or_r2.removeprefix("r2://")
        # Sanitize key to a safe filename
        safe_name = key.replace("/", "_") + ".mp4"
        local_path = os.path.join(tmp_dir, safe_name)
        from core import r2 as _r2
        _r2.download_file(key, local_path)
        log.debug("Downloaded R2 key %s to %s for gate check", key, local_path)
        return local_path, True

    return video_path_or_r2, False


# ── Public interface ──────────────────────────────────────────────────────────

def run_gate(
    clip_row: Any,
    video_path_or_r2: str,
    transcript_segments: list[dict] | None,
    campaign_cfg: Any,
    session: Any,
    preference_context: str = "",
) -> GateResult:
    """Run the two-phase AI review gate on a rendered clip.

    Args:
        clip_row:            SQLAlchemy Clip ORM row (or any object with .id,
                             .hook, .campaign, .kind, .source_id attributes).
        video_path_or_r2:   Local path or 'r2://...' key to the rendered mp4.
        transcript_segments: Segment list from Transcript.segments (may be None).
        campaign_cfg:        CampaignConfig for ranking_rules and campaign name.
        session:             Active SQLAlchemy session (not used for writes here;
                             the caller commits gate results).
        preference_context:  optional learned-preference block (contract §6).
                             Injected into Phase 2 prompt after ranking rules.

    Returns:
        GateResult(gate_status, gate_reasons, formula_score).

    Contract:
        - On LLM/vision transport errors: gate_status='pending', never 'didnt_pass'.
        - Meme clips (kind='meme') always return gate_status='pending' with a note
          (memes use the meme classifier path, not this gate).
        - If video_path_or_r2 is empty: gate_status='pending'.
    """
    clip_id = getattr(clip_row, "id", "?")
    campaign_name = getattr(clip_row, "campaign", "") or ""
    clip_kind = getattr(clip_row, "kind", "clip")

    # Memes use the meme classifier; this gate is for video clips only.
    if clip_kind == "meme":
        return GateResult(
            gate_status="pending",
            gate_reasons=[{
                "phase": "0",
                "check": "kind",
                "pass": True,
                "reason": "Meme clips use the meme classifier path; AI video gate skipped",
            }],
            formula_score=None,
        )

    if not video_path_or_r2:
        return GateResult(
            gate_status="pending",
            gate_reasons=[{
                "phase": "0",
                "check": "video_path",
                "pass": False,
                "reason": "gate unavailable: no video path supplied",
            }],
            formula_score=None,
        )

    log.info("Running AI review gate for clip %s (campaign=%s)", clip_id, campaign_name)

    # ── Phase 1: Design ───────────────────────────────────────────────────────
    try:
        phase1_reasons, phase1_passed, probe_data = _run_phase1(
            clip_row, video_path_or_r2, transcript_segments, campaign_name
        )
    except Exception as exc:
        log.warning("Gate Phase 1 transport error for clip %s: %s", clip_id, exc)
        return GateResult(
            gate_status="pending",
            gate_reasons=[{
                "phase": "1",
                "check": "transport",
                "pass": False,
                "reason": f"gate unavailable: {exc}",
            }],
            formula_score=None,
        )

    if not phase1_passed:
        log.info(
            "Clip %s FAILED Phase 1 design gate. Reasons: %s",
            clip_id,
            [r for r in phase1_reasons if not r["pass"]],
        )
        return GateResult(
            gate_status="didnt_pass",
            gate_reasons=phase1_reasons,
            formula_score=None,
        )

    log.info("Clip %s passed Phase 1 design gate", clip_id)

    # ── Phase 2: Content ──────────────────────────────────────────────────────
    try:
        gate_status, formula_score, all_reasons = _run_phase2(
            clip_row, transcript_segments, campaign_cfg, phase1_reasons,
            preference_context=preference_context,
        )
    except Exception as exc:
        log.warning("Gate Phase 2 transport error for clip %s: %s", clip_id, exc)
        # Phase 1 passed but Phase 2 infra unavailable — stay pending
        return GateResult(
            gate_status="pending",
            gate_reasons=phase1_reasons + [{
                "phase": "2",
                "check": "transport",
                "pass": False,
                "reason": f"gate unavailable: {exc}",
            }],
            formula_score=None,
        )

    log.info(
        "Clip %s gate result: %s (formula_score=%.3f)",
        clip_id, gate_status, formula_score or 0,
    )
    return GateResult(
        gate_status=gate_status,
        gate_reasons=all_reasons,
        formula_score=formula_score,
    )


# ── Phase runners (internal) ──────────────────────────────────────────────────

def _run_phase1(
    clip_row: Any,
    video_path_or_r2: str,
    transcript_segments: list[dict] | None,
    campaign_name: str,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any]]:
    """Execute Phase 1 design checks.

    Returns (reasons, phase_passed, probe_data).
    Raises on transport errors so run_gate can return 'pending'.
    """
    reasons: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="ce_gate_") as tmp_dir:
        # Resolve to local path (download R2 if needed)
        local_path, is_temp = _resolve_to_local_path(video_path_or_r2, tmp_dir)

        # ── Deterministic checks (ffprobe) ────────────────────────────────
        probe = _probe_video(local_path)
        res_dur_reasons = _check_resolution_and_duration(probe)
        reasons.extend(res_dur_reasons)

        # If basic ffprobe checks already failed, skip vision (save cost)
        det_failed = [r for r in res_dur_reasons if not r["pass"]]
        if det_failed:
            # Still log but short-circuit — no vision call
            log.debug("Phase 1 deterministic fail for %s: %s", getattr(clip_row, "id", "?"), det_failed)
            return reasons, False, probe

        # ── Caption–speech check (no vision) ────────────────────────────
        captions_reason = _check_captions_no_vision(clip_row, transcript_segments)
        reasons.append(captions_reason)

        # ── Frame extraction ─────────────────────────────────────────────
        duration = _video_duration(probe)
        if duration <= 0:
            duration = float(
                (probe.get("format") or {}).get("duration")
                or (probe.get("streams") or [{}])[0].get("duration")
                or 30.0
            )

        # Frame timestamps: ~3s in-hook, mid, ~1s before end, final
        t_hook = min(3.0, duration * 0.1)
        t_mid = duration * 0.5
        t_near_end = max(0.0, duration - 2.0)
        t_final = max(0.0, duration - 0.5)
        timestamps = [t_hook, t_mid, t_near_end, t_final]

        frames = _extract_frames(local_path, timestamps)

        # ── Deterministic blur check (no LLM, no network) ────────────────
        # Must run while frames are still in scope (before the tempdir exits).
        # This check is self-contained: cv2 import failure → pass=None (skip).
        # A failed blur check (pass=False) is an immediate Phase 1 auto-fail
        # and short-circuits the vision call (saves LLM cost).
        blur_reason = _check_frames_sharp(frames)
        reasons.append(blur_reason)
        if blur_reason.get("pass") is False:
            log.info(
                "Phase 1 blur auto-fail for clip %s: %s",
                getattr(clip_row, "id", "?"),
                blur_reason["reason"],
            )
            return reasons, False, probe

    # ── Vision LLM call ───────────────────────────────────────────────────
    style_refs = _load_style_refs(campaign_name)
    vision_verdict = _vision_llm_call(frames, style_refs, duration)
    vision_reasons = _vision_verdict_to_reasons(vision_verdict)
    reasons.extend(vision_reasons)

    # Determine pass/fail: all checks must pass
    # animation_detected=True is always auto-fail (handled in vision_reasons)
    # blur check pass=None (cv2 unavailable) does not block the phase
    phase_passed = all(r["pass"] is True or r["pass"] is None for r in reasons)

    return reasons, phase_passed, probe


def _run_phase2(
    clip_row: Any,
    transcript_segments: list[dict] | None,
    campaign_cfg: Any,
    phase1_reasons: list[dict[str, Any]],
    preference_context: str = "",
) -> tuple[str, float, list[dict[str, Any]]]:
    """Execute Phase 2 content scoring.

    Returns (gate_status, formula_score, all_reasons).
    Raises on transport errors so run_gate can return 'pending'.

    preference_context: optional learned-preference block (contract §6).
    """
    hook = getattr(clip_row, "hook", "") or ""
    start = getattr(clip_row, "start", None)
    end = getattr(clip_row, "end", None)
    ranking_rules = ""
    try:
        ranking_rules = campaign_cfg.ranking.ranking_rules or ""
    except Exception:
        pass

    # Build transcript text slice for this clip's time range
    transcript_text = _build_transcript_slice(transcript_segments, start, end)
    # Look-ahead: what is said right after the clip ends, so the boundary check
    # can tell whether the clip bleeds into the first sentence of a new topic.
    next_context = _build_lookahead_slice(transcript_segments, end)

    # Per-campaign safety relaxation (e.g. peptides -> medical_claims):
    # the check still runs, but a triggered relaxed check does not auto-fail.
    relaxed_checks: list[str] = []
    try:
        relaxed_checks = list(campaign_cfg.gate.relaxed_safety_checks or [])
    except Exception:
        pass

    # Campaign stance (R4): used in the content LLM prompt and for alignment check.
    stance = ""
    try:
        stance = str(campaign_cfg.ranking.stance or "")
    except Exception:
        pass

    verdict = _content_llm_call(
        hook, transcript_text, ranking_rules, next_context,
        preference_context=preference_context,
        stance=stance,
    )
    formula_score, content_reasons = _score_content_verdict(
        verdict, relaxed_safety_checks=relaxed_checks
    )

    all_reasons = list(phase1_reasons) + content_reasons

    # Determine final gate status
    safety_fail = any(
        not r["pass"] and r["check"].startswith("safety_")
        for r in content_reasons
    )
    boundary_fail = any(
        not r["pass"] and r["check"] == "self_contained"
        for r in content_reasons
    )
    # campaign_alignment is a HARD fail — not relaxable (R4)
    alignment_fail = any(
        not r["pass"] and r["check"] == "campaign_alignment"
        for r in content_reasons
    )
    # hook_body_match is a HARD fail — not relaxable (Req A1)
    hook_body_fail = any(
        not r["pass"] and r["check"] == "hook_body_match"
        for r in content_reasons
    )
    # topical_relevance is a HARD fail — not relaxable (Req A3)
    topical_fail = any(
        not r["pass"] and r["check"] == "topical_relevance"
        for r in content_reasons
    )
    score_fail = formula_score < FORMULA_SCORE_THRESHOLD
    gate_status = (
        "ready"
        if (
            not safety_fail
            and not score_fail
            and not boundary_fail
            and not alignment_fail
            and not hook_body_fail
            and not topical_fail
        )
        else "didnt_pass"
    )

    return gate_status, formula_score, all_reasons


def _build_transcript_slice(
    segments: list[dict] | None,
    start: float | None,
    end: float | None,
) -> str:
    """Extract transcript text for the clip's time range.

    If start/end are provided, include only segments that overlap the range.
    Otherwise return all available text (up to 4000 chars).
    """
    if not segments:
        return "(transcript not available)"

    relevant: list[str] = []
    for seg in segments:
        seg_start = float(seg.get("start") or 0)
        seg_end = float(seg.get("end") or seg_start + 1)
        text = str(seg.get("text") or seg.get("word") or "").strip()
        if not text:
            continue
        # Include if range overlaps or no range provided
        if start is None or end is None:
            relevant.append(text)
        elif seg_end >= start and seg_start <= end:
            relevant.append(text)

    combined = " ".join(relevant)
    return combined[:4000] if combined else "(no transcript in clip range)"


def _build_lookahead_slice(
    segments: list[dict] | None,
    end: float | None,
    window: float = 15.0,
) -> str:
    """Return the transcript text spoken in [end, end + window] seconds.

    Used by the self-contained boundary check to see whether the clip ends just
    as a new topic begins. Returns "" when unavailable so the content call
    simply omits the look-ahead section.
    """
    if not segments or end is None:
        return ""
    out: list[str] = []
    for seg in segments:
        seg_start = float(seg.get("start") or 0)
        text = str(seg.get("text") or seg.get("word") or "").strip()
        if not text:
            continue
        if end <= seg_start <= end + window:
            out.append(text)
    return " ".join(out)[:1500]
