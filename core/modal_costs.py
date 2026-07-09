"""
core/modal_costs.py — GPU USD/sec rate table and cost estimation helpers.

Rates verified 2026-07-08 from modal.com/pricing; these are estimates only.
Modal's billing API is gated to Team/Enterprise plans — we record wall-clock
duration from our own job records and multiply by the published per-second
rate.  Estimates are labelled as such throughout the dashboard.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# GPU rate table — USD per second of wall-clock render time
# Source: modal.com/pricing, verified 2026-07-08
# ---------------------------------------------------------------------------

GPU_RATES: dict[str, float] = {
    "l4": 0.000222,
    "t4": 0.000164,
    "a10g": 0.000306,
    "any": 0.000306,  # fallback when GPU selection is unspecified
}

# Default rate when the GPU type is unknown or not recorded
_FALLBACK_RATE: float = GPU_RATES["any"]


def rate_for(gpu: str | None) -> float:
    """Return the USD/sec rate for a GPU type.

    Falls back to the 'any' rate when *gpu* is None or unrecognised.  This
    makes cost estimates conservative (any/a10g is the highest published rate).
    """
    if not gpu:
        return _FALLBACK_RATE
    return GPU_RATES.get(gpu.lower().strip(), _FALLBACK_RATE)


def estimate_cost(gpu: str | None, duration_s: float) -> float:
    """Estimate the USD cost of a Modal render job.

    Args:
        gpu:        GPU type string ('l4', 't4', 'a10g', 'any') or None.
        duration_s: Wall-clock duration in seconds (must be >= 0).

    Returns:
        Estimated cost in USD (>= 0).  Returned value is labelled 'estimated'
        in all API responses — do not treat it as a billing figure.
    """
    if duration_s < 0:
        duration_s = 0.0
    return rate_for(gpu) * duration_s
