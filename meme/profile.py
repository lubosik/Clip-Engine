"""
meme/profile.py — meme style profile extraction and retrieval.

Reads reference meme images from campaign.meme.refs_dir, sends them to
the LLM vision endpoint, and parses a structured meme_style_profile.

Profile JSON schema (stored in MemeProfile.profile):
{
    "aspect": "1:1" | "4:5",
    "visual_format": {
        "layout": str,
        "image_style": str,
        "text_placement": str,
        "colors": [str],
        "typical_composition": str
    },
    "caption_voice": {
        "tone": str,
        "person": "first" | "second" | "third",
        "sentence_length": "short" | "medium" | "long",
        "punctuation_habits": str,
        "slang_level": "none" | "low" | "medium" | "high"
    },
    "measurable_rules": [
        {"rule": str, "confidence": float}   # confidence in [0,1]
    ]
}

Public API:
    extract_profile(campaign_cfg, session) -> MemeProfile
    get_active_profile(campaign, session) -> MemeProfile | None
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from core.models import MemeProfile
from meme._client import _extract_json_object, call_vision

log = logging.getLogger(__name__)

# Supported reference image extensions
_REF_EXTS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})

_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


# ---------------------------------------------------------------------------
# Profile JSON schema helpers
# ---------------------------------------------------------------------------

def _validate_profile(data: dict) -> dict:
    """
    Validate and normalise a raw profile dict from the LLM.

    Raises ValueError if required top-level keys are missing.
    Normalises nested values to expected types and ranges.
    """
    required = {"visual_format", "caption_voice", "measurable_rules"}
    missing = required - set(data.keys())
    if missing:
        raise ValueError(
            f"Profile response missing required keys: {sorted(missing)}. "
            f"Got keys: {sorted(data.keys())}"
        )

    # Aspect — default to "1:1" if absent or invalid
    aspect = str(data.get("aspect", "1:1")).strip()
    if aspect not in {"1:1", "4:5"}:
        log.warning("Unknown aspect %r in profile; defaulting to 1:1", aspect)
        aspect = "1:1"
    data["aspect"] = aspect

    # visual_format — must be a dict
    if not isinstance(data.get("visual_format"), dict):
        data["visual_format"] = {}

    vf = data["visual_format"]
    vf.setdefault("layout", "")
    vf.setdefault("image_style", "")
    vf.setdefault("text_placement", "")
    vf.setdefault("colors", [])
    vf.setdefault("typical_composition", "")

    if not isinstance(vf.get("colors"), list):
        vf["colors"] = []

    # caption_voice — must be a dict
    if not isinstance(data.get("caption_voice"), dict):
        data["caption_voice"] = {}

    cv = data["caption_voice"]
    cv.setdefault("tone", "")
    cv.setdefault("person", "second")
    cv.setdefault("sentence_length", "short")
    cv.setdefault("punctuation_habits", "")
    cv.setdefault("slang_level", "low")

    # measurable_rules — must be a list of {rule, confidence} dicts
    rules = data.get("measurable_rules")
    if not isinstance(rules, list):
        data["measurable_rules"] = []
    else:
        valid_rules = []
        for r in rules:
            if not isinstance(r, dict):
                continue
            rule_text = str(r.get("rule", "")).strip()
            if not rule_text:
                continue
            try:
                confidence = float(r.get("confidence", 0.7))
            except (TypeError, ValueError):
                confidence = 0.7
            confidence = max(0.0, min(1.0, confidence))
            valid_rules.append({"rule": rule_text, "confidence": confidence})
        data["measurable_rules"] = valid_rules

    return data


def _build_extraction_prompt() -> str:
    """Build the profile extraction prompt sent alongside reference images."""
    schema = json.dumps(
        {
            "aspect": "1:1 or 4:5 — choose based on the dominant image format",
            "visual_format": {
                "layout": "describe the typical layout of text and images",
                "image_style": "describe the image style (photography, illustration, screenshot, etc.)",
                "text_placement": "where does text appear (top, bottom, center, overlay, etc.)",
                "colors": ["dominant colors used"],
                "typical_composition": "describe the typical composition",
            },
            "caption_voice": {
                "tone": "e.g. humorous, direct, motivational, sarcastic",
                "person": "first, second, or third",
                "sentence_length": "short, medium, or long",
                "punctuation_habits": "describe punctuation style",
                "slang_level": "none, low, medium, or high",
            },
            "measurable_rules": [
                {
                    "rule": "concrete, testable rule extracted from the meme style",
                    "confidence": 0.9,
                }
            ],
        },
        indent=2,
    )

    return f"""You are a meme style analyst.  Analyse the reference meme images provided
