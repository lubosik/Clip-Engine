"""
captions.py — Word-by-word ASS karaoke caption generation.

Design contract
---------------
* build_ass() expects word timings that are CUT-RELATIVE (t=0 equals the first
  frame of the rendered clip).  Converting from source-relative to cut-relative
  is the caller's responsibility (render_clip in __init__.py subtracts
  clip["start"] before calling this module).

* If word timings are not available the caller should pass words=None and call
  get_word_timings() to run faster-whisper on the cut clip's extracted WAV.
  The result is already cut-relative.

ASS karaoke approach
--------------------
We use per-word Dialogue events.  For each line (group of ≤ max_words_per_line
words), we emit N dialogue events (one per word).  Each event:
  * spans from that word's start time to the next word's start time (or the
    word's own end time for the last word in the line).
  * renders the full line text with the active word coloured in highlight_color
    and the other words in base_color.

This produces seamless word-by-word highlighting with no gaps in display.  It
renders identically to a traditional karaoke approach and is simpler to
generate and debug.

ASS colour format: &HAABBGGRR  (AA = 00 for opaque; colours in BGR order).
"""

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_ass(
    words: list[dict],
    template: Any,
    output_path: Path,
) -> Path:
    """Write an ASS file for the word timings to *output_path*.

    Parameters
    ----------
    words :
        List of ``{"word": str, "start": float, "end": float}`` all with times
        relative to the CUT clip (t=0 = clip start).
    template :
        ``cfg.template`` (CampaignConfig template sub-model or compatible
        namespace).  Must expose ``.captions`` and ``.resolution``.
    output_path :
        Destination path for the ``.ass`` file.

    Returns
    -------
    Path
        Same as *output_path*.
    """
    cap = template.captions
    out_w, out_h = template.resolution

    # cap.font may be None (wizard campaigns without an uploaded font) —
    # fall back to a widely available bold system font.
    font_name = _font_name_from_path(Path(cap.font)) if cap.font else "DejaVu Sans"
    font_size = max(48, int(out_h * 0.042))   # ~80 px at 1920h

    base_color = _hex_to_ass_color(cap.base_color)
    highlight_color = _hex_to_ass_color(cap.highlight_color)
    outline_color = _hex_to_ass_color(cap.outline_color)
    back_color = "&H00000000"               # transparent background

    alignment, margin_v = _position_to_alignment(cap.position, out_h)

    lines_of_words = _group_into_lines(words, cap.max_words_per_line)

    dialogue_events = _build_dialogue_events(
        lines_of_words,
        base_color=base_color,
        highlight_color=highlight_color,
    )

    ass_content = _render_ass(
        play_res_x=out_w,
        play_res_y=out_h,
        font_name=font_name,
        font_size=font_size,
        primary_color=base_color,
        outline_color=outline_color,
        back_color=back_color,
        outline_px=cap.outline_px,
        shadow_px=2,
        alignment=alignment,
        margin_v=margin_v,
        events=dialogue_events,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(ass_content, encoding="utf-8-sig")
    log.info(
        "build_ass: %d words → %d lines → %d events → %s",
        len(words), len(lines_of_words), len(dialogue_events), output_path.name,
    )
    return output_path


def get_word_timings(audio_path: Path, *, model_size: str = "small") -> list[dict]:
    """Transcribe *audio_path* with faster-whisper and return cut-relative words.

    This is the fallback path when the upstream pipeline did not supply
    word-level timestamps.  The audio file must already be the extracted WAV
    from the cut clip so that timestamps are cut-relative.

    Parameters
    ----------
    audio_path :
        16-kHz mono WAV produced by ``cut.cut_clip``.
    model_size :
        faster-whisper model size (default ``"small"`` per SPEC §10).

    Returns
    -------
    List of ``{"word": str, "start": float, "end": float}``.
    """
    # Guarded heavy import — do not move to module level
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed.  Install it with: "
            "pip install faster-whisper"
        ) from exc

    log.info("get_word_timings: running faster-whisper '%s' on %s", model_size, audio_path)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    segments, _ = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        language=None,   # auto-detect
    )

    words: list[dict] = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append({
                    "word": w.word.strip(),
                    "start": float(w.start),
                    "end": float(w.end),
                })

    log.info("get_word_timings: got %d words", len(words))
    return words


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def _hex_to_ass_color(hex_color: str) -> str:
    """Convert an HTML colour to ASS ``&HAABBGGRR`` format.

    Accepted inputs: ``#RRGGBB`` or ``#RRGGBBAA`` where AA is HTML opacity
    (FF = fully opaque).  ASS alpha is the inverse (00 = fully opaque).
    """
    h = hex_color.lstrip("#")
    if len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        aa = 0x00  # fully opaque
    elif len(h) == 8:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        alpha_html = int(h[6:8], 16)   # FF = fully opaque in HTML
        aa = 255 - alpha_html          # 00 = fully opaque in ASS
    else:
        raise ValueError(
            f"_hex_to_ass_color: unrecognised colour '{hex_color}'. "
            "Expected #RRGGBB or #RRGGBBAA."
        )
    return f"&H{aa:02X}{b:02X}{g:02X}{r:02X}"


# ---------------------------------------------------------------------------
# Position / alignment mapping
# ---------------------------------------------------------------------------

def _position_to_alignment(position: str, out_h: int) -> tuple[int, int]:
    """Return (ass_alignment, margin_v_px) for the given position name.

    ASS numpad layout:
      7 8 9  (top)
      4 5 6  (middle)
      1 2 3  (bottom)

    ``upper_mid`` → alignment 8, MarginV ≈ 38% of height from top.
    ``center``    → alignment 5, MarginV = 0 (vertically centred).
    ``lower_third`` → alignment 2, MarginV ≈ 15% of height from bottom.
    """
    pos = (position or "upper_mid").lower()
    if pos == "upper_mid":
        return 8, int(out_h * 0.38)
    if pos == "center":
        return 5, 0
    if pos == "lower_third":
        return 2, int(out_h * 0.15)
    # Default fallback
    log.warning("captions: unknown position '%s'; defaulting to upper_mid", position)
    return 8, int(out_h * 0.38)


