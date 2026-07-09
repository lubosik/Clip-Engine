"""
meme/text_posts.py — text-only / X post generation.

Uses the same voice profile as image memes but generates caption-only content.
A simple text card PNG (1080×1080, Pillow) is rendered and stored so the
review console has a visual to display.

Pipeline:
  1. Get active style profile.
  2. LLM generates a caption (text-only LLM call, same voice rules).
  3. Render a text card PNG with Pillow.
  4. Save image to R2 or local storage.
  5. Run classifier in text-only mode (voice + compliance only).
  6. Insert Clip row: kind='meme', aspect='1:1', meme_meta={text_only: True, ...}.

Public API:
    generate_text_posts(campaign_cfg, n, session, mode_override=None) -> list[int]
    render_text_card(caption, width=1080, height=1080) -> bytes
"""

from __future__ import annotations

import io
import logging
import textwrap
from typing import Any

from meme._client import _extract_json_object, call_text
from meme._storage import save_meme_image
from meme.classifier import GLOBAL_HARD_RULES, classify
from meme.generate import _build_concept_prompt  # reuse concept prompt logic

log = logging.getLogger(__name__)

# Text card rendering constants
_BG_COLOR = (18, 18, 18)        # near-black
_TEXT_COLOR = (240, 240, 240)   # near-white
_ACCENT_COLOR = (0, 229, 255)   # cyan (matches brand highlight)
_FONT_SIZE_BASE = 72            # starting font size; reduced to fit
_PADDING = 80                   # pixels of padding on each side
_LINE_SPACING_FACTOR = 1.35


# ---------------------------------------------------------------------------
# Text card renderer
# ---------------------------------------------------------------------------

def render_text_card(
    caption: str,
    *,
    width: int = 1080,
    height: int = 1080,
) -> bytes:
    """
    Render *caption* as a PNG text card.

    Uses Pillow.  The font is the default Pillow bitmap font (always available);
    no external font file is required.  For production-quality cards the
    operator can extend this with a proper TTF but the fallback must always work.

    Returns raw PNG bytes.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for text card rendering.  "
            "Install it with: pip install pillow"
        ) from exc

    img = Image.new("RGB", (width, height), color=_BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Try to load a reasonable-quality font; fall back to Pillow's built-in.
    font = None
    font_size = _FONT_SIZE_BASE

    # Attempt to find a system sans-serif TTF
    _candidate_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    ]
    for font_path in _candidate_fonts:
        try:
            font = ImageFont.truetype(font_path, font_size)
            break
        except (IOError, OSError):
            continue

    if font is None:
        # Pillow's built-in bitmap font — always available
        font = ImageFont.load_default()
        font_size = 20  # built-in font is tiny; adjust wrap accordingly

    usable_width = width - 2 * _PADDING

    # Wrap text to fit usable width, reducing font size until it fits
    def _wrap_and_measure(txt: str, fnt: Any, max_w: int) -> list[str]:
        """Wrap text to lines that fit within max_w pixels."""
        words = txt.split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = (current + " " + word).strip() if current else word
            bbox = draw.textbbox((0, 0), candidate, font=fnt)
            if bbox[2] - bbox[0] <= max_w:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    # Reduce font size until text fits vertically
    if hasattr(font, "path"):  # TrueType font
        while font_size > 24:
            try:
                font = ImageFont.truetype(font.path, font_size)
            except Exception:
                break
            lines = _wrap_and_measure(caption, font, usable_width)
            line_h = int(font_size * _LINE_SPACING_FACTOR)
            total_h = len(lines) * line_h
            if total_h <= height - 2 * _PADDING:
                break
            font_size -= 4

    lines = _wrap_and_measure(caption, font, usable_width)
    line_h = int(font_size * _LINE_SPACING_FACTOR)
    total_text_h = len(lines) * line_h

    # Draw a subtle cyan accent bar at the top
    bar_h = max(8, height // 60)
    draw.rectangle([(0, 0), (width, bar_h)], fill=_ACCENT_COLOR)

    # Center text block vertically
    y_start = (height - total_text_h) // 2

    for idx, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (width - line_w) // 2
        y = y_start + idx * line_h
        draw.text((x, y), line, fill=_TEXT_COLOR, font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Caption generation (text-only)
# ---------------------------------------------------------------------------

def _build_text_caption_prompt(campaign_cfg: Any, profile: Any) -> str:
    """Build a caption-only generation prompt (no image concept)."""
    import json

    profile_data = profile.profile
    merged_rules = GLOBAL_HARD_RULES + (
        list(campaign_cfg.meme.hard_rules)
        if campaign_cfg.meme and campaign_cfg.meme.hard_rules
        else []
    )
    rules_text = "\n".join(f"- {r}" for r in merged_rules)
    creative_dir = campaign_cfg.creative_direction or "Produce engaging content."

    voice_summary = json.dumps(profile_data.get("caption_voice", {}), indent=2)

    return f"""You are writing a text post / X (Twitter) post for the '{campaign_cfg.name}' campaign.