and extract a precise, structured style profile that can be used to generate
new memes in exactly the same style.

Your output will be used to:
1. Generate new meme concepts and captions
2. Generate meme images using an AI image model
3. Automatically grade new memes for on-brand quality

Focus on MEASURABLE, CONCRETE attributes — things that can be objectively
checked in new memes.  At least 3 measurable rules are expected.

Return ONLY a JSON object (no prose, no code fences) exactly matching this schema:
{schema}

Important: all string values must be in English.
Do not include any em-dashes (—) in your output.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_active_profile(campaign: str, session) -> MemeProfile | None:
    """
    Return the active (highest-version) MemeProfile for *campaign*.

    Returns None if no profile has been extracted yet.
    """
    row = (
        session.query(MemeProfile)
        .filter(MemeProfile.campaign == campaign)
        .order_by(MemeProfile.version.desc())
        .first()
    )
    return row


def extract_profile(campaign_cfg, session) -> MemeProfile:
    """
    Extract a new meme style profile from reference images and store it.

    Reads images from campaign_cfg.meme.refs_dir (supports .png/.jpg/.jpeg/.webp).
    Sends images (base64) to the LLM vision endpoint.
    Parses the structured profile JSON.
    Stores as a new MemeProfile row (version = prev_version + 1).

    Args:
        campaign_cfg: CampaignConfig instance (must have .meme set).
        session:      SQLAlchemy session.

    Returns:
        The newly created MemeProfile row.

    Raises:
        ValueError:   if refs_dir is not configured, is empty, or the LLM
                      response cannot be parsed after a retry.
        RuntimeError: if LLM credentials are missing.
    """
    if not campaign_cfg.meme or not campaign_cfg.meme.refs_dir:
        raise ValueError(
            f"Campaign '{campaign_cfg.name}' has no meme.refs_dir configured. "
            "Set meme.refs_dir in the campaign YAML to the directory containing "
            "reference meme images."
        )

    refs_dir = Path(campaign_cfg.meme.refs_dir)
    if not refs_dir.exists():
        raise ValueError(
            f"meme.refs_dir does not exist: {refs_dir.resolve()}"
        )

    ref_files = sorted(
        p for p in refs_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _REF_EXTS
    )
    if not ref_files:
        raise ValueError(
            f"refs_dir '{refs_dir}' contains no reference images "
            f"(supported extensions: {sorted(_REF_EXTS)}). "
            "Drop .png/.jpg/.webp reference meme images in that directory "
            "then re-run."
        )

    log.info(
        "Extracting meme style profile",
        extra={
            "campaign": campaign_cfg.name,
            "refs_dir": str(refs_dir),
            "ref_count": len(ref_files),
        },
    )

    # Load images as (bytes, media_type) pairs
    images: list[tuple[bytes, str]] = []
    for p in ref_files:
        try:
            raw = p.read_bytes()
            mime = _MIME_MAP.get(p.suffix.lower(), "image/jpeg")
            images.append((raw, mime))
        except OSError as exc:
            log.warning("Skipping unreadable ref image %s: %s", p, exc)

    if not images:
        raise ValueError(
            f"All reference images in '{refs_dir}' were unreadable."
        )

    prompt = _build_extraction_prompt()

    # First attempt
    raw_response = call_vision(prompt, images, max_tokens=2048)
    log.debug("Profile extraction raw response length=%d", len(raw_response))

    profile_data = _extract_json_object(raw_response)

    if profile_data is None:
        log.warning(
            "Profile extraction did not return valid JSON; retrying once",
            extra={"preview": raw_response[:300]},
        )
        raw_response = call_vision(prompt, images, max_tokens=2048)
        profile_data = _extract_json_object(raw_response)

    if profile_data is None:
        raise ValueError(
            f"LLM failed to return a valid profile JSON after retry. "
            f"Response preview: {raw_response[:300]}"
        )

    # Validate and normalise
    try:
        profile_data = _validate_profile(profile_data)
    except ValueError as exc:
        raise ValueError(
            f"Profile validation failed for campaign '{campaign_cfg.name}': {exc}"
        ) from exc

    # Determine next version number
    existing = get_active_profile(campaign_cfg.name, session)
    next_version = (existing.version + 1) if existing is not None else 1

    row = MemeProfile(
        campaign=campaign_cfg.name,
        version=next_version,
        profile=profile_data,
    )
    session.add(row)
    session.flush()  # get the id before returning

    log.info(
        "Meme profile extracted and stored",
        extra={
            "campaign": campaign_cfg.name,
            "version": next_version,
            "aspect": profile_data.get("aspect"),
            "rules_count": len(profile_data.get("measurable_rules", [])),
        },
    )
    return row
