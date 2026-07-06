"""
overlay.py — Single-pass ffmpeg overlay compositor.

Burns (in this order) onto the reframed 9:16 video:
  1. ASS word-by-word captions (subtitles filter via libass).
  2. Hook text box (drawtext, enabled only during show_seconds).
  3. Centred semi-transparent watermark (scaled to watermark.scale * output_w).
  4. Corner badge (scaled to corner_badge.scale * output_w, positioned by name).
  5. Lower-third source credit ("via @handle") at ~88% of height.

All positions, colours, sizes, and opacities come from cfg.template — nothing
is hardcoded to a brand or niche.

Implementation notes
--------------------
* We write the filtergraph to a temp script file (`-filter_complex_script`) to
  avoid shell escaping complexity and to keep the long filter string readable.
* Watermark and badge are optional inputs.  If the asset file does not exist
  the corresponding overlay step is skipped and no input slot is consumed.
* Hook text is written to a sidecar file (`hook_text.txt`) and referenced via
  `textfile=` to avoid special-character escaping in the filter string.
* The subtitles filter requires a fontsdir pointing at the directory containing
  the campaign font file so libass can find custom fonts.
"""

import logging
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_overlays(
    reframed_video: Path,
    ass_path: Path,
    cfg_template: Any,
    source_meta: dict,
    clip: dict,
    workdir: Path,
    *,
    stem: str = "overlaid",
) -> Path:
    """Composite captions, hook, watermark, badge, and credit onto *reframed_video*.

    Parameters
    ----------
    reframed_video : Path
        9:16 clip from reframe.py.
    ass_path : Path
        ASS subtitle file from captions.py.
    cfg_template :
        ``cfg.template`` (CampaignConfig template or compatible namespace).
    source_meta : dict
        Must contain ``"source_handle"`` key for the credit text.
    clip : dict
        Must contain ``"hook"`` string and ``"score"`` float.
    workdir : Path
        Temporary directory; sidecar files written here.

    Returns
    -------
    Path to the composited MP4.
    """
    tmpl = cfg_template
    cap = tmpl.captions
    hook_cfg = tmpl.hook
    wm_cfg = tmpl.watermark
    badge_cfg = tmpl.corner_badge
    lt_cfg = tmpl.lower_third
    out_w, out_h = tmpl.resolution

    output_path = workdir / f"{stem}.mp4"
    hook_text_file = workdir / "hook_text.txt"

    # ------------------------------------------------------------------
    # Font path / fontsdir
    # ------------------------------------------------------------------
    font_path = (
        Path(cap.font).resolve() if cap.font and Path(cap.font).exists() else None
    )
    fontsdir = str(font_path.parent) if font_path else ""

    # Also look for the hook font (may differ from caption font)
    hook_font_path = (
        Path(hook_cfg.font).resolve()
        if hasattr(hook_cfg, "font") and hook_cfg.font and Path(hook_cfg.font).exists()
        else font_path
    )

    # ------------------------------------------------------------------
    # Hook text setup
    # ------------------------------------------------------------------
    hook_enabled = getattr(hook_cfg, "enabled", False)
    hook_show_start = 0.0
    hook_show_end = 8.0
    if hook_enabled and hasattr(hook_cfg, "show_seconds"):
        ss = hook_cfg.show_seconds
        hook_show_start = float(ss[0]) if ss else 0.0
        hook_show_end = float(ss[1]) if len(ss) > 1 else 8.0

    hook_text = str(clip.get("hook", "")).strip()
    # Write hook text to file to avoid all drawtext escaping concerns
    hook_text_file.write_text(
        _wrap_text(hook_text, chars_per_line=32), encoding="utf-8"
    )

    # Hook box colour (ffmpeg format: 0xRRGGBBAA  or  color@alpha)
    hook_box_color_raw = getattr(hook_cfg, "box_color", "#111111CC")
    hook_box_color = _html_to_ffmpeg_color(hook_box_color_raw)

    # ------------------------------------------------------------------
    # Watermark setup
    # ------------------------------------------------------------------
    wm_path = Path(wm_cfg.image) if wm_cfg and getattr(wm_cfg, "image", None) else None
    wm_exists = wm_path is not None and wm_path.exists()
    wm_opacity = float(getattr(wm_cfg, "opacity", 0.18)) if wm_cfg else 0.18
    wm_scale = float(getattr(wm_cfg, "scale", 0.5)) if wm_cfg else 0.5
    wm_w = int(out_w * wm_scale)

    # ------------------------------------------------------------------
    # Badge setup
    # ------------------------------------------------------------------
    badge_path = (
        Path(badge_cfg.image)
        if badge_cfg and getattr(badge_cfg, "image", None)
        else None
    )
    badge_exists = badge_path is not None and badge_path.exists()
    badge_opacity = float(getattr(badge_cfg, "opacity", 1.0)) if badge_cfg else 1.0
    badge_scale = float(getattr(badge_cfg, "scale", 0.12)) if badge_cfg else 0.12
    badge_w = int(out_w * badge_scale)
    badge_pos = getattr(badge_cfg, "position", "top_right") if badge_cfg else "top_right"

    # ------------------------------------------------------------------
    # Lower-third credit text
    # ------------------------------------------------------------------
    show_credit = getattr(lt_cfg, "show_source_handle", True) if lt_cfg else True
    credit_format = getattr(lt_cfg, "format", "via @{source_handle}") if lt_cfg else "via @{source_handle}"
    source_handle = source_meta.get("source_handle", source_meta.get("channelName", ""))
    credit_text = credit_format.format(source_handle=source_handle)

    # ------------------------------------------------------------------
    # Build filtergraph
    # ------------------------------------------------------------------
    inputs: list[str] = ["-i", str(reframed_video)]
    input_slots: dict[str, int] = {}  # "wm" → input index, "badge" → input index

    if wm_exists:
        input_slots["wm"] = len(inputs) // 2
        inputs += ["-i", str(wm_path)]
    if badge_exists:
        input_slots["badge"] = len(inputs) // 2
        inputs += ["-i", str(badge_path)]

    filter_lines: list[str] = []
    current = "[v0]"
    filter_lines.append(f"[0:v]copy{current}")

    # 1. Subtitles (captions)
    ass_escaped = _escape_fc_path(str(ass_path.resolve()))
    fontsdir_escaped = _escape_fc_path(fontsdir) if fontsdir else ""
    subtitle_arg = f"subtitles='{ass_escaped}'"
    if fontsdir_escaped:
        subtitle_arg += f":fontsdir='{fontsdir_escaped}'"
    filter_lines.append(f"{current}{subtitle_arg}[v1]")
    current = "[v1]"

    # 2. Hook box (drawtext)
    if hook_enabled and hook_text:
        cap_fontsize = max(48, int(out_h * 0.042))
        hook_fontsize = max(44, int(out_h * 0.038))
        hook_y = int(out_h * 0.08)

        hook_filter = _build_drawtext(
            textfile=str(hook_text_file.resolve()),
            fontfile=str(hook_font_path) if hook_font_path else None,
            fontsize=hook_fontsize,
            fontcolor="white",
            box=True,
            boxcolor=hook_box_color,
            boxborderw=20,
            x="(w-text_w)/2",
            y=str(hook_y),
            enable_expr=f"between(t\\,{hook_show_start:.3f}\\,{hook_show_end:.3f})",
        )
        filter_lines.append(f"{current}{hook_filter}[v2]")
        current = "[v2]"

    # 3. Watermark overlay
    v_idx = int(current[2:-1])  # extract number from [vN]
    if wm_exists:
        wm_slot = input_slots["wm"]
        v_idx += 1
        wm_alpha_label = f"[wm_rgba]"
        wm_scaled_label = f"[wm_sc]"
        next_v = f"[v{v_idx}]"

        filter_lines.append(
            f"[{wm_slot}:v]format=rgba,colorchannelmixer=aa={wm_opacity:.4f}{wm_alpha_label}"
        )
        filter_lines.append(
            f"{wm_alpha_label}scale={wm_w}:-1{wm_scaled_label}"
        )
        filter_lines.append(
            f"{current}{wm_scaled_label}overlay=(W-w)/2:(H-h)/2{next_v}"
        )
        current = next_v

    # 4. Corner badge overlay
    if badge_exists:
        badge_slot = input_slots["badge"]
        v_idx += 1
        badge_raw_label = "[badge_raw]"
        badge_sc_label = "[badge_sc]"
        next_v = f"[v{v_idx}]"

        badge_x_expr, badge_y_expr = _badge_position_expr(badge_pos)
        filter_lines.append(
            f"[{badge_slot}:v]format=rgba,colorchannelmixer=aa={badge_opacity:.4f}{badge_raw_label}"
        )
        filter_lines.append(
            f"{badge_raw_label}scale={badge_w}:-1{badge_sc_label}"
        )
        filter_lines.append(
            f"{current}{badge_sc_label}overlay={badge_x_expr}:{badge_y_expr}{next_v}"
        )
        current = next_v

    # 5. Lower-third credit (drawtext)
    if show_credit and credit_text:
        v_idx += 1
        next_v = f"[v{v_idx}]"
        credit_fontsize = max(24, int(out_h * 0.018))
        credit_escaped = _escape_drawtext(credit_text)
        credit_fontfile_arg = (
            f":fontfile='{_escape_fc_path(str(hook_font_path))}'"
            if hook_font_path
            else ""
        )
        credit_filter = (
            f"drawtext=text='{credit_escaped}'"
            f"{credit_fontfile_arg}"
            f":fontsize={credit_fontsize}"
            f":fontcolor=white@0.75"
            f":x=(w-text_w)/2"
            f":y=h*0.88"
        )
        filter_lines.append(f"{current}{credit_filter}{next_v}")
        current = next_v

    # Rename final label to [out]
    filter_lines.append(f"{current}copy[out]")

    # ------------------------------------------------------------------
    # Write filtergraph script
    # ------------------------------------------------------------------
    fg_script = workdir / "filtergraph.txt"
    fg_script.write_text(";\n".join(filter_lines), encoding="utf-8")

    # ------------------------------------------------------------------
    # Run ffmpeg
    # ------------------------------------------------------------------
    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex_script", str(fg_script),
            "-map", "[out]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(
            "apply_overlays: ffmpeg failed.\nFILTERGRAPH:\n%s\nSTDERR:\n%s",
            fg_script.read_text(),
            result.stderr[-4000:],
        )
        raise RuntimeError(
            f"apply_overlays: ffmpeg exited {result.returncode}. "
            f"See log for filtergraph and stderr."
        )

    log.info("apply_overlays: wrote %s", output_path.name)
    return output_path


