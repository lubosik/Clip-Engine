"""
meme/generate.py — meme generation pipeline.

For each meme:
  1. Load active style profile (extract on first run if refs_dir has images).
  2. LLM generates concept + caption from profile + creative_direction + hard rules.
  3. Render image via the configured image model (meme/image_client.py).
  4. Save image to R2 or local storage.
  5. Run the on-brand classifier.
  6. Insert Clip row: kind='meme', status='pending_review' or 'rejected'.

Public API:
    generate_memes(campaign_cfg, n, session, mode_override=None) -> list[int]
"""

from __future__ import annotations

import json
import logging
from typing import Any

from meme._client import _extract_json_object, call_text
from meme._storage import save_meme_image
from meme.classifier import GLOBAL_HARD_RULES, classify

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Concept + caption generation
# ---------------------------------------------------------------------------

def _build_concept_prompt(
    campaign_cfg: Any,
    profile: Any,
    hard_rules: list[str],
) -> str:
    """Build the text-only LLM prompt for concept + caption generation."""
    profile_data = profile.profile
    merged_rules = GLOBAL_HARD_RULES + hard_rules
    rules_text = "\n".join(f"- {r}" for r in merged_rules)

    creative_dir = campaign_cfg.creative_direction or "Produce engaging, shareable content."

    profile_summary = json.dumps(
        {
            "aspect": profile_data.get("aspect"),
            "visual_format": profile_data.get("visual_format", {}),
            "caption_voice": profile_data.get("caption_voice", {}),
            "measurable_rules": profile_data.get("measurable_rules", []),
        },
        indent=2,
    )

    return f"""You are creating a meme for the '{campaign_cfg.name}' campaign.

CREATIVE DIRECTION:
{creative_dir}

MEME STYLE PROFILE:
{profile_summary}

HARD RULES (you must follow ALL of these):
{rules_text}

Generate a single meme concept and caption that:
- Matches the style profile exactly
- Follows the caption voice: tone, person, sentence length, and punctuation habits
- Obeys ALL hard rules without exception
- Is original, engaging, and suitable for social media

Return ONLY a JSON object (no prose, no code fences):
{{
  "concept": "<description of what the meme image should show — clear enough for an AI image generator>",
  "caption": "<the exact text caption for the meme>"
}}
"""


def _parse_concept_response(raw_text: str) -> dict | None:
    """
    Parse a concept+caption JSON object from the LLM response.

    Returns None if neither key is present or parsing fails entirely.
    """
    data = _extract_json_object(raw_text)
    if data is None:
        return None
    if "concept" not in data or "caption" not in data:
        return None
    return data


def _generate_concept(campaign_cfg: Any, profile: Any) -> dict:
    """
    Ask the LLM to produce a meme concept + caption.

    Returns {"concept": str, "caption": str}.
    Raises ValueError on failure after one retry.
    """
    hard_rules = (
        list(campaign_cfg.meme.hard_rules)
        if campaign_cfg.meme and campaign_cfg.meme.hard_rules
        else []
    )
    prompt = _build_concept_prompt(campaign_cfg, profile, hard_rules)

    raw = call_text(prompt, max_tokens=512)
    log.debug("Concept LLM raw response length=%d", len(raw))

    parsed = _parse_concept_response(raw)

    if parsed is None:
        log.warning("Concept generation failed to parse JSON; retrying once")
        raw = call_text(prompt, max_tokens=512)
        parsed = _parse_concept_response(raw)

    if parsed is None:
        raise ValueError(
            f"LLM failed to produce a valid concept+caption JSON after retry. "
            f"Preview: {raw[:300]}"
        )

    return parsed


# ---------------------------------------------------------------------------
# Image generation
# ---------------------------------------------------------------------------

def _get_image_model(campaign_cfg: Any) -> str:
    """Resolve the image generation model from campaign config or env."""
    from core.settings import get_settings

    model = ""
    if campaign_cfg.meme and campaign_cfg.meme.image_model:
        model = campaign_cfg.meme.image_model.strip()
    if not model:
        model = (get_settings().meme_image_model or "").strip()
    if not model:
        raise RuntimeError(
            "No image model configured for meme generation. "
            "Set meme.image_model in the campaign YAML or "
            "MEME_IMAGE_MODEL in your environment variables."
        )
    return model


def _build_image_prompt(concept: str, profile_data: dict) -> str:
    """Convert concept + profile visual_format into an image generation prompt."""
    vf = profile_data.get("visual_format", {})
    style_notes = (
        f"Style: {vf.get('image_style', 'bold, high-contrast')}.  "
        f"Layout: {vf.get('layout', 'centered')}.  "
        f"Colors: {', '.join(vf.get('colors', []))}.  "
        f"Composition: {vf.get('typical_composition', '')}."
    ).strip()

    return f"Meme image: {concept}  {style_notes}  Make it bold, clear, and shareable."


