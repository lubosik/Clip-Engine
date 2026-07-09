"""
web/campaigns_io.py — Campaign wizard I/O: YAML writing and asset saving.

Handles:
- Validating a config dict against the core.config Pydantic schema.
- Writing campaigns/<slug>.yaml safely (refusing path traversal).
- Saving uploaded asset files to assets/<slug>/ under fixed names:
    logo.png, logo_circle.png, outro.<ext>, font.ttf
- Patching the config's template paths to reflect the saved asset locations.

All filesystem operations are restricted to the project root so that a
malicious campaign name cannot escape the directory tree.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Allowed characters in a campaign slug: lowercase letters, digits, underscores, hyphens.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Fixed asset file names (extensions normalised at write time for non-fixed ones).
_ASSET_LOGO = "logo.png"
_ASSET_LOGO_CIRCLE = "logo_circle.png"
_ASSET_FONT = "font.ttf"

# Project root: two levels up from this file (clip-engine/).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _campaigns_dir() -> Path:
    return _PROJECT_ROOT / "campaigns"


def _assets_dir(slug: str) -> Path:
    return _PROJECT_ROOT / "assets" / slug


def slugify(name: str) -> str:
    """Convert a campaign name to a safe filesystem slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9_-]", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_-")
    return slug[:64]


def _assert_safe_slug(slug: str) -> None:
    """Raise ValueError if the slug fails validation."""
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"Campaign name {slug!r} is not a valid slug. "
            "Use only lowercase letters, digits, underscores, and hyphens."
        )
    # Paranoid path traversal guard.
    resolved = (_campaigns_dir() / (slug + ".yaml")).resolve()
    if not str(resolved).startswith(str(_campaigns_dir().resolve())):
        raise ValueError(f"Potential path traversal detected in slug {slug!r}")


def validate_campaign_config(config: dict[str, Any]) -> Any:
    """Validate *config* against core.config's CampaignConfig Pydantic schema.

    Returns the validated CampaignConfig model.
    Raises pydantic.ValidationError (or ImportError if core not installed yet).
    """
    from core.config import load_campaign_dict  # type: ignore[import]
    return load_campaign_dict(config)


def write_campaign_yaml(slug: str, config: dict[str, Any]) -> Path:
    """Write *config* to campaigns/<slug>.yaml.

    Returns the path of the written file.

    Raises:
        ValueError: if the slug is unsafe.
        OSError: if the write fails.
    """
    _assert_safe_slug(slug)
    campaigns_dir = _campaigns_dir()
    campaigns_dir.mkdir(parents=True, exist_ok=True)

    out_path = campaigns_dir / f"{slug}.yaml"
    with out_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)

    logger.info("Campaign YAML written: %s", out_path)
    return out_path


def save_asset(
    slug: str,
    asset_type: str,
    data: bytes,
    *,
    original_filename: str = "",
) -> Path:
    """Save an uploaded asset file to assets/<slug>/<fixed_name>.

    Args:
        slug:              Campaign slug (validated).
        asset_type:        One of: "logo", "corner_badge", "outro", "font".
        data:              Raw file bytes.
        original_filename: Original filename from the upload (used to
                           determine extension for outro).

    Returns:
        Path of the saved file (absolute).

    Raises:
        ValueError: on unknown asset_type or unsafe slug.
    """
    _assert_safe_slug(slug)
    asset_dir = _assets_dir(slug)
    asset_dir.mkdir(parents=True, exist_ok=True)

    filename = _fixed_asset_name(asset_type, original_filename)
    out_path = asset_dir / filename

    out_path.write_bytes(data)
    logger.info("Asset saved: type=%s path=%s size=%d bytes", asset_type, out_path, len(data))
    return out_path


def _fixed_asset_name(asset_type: str, original_filename: str) -> str:
    """Return the canonical filename for an asset type."""
    if asset_type == "logo":
        return _ASSET_LOGO
    if asset_type == "corner_badge":
        return _ASSET_LOGO_CIRCLE
    if asset_type == "font":
        return _ASSET_FONT
    if asset_type == "outro":
        # Preserve extension (mov/mp4/webm) but fix the basename.
        original_filename = original_filename or "outro.mov"
        suffix = Path(original_filename).suffix.lower() or ".mov"
        # Only allow known video extensions.
        if suffix not in {".mov", ".mp4", ".webm", ".avi", ".mkv"}:
            suffix = ".mov"
        return f"outro{suffix}"
    raise ValueError(
        f"Unknown asset_type {asset_type!r}. "
        "Expected: logo | corner_badge | outro | font"
    )