# ---------------------------------------------------------------------------
# drawtext builder
# ---------------------------------------------------------------------------

def _build_drawtext(
    *,
    textfile: str | None = None,
    text: str | None = None,
    fontfile: str | None = None,
    fontsize: int,
    fontcolor: str,
    box: bool = False,
    boxcolor: str = "black@0.5",
    boxborderw: int = 10,
    x: str = "(w-text_w)/2",
    y: str = "h*0.5",
    enable_expr: str | None = None,
    line_spacing: int = 4,
) -> str:
    """Build a drawtext filter segment string."""
    parts: list[str] = ["drawtext="]

    if textfile:
        parts.append(f"textfile='{_escape_fc_path(textfile)}'")
    elif text:
        parts.append(f"text='{_escape_drawtext(text)}'")
    else:
        raise ValueError("_build_drawtext: either textfile or text must be provided")

    if fontfile:
        parts.append(f"fontfile='{_escape_fc_path(fontfile)}'")
    parts.append(f"fontsize={fontsize}")
    parts.append(f"fontcolor={fontcolor}")
    if box:
        parts.append("box=1")
        parts.append(f"boxcolor={boxcolor}")
        parts.append(f"boxborderw={boxborderw}")
    parts.append(f"x={x}")
    parts.append(f"y={y}")
    parts.append(f"line_spacing={line_spacing}")
    if enable_expr:
        parts.append(f"enable='{enable_expr}'")

    # Join with the ffmpeg filter option separator ':'
    # The first element ("drawtext=") already has '=' so join rest with ':'
    return parts[0] + ":".join(parts[1:])


