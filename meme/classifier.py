"""
meme/classifier.py — LLM-as-judge meme quality classifier.

Scores each meme against the active style profile and hard rules before
it enters the review queue.  Any compliance failure (hard-rule violation)
causes an automatic rejection; low quality scores also reject.

Public API:
    violates_hard_rules(text) -> list[str]   # pure, testable
    classify(image_data, caption, profile, hard_rules) -> ClassifierResult
"""

from __future__ import annotations

import json
import logging
from typing import TypedDict

from meme._client import _extract_json_object, call_text, call_vision

log = logging.getLogger(__name__)

# Quality threshold: mean of non-compliance scores must be >= this value.
PASS_THRESHOLD: float = 0.65

# EM-dash Unicode character — prohibited in all output text.
_EM_DASH: str = "—"

# Global hard rules always merged into every campaign's rules.
GLOBAL_HARD_RULES: list[str] = [
    "No em-dashes (—) anywhere in the text",
    "No medical or health claims (e.g. 'cures', 'treats', 'clinically proven')",
    "No content promoting unsafe dieting or disordered eating",
]


# ---------------------------------------------------------------------------
# TypedDict for classifier output
# ---------------------------------------------------------------------------

class ClassifierResult(TypedDict):
    """Structured output from classify()."""
    on_format: float | None    # 0-1; None for text-only posts
    on_voice: float            # 0-1
    on_brand: float | None     # 0-1; None for text-only posts
    legibility: float | None   # 0-1; None for text-only posts
    compliance: float          # 0-1; < 1.0 always rejects
    verdict: str               # 'pass' | 'fail'
    reasons: list[str]


# ---------------------------------------------------------------------------
# Pure helper — deterministic rule checking
# ---------------------------------------------------------------------------

def violates_hard_rules(text: str) -> list[str]:
    """
    Check *text* against deterministic hard rules.

    Returns a list of human-readable violation descriptions.
    An empty list means the text is clean.

    This is a pure function — no I/O, no LLM call.
    The LLM compliance score handles nuanced content rules (medical claims,
    disordered-eating promotion); this covers rules that are
    reliably detectable via pattern matching.
    """
    violations: list[str] = []

    if _EM_DASH in text:
        violations.append(
            "contains em-dash (—) which is prohibited; use a plain dash or reword"
        )

    return violations


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _verdict_from_scores(scores: dict) -> tuple[str, list[str]]:
    """
    Compute pass/fail verdict from the LLM-returned score dict.

    Returns:
        ('pass' | 'fail', [list of reasons for failure])

    Rules (per MASTER_SPEC Part E):
        1. compliance < 1.0 → automatic fail (any hard rule violation)
        2. mean of quality scores < PASS_THRESHOLD → fail
           Quality scores: on_format, on_voice, on_brand, legibility
           For text-only (only on_voice present): just on_voice
    """
    reasons: list[str] = []

    compliance = float(scores.get("compliance", 1.0))
    if compliance < 1.0:
        reasons.append(
            f"compliance score {compliance:.2f} < 1.0 (hard rule violation)"
        )
        return "fail", reasons

    # Collect all quality scores (everything except compliance and reasons)
    quality_keys = [
        k for k in scores
        if k not in ("compliance", "reasons") and isinstance(scores[k], (int, float))
    ]

    if quality_keys:
        vals = [float(scores[k]) for k in quality_keys]
        mean_q = sum(vals) / len(vals)
        if mean_q < PASS_THRESHOLD:
            detail = ", ".join(f"{k}={scores[k]:.2f}" for k in quality_keys)
            reasons.append(
                f"quality mean {mean_q:.2f} below threshold {PASS_THRESHOLD} "
                f"({detail})"
            )
            return "fail", reasons

    return "pass", []


def _build_classifier_prompt(
    caption: str,
    profile: dict,
    hard_rules: list[str],
    *,
    text_only: bool,
) -> str:
    """Construct the LLM-as-judge prompt."""
    profile_summary = json.dumps(
        {
            "caption_voice": profile.get("caption_voice", {}),
            "measurable_rules": profile.get("measurable_rules", []),
        },
        indent=2,
    )

    rules_text = "\n".join(f"- {r}" for r in hard_rules)

    if text_only:
        score_spec = (
            '{\n'
            '  "on_voice": <float 0-1>,\n'
            '  "compliance": <float 0-1>,\n'
            '  "reasons": ["<brief reason>"]\n'
            '}'
        )
        vision_instruction = ""
    else:
        score_spec = (
            '{\n'
            '  "on_format": <float 0-1>,\n'
            '  "on_voice": <float 0-1>,\n'
            '  "on_brand": <float 0-1>,\n'
            '  "legibility": <float 0-1>,\n'
            '  "compliance": <float 0-1>,\n'
            '  "reasons": ["<brief reason>"]\n'
            '}'
        )
        vision_instruction = (
            "You are evaluating the meme IMAGE provided. "
            "Score on_format, on_brand, and legibility based on the image. "
        )

    return f"""You are a meme quality judge. {vision_instruction}Score this meme against the style profile and hard rules.

CAPTION: "{caption}"

STYLE PROFILE:
{profile_summary}

HARD RULES (compliance must be 1.0 unless any rule is violated):
{rules_text}

SCORING GUIDE:
- on_format: does the visual layout / composition match the profile? (image only)
- on_voice:  does the caption match the profile's voice, tone, and style?
- on_brand:  does the overall meme feel on-brand for this campaign? (image only)
- legibility: is the text readable, clear, and properly sized? (image only)
- compliance: 1.0 if ALL hard rules are satisfied; 0.0 if ANY rule is violated.

Return ONLY a JSON object with no prose or code fences:
{score_spec}
"""


