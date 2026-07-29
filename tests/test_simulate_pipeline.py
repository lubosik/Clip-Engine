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
    _SRC_S1,
    _SRC_S2,
    _SRC_S3,
    _SRC_S4,
    _SRC_S5,
    _SRC_S6,
    _assert_clip_terminals,
    _assert_order,
    _assert_replay,
    _assert_vocab_clean,
    _collect_events,
    assert_event_sequence_s1,
    assert_event_sequence_s2,
    assert_event_sequence_s3,
    assert_event_sequence_s4,
    assert_event_sequence_s5,
    assert_event_sequence_s6,
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


# ===========================================================================
# EVENT-SEQUENCE TESTS (§5 PROGRESS_EVENTS_CONTRACTS.md)
#
# These tests run AFTER the scenario tests above (definition order) and query
# the events that those scenarios wrote to the shared harness DB.
# They reuse the helper functions from scripts.simulate_pipeline rather than
# re-running scenarios via subprocess.
# ===========================================================================


# ---------------------------------------------------------------------------
# Full event-sequence assertion functions (one per scenario)
# ---------------------------------------------------------------------------


class TestEventSequences:
    """Call the full per-scenario assert functions on the shared harness DB."""

    def test_event_sequence_s1_happy_path(self, harness: HarnessContext) -> None:
        """S1: §2 stage order, all clips terminal, replay, no unknown stages."""
        assert_event_sequence_s1(harness)

    def test_event_sequence_s2_correction(self, harness: HarnessContext) -> None:
        """S2: correction event present with reason + fix detail, re-render, ready."""
        assert_event_sequence_s2(harness)

    def test_event_sequence_s3_loop_bound(self, harness: HarnessContext) -> None:
        """S3: ≥2 correction events, terminal = didnt_pass."""
        assert_event_sequence_s3(harness)

    def test_event_sequence_s4_safety_terminal(self, harness: HarnessContext) -> None:
        """S4: no correction events, terminal = didnt_pass."""
        assert_event_sequence_s4(harness)

    def test_event_sequence_s5_malformed_critic(self, harness: HarnessContext) -> None:
        """S5: ValidationError → escalated → didnt_pass terminal."""
        assert_event_sequence_s5(harness)

    def test_event_sequence_s6_stage_exception(self, harness: HarnessContext) -> None:
        """S6: failed status on identifying, no clip events."""
        assert_event_sequence_s6(harness)


# ---------------------------------------------------------------------------
# Stage vocabulary tests — every emitted stage must be in the §2 vocabulary
# ---------------------------------------------------------------------------


class TestEventVocabulary:
    """No stage outside the §2 vocabulary must ever appear."""

    def test_vocab_s1(self, harness: HarnessContext) -> None:
        _assert_vocab_clean(_collect_events(_SRC_S1), "vocab/S1")

    def test_vocab_s2(self, harness: HarnessContext) -> None:
        _assert_vocab_clean(_collect_events(_SRC_S2), "vocab/S2")

    def test_vocab_s3(self, harness: HarnessContext) -> None:
        _assert_vocab_clean(_collect_events(_SRC_S3), "vocab/S3")

    def test_vocab_s4(self, harness: HarnessContext) -> None:
        _assert_vocab_clean(_collect_events(_SRC_S4), "vocab/S4")

    def test_vocab_s5(self, harness: HarnessContext) -> None:
        _assert_vocab_clean(_collect_events(_SRC_S5), "vocab/S5")

    def test_vocab_s6(self, harness: HarnessContext) -> None:
        _assert_vocab_clean(_collect_events(_SRC_S6), "vocab/S6")


# ---------------------------------------------------------------------------
# Terminal-event tests — every clip must reach ready or didnt_pass
# ---------------------------------------------------------------------------


class TestClipTerminalEvents:
    """Every clip_id that appears in events must reach a terminal event."""

    def test_all_clips_terminal_s1(self, harness: HarnessContext) -> None:
        _assert_clip_terminals(_collect_events(_SRC_S1), "terminals/S1")

    def test_all_clips_terminal_s2(self, harness: HarnessContext) -> None:
        _assert_clip_terminals(_collect_events(_SRC_S2), "terminals/S2")

    def test_all_clips_terminal_s3_loop_bound(self, harness: HarnessContext) -> None:
        _assert_clip_terminals(_collect_events(_SRC_S3), "terminals/S3")

    def test_all_clips_terminal_s4_safety(self, harness: HarnessContext) -> None:
        _assert_clip_terminals(_collect_events(_SRC_S4), "terminals/S4")

    def test_all_clips_terminal_s5_malformed(self, harness: HarnessContext) -> None:
        _assert_clip_terminals(_collect_events(_SRC_S5), "terminals/S5")


