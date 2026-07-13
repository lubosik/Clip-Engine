"""
core/fewshot.py — Real contrasting-pair few-shots for boundary prompts (B3).

Loads tests/fixtures/segmentation/boundary_failure_pairs.json at import time
and renders the 4 pairs into REAL_BOUNDARY_PAIRS (a prompt-ready string).

Updating the JSON automatically updates every prompt that imports this module —
no code change needed when new failure pairs are added.

Public:
    REAL_BOUNDARY_PAIRS       str   compact few-shot block for LLM prompts
    REAL_BOUNDARY_PAIRS_RAW   list  parsed pair dicts (for the eval harness)
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locate + load the JSON (path relative to this file's parent's parent)
# ---------------------------------------------------------------------------

_PAIRS_PATH = (
    pathlib.Path(__file__).parent.parent
    / "tests"
    / "fixtures"
    / "segmentation"
    / "boundary_failure_pairs.json"
)


def _load_pairs() -> list[dict[str, Any]]:
    try:
        with open(_PAIRS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("pairs", [])
    except Exception as exc:
        log.warning("Failed to load boundary failure pairs from %s: %s", _PAIRS_PATH, exc)
        return []


REAL_BOUNDARY_PAIRS_RAW: list[dict[str, Any]] = _load_pairs()


# ---------------------------------------------------------------------------
# Render pairs into compact few-shot text
# ---------------------------------------------------------------------------

def _render_pairs(pairs: list[dict[str, Any]]) -> str:
    if not pairs:
        return ""

    lines: list[str] = [
        "REAL FAILURE CASES (from production — use these as the primary boundary examples):",
        "",
    ]
    for p in pairs:
        pid = p.get("id", "?")
        w = p.get("wrong", {})
        c = p.get("correct", {})
        wr = (p.get("wrong_reason") or "").strip()
        cr = (p.get("correct_reason") or "").strip()
        lines.append(
            f"[{pid}] WRONG cut {w.get('start', '?')}-{w.get('end', '?')}s: {wr}"
        )
        lines.append(
            f"        CORRECT cut {c.get('start', '?')}-{c.get('end', '?')}s: {cr}"
        )
        lines.append("")

    lines.append(
        "Apply the same logic: identify where the idea OPENS and where it RESOLVES; "
        "never end on a new list item, new speaker subject, or new question."
    )
    return "\n".join(lines)


REAL_BOUNDARY_PAIRS: str = _render_pairs(REAL_BOUNDARY_PAIRS_RAW)


# ---------------------------------------------------------------------------
# Positive gold-standard exemplar, distilled from the reference TikTok
# (style_refs/REFERENCE_TEMPLATE.md §Structural Exemplar). A clip should match
# THIS structural shape: open on the claim, one concrete example, end the moment
# the payoff lands.
# ---------------------------------------------------------------------------

REFERENCE_EXEMPLAR: str = (
    "GOLD-STANDARD POSITIVE EXAMPLE (a real published clip that works):\n"
    "- STARTS on the first word of the substantive answer/claim (no interviewer "
    "question, no preamble, no mid-sentence clarification).\n"
    "- DEVELOPS the point with ONE concrete example using specific numbers, so a "
    "first-time viewer can follow a real decision path.\n"
    "- ENDS the instant the payoff lands — the confident closing statement that "
    "crystallises the whole argument — and NOT one sentence later into the next "
    "topic. Arc: principle -> concrete example -> bold payoff. Every sentence "
    "advances the point; no filler. The clip stands alone with zero prior context."
)