# ---------------------------------------------------------------------------
# Line grouping
# ---------------------------------------------------------------------------

def _group_into_lines(
    words: list[dict], max_words_per_line: int
) -> list[list[dict]]:
    """Split *words* into lines of at most *max_words_per_line* words."""
    if not words:
        return []
    lines = []
    for i in range(0, len(words), max_words_per_line):
        chunk = words[i : i + max_words_per_line]
        if chunk:
            lines.append(chunk)
    return lines


# ---------------------------------------------------------------------------
# Dialogue event construction
# ---------------------------------------------------------------------------

def _build_dialogue_events(
    lines_of_words: list[list[dict]],
    *,
    base_color: str,
    highlight_color: str,
) -> list[tuple[float, float, str]]:
    """Return list of (start_secs, end_secs, ass_text) for every word highlight.

    Each word in each line gets its own event spanning from that word's start
    to the next word's start (or the word's own end for the last word).
    """
    events: list[tuple[float, float, str]] = []

    for line_words in lines_of_words:
        for idx, word in enumerate(line_words):
            evt_start = word["start"]
            # Event ends when the next word begins, or at this word's own end
            if idx + 1 < len(line_words):
                evt_end = line_words[idx + 1]["start"]
            else:
                evt_end = word["end"]

            # Guard against zero/negative duration
            if evt_end <= evt_start:
                evt_end = evt_start + 0.05  # minimum 50 ms

            text = _build_line_text(line_words, idx, base_color, highlight_color)
            events.append((evt_start, evt_end, text))

    return events


def _build_line_text(
    line_words: list[dict],
    active_idx: int,
    base_color: str,
    highlight_color: str,
) -> str:
    """Build the ASS dialogue text for one word highlight within a line.

    Non-active words are rendered in *base_color*; the active word in
    *highlight_color*.  The style primary color is base_color so only the
    active word needs an explicit override tag.

    Example (active=1, words=[WORD0, WORD1, WORD2]):
        WORD0 {\\c&H00FFE500&}WORD1{\\c&H00FFFFFF&} WORD2
    """
    parts: list[str] = []
    for i, w in enumerate(line_words):
        token = w["word"]
        if i == active_idx:
            # Highlight override → reset back to base after the word
            parts.append(
                f"{{\\c{highlight_color}}}{token}{{\\c{base_color}}}"
            )
        else:
            parts.append(token)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# ASS file renderer
# ---------------------------------------------------------------------------

def _render_ass(
    *,
    play_res_x: int,
    play_res_y: int,
    font_name: str,
    font_size: int,
    primary_color: str,
    outline_color: str,
    back_color: str,
    outline_px: int,
    shadow_px: int,
    alignment: int,
    margin_v: int,
    events: list[tuple[float, float, str]],
) -> str:
    """Render the full ASS file content as a string."""
    style_line = (
        f"Style: Default,"
        f"{font_name},{font_size},"
        f"{primary_color},"       # PrimaryColour  (base = non-active words)
        f"&H000000FF,"             # SecondaryColour (karaoke fill, unused here)
        f"{outline_color},"       # OutlineColour
        f"{back_color},"          # BackColour
        f"-1,"                     # Bold (-1 = true)
        f"0,"                      # Italic
        f"0,"                      # Underline
        f"0,"                      # StrikeOut
        f"100,100,"                # ScaleX, ScaleY
        f"0,"                      # Spacing
        f"0,"                      # Angle
        f"1,"                      # BorderStyle (1 = outline+shadow)
        f"{outline_px},"          # Outline
        f"{shadow_px},"           # Shadow
        f"{alignment},"           # Alignment (numpad)
        f"10,10,{margin_v},"      # MarginL, MarginR, MarginV
        f"1"                       # Encoding
    )

    event_lines: list[str] = []
    for start, end, text in events:
        event_lines.append(
            f"Dialogue: 0,"
            f"{_ass_time(start)},"
            f"{_ass_time(end)},"
            f"Default,,0,0,0,,"
            f"{text}"
        )

    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "Collisions: Normal",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        style_line,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *event_lines,
        "",
    ])


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------

def _ass_time(seconds: float) -> str:
    """Format seconds as ASS timestamp ``H:MM:SS.cc`` (centiseconds)."""
    total_cs = max(0, int(round(seconds * 100)))
    h = total_cs // 360000
    total_cs -= h * 360000
    m = total_cs // 6000
    total_cs -= m * 6000
    s = total_cs // 100
    cs = total_cs % 100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ---------------------------------------------------------------------------
# Font name helper
# ---------------------------------------------------------------------------

def _font_name_from_path(font_path: Path) -> str:
    """Derive a display font name from the TTF filename.

    ``Montserrat-ExtraBold.ttf`` → ``Montserrat ExtraBold``

    If fonttools is available it reads the actual name table entry; otherwise
    it falls back to the stem-based heuristic.
    """
    try:
        from fontTools.ttLib import TTFont  # type: ignore  # optional dep
        tt = TTFont(str(font_path))
        name_table = tt["name"]
        # nameID 4 = Full font name
        for record in name_table.names:
            if record.nameID == 4 and record.platformID in (1, 3):
                try:
                    return record.toUnicode()
                except Exception:
                    pass
    except Exception:
        pass

    stem = font_path.stem
    return re.sub(r"[-_]", " ", stem)