# ---------------------------------------------------------------------------
# Happy-path ordering tests (S1) — detailed stage-order assertions
# ---------------------------------------------------------------------------


class TestHappyPathStageOrder:
    """Happy path (S1) must emit stages in the §2 pipeline order."""

    def test_s1_queued_before_transcribing(self, harness: HarnessContext) -> None:
        _assert_order(_collect_events(_SRC_S1), "queued", "transcribing", "order/S1")

    def test_s1_transcribing_before_downloading(self, harness: HarnessContext) -> None:
        _assert_order(_collect_events(_SRC_S1), "transcribing", "downloading", "order/S1")

    def test_s1_downloading_before_rendering(self, harness: HarnessContext) -> None:
        _assert_order(_collect_events(_SRC_S1), "downloading", "rendering", "order/S1")

    def test_s1_rendering_before_reviewing(self, harness: HarnessContext) -> None:
        _assert_order(_collect_events(_SRC_S1), "rendering", "reviewing", "order/S1")

    def test_s1_reviewing_before_judging(self, harness: HarnessContext) -> None:
        _assert_order(_collect_events(_SRC_S1), "reviewing", "judging", "order/S1")

    def test_s1_judging_before_ready(self, harness: HarnessContext) -> None:
        _assert_order(_collect_events(_SRC_S1), "judging", "ready", "order/S1")

    def test_s1_ready_before_complete(self, harness: HarnessContext) -> None:
        _assert_order(_collect_events(_SRC_S1), "ready", "complete", "order/S1")

    def test_s1_first_event_is_queued(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S1)
        assert events, "S1: no events"
        assert events[0]["stage"] == "queued", (
            f"S1: first stage={events[0]['stage']!r}, expected 'queued'"
        )

    def test_s1_last_event_is_complete(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S1)
        assert events, "S1: no events"
        assert events[-1]["stage"] == "complete", (
            f"S1: last stage={events[-1]['stage']!r}, expected 'complete'"
        )


# ---------------------------------------------------------------------------
# Correction-path event tests
# ---------------------------------------------------------------------------


class TestCorrectionEvents:
    """Correction-specific event properties for S2 and S3."""

    def test_s2_correction_event_has_reason(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S2)
        corr = [e for e in events if e["stage"] == "correction" and e["status"] == "running"]
        assert corr, "S2: no correction:running events"
        assert any(e["reason"] is not None for e in corr), (
            "S2: correction event missing reason field"
        )

    def test_s2_correction_detail_has_fix_attempt(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S2)
        corr = [e for e in events if e["stage"] == "correction"]
        assert corr, "S2: no correction events"
        assert any("fix" in (e["detail"] or "").lower() for e in corr), (
            "S2: no correction event detail contains 'fix'"
        )

    def test_s2_rendering_follows_correction(self, harness: HarnessContext) -> None:
        """A rendering event must appear after every correction:running event."""
        events = _collect_events(_SRC_S2)
        corr_indices = [
            i for i, e in enumerate(events)
            if e["stage"] == "correction" and e["status"] == "running"
        ]
        assert corr_indices, "S2: no correction:running events"
        for ci in corr_indices:
            later_rendering = [e for e in events[ci:] if e["stage"] == "rendering"]
            assert later_rendering, (
                f"S2: no rendering event after correction at index {ci}"
            )

    def test_s2_rendering_done_for_rerender(self, harness: HarnessContext) -> None:
        """§2 fix: rendering:done must be emitted for correction re-renders too."""
        events = _collect_events(_SRC_S2)
        r_done = [e for e in events if e["stage"] == "rendering" and e["status"] == "done"]
        assert len(r_done) >= 2, (
            f"S2: expected ≥2 rendering:done events (initial + re-render), got {len(r_done)}"
        )

    def test_s3_multiple_corrections(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S3)
        corr_running = [
            e for e in events if e["stage"] == "correction" and e["status"] == "running"
        ]
        assert len(corr_running) >= 2, (
            f"S3: expected ≥2 correction:running (loop bound = 2 corrections), "
            f"got {len(corr_running)}"
        )


