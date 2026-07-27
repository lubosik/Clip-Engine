"""
tests/test_simulate_pipeline.py — CI wrapper for the pipeline simulation harness.

Calls the harness scenario functions directly (not via subprocess) so CI keeps
the simulation green on every push.  A single fixture creates the HarnessContext
once per module; all scenario tests share it to avoid redundant DB + ffmpeg setup.

Contract: all 7 scenarios + global asserts must pass at 100%.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure repo root is on sys.path (handles direct `pytest tests/` invocation)
_REPO = Path(__file__).parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts.simulate_pipeline import (
    HarnessContext,
    run_global_asserts,
    run_scenario_1,
    run_scenario_2,
    run_scenario_3,
    run_scenario_4,
    run_scenario_5,
    run_scenario_6,
    run_scenario_7,
    setup_harness,
    teardown_harness,
)


# ---------------------------------------------------------------------------
# Module-scoped harness fixture (setup once, all tests share it)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def harness() -> HarnessContext:  # type: ignore[misc]
    """Create a HarnessContext valid for the whole test module."""
    ctx = setup_harness()
    yield ctx
    teardown_harness(ctx)


# ---------------------------------------------------------------------------
# Individual scenario tests
# ---------------------------------------------------------------------------


def test_scenario_1_happy_path(harness: HarnessContext) -> None:
    """N clips → all critic-clean → approved → gate_status ready."""
    run_scenario_1(harness, verbose=False)


def test_scenario_2_correctable(harness: HarnessContext) -> None:
    """Correctable failure → adjust_end re-render → approved; bounds moved."""
    run_scenario_2(harness, verbose=False)


def test_scenario_3_loop_bound(harness: HarnessContext) -> None:
    """Fail twice → loop bound hit → escalate_to_human → didnt_pass."""
    run_scenario_3(harness, verbose=False)


def test_scenario_4_safety_terminal(harness: HarnessContext) -> None:
    """Safety failure → judge rejected immediately, 0 corrections, 1 render."""
    run_scenario_4(harness, verbose=False)


def test_scenario_5_malformed_critic(harness: HarnessContext) -> None:
    """ValidationError from critic → caught by _run_clip_loop → clip escalated."""
    run_scenario_5(harness, verbose=False)


def test_scenario_6_stage_exception(harness: HarnessContext) -> None:
    """Stage exception → source.stage='failed', nothing stuck non-terminal."""
    run_scenario_6(harness, verbose=False)


def test_scenario_7_regression_segmentation(harness: HarnessContext) -> None:
    """4 boundary_failure_pairs → real guards → sentence-aligned, non-straddling."""
    run_scenario_7(harness, verbose=False)


# ---------------------------------------------------------------------------
# Global asserts (run after all scenario tests have populated the DB)
# ---------------------------------------------------------------------------


def test_global_asserts(harness: HarnessContext) -> None:
    """
    Cross-scenario assertions on the shared SQLite DB:
      - No clip stuck in gate_status='pending'
      - correction_attempts <= 2 for every clip
      - Every committed clip has judge_decision
      - Judge is deterministic (same inputs → same output)
      - RenderJob rows present in DB
    """
    ok, msg = run_global_asserts(verbose=False)
    assert ok, f"Global asserts failed:\n{msg}"


# ---------------------------------------------------------------------------
# Convenience: one test that asserts 100% pass rate end-to-end
# ---------------------------------------------------------------------------


def test_harness_100_percent_pass_rate() -> None:
    """
    Smoke-check: run all scenarios in one call on a fresh harness and assert
    100% pass rate.  This is the same check the Makefile simulate-pipeline
    target performs.

    Uses an independent harness (not the module-scoped fixture) because the
    individual scenario tests mark their sources as status='done'.  Re-running
    the same scenarios on the shared DB would trigger the source-exhaustion
    guard in run_video and produce false failures.  A fresh harness guarantees
    a clean slate identical to what `make simulate-pipeline` runs.
    """
    from scripts.simulate_pipeline import run_all_scenarios, setup_harness, teardown_harness

    ctx = setup_harness()
    try:
        results = run_all_scenarios(ctx, verbose=False)
        failures = [(label, err) for label, ok, err in results if not ok]
        assert not failures, (
            f"Simulation not at 100%: {len(failures)} failure(s):\n"
            + "\n".join(f"  [{label}] {err}" for label, err in failures)
        )
    finally:
        teardown_harness(ctx)