def patch_template_paths(config: dict[str, Any], slug: str) -> dict[str, Any]:
    """Update template asset paths in *config* to point to the saved locations.

    Modifies and returns a shallow copy.  Callers should use the returned dict
    when writing the YAML.
    """
    config = dict(config)
    template = dict(config.get("template", {}))
    asset_prefix = f"assets/{slug}"
    asset_dir = _assets_dir(slug)

    # Patch based on which asset files actually exist on disk — the wizard
    # frontend intentionally omits path fields; the server is the only party
    # that knows the final locations.
    if (asset_dir / _ASSET_FONT).exists():
        for section_key in ("captions", "hook"):
            section = dict(template.get(section_key, {}))
            section["font"] = f"{asset_prefix}/{_ASSET_FONT}"
            template[section_key] = section

    for section_key, asset_name in (
        ("watermark", _ASSET_LOGO),
        ("corner_badge", _ASSET_LOGO_CIRCLE),
    ):
        if (asset_dir / asset_name).exists():
            section = dict(template.get(section_key, {}))
            section["image"] = f"{asset_prefix}/{asset_name}"
            template[section_key] = section

    # Outro: point at the saved file; disable if enabled but no file exists.
    outro = dict(template.get("outro", {}))
    outro_file = None
    for ext in (".mov", ".mp4", ".webm", ".avi", ".mkv"):
        if (asset_dir / f"outro{ext}").exists():
            outro_file = f"{asset_prefix}/outro{ext}"
            break
    if outro_file:
        outro["clip"] = outro_file
    elif outro.get("enabled"):
        logger.warning(
            "Campaign %s: outro enabled but no outro file uploaded — disabling outro",
            slug,
        )
        outro["enabled"] = False
    template["outro"] = outro

    config["template"] = template
    return config


def save_meme_refs(
    slug: str,
    files: list[tuple[bytes, str]],
) -> Path:
    """Save meme reference images to campaigns/<slug>/meme_refs/.

    Args:
        slug:  Campaign slug (validated).
        files: List of (bytes, original_filename) pairs.

    Returns:
        Path of the meme_refs directory.
    """
    _assert_safe_slug(slug)
    refs_dir = _PROJECT_ROOT / "campaigns" / slug / "meme_refs"
    refs_dir.mkdir(parents=True, exist_ok=True)

    for data, filename in files:
        # Sanitise filename — allow only safe characters
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(filename).name)
        if not safe_name:
            safe_name = "ref.png"
        out = refs_dir / safe_name
        out.write_bytes(data)
        logger.info("Meme ref saved: path=%s size=%d bytes", out, len(data))

    return refs_dir


def save_visual_refs(
    slug: str,
    files: list[tuple[bytes, str]],
) -> Path:
    """Save visual reference images (desired clip look) to campaigns/<slug>/visual_refs/.

    These are passed to render/ranking as creative guidance (MASTER_SPEC Part L).

    Args:
        slug:  Campaign slug (validated).
        files: List of (bytes, original_filename) pairs.

    Returns:
        Path of the visual_refs directory.
    """
    _assert_safe_slug(slug)
    refs_dir = _PROJECT_ROOT / "campaigns" / slug / "visual_refs"
    refs_dir.mkdir(parents=True, exist_ok=True)

    for data, filename in files:
        safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", Path(filename).name)
        if not safe_name:
            safe_name = "ref.png"
        out = refs_dir / safe_name
        out.write_bytes(data)
        logger.info("Visual ref saved: path=%s size=%d bytes", out, len(data))

    return refs_dir