# ---------------------------------------------------------------------------
# Failure and safety event tests
# ---------------------------------------------------------------------------


class TestFailureEvents:
    """Events specific to failure and safety-terminal paths."""

    def test_s4_no_correction_events(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S4)
        corr = [e for e in events if e["stage"] == "correction"]
        assert not corr, (
            f"S4: correction events found in safety-terminal path: {corr}"
        )

    def test_s4_terminal_is_didnt_pass(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S4)
        terminal = [e for e in events if e["stage"] in {"ready", "didnt_pass"}]
        assert any(e["stage"] == "didnt_pass" for e in terminal), (
            "S4: expected didnt_pass terminal for safety failure"
        )

    def test_s6_failed_status_emitted(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S6)
        failed = [e for e in events if e["status"] == "failed"]
        assert failed, "S6: no failed-status events emitted"

    def test_s6_identifying_stage_failed(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S6)
        failed = [e for e in events if e["status"] == "failed"]
        assert any(e["stage"] == "identifying" for e in failed), (
            f"S6: failed event not on 'identifying' stage; "
            f"failed stages={[e['stage'] for e in failed]}"
        )

    def test_s6_no_clip_events_after_stage_failure(self, harness: HarnessContext) -> None:
        events = _collect_events(_SRC_S6)
        clip_evs = [e for e in events if e["clip_id"] is not None]
        assert not clip_evs, (
            "S6: clip events found after stage exception before rendering"
        )


# ---------------------------------------------------------------------------
# Last-Event-ID replay tests (offline DB query)
# ---------------------------------------------------------------------------


class TestLastEventIDReplay:
    """Verify SSE Last-Event-ID replay logic using the sim DB."""

    def test_replay_s1_from_midpoint(self, harness: HarnessContext) -> None:
        """Query id > mid-run id returns exactly the second-half events."""
        _assert_replay(_SRC_S1, "replay/S1/mid")

    def test_replay_s2_from_midpoint(self, harness: HarnessContext) -> None:
        _assert_replay(_SRC_S2, "replay/S2/mid")

    def test_replay_s1_from_last_id_returns_empty(self, harness: HarnessContext) -> None:
        """Query id > last_id returns empty list."""
        from core.db import get_session
        from core.models import PipelineEvent

        with get_session() as session:
            rows = (
                session.query(PipelineEvent)
                .filter_by(source_id=_SRC_S1)
                .order_by(PipelineEvent.id.desc())
                .all()
            )
        assert rows, "S1: no events for replay test"
        last_id = rows[0].id

        with get_session() as session:
            after = (
                session.query(PipelineEvent)
                .filter(
                    PipelineEvent.source_id == _SRC_S1,
                    PipelineEvent.id > last_id,
                )
                .all()
            )
        assert after == [], (
            f"replay/S1/last: expected empty list after id={last_id}, got {len(after)} rows"
        )

    def test_replay_from_first_id_returns_all_but_first(self, harness: HarnessContext) -> None:
        """Query id > first_id returns all events except the first."""
        from core.db import get_session
        from core.models import PipelineEvent

        with get_session() as session:
            rows = (
                session.query(PipelineEvent)
                .filter_by(source_id=_SRC_S1)
                .order_by(PipelineEvent.id)
                .all()
            )
        assert len(rows) >= 2, "S1: need at least 2 events for this test"
        first_id = rows[0].id

        with get_session() as session:
            after = (
                session.query(PipelineEvent)
                .filter(
                    PipelineEvent.source_id == _SRC_S1,
                    PipelineEvent.id > first_id,
                )
                .order_by(PipelineEvent.id)
                .all()
            )
        assert [r.id for r in after] == [r.id for r in rows[1:]], (
            "replay/S1/first: expected all events except first"
        )

    def test_replay_cross_source_isolation(self, harness: HarnessContext) -> None:
        """Replay query on S1 must not return S2 events (source_id filter)."""
        from core.db import get_session
        from core.models import PipelineEvent

        with get_session() as session:
            s1_rows = (
                session.query(PipelineEvent)
                .filter_by(source_id=_SRC_S1)
                .order_by(PipelineEvent.id)
                .all()
            )
        assert s1_rows, "S1: no events"
        first_s1_id = s1_rows[0].id

        # Replay from before first S1 event — must only return S1 rows
        with get_session() as session:
            replayed = (
                session.query(PipelineEvent)
                .filter(
                    PipelineEvent.source_id == _SRC_S1,
                    PipelineEvent.id > first_s1_id - 1,
                )
                .all()
            )
        source_ids = {r.source_id for r in replayed}
        assert source_ids == {_SRC_S1}, (
            f"replay/cross-source: expected only {_SRC_S1!r}, got {source_ids}"
        )