def _load_ref_images(campaign_cfg: Any) -> list[tuple[bytes, str]]:
    """
    Load up to 3 reference images for style guidance in image generation.

    Returns empty list if refs_dir is not configured or contains no images.
    """
    from pathlib import Path

    _MIME: dict[str, str] = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    _EXTS = frozenset(_MIME)

    if not campaign_cfg.meme or not campaign_cfg.meme.refs_dir:
        return []

    refs_dir = Path(campaign_cfg.meme.refs_dir)
    if not refs_dir.exists():
        return []

    images: list[tuple[bytes, str]] = []
    for p in sorted(refs_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in _EXTS:
            continue
        try:
            images.append((p.read_bytes(), _MIME[p.suffix.lower()]))
        except OSError as exc:
            log.warning("Skipping unreadable ref image %s: %s", p, exc)
        if len(images) >= 3:
            break

    return images


# ---------------------------------------------------------------------------
# Main generate_memes function
# ---------------------------------------------------------------------------

def generate_memes(
    campaign_cfg: Any,
    n: int,
    session: Any,
    *,
    mode_override: str | None = None,
) -> list[int]:
    """
    Generate *n* memes for *campaign_cfg* and insert Clip rows.

    Args:
        campaign_cfg:  CampaignConfig instance with engines.memes True.
        n:             Number of memes to attempt to generate.
        session:       SQLAlchemy session (caller manages commit/rollback).
        mode_override: Force 'demo' or 'production' mode; defaults to
                       campaign_cfg.mode.

    Returns:
        List of inserted Clip.id values (includes rejected memes).

    Raises:
        ValueError:   if no profile exists and no refs_dir is configured
                      or refs_dir is empty.
        RuntimeError: if LLM or image model credentials are missing.
    """
    from core.models import Clip
    from meme.image_client import build_image_client
    from meme.profile import extract_profile, get_active_profile

    campaign_name = campaign_cfg.name
    mode = mode_override or campaign_cfg.mode

    # Ensure an active profile exists
    profile = get_active_profile(campaign_name, session)
    if profile is None:
        log.info(
            "No meme profile found; extracting on first run",
            extra={"campaign": campaign_name},
        )
        profile = extract_profile(campaign_cfg, session)
        session.commit()

    profile_data = profile.profile
    aspect = profile_data.get("aspect", "1:1")

    image_model = _get_image_model(campaign_cfg)
    img_client = build_image_client(image_model)
    ref_images = _load_ref_images(campaign_cfg)

    hard_rules: list[str] = (
        list(campaign_cfg.meme.hard_rules)
        if campaign_cfg.meme and campaign_cfg.meme.hard_rules
        else []
    )

    inserted_ids: list[int] = []

    for i in range(n):
        log.info(
            "Generating meme %d/%d",
            i + 1,
            n,
            extra={"campaign": campaign_name},
        )

        try:
            # Step 1: generate concept + caption
            concept_data = _generate_concept(campaign_cfg, profile)
            concept = concept_data["concept"]
            caption = concept_data["caption"]

            # Step 2: render image
            image_prompt = _build_image_prompt(concept, profile_data)
            image_bytes = img_client.generate(image_prompt, ref_images)

            # Step 3: classify (uses profile + hard rules)
            classification = classify(
                image_bytes,
                caption,
                profile_data,
                hard_rules,
            )
            verdict = classification["verdict"]

            # Step 4: insert Clip row (file_path set after we know the ID)
            clip_row = Clip(
                campaign=campaign_name,
                source_id=None,
                start=None,
                end=None,
                kind="meme",
                mode=mode,
                aspect=aspect,
                caption=caption,
                destination_channels=campaign_cfg.destinations.postiz_channels,
                status="pending_review" if verdict == "pass" else "rejected",
                reject_reason=(
                    "; ".join(classification["reasons"])
                    if verdict == "fail"
                    else None
                ),
                meme_meta={
                    "concept": concept,
                    "classifier_scores": {
                        k: v
                        for k, v in classification.items()
                        if k not in ("verdict", "reasons")
                    },
                    "profile_version": profile.version,
                },
                file_path=None,  # filled in below after flush
                thumb_path=None,
            )
            session.add(clip_row)
            session.flush()  # obtain clip_row.id

            # Step 5: persist image, update file_path
            stored_path = save_meme_image(clip_row.id, campaign_name, image_bytes)
            clip_row.file_path = stored_path
            clip_row.thumb_path = stored_path  # meme: thumb = the image itself

            inserted_ids.append(clip_row.id)

            log.info(
                "Meme generated",
                extra={
                    "clip_id": clip_row.id,
                    "campaign": campaign_name,
                    "verdict": verdict,
                    "aspect": aspect,
                    "mode": mode,
                    "file_path": stored_path,
                },
            )

        except Exception as exc:
            log.error(
                "Meme generation failed for item %d/%d",
                i + 1,
                n,
                extra={"campaign": campaign_name, "error": str(exc)},
                exc_info=True,
            )
            try:
                session.rollback()
            except Exception:
                pass

    return inserted_ids
