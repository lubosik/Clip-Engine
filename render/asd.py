"""
render/asd.py — Active Speaker Detection integration + improved fallback.

Feature flag: REFRAME_ASD=1 (env var, default 0 / off locally).

LR-ASD (Junhua-Liao/LR-ASD, MIT, IJCV 2025)
---------------------------------------------
LR-ASD consumes YuNet face tracks + audio mel windows → per-track speaking
probability; pick highest mean per scene.  It is 0.84M params and runs ~free
on L4.  The model requires:
  - torch, torchaudio, scipy, python_speech_features
  - The LR-ASD repository code (fetch at image build, pin a commit SHA)
  - AVA weights (download at image build, ~4 MB)

Current status: STUBBED.
The integration point (`select_active_speaker_asd`) is defined below, calls
the stub, and propagates the NotImplementedError so render/reframe.py catches
it and falls through to the improved heuristic.

To activate full LR-ASD:
1. Add the repo + weights download to render/modal_app.py image definition.
2. Implement the actual LR-ASD inference in `_run_lrasd()` below.
3. Set REFRAME_ASD=1 in the Modal secret or local .env.
4. Verify with a real clip segment that has 2+ faces.

Fallback chain (always active even when ASD is enabled):
  LR-ASD (if REFRAME_ASD=1 and weights loaded)
    → mouth-variance heuristic (render/reframe.py _mouth_movement_variance_pixel)
    → largest+most-central face (render/reframe.py _select_largest_and_most_central)
    → center crop (no faces)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

# --------------------------------------------------------------------------
# Module-level flag
# --------------------------------------------------------------------------

REFRAME_ASD: bool = os.environ.get("REFRAME_ASD", "0") == "1"

# Path where LR-ASD weights would be placed in the Modal container.
# At image-build time: download to /models/lrasd/ and pin the commit SHA.
_LRASD_WEIGHTS_PATH = Path(
    os.environ.get("LRASD_WEIGHTS_PATH", "/models/lrasd/weight/finetuning_AVA.model")
)
# Commit SHA of the LR-ASD repo to use when it is fetched at image-build time.
_LRASD_REPO_COMMIT = "a8d5b7f"  # placeholder — update when activating


# --------------------------------------------------------------------------
# FaceBox import — avoid circular import by redefining the type locally
# --------------------------------------------------------------------------

# render.reframe.FaceBox is the actual type; we reference it as Any here
# to avoid a circular import. The function signatures use list[Any] instead.
from typing import Any  # noqa: E402


# --------------------------------------------------------------------------
# Public interface (called from render/reframe.py)
# --------------------------------------------------------------------------

def select_active_speaker_asd(
    frames: list[Optional[np.ndarray]],
    frame_faces: list[list[Any]],   # list[list[FaceBox]]
    timestamps: list[float],
    src_w: int,
    src_h: int,
    log: logging.Logger,
) -> Optional[float]:
    """Attempt LR-ASD speaker selection.

    Args:
        frames:       BGR frames (same length as timestamps); None for missing.
        frame_faces:  Detected face boxes per frame (normalised coords).
        timestamps:   Clip-relative timestamps for each frame.
        src_w, src_h: Source frame dimensions in pixels.
        log:          Logger from the calling pipeline.

    Returns:
        Active speaker face center_x in [0, 1], or None on failure.

    Raises:
        NotImplementedError: when weights are unavailable (stub state).
        Any torch/model exception: caught by the caller in render/reframe.py,
            which falls through to the heuristic chain.
    """
    if not REFRAME_ASD:
        # Feature flag off: should not be called, but be defensive.
        return None

    if not _LRASD_WEIGHTS_PATH.exists():
        raise NotImplementedError(
            f"LR-ASD weights not found at {_LRASD_WEIGHTS_PATH}. "
            "Set LRASD_WEIGHTS_PATH or disable REFRAME_ASD. "
            "Falling back to heuristic speaker selection."
        )

    # -----------------------------------------------------------------------
    # Full LR-ASD inference — STUB (not yet implemented).
    # When activating:
    # 1. Import the LR-ASD model class from the fetched repo.
    # 2. Load weights from _LRASD_WEIGHTS_PATH.
    # 3. Build face tracks from frame_faces + timestamps.
    # 4. Extract audio mel windows aligned to the face tracks.
    # 5. Run inference → per-track speaking probability.
    # 6. Return the center_x of the track with the highest mean probability.
    # -----------------------------------------------------------------------
    raise NotImplementedError(
        "LR-ASD inference not yet implemented. "
        "Weights path exists but model code is stubbed. "
        "Falling back to heuristic speaker selection."
    )


# --------------------------------------------------------------------------
# Improved heuristic (available for direct import if needed)
# --------------------------------------------------------------------------

def select_by_size_and_centrality(
    frame_faces: list[list[Any]],
) -> Optional[float]:
    """Score each face by (area × centrality) and return the winner's center_x.

    centrality = 1 - 2 * |center_x - 0.5|  (0 at left/right edge, 1 at center)

    This is the same logic as _select_largest_and_most_central in reframe.py;
    kept here so it can be tested and reused independently.
    """
    best_score = -1.0
    best_cx: Optional[float] = None
    for ff in frame_faces:
        for face in ff:
            centrality = 1.0 - 2.0 * abs(face.center_x - 0.5)
            score = face.area * centrality
            if score > best_score:
                best_score = score
                best_cx = face.center_x
    return best_cx