CREATIVE DIRECTION:
{creative_dir}

CAPTION VOICE PROFILE:
{voice_summary}

MEASURABLE RULES:
{json.dumps(profile_data.get("measurable_rules", []), indent=2)}

HARD RULES (you must follow ALL):
{rules_text}

Write a single caption that:
- Perfectly matches the voice profile (tone, person, sentence length, punctuation)
- Is under 280 characters (suitable for X)
- Obeys ALL hard rules

Return ONLY a JSON object (no prose, no code fences):
{{
  "caption": "<the exact post text>"
}}
"""


def _parse_caption_response(raw_text: str) -> str | None:
    """Extract the caption string from the LLM response."""
    data = _extract_json_object(raw_text)
    if data is None:
        return None
    caption = data.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        return None
    return caption.strip()


def _generate_caption(campaign_cfg: Any, profile: Any) -> str:
    """
    Ask the LLM to produce a text post caption.

    Returns the caption string.
    Raises ValueError on failure after one retry.
    """
    prompt = _build_text_caption_prompt(campaign_cfg, profile)
    raw = call_text(prompt, max_tokens=256)
    log.debug("Text-post caption LLM response length=%d", len(raw))

    caption = _parse_caption_response(raw)

    if caption is None:
        log.warning("Text-post caption failed to parse JSON; retrying once")
        raw = call_text(prompt, max_tokens=256)
        caption = _parse_caption_response(raw)

    if caption is None:
        raise ValueError(
            f"LLM failed to produce a valid caption JSON after retry. "
            f"Preview: {raw[:300]}"
        )

    return caption


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_text_posts(
    campaign_cfg: Any,
    n: int,
    session: Any,
    *,
    mode_override: str | None = None,
) -> list[int]:
    """
    Generate *n* text-only posts and insert Clip rows.

    Args:
        campaign_cfg:  CampaignConfig instance (engines.memes should be True,
                       but this function can be called independently).
        n:             Number of text posts to generate.
        session:       SQLAlchemy session (caller manages commit/rollback).
        mode_override: Force 'demo' or 'production' mode.

    Returns:
        List of inserted Clip.id values.
    """
    from core.models import Clip
    from meme.profile import extract_profile, get_active_profile

    campaign_name = campaign_cfg.name
    mode = mode_override or campaign_cfg.mode

    # Ensure profile exists
    profile = get_active_profile(campaign_name, session)
    if profile is None:
        log.info(
            "No meme profile found; extracting for text posts",
            extra={"campaign": campaign_name},
        )
        profile = extract_profile(campaign_cfg, session)
        session.commit()

    hard_rules: list[str] = (
        list(campaign_cfg.meme.hard_rules)
        if campaign_cfg.meme and campaign_cfg.meme.hard_rules
        else []
    )

    inserted_ids: list[int] = []

    for i in range(n):
        log.info(
            "Generating text post %d/%d",
            i + 1,
            n,
            extra={"campaign": campaign_name},
        )

        try:
            # Step 1: generate caption
            caption = _generate_caption(campaign_cfg, profile)

            # Step 2: render text card (for review console display)
            card_bytes = render_text_card(caption)

            # Step 3: classify (text-only — voice + compliance, no visual scores)
            classification = classify(
                None,   # no image data → text-only mode
                caption,
                profile.profile,
                hard_rules,
            )
            verdict = classification["verdict"]

            # Step 4: insert Clip row
            clip_row = Clip(
                campaign=campaign_name,
                source_id=None,
                start=None,
                end=None,
                kind="meme",
                mode=mode,
                aspect="1:1",
                caption=caption,
                destination_channels=campaign_cfg.destinations.postiz_channels,
                status="pending_review" if verdict == "pass" else "rejected",
                reject_reason=(
                    "; ".join(classification["reasons"])
                    if verdict == "fail"
                    else None
                ),
                meme_meta={
                    "text_only": True,
                    "classifier_scores": {
                        k: v
                        for k, v in classification.items()
                        if k not in ("verdict", "reasons")
                    },
                    "profile_version": profile.version,
                },
                file_path=None,
                thumb_path=None,
            )
            session.add(clip_row)
            session.flush()

            # Step 5: save text card image
            stored_path = save_meme_image(clip_row.id, campaign_name, card_bytes)
            clip_row.file_path = stored_path
            clip_row.thumb_path = stored_path

            inserted_ids.append(clip_row.id)

            log.info(
                "Text post generated",
                extra={
                    "clip_id": clip_row.id,
                    "campaign": campaign_name,
                    "verdict": verdict,
                    "mode": mode,
                    "file_path": stored_path,
                },
            )

        except Exception as exc:
            log.error(
                "Text post generation failed for item %d/%d",
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