def _parse_scores(raw_text: str, *, text_only: bool) -> dict:
    """
    Parse the LLM score JSON, falling back gracefully on malformed output.

    Returns a dict with all expected score keys present (defaulting to 0.5
    on parse failure so erroneous responses fail the quality threshold).
    """
    data = _extract_json_object(raw_text)
    if data is None:
        log.warning(
            "Classifier could not parse JSON from response; defaulting to failing scores",
            extra={"preview": raw_text[:200]},
        )
        if text_only:
            return {"on_voice": 0.0, "compliance": 0.0, "reasons": ["parse error"]}
        return {
            "on_format": 0.0,
            "on_voice": 0.0,
            "on_brand": 0.0,
            "legibility": 0.0,
            "compliance": 0.0,
            "reasons": ["parse error"],
        }

    def _clamp(v: object) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.5

    out: dict = {"reasons": list(data.get("reasons", []))}

    if text_only:
        out["on_voice"] = _clamp(data.get("on_voice", 0.5))
        out["compliance"] = _clamp(data.get("compliance", 0.5))
    else:
        out["on_format"] = _clamp(data.get("on_format", 0.5))
        out["on_voice"] = _clamp(data.get("on_voice", 0.5))
        out["on_brand"] = _clamp(data.get("on_brand", 0.5))
        out["legibility"] = _clamp(data.get("legibility", 0.5))
        out["compliance"] = _clamp(data.get("compliance", 0.5))

    return out


# ---------------------------------------------------------------------------
# Public classifier
# ---------------------------------------------------------------------------

def classify(
    image_data: bytes | None,
    caption: str,
    profile: dict,
    hard_rules: list[str] | None = None,
) -> ClassifierResult:
    """
    Score a meme against the style profile and hard rules.

    Args:
        image_data:  Raw image bytes.  Pass None for text-only posts.
        caption:     Caption text to evaluate.
        profile:     Active meme style profile dict (from MemeProfile.profile).
        hard_rules:  Additional campaign-specific hard rules; merged with
                     GLOBAL_HARD_RULES automatically.

    Returns:
        ClassifierResult with scores and a final verdict ('pass' or 'fail').
    """
    merged_rules = GLOBAL_HARD_RULES + (hard_rules or [])
    text_only = image_data is None

    # Fast-path: deterministic pre-check before spending LLM tokens
    violations = violates_hard_rules(caption)
    if violations:
        log.info(
            "Caption failed hard-rule pre-check; skipping LLM call",
            extra={"violations": violations},
        )
        result: ClassifierResult = {
            "on_format": None if text_only else 0.0,
            "on_voice": 0.0,
            "on_brand": None if text_only else 0.0,
            "legibility": None if text_only else 0.0,
            "compliance": 0.0,
            "verdict": "fail",
            "reasons": violations,
        }
        return result

    prompt = _build_classifier_prompt(
        caption=caption,
        profile=profile,
        hard_rules=merged_rules,
        text_only=text_only,
    )

    # One retry: parse failure on first attempt → retry call
    def _call_and_parse() -> dict:
        if text_only:
            raw = call_text(prompt, max_tokens=512)
        else:
            assert image_data is not None  # type narrowing
            media_type = "image/png"
            raw = call_vision(
                prompt,
                [(image_data, media_type)],
                max_tokens=512,
            )
        return _parse_scores(raw, text_only=text_only)

    scores = _call_and_parse()

    # Retry once if all scores came back at 0.0 (parse failure default)
    all_zero = all(
        v == 0.0
        for k, v in scores.items()
        if k not in ("reasons",) and isinstance(v, float)
    )
    if all_zero:
        log.warning("Classifier all-zero scores; retrying once")
        scores = _call_and_parse()

    verdict, reasons = _verdict_from_scores(scores)

    log.info(
        "Classifier verdict=%s compliance=%.2f",
        verdict,
        scores.get("compliance", 0.0),
        extra={"caption_preview": caption[:60], "scores": scores},
    )

    return ClassifierResult(
        on_format=scores.get("on_format"),
        on_voice=float(scores.get("on_voice", 0.0)),
        on_brand=scores.get("on_brand"),
        legibility=scores.get("legibility"),
        compliance=float(scores.get("compliance", 0.0)),
        verdict=verdict,
        reasons=reasons + list(scores.get("reasons", [])),
    )