# ---------------------------------------------------------------------------
# Badge position expressions
# ---------------------------------------------------------------------------

_BADGE_POSITIONS = {
    "top_right":    ("W-w-W*0.03", "H*0.03"),
    "top_left":     ("W*0.03",     "H*0.03"),
    "bottom_right": ("W-w-W*0.03", "H-h-H*0.03"),
    "bottom_left":  ("W*0.03",     "H-h-H*0.03"),
    "center":       ("(W-w)/2",    "(H-h)/2"),
}


def _badge_position_expr(position: str) -> tuple[str, str]:
    pos = (position or "top_right").lower().replace(" ", "_")
    return _BADGE_POSITIONS.get(pos, _BADGE_POSITIONS["top_right"])


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def _wrap_text(text: str, chars_per_line: int = 32) -> str:
    """Wrap text at word boundaries for drawtext textfile."""
    words = text.split()
    lines: list[str] = []
    current_line: list[str] = []
    current_len = 0
    for word in words:
        if current_len + len(word) + (1 if current_line else 0) > chars_per_line:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
            current_len = len(word)
        else:
            current_line.append(word)
            current_len += len(word) + (1 if len(current_line) > 1 else 0)
    if current_line:
        lines.append(" ".join(current_line))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Escaping helpers
# ---------------------------------------------------------------------------

def _escape_fc_path(path: str) -> str:
    """Escape a file path for use inside a filter_complex option value.

    In a filter_complex string, ':' separates filter options (must be escaped
    as '\\:') and backslash is the escape character.  On Linux, paths rarely
    contain these characters but we escape them defensively.
    """
    return path.replace("\\", "\\\\").replace(":", "\\:")


def _escape_drawtext(text: str) -> str:
    """Escape special characters for drawtext text= value."""
    # Order matters: backslash first
    return (
        text
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _html_to_ffmpeg_color(html_color: str) -> str:
    """Convert #RRGGBB or #RRGGBBAA to ffmpeg color notation 0xRRGGBBAA.

    In ffmpeg drawtext, boxcolor uses 0xRRGGBBAA where AA is opacity
    (FF = fully opaque, 00 = transparent) — consistent with HTML RGBA.
    """
    h = html_color.lstrip("#")
    if len(h) == 6:
        return f"0x{h}FF"
    if len(h) == 8:
        return f"0x{h}"
    # Fallback: black semi-transparent
    return "0x111111CC"