# ---------------------------------------------------------------------------
# Migration-008 table + general coverage tests
# ---------------------------------------------------------------------------


class TestMigration008AndCoverage:
    """pipeline_events table exists; all scenarios wrote events to the DB."""

    def test_pipeline_events_table_in_harness_db(self, harness: HarnessContext) -> None:
        """Base.metadata.create_all must create the pipeline_events table."""
        from core.db import get_session
        from core.models import PipelineEvent

        with get_session() as session:
            count = session.query(PipelineEvent).count()
        assert count > 0, (
            "pipeline_events table empty — migration-008 table not created by create_all"
        )

    def test_all_scenarios_wrote_events_to_db(self, harness: HarnessContext) -> None:
        """Every run-video scenario (S1–S5) wrote at least one event."""
        for src_id, label in [
            (_SRC_S1, "S1"), (_SRC_S2, "S2"), (_SRC_S3, "S3"),
            (_SRC_S4, "S4"), (_SRC_S5, "S5"),
        ]:
            events = _collect_events(src_id)
            assert events, f"{label}: no pipeline_events rows written to sim DB"

    def test_s6_also_wrote_events_despite_failure(self, harness: HarnessContext) -> None:
        """S6 fails before rendering but must still write at least one event (identifying)."""
        events = _collect_events(_SRC_S6)
        assert events, "S6: no events even though identifying stage was reached"

    def test_judging_emitted_in_s1(self, harness: HarnessContext) -> None:
        """§2 fix: judging stage must be emitted before terminal events in S1."""
        events = _collect_events(_SRC_S1)
        assert any(e["stage"] == "judging" for e in events), (
            "S1: judging stage not emitted (§2 bug: judging → terminal per clip)"
        )

    def test_judging_emitted_in_s4_safety(self, harness: HarnessContext) -> None:
        """judging must be emitted even for safety-terminal clips."""
        events = _collect_events(_SRC_S4)
        assert any(e["stage"] == "judging" for e in events), (
            "S4: judging stage not emitted for safety-terminal clip"
        )

    def test_s1_identified_events_have_progress_fields(self, harness: HarnessContext) -> None:
        """identified:running events must carry progress n/total."""
        events = _collect_events(_SRC_S1)
        id_running = [
            e for e in events if e["stage"] == "identified" and e["status"] == "running"
        ]
        assert id_running, "S1: no identified:running events"
        assert all(e["n"] is not None and e["total"] is not None for e in id_running), (
            "S1: identified:running events missing n/total progress fields"
        )

    def test_s1_events_have_source_id(self, harness: HarnessContext) -> None:
        """Every event must have the correct source_id."""
        events = _collect_events(_SRC_S1)
        assert events, "S1: no events"
        bad = [e for e in events if e["source_id"] != _SRC_S1]
        assert not bad, (
            f"S1: events with wrong source_id: {[(e['id'], e['source_id']) for e in bad]}"
        )

    def test_s2_correction_event_has_clip_id(self, harness: HarnessContext) -> None:
        """correction events must have clip_id set (per-clip event)."""
        events = _collect_events(_SRC_S2)
        corr = [e for e in events if e["stage"] == "correction"]
        assert corr, "S2: no correction events"
        assert all(e["clip_id"] is not None for e in corr), (
            "S2: correction events missing clip_id"
        )

    def test_reviewing_before_terminal_s1(self, harness: HarnessContext) -> None:
        """reviewing must appear before the first ready event."""
        _assert_order(_collect_events(_SRC_S1), "reviewing", "ready", "coverage/S1")