def _upload_assets_to_r2(slug: str) -> None:
    """Upload all saved assets for *slug* to R2 under campaigns/{slug}/assets/.

    Best-effort: logs errors but does not raise so that a failed R2 upload
    does not block the local save.
    """
    try:
        from core.settings import get_settings
        from core.r2 import upload_file as r2_upload
        from core.storage import r2_key_for_asset

        if not get_settings().r2_enabled:
            return

        asset_dir = _assets_dir(slug)
        if not asset_dir.exists():
            return

        for asset_file in asset_dir.iterdir():
            if not asset_file.is_file():
                continue
            key = r2_key_for_asset(slug, asset_file.name)
            try:
                r2_upload(asset_file, key)
                logger.info("Asset uploaded to R2: key=%s", key)
            except Exception as exc:
                logger.error(
                    "R2 asset upload failed (local copy still saved): "
                    "key=%s error=%s",
                    key,
                    exc,
                )
    except ImportError:
        pass  # core.r2 not available — skip silently
    except Exception as exc:
        logger.error("_upload_assets_to_r2 failed for slug=%s: %s", slug, exc)


def create_or_update_campaign(
    config: dict[str, Any],
    *,
    logo_bytes: bytes | None = None,
    logo_filename: str = "",
    corner_badge_bytes: bytes | None = None,
    corner_badge_filename: str = "",
    outro_bytes: bytes | None = None,
    outro_filename: str = "",
    font_bytes: bytes | None = None,
    font_filename: str = "",
    meme_refs_files: list[tuple[bytes, str]] | None = None,
    visual_refs_files: list[tuple[bytes, str]] | None = None,
) -> tuple[str, Path]:
    """High-level: validate, save assets, patch paths, write YAML.

    New fields accepted in *config* (all optional, handled transparently):
        mode              — 'demo' | 'production'
        engines           — {clips, memes}
        creative_direction — free-text brief string
        meme              — {refs_dir, image_model, hard_rules}
        demo              — {test_channels}
        template.watermark — placement / opacity accepted as-is
        template.hook.show_seconds — accepted as-is

    Args:
        config:           Campaign config dict (validated against schema).
        logo_bytes:       Raw bytes of logo image upload, or None.
        logo_filename:    Original filename of logo upload.
        corner_badge_bytes: Raw bytes of corner badge upload, or None.
        corner_badge_filename: Original filename of corner badge upload.
        outro_bytes:      Raw bytes of outro video upload, or None.
        outro_filename:   Original filename of outro video upload.
        font_bytes:       Raw bytes of caption font upload, or None.
        font_filename:    Original filename of font upload.
        meme_refs_files:  List of (bytes, filename) pairs for meme reference
                          images.  Saved to campaigns/<slug>/meme_refs/.
        visual_refs_files: List of (bytes, filename) pairs for visual reference
                          images (desired clip look).  Saved to
                          campaigns/<slug>/visual_refs/.

    Returns:
        (slug, yaml_path) tuple.

    Raises:
        ValueError: on invalid slug, unknown asset type, or schema violation.
    """
    raw_name: str = config.get("name", "")
    if not raw_name:
        raise ValueError("Campaign config must include a 'name' field")

    slug = slugify(raw_name)
    _assert_safe_slug(slug)

    # Normalise name to slug in the config.
    config = dict(config, name=slug)

    # Save standard asset files if provided.
    asset_map = [
        ("logo", logo_bytes, logo_filename),
        ("corner_badge", corner_badge_bytes, corner_badge_filename),
        ("outro", outro_bytes, outro_filename),
        ("font", font_bytes, font_filename),
    ]
    for asset_type, data, original_filename in asset_map:
        if data:
            save_asset(slug, asset_type, data, original_filename=original_filename)

    # Save meme reference images and update refs_dir in config.
    if meme_refs_files:
        refs_dir = save_meme_refs(slug, meme_refs_files)
        # Ensure meme.refs_dir points at the saved location (relative to project root).
        meme_block = dict(config.get("meme") or {})
        meme_block["refs_dir"] = f"campaigns/{slug}/meme_refs"
        config = dict(config, meme=meme_block)

    # Save visual reference images (creative guidance for render/ranking).
    if visual_refs_files:
        save_visual_refs(slug, visual_refs_files)

    # Patch template paths to point at the saved asset locations.
    config = patch_template_paths(config, slug)

    # Optional: validate against schema (requires core to be installed).
    try:
        validate_campaign_config(config)
    except ImportError:
        logger.debug("core.config not available; skipping schema validation")
    except Exception as exc:
        raise ValueError(f"Campaign config validation failed: {exc}") from exc

    yaml_path = write_campaign_yaml(slug, config)

    # Best-effort: upload assets to R2 when configured.
    _upload_assets_to_r2(slug)

    return slug, yaml_path
