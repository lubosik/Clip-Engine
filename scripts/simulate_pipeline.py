"""
scripts/simulate_pipeline.py — Offline pipeline simulation harness.

Implements docs/ADD_VIDEO_CONTRACTS.md §8 EXACTLY.

FULLY OFFLINE: zero network, zero LLM, zero GPU.

Tripwires installed in setup_harness():
  - core.llm.create_completion  → raises AssertionError on any real call
  - core.apify.Apify            → _FakeApify; real methods raise AssertionError
  - modal                       → blocked in sys.modules

Scenarios (all must pass for exit 0):
  1. Happy path: N clips → all critic-clean → approved → gate_status ready
  2. Correctable failure: adjust_end(-1) → re-render → critic passes → approved
  3. Fail twice → loop-bound hit → judge escalate_to_human → didnt_pass
  4. Safety terminal: unrelaxed safety failure → judge rejected immediately
  5. Malformed critic output (ValidationError) → schema catch → clip escalated
  6. Stage exception mid-pipeline → source failed, nothing stuck non-terminal
  7. Regression: 4 boundary_failure_pairs cases through REAL deterministic guards

Global asserts (run after scenarios 1–6, logged per scenario):
  - Every clip reaches a terminal gate_status (never 'pending')
  - correction_attempts <= 2 for every clip
  - judge_decision present on every committed clip
  - judge determinism: same inputs → same output (verified with fixture data)
  - RenderJob row recorded for every simulated render

Usage:
    .venv/bin/python scripts/simulate_pipeline.py [--verbose]
    make simulate-pipeline

Exit 0 only when all scenarios pass at 100%.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Repo root on sys.path (must come before any local imports)
# ---------------------------------------------------------------------------

_REPO = Path(__file__).parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# ---------------------------------------------------------------------------
# Module-level imports that do NOT touch the DB or network
# ---------------------------------------------------------------------------

import producer.video_pipeline as vp
import producer.judge as _pjdg
from producer.pipeline_contracts import (
    CriticReport,
    CriticFailure,
    JudgeDecision,
    Correction,
)
from producer.judge import judge as real_judge, SAFETY_SET

log = logging.getLogger("simulate_pipeline")

# ---------------------------------------------------------------------------
# Fixtures path and eval_segmentation module (for scenario 7)
# ---------------------------------------------------------------------------

_FIXTURES_DIR = _REPO / "tests" / "fixtures" / "segmentation"
_PAIRS_FILE = _FIXTURES_DIR / "boundary_failure_pairs.json"

import scripts.eval_segmentation as _evalseg  # noqa: E402

# ---------------------------------------------------------------------------
# Harness context
# ---------------------------------------------------------------------------


@dataclass
class HarnessContext:
    tmpdir: Path
    db_path: Path
    workdir: Path
    tiny_mp4: Path
    orig_env: dict = field(default_factory=dict)
    render_log: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Setup / teardown
# ---------------------------------------------------------------------------


def setup_harness() -> HarnessContext:
    """
    Create temp dir + SQLite DB + tiny mp4.
    Install tripwires on LLM, Apify, modal.
    Must be called exactly once before any scenario.
    """
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="clip_sim_"))
    db_path = tmpdir / "sim.db"
    workdir = tmpdir / "work"
    workdir.mkdir()
    tiny_mp4 = tmpdir / "tiny.mp4"

    ctx = HarnessContext(
        tmpdir=tmpdir, db_path=db_path, workdir=workdir, tiny_mp4=tiny_mp4
    )

    # ── Save env vars for teardown ─────────────────────────────────────────
    for key in ("DATABASE_URL", "STORAGE_DIR", "LLM_API_KEY", "LLM_MODEL",
                "APIFY_TOKEN", "WEB_ADMIN_PASSWORD"):
        ctx.orig_env[key] = os.environ.get(key)

    # ── Inject simulation env vars ─────────────────────────────────────────
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["STORAGE_DIR"] = str(tmpdir)
    os.environ.setdefault("LLM_API_KEY", "sim-not-real-key")
    os.environ.setdefault("LLM_MODEL", "sim-model")
    os.environ.setdefault("APIFY_TOKEN", "sim-not-real-token")
    os.environ.setdefault("WEB_ADMIN_PASSWORD", "sim-test")

    # Clear settings LRU cache so it re-reads the env
    try:
        from core.settings import get_settings
        get_settings.cache_clear()
    except Exception:
        pass

    # ── Block modal in sys.modules ─────────────────────────────────────────
    _modal_stub = types.ModuleType("modal")

    def _modal_tripwire(*a: Any, **kw: Any) -> None:
        raise AssertionError(
            "TRIPWIRE: real modal call attempted in simulation — "
            "all GPU dispatch must be faked"
        )

    _modal_stub.__getattr__ = lambda name: _modal_tripwire  # type: ignore[method-assign]
    sys.modules.setdefault("modal", _modal_stub)

    # ── LLM tripwire ──────────────────────────────────────────────────────
    import core.llm as _cllm
    _cllm._sim_original_create_completion = getattr(  # type: ignore[attr-defined]
        _cllm, "create_completion", None
    )

    def _llm_tripwire(*a: Any, **kw: Any) -> None:
        raise AssertionError(
            "TRIPWIRE: real LLM core.llm.create_completion called in simulation — "
            "all LLM responses must be scripted"
        )

    _cllm.create_completion = _llm_tripwire  # type: ignore[attr-defined]

    # ── Apify tripwire ────────────────────────────────────────────────────
    import core.apify as _capify
    _capify._sim_OrigApify = _capify.Apify  # type: ignore[attr-defined]

    class _FakeApify:
        total_cost_usd: float = 0.0

        def run(self, *a: Any, **kw: Any) -> None:
            raise AssertionError(
                "TRIPWIRE: real Apify.run() called in simulation — "
                "transcript fetch must be faked"
            )

        def fetch_items(self, *a: Any, **kw: Any) -> None:
            raise AssertionError("TRIPWIRE: Apify.fetch_items() in simulation")

    _capify.Apify = _FakeApify  # type: ignore[attr-defined]

    # ── SQLite DB init ────────────────────────────────────────────────────
    import core.db as _cdb
    _cdb._engine = None  # type: ignore[attr-defined]
    _cdb._SessionLocal = None  # type: ignore[attr-defined]

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from core.models import Base

    _sim_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(_sim_engine)
    _cdb._engine = _sim_engine  # type: ignore[attr-defined]
    _cdb._SessionLocal = sessionmaker(  # type: ignore[attr-defined]
        bind=_sim_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    # ── Generate tiny ffmpeg testsrc mp4 (once) ───────────────────────────
    _generate_tiny_mp4(tiny_mp4)

    return ctx


def teardown_harness(ctx: HarnessContext) -> None:
    """Restore env, tripwires, DB engine. Remove temp dir."""
    # Restore env
    for key, val in ctx.orig_env.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val

    # Restore LLM
    try:
        import core.llm as _cllm
        if hasattr(_cllm, "_sim_original_create_completion") and _cllm._sim_original_create_completion:
            _cllm.create_completion = _cllm._sim_original_create_completion
    except Exception:
        pass

    # Restore Apify
    try:
        import core.apify as _capify
        if hasattr(_capify, "_sim_OrigApify"):
            _capify.Apify = _capify._sim_OrigApify
    except Exception:
        pass

    # Reset DB engine so later code re-connects via real DATABASE_URL
    try:
        import core.db as _cdb
        _cdb._engine = None
        _cdb._SessionLocal = None
    except Exception:
        pass

    # Clear settings cache so it picks up restored env
    try:
        from core.settings import get_settings
        get_settings.cache_clear()
    except Exception:
        pass

    # Remove temp dir
    shutil.rmtree(ctx.tmpdir, ignore_errors=True)


def _generate_tiny_mp4(out_path: Path) -> None:
    """Generate a 3-second 1080x1920 test-pattern mp4 with ffmpeg (local, no network)."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi",
        "-i", "testsrc=size=1080x1920:rate=24:duration=3",
        "-vcodec", "libx264",
        "-preset", "ultrafast",
        "-crf", "40",
        "-t", "3",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg testsrc generation failed (exit {result.returncode}): "
            f"{result.stderr.decode()[:400]}"
        )


# ---------------------------------------------------------------------------
# Fake data builders
# ---------------------------------------------------------------------------

_N_SENTENCES = 20  # total fake sentence count


def _make_fake_segments(n: int = _N_SENTENCES) -> list[dict]:
    """Generate n fake transcript segments (5 s each)."""
    return [
        {
            "start": float(i * 5),
            "end": float(i * 5 + 5),
            "text": f"Sentence {i}: compelling fitness insight here.",
        }
        for i in range(n)
    ]


def _make_fake_sentences(n: int = _N_SENTENCES) -> list[dict]:
    """Generate n fake sentence spans (5 s each, sentence-boundary-aligned)."""
    return [
        {
            "text": f"Sentence {i}: compelling fitness insight here.",
            "start": float(i * 5),
            "end": float(i * 5 + 5),
        }
        for i in range(n)
    ]


def _make_fake_candidates(n: int = 2) -> list[dict]:
    """N sentence-aligned candidates (25 s each, well within 10–90 s range)."""
    return [
        {
            "start": float(i * 30),       # e.g. 0.0, 30.0
            "end": float(i * 30 + 25),    # e.g. 25.0, 55.0
            "score": 0.85,
            "hook": f"Hook {i}: you need to know this about training",
            "reason": "Strong standalone moment",
            "attempt": 0,
        }
        for i in range(n)
    ]


def _make_sim_cfg(name: str = "simcampaign") -> Any:
    """Minimal CampaignConfig-shaped SimpleNamespace."""
    return SimpleNamespace(
        name=name,
        enabled=True,
        ranking=SimpleNamespace(
            ranking_rules="Prefer actionable, standalone moments.",
            clip_length=(10.0, 90.0),
            max_clips_per_source=5,
            stance="",
        ),
        gate=SimpleNamespace(relaxed_safety_checks=[]),
        destinations=SimpleNamespace(
            caption_template="{hook}",
            hashtags=[],
            postiz_channels=[],
        ),
        model_dump=lambda mode=None: {"name": name, "enabled": True},
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _db_setup_source(
    source_id: str,
    segments: list[dict],
    sentences: list[dict],
) -> None:
    """Pre-populate DB: Campaign + Source + Transcript rows for one scenario."""
    from core.db import get_session
    from core.models import Campaign, Source, Transcript

    with get_session() as session:
        if not session.query(Campaign).filter_by(name="simcampaign").first():
            session.add(Campaign(name="simcampaign", enabled=True))
        if not session.query(Source).filter_by(source_id=source_id).first():
            session.add(Source(
                source_id=source_id,
                campaign="simcampaign",
                platform="youtube",
                url=f"https://www.youtube.com/watch?v={source_id.split(':')[1]}",
                status="pending",
                stage="queued",
            ))
        if not session.query(Transcript).filter_by(source_id=source_id).first():
            session.add(Transcript(
                source_id=source_id,
                segments=segments,
                sentences=sentences,
            ))
        session.commit()


def _db_get_clips(source_id: str) -> list[Any]:
    """Return all committed Clip rows for a source_id."""
    from core.db import get_session
    from core.models import Clip

    with get_session() as session:
        return session.query(Clip).filter_by(source_id=source_id).all()


def _db_get_source(source_id: str) -> Any:
    """Return the Source row (or None)."""
    from core.db import get_session
    from core.models import Source

    with get_session() as session:
        return session.query(Source).filter_by(source_id=source_id).first()


def _db_count_render_jobs_since(n_before: int) -> int:
    """Return count of RenderJob rows inserted since n_before total rows."""
    from core.db import get_session
    from core.models import RenderJob

    with get_session() as session:
        total = session.query(RenderJob).count()
        return total - n_before


def _db_total_render_jobs() -> int:
    from core.db import get_session
    from core.models import RenderJob

    with get_session() as session:
        return session.query(RenderJob).count()


# ---------------------------------------------------------------------------
# Fake render factory
# ---------------------------------------------------------------------------


def _make_fake_render(ctx: HarnessContext, render_log: list[dict]) -> Callable:
    """Return a fake _pipeline_render_and_record that:
    - copies the tiny mp4 into workdir
    - inserts a RenderJob row (for the global spend-ledger assert)
    - returns a dispatch-like SimpleNamespace
    """

    def _fake_render(
        cfg: Any,
        source_meta: dict,
        clip_candidate: dict,
        source_video: Path,
        workdir: Path,
        *,
        campaign_name: str,
        campaign_mode: str,
        session: Any,
    ) -> Any:
        from core.models import RenderJob

        attempt = clip_candidate.get("attempt", 0)
        call_n = len(render_log)
        out_name = f"clip_{call_n}_r{attempt}.mp4"
        out_path = ctx.workdir / out_name
        shutil.copy(ctx.tiny_mp4, out_path)

        rj = RenderJob(
            clip_id=None,
            campaign=campaign_name,
            backend="local",
            gpu=None,
            duration_s=3.0,
            rate_per_s=0.0,
            cost_estimate=0.0,
            status="ok",
        )
        session.add(rj)
        session.flush()

        render_log.append({
            "call_n": call_n,
            "campaign": campaign_name,
            "start": clip_candidate.get("start"),
            "end": clip_candidate.get("end"),
            "attempt": attempt,
            "out_path": str(out_path),
        })

        return SimpleNamespace(
            file_path=str(out_path),
            thumb_path=str(out_path),
            status="ok",
            error=None,
            backend="local",
            gpu=None,
        )

    return _fake_render


# ---------------------------------------------------------------------------
# Patch context manager
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patches(patch_dict: dict[tuple[Any, str], Any]):  # type: ignore[type-arg]
    """Temporarily apply module-attribute patches, restoring originals on exit."""
    originals: dict[tuple[Any, str], Any] = {}
    for (obj, attr), val in patch_dict.items():
        originals[(obj, attr)] = getattr(obj, attr)
        setattr(obj, attr, val)
    try:
        yield
    finally:
        for (obj, attr), orig in originals.items():
            setattr(obj, attr, orig)


# ---------------------------------------------------------------------------
# Common pipeline patch dict builder
# ---------------------------------------------------------------------------


def _common_patches(
    ctx: HarnessContext,
    render_log: list[dict],
    segments: list[dict],
    sentences: list[dict],
    candidates: list[dict],
    critic_fn: Callable,
    rank_fn: Callable | None = None,
) -> dict:
    """Build the full patch dict for a run_video call."""
    import core.config as _cc
    import core.topics as _ct
    import core.storage as _cs
    import core.punctuate as _cpu
    import core.apify as _capify
    import producer.dedupe as _pd
    import producer.download as _pdl
    import producer.render_dispatch as _prd
    import producer.run as _pr

    fake_render = _make_fake_render(ctx, render_log)
    sim_cfg = _make_sim_cfg()

    patches: dict[tuple[Any, str], Any] = {
        # video_pipeline module-level seams
        (vp, "_pipeline_ensure_campaign"): lambda *a, **kw: None,
        (vp, "_pipeline_upsert_source"): lambda *a, **kw: None,
        (vp, "_pipeline_probe_youtube"): lambda url: None,
        (vp, "_pipeline_fetch_transcript"): lambda *a, **kw: segments,
        (vp, "_pipeline_download_source"): lambda *a, **kw: str(ctx.tiny_mp4),
        (vp, "_pipeline_render_and_record"): fake_render,
        (vp, "_pipeline_run_critic"): critic_fn,
        (vp, "_pipeline_rank_moments"): (
            rank_fn if rank_fn is not None else (lambda *a, **kw: candidates)
        ),
        # Lazy imports inside run_video
        (_cc, "load_campaign"): lambda path, strict_assets=True: sim_cfg,
        (_capify, "Apify"): lambda: SimpleNamespace(total_cost_usd=0.0),
        (_cs, "work_dir"): lambda s: ctx.workdir,
        (_cpu, "restore_sentences"): lambda segs: sentences,
        (_pd, "mark_source_status"): lambda session, sid, status: None,
        (_pd, "update_used_ranges"): lambda session, sid, ranges: None,
        (_pd, "upsert_source"): lambda session, candidate, campaign: None,
        (_pdl, "cleanup_source"): lambda s: None,
        (_prd, "estimate_modal_batch_cost"): lambda n, s: 0.10,
        (_prd, "month_to_date_modal_spend"): lambda s: 0.0,
        # Deterministic guard pass-throughs (scenario 7 uses real guards separately)
        (_ct, "detect_unit_boundaries"): lambda spans: [],
        (_ct, "build_units_from_boundaries"): lambda spans, bounds: [],
        (_ct, "clip_within_unit"): lambda c, units, spans, **kw: c,
    }

    # boundary_check pass-throughs
    try:
        import producer.boundary_check as _pbc
        patches[(_pbc, "apply_prefilters")] = lambda c, spans, clip_len: c
        patches[(_pbc, "verify_boundaries")] = lambda c, spans, **kw: (c, True)
    except Exception:
        pass

    # hook_style pass-through
    try:
        import core.hook_style as _chs
        patches[(_chs, "enforce_hook_style")] = lambda h: h
    except Exception:
        pass

    # producer.run: set_source_stage and _build_caption are real (work with SQLite)
    # We do NOT patch set_source_stage — it talks to our SQLite DB correctly.

    return patches


# ---------------------------------------------------------------------------
# Judge call counter (for "judge called exactly once per clip" assert)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _counting_judge():
    """Patch producer.judge.judge with a counting wrapper; yield call list."""
    calls: list[dict] = []

    def _counted(report: CriticReport, attempts_used: int, max_corrections: int = 2) -> JudgeDecision:
        decision = real_judge(report, attempts_used, max_corrections)
        calls.append({
            "clip_id": report.clip_id,
            "attempts_used": attempts_used,
            "decision": decision.decision,
        })
        return decision

    with _patches({(_pjdg, "judge"): _counted}):
        yield calls


# ---------------------------------------------------------------------------
# Scenario 1: Happy path
# ---------------------------------------------------------------------------

_SRC_S1 = "youtube:SIM001AAAAB"


def run_scenario_1(ctx: HarnessContext, verbose: bool = False) -> None:
    """
    Happy path: 2 clips, critic always clean on attempt 0.
    Expected: both clips gate_status='ready', judge=approved.
    """
    segments = _make_fake_segments()
    sentences = _make_fake_sentences()
    candidates = _make_fake_candidates(2)
    _db_setup_source(_SRC_S1, segments, sentences)

    render_log: list[dict] = []

    def _always_pass(clip_row: Any, *a: Any, **kw: Any) -> CriticReport:
        return CriticReport(
            clip_id=clip_row.id,
            attempt=getattr(clip_row, "correction_attempts", 0),
            failures=[],
            formula_score=0.92,
            passed=True,
        )

    rj_before = _db_total_render_jobs()

    with _counting_judge() as judge_calls, \
         _patches(_common_patches(ctx, render_log, segments, sentences, candidates, _always_pass)):

        url = f"https://www.youtube.com/watch?v={_SRC_S1.split(':')[1]}"
        result = vp.run_video("simcampaign", url, run_mode="demo",
                              max_apify_spend=2.0, max_modal_spend=3.0)

    clips = _db_get_clips(_SRC_S1)

    assert result.status in ("complete", "partial"), (
        f"S1: run_video returned status={result.status!r}, error={result.error!r}"
    )
    assert len(clips) == 2, f"S1: expected 2 clips, got {len(clips)}"

    for c in clips:
        assert c.gate_status == "ready", (
            f"S1: clip {c.id} gate_status={c.gate_status!r}, expected 'ready'"
        )
        assert c.correction_attempts == 0, (
            f"S1: clip {c.id} correction_attempts={c.correction_attempts}, expected 0"
        )
        assert c.judge_decision is not None, f"S1: clip {c.id} missing judge_decision"
        assert c.judge_decision["decision"] == "approved", (
            f"S1: clip {c.id} judge_decision={c.judge_decision['decision']!r}"
        )

    # Judge called exactly once per clip
    assert len(judge_calls) == 2, f"S1: judge called {len(judge_calls)} times, expected 2"
    for jc in judge_calls:
        assert jc["decision"] == "approved", f"S1: judge call decision={jc['decision']!r}"

    # RenderJob rows inserted
    rj_after = _db_total_render_jobs()
    assert rj_after - rj_before == 2, (
        f"S1: expected 2 new RenderJob rows, got {rj_after - rj_before}"
    )
    assert len(render_log) == 2, f"S1: render_log has {len(render_log)} entries"

    if verbose:
        print(f"  S1: 2 clips approved, 2 renders, {len(judge_calls)} judge calls")


# ---------------------------------------------------------------------------
# Scenario 2: Correctable failure → re-render → approved
# ---------------------------------------------------------------------------

_SRC_S2 = "youtube:SIM002AAAAB"


def run_scenario_2(ctx: HarnessContext, verbose: bool = False) -> None:
    """
    1 clip. Attempt 0: critic fails self_contained (adjust_end, delta=-1).
    Attempt 1: critic passes.
    Assert: correction_attempts==1, end moved earlier, 2 renders.
    """
    sentences = _make_fake_sentences()
    segments = _make_fake_segments()
    # Candidate: starts at sentence 2 (10.0s), ends at sentence 6 (30.0s)
    # After adjust_end(-1): should end at sentence 5 (25.0s)
    candidates = [{
        "start": 10.0,
        "end": 30.0,
        "score": 0.80,
        "hook": "Training damages muscle — that is the point",
        "reason": "Strong correctable",
        "attempt": 0,
    }]
    _db_setup_source(_SRC_S2, segments, sentences)

    render_log: list[dict] = []
    call_count = [0]

    def _scripted_critic(clip_row: Any, *a: Any, **kw: Any) -> CriticReport:
        i = call_count[0]
        call_count[0] += 1
        clip_id = clip_row.id
        attempt = getattr(clip_row, "correction_attempts", 0)

        if i == 0:
            # First render: self_contained failure, correctable
            return CriticReport(
                clip_id=clip_id,
                attempt=attempt,
                failures=[
                    CriticFailure(
                        phase="2",
                        check="self_contained",
                        reason="Clip bleeds into new topic at the end",
                        severity="correctable",
                        correction=Correction(
                            kind="adjust_end",
                            delta_sentences=-1,
                            note="Trim end by 1 sentence",
                        ),
                    )
                ],
                formula_score=0.55,
                passed=False,
            )
        else:
            # Second render: passes
            return CriticReport(
                clip_id=clip_id,
                attempt=attempt,
                failures=[],
                formula_score=0.88,
                passed=True,
            )

    rj_before = _db_total_render_jobs()

    with _counting_judge() as judge_calls, \
         _patches(_common_patches(ctx, render_log, segments, sentences, candidates, _scripted_critic)):

        url = f"https://www.youtube.com/watch?v={_SRC_S2.split(':')[1]}"
        result = vp.run_video("simcampaign", url, run_mode="demo",
                              max_apify_spend=2.0, max_modal_spend=3.0)

    clips = _db_get_clips(_SRC_S2)

    assert len(clips) == 1, f"S2: expected 1 clip, got {len(clips)}"
    c = clips[0]

    assert c.correction_attempts == 1, (
        f"S2: correction_attempts={c.correction_attempts}, expected 1"
    )
    assert c.gate_status == "ready", (
        f"S2: gate_status={c.gate_status!r}, expected 'ready'"
    )
    assert c.judge_decision is not None
    assert c.judge_decision["decision"] == "approved", (
        f"S2: judge_decision={c.judge_decision['decision']!r}"
    )

    # Bounds actually moved: end should now be at an earlier sentence boundary
    # Sentence 6 end = 30.0; after adjust_end(-1) → sentence 5 end = 25.0
    assert c.end is not None and c.end < 30.0, (
        f"S2: clip.end={c.end} should be < 30.0 (correction should have moved it)"
    )
    # End should land on a sentence boundary (within tolerance)
    _EPS = 0.75
    assert any(abs(c.end - s["end"]) <= _EPS for s in sentences), (
        f"S2: clip.end={c.end} is not on any sentence boundary"
    )

    # Exactly 2 renders
    assert len(render_log) == 2, f"S2: render_log={len(render_log)}, expected 2"
    rj_after = _db_total_render_jobs()
    assert rj_after - rj_before == 2, (
        f"S2: expected 2 RenderJob rows, got {rj_after - rj_before}"
    )

    # Judge called exactly once
    assert len(judge_calls) == 1, f"S2: judge calls={len(judge_calls)}, expected 1"

    # 2 critic reports stored (one per render attempt)
    assert len(c.critic_reports or []) == 2, (
        f"S2: critic_reports count={len(c.critic_reports or [])}, expected 2"
    )

    if verbose:
        print(f"  S2: clip end moved {30.0} → {c.end}, 2 renders, approved")


# ---------------------------------------------------------------------------
# Scenario 3: Fail twice → loop-bound hit → escalate_to_human
# ---------------------------------------------------------------------------

_SRC_S3 = "youtube:SIM003AAAAB"


def run_scenario_3(ctx: HarnessContext, verbose: bool = False) -> None:
    """
    1 clip. Critic always returns correctable failure (adjust_end -1).
    After 3rd critic run (attempt==2, loop_bound_hit), judge escalates.
    Assert: exactly 2 corrections, 3 renders, gate_status='didnt_pass'.
    """
    sentences = _make_fake_sentences()
    segments = _make_fake_segments()
    candidates = [{
        "start": 0.0, "end": 25.0, "score": 0.75,
        "hook": "Loop bound test: correctable forever",
        "reason": "", "attempt": 0,
    }]
    _db_setup_source(_SRC_S3, segments, sentences)

    render_log: list[dict] = []
    call_count = [0]

    def _always_correctable(clip_row: Any, *a: Any, **kw: Any) -> CriticReport:
        i = call_count[0]
        call_count[0] += 1
        return CriticReport(
            clip_id=clip_row.id,
            attempt=getattr(clip_row, "correction_attempts", 0),
            failures=[
                CriticFailure(
                    phase="2",
                    check="self_contained",
                    reason=f"Still bleeds into new topic (attempt {i})",
                    severity="correctable",
                    correction=Correction(
                        kind="adjust_end",
                        delta_sentences=-1,
                        note="Trim end by 1 sentence",
                    ),
                )
            ],
            formula_score=0.50,
            passed=False,
        )

    rj_before = _db_total_render_jobs()

    with _counting_judge() as judge_calls, \
         _patches(_common_patches(ctx, render_log, segments, sentences, candidates, _always_correctable)):

        url = f"https://www.youtube.com/watch?v={_SRC_S3.split(':')[1]}"
        result = vp.run_video("simcampaign", url, run_mode="demo",
                              max_apify_spend=2.0, max_modal_spend=3.0)

    clips = _db_get_clips(_SRC_S3)
    assert len(clips) == 1, f"S3: expected 1 clip, got {len(clips)}"
    c = clips[0]

    assert c.correction_attempts == 2, (
        f"S3: correction_attempts={c.correction_attempts}, expected 2 (max)"
    )
    assert c.gate_status == "didnt_pass", (
        f"S3: gate_status={c.gate_status!r}, expected 'didnt_pass'"
    )
    assert c.judge_decision is not None
    assert c.judge_decision["decision"] == "escalate_to_human", (
        f"S3: judge_decision={c.judge_decision['decision']!r}, expected 'escalate_to_human'"
    )

    # Must be exactly 3 renders (attempt 0, 1, 2)
    assert len(render_log) == 3, (
        f"S3: render_log={len(render_log)}, expected 3 (no more renders after bound)"
    )
    rj_after = _db_total_render_jobs()
    assert rj_after - rj_before == 3, (
        f"S3: expected 3 RenderJob rows, got {rj_after - rj_before}"
    )

    # Critic called 3 times (once per render), judge called once
    assert call_count[0] == 3, f"S3: critic called {call_count[0]} times, expected 3"
    assert len(judge_calls) == 1, f"S3: judge called {len(judge_calls)} times, expected 1"

    # Judge reasons mention loop bound
    reasons_text = " ".join(c.judge_decision.get("reasons", []))
    assert "2" in reasons_text or "bound" in reasons_text.lower() or "correction" in reasons_text.lower(), (
        f"S3: judge reasons should mention loop bound: {c.judge_decision['reasons']}"
    )

    if verbose:
        print(f"  S3: 3 renders, 2 corrections, escalated (loop bound)")


# ---------------------------------------------------------------------------
# Scenario 4: Safety terminal → rejected immediately
# ---------------------------------------------------------------------------

_SRC_S4 = "youtube:SIM004AAAAB"


def run_scenario_4(ctx: HarnessContext, verbose: bool = False) -> None:
    """
    1 clip. Critic returns unrelaxed safety failure on attempt 0.
    Judge must immediately reject (0 corrections, 1 render).
    """
    sentences = _make_fake_sentences()
    segments = _make_fake_segments()
    candidates = [{
        "start": 0.0, "end": 20.0, "score": 0.70,
        "hook": "Safety terminal test: dangerous claim",
        "reason": "", "attempt": 0,
    }]
    _db_setup_source(_SRC_S4, segments, sentences)

    render_log: list[dict] = []

    def _safety_failure(clip_row: Any, *a: Any, **kw: Any) -> CriticReport:
        return CriticReport(
            clip_id=clip_row.id,
            attempt=0,
            failures=[
                CriticFailure(
                    phase="2",
                    check="safety_medical_claims",
                    reason="Clip makes unqualified medical claims about dosages",
                    severity="terminal",
                    correction=None,
                )
            ],
            formula_score=None,
            passed=False,
        )

    rj_before = _db_total_render_jobs()

    with _counting_judge() as judge_calls, \
         _patches(_common_patches(ctx, render_log, segments, sentences, candidates, _safety_failure)):

        url = f"https://www.youtube.com/watch?v={_SRC_S4.split(':')[1]}"
        result = vp.run_video("simcampaign", url, run_mode="demo",
                              max_apify_spend=2.0, max_modal_spend=3.0)

    clips = _db_get_clips(_SRC_S4)
    assert len(clips) == 1, f"S4: expected 1 clip, got {len(clips)}"
    c = clips[0]

    assert c.correction_attempts == 0, (
        f"S4: correction_attempts={c.correction_attempts}, expected 0 (terminal = no correction)"
    )
    assert c.gate_status == "didnt_pass", (
        f"S4: gate_status={c.gate_status!r}, expected 'didnt_pass'"
    )
    assert c.judge_decision is not None
    assert c.judge_decision["decision"] == "rejected", (
        f"S4: judge_decision={c.judge_decision['decision']!r}, expected 'rejected'"
    )

    # Only 1 render (no re-renders for safety failures)
    assert len(render_log) == 1, f"S4: render_log={len(render_log)}, expected 1"
    rj_after = _db_total_render_jobs()
    assert rj_after - rj_before == 1, (
        f"S4: expected 1 RenderJob, got {rj_after - rj_before}"
    )

    # Judge called once
    assert len(judge_calls) == 1, f"S4: judge called {len(judge_calls)}, expected 1"
    assert judge_calls[0]["decision"] == "rejected"

    # Reasons prefixed "SAFETY"
    reasons_text = " ".join(c.judge_decision.get("reasons", []))
    assert "SAFETY" in reasons_text.upper(), (
        f"S4: safety reasons should mention SAFETY: {c.judge_decision['reasons']}"
    )

    if verbose:
        print(f"  S4: rejected immediately, 1 render, 0 corrections")


# ---------------------------------------------------------------------------
# Scenario 5: Malformed critic output → ValidationError → clip escalated
# ---------------------------------------------------------------------------

_SRC_S5 = "youtube:SIM005AAAAB"


def run_scenario_5(ctx: HarnessContext, verbose: bool = False) -> None:
    """
    1 clip. _pipeline_run_critic raises a pydantic.ValidationError (simulating
    malformed LLM output). After the bug fix in _run_clip_loop (except Exception
    instead of except CriticUnavailable), the pipeline catches this and escalates
    the clip. Pipeline continues; run terminates cleanly.
    """
    sentences = _make_fake_sentences()
    segments = _make_fake_segments()
    candidates = [{
        "start": 5.0, "end": 25.0, "score": 0.78,
        "hook": "Malformed output test",
        "reason": "", "attempt": 0,
    }]
    _db_setup_source(_SRC_S5, segments, sentences)

    render_log: list[dict] = []

    def _malformed_critic(clip_row: Any, *a: Any, **kw: Any) -> CriticReport:
        from pydantic import ValidationError as _VE
        # Constructing CriticReport with missing required fields raises ValidationError
        try:
            CriticReport.model_validate({"attempt": 0})  # missing clip_id, failures, passed
        except _VE as exc:
            raise exc
        # Should not reach here
        raise AssertionError("Expected ValidationError was not raised")  # pragma: no cover

    rj_before = _db_total_render_jobs()

    with _counting_judge() as judge_calls, \
         _patches(_common_patches(ctx, render_log, segments, sentences, candidates, _malformed_critic)):

        url = f"https://www.youtube.com/watch?v={_SRC_S5.split(':')[1]}"
        result = vp.run_video("simcampaign", url, run_mode="demo",
                              max_apify_spend=2.0, max_modal_spend=3.0)

    # Pipeline should terminate cleanly (not crash)
    assert result.status in ("complete", "partial"), (
        f"S5: run_video returned status={result.status!r}, error={result.error!r} — "
        "expected clean termination after ValidationError"
    )

    # The clip MUST be in the DB and in a terminal state (not pending, not non-existent)
    clips = _db_get_clips(_SRC_S5)
    assert len(clips) == 1, (
        f"S5: expected 1 clip committed (escalated), got {len(clips)} — "
        "ValidationError must be caught by _run_clip_loop, not propagated to rollback"
    )
    c = clips[0]
    assert c.gate_status == "didnt_pass", (
        f"S5: gate_status={c.gate_status!r}, expected 'didnt_pass' (escalated)"
    )
    assert c.judge_decision is not None
    assert c.judge_decision["decision"] == "escalate_to_human", (
        f"S5: judge_decision={c.judge_decision['decision']!r}, expected 'escalate_to_human'"
    )

    # 1 render, 1 judge call
    assert len(render_log) == 1, f"S5: render_log={len(render_log)}, expected 1"
    assert len(judge_calls) == 1, f"S5: judge_calls={len(judge_calls)}, expected 1"

    if verbose:
        print(f"  S5: ValidationError caught, clip escalated (not lost to rollback)")


# ---------------------------------------------------------------------------
# Scenario 6: Stage exception mid-pipeline → source failed, nothing stuck
# ---------------------------------------------------------------------------

_SRC_S6 = "youtube:SIM006AAAAB"


def run_scenario_6(ctx: HarnessContext, verbose: bool = False) -> None:
    """
    _pipeline_rank_moments raises RuntimeError (simulates stage timeout/exception).
    Expected: run_video returns status='failed', Source.stage='failed', no clips stuck.
    """
    sentences = _make_fake_sentences()
    segments = _make_fake_segments()
    candidates: list[dict] = []  # never reached
    _db_setup_source(_SRC_S6, segments, sentences)

    render_log: list[dict] = []

    def _critic_s6(clip_row: Any, *a: Any, **kw: Any) -> CriticReport:  # pragma: no cover
        raise AssertionError("S6: critic should never be called (rank_moments failed)")

    def _rank_raise(*a: Any, **kw: Any) -> list:
        raise RuntimeError("Simulated stage timeout during rank_moments")

    with _patches(_common_patches(ctx, render_log, segments, sentences, candidates, _critic_s6,
                                  rank_fn=_rank_raise)):

        url = f"https://www.youtube.com/watch?v={_SRC_S6.split(':')[1]}"
        result = vp.run_video("simcampaign", url, run_mode="demo",
                              max_apify_spend=2.0, max_modal_spend=3.0)

    # run_video must report failure
    assert result.status == "failed", (
        f"S6: run_video returned status={result.status!r}, expected 'failed'"
    )

    # Source stage must be 'failed' in DB
    src = _db_get_source(_SRC_S6)
    assert src is not None, "S6: Source row not found in DB"
    assert src.stage == "failed", (
        f"S6: Source.stage={src.stage!r}, expected 'failed'"
    )

    # No clips stuck in non-terminal state
    clips = _db_get_clips(_SRC_S6)
    for c in clips:
        assert c.gate_status != "pending", (
            f"S6: clip {c.id} stuck in gate_status='pending' — "
            "every clip must reach a terminal state"
        )

    # No renders happened (exception in stage 3, before rendering)
    assert len(render_log) == 0, (
        f"S6: render_log={len(render_log)}, expected 0 (failed before rendering)"
    )

    if verbose:
        print(f"  S6: stage exception → source.stage='failed', 0 clips stuck")


# ---------------------------------------------------------------------------
# Scenario 7: Regression — 4 boundary_failure_pairs through real guard chain
# ---------------------------------------------------------------------------


def run_scenario_7(_ctx: HarnessContext, verbose: bool = False) -> None:
    """
    Load the 4 boundary_failure_pairs.json cases and run them through the REAL
    deterministic guard chain (eval_segmentation.run_case).

    Hard assertions (a) and (b) must pass for all 4 cases:
      (a) start lands on a sentence boundary (within _EPS tolerance)
      (b) end lands on a sentence boundary (within _EPS tolerance)

    (c) and (d) are soft — PARTIAL is acceptable (same policy as eval-segmentation).
    """
    import json

    with open(_PAIRS_FILE, encoding="utf-8") as fh:
        pairs = json.load(fh)["pairs"]

    assert len(pairs) == 4, f"S7: expected 4 pairs in fixture, got {len(pairs)}"

    failures: list[str] = []
    for pair in pairs:
        pid = pair["id"]
        result = _evalseg.run_case(pair, verbose=verbose)

        a = result["assertions"]
        if not a["a_start_on_boundary"] or not a["b_end_on_boundary"]:
            failures.append(
                f"  [{pid}] FAIL: a_start={a['a_start_on_boundary']} "
                f"b_end={a['b_end_on_boundary']}\n"
                + "\n".join(f"    {n}" for n in result["notes"])
            )
        else:
            if verbose:
                status = result["verdict"]
                print(f"  [{status}] {pid}: a={a['a_start_on_boundary']} "
                      f"b={a['b_end_on_boundary']} c={a['c_end_in_same_or_earlier_unit']} "
                      f"d={a['d_end_not_transition_opener']}")

    assert not failures, (
        "S7: boundary_failure_pairs hard assertions (a)+(b) failed:\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# Global asserts (run after scenarios 1–6)
# ---------------------------------------------------------------------------


def run_global_asserts(verbose: bool = False) -> tuple[bool, str]:
    """
    Run global cross-scenario assertions:
      1. No clip in DB has gate_status='pending' (every clip reached terminal)
      2. All correction_attempts <= 2 (loop bound enforced)
      3. Every committed clip has judge_decision set
      4. judge determinism: same inputs → same output (pure function verified)
      5. RenderJob rows exist in DB (spend ledger was written)

    Returns (passed: bool, message: str).
    """
    from core.db import get_session
    from core.models import Clip, RenderJob

    errors: list[str] = []

    with get_session() as session:
        all_clips = session.query(Clip).all()
        total_rj = session.query(RenderJob).count()

    # Assert 1: No pending clips
    stuck = [c for c in all_clips if c.gate_status == "pending"]
    if stuck:
        errors.append(
            f"GLOBAL: {len(stuck)} clips still in gate_status='pending' "
            f"(IDs: {[c.id for c in stuck]})"
        )

    # Assert 2: Correction loop bound
    over_bound = [c for c in all_clips if (c.correction_attempts or 0) > 2]
    if over_bound:
        errors.append(
            f"GLOBAL: {len(over_bound)} clips exceeded correction_attempts=2: "
            f"{[(c.id, c.correction_attempts) for c in over_bound]}"
        )

    # Assert 3: All committed clips have judge_decision
    no_judge = [c for c in all_clips if c.judge_decision is None]
    if no_judge:
        errors.append(
            f"GLOBAL: {len(no_judge)} clips missing judge_decision: "
            f"{[c.id for c in no_judge]}"
        )

    # Assert 4: Judge determinism (pure function — same input → same output)
    _test_reports = [
        # Passed
        CriticReport(clip_id=999, attempt=0, failures=[], formula_score=0.9, passed=True),
        # Safety terminal
        CriticReport(
            clip_id=999, attempt=0,
            failures=[
                CriticFailure(
                    phase="2", check="safety_medical_claims",
                    reason="Unqualified claim", severity="terminal", correction=None,
                )
            ],
            formula_score=None, passed=False,
        ),
        # Loop bound with correctable failure
        CriticReport(
            clip_id=999, attempt=2,
            failures=[
                CriticFailure(
                    phase="2", check="self_contained",
                    reason="Ends on new topic", severity="correctable",
                    correction=Correction(kind="adjust_end", delta_sentences=-1, note=""),
                )
            ],
            formula_score=0.55, passed=False,
        ),
    ]
    for report in _test_reports:
        d1 = real_judge(report, report.attempt, 2)
        d2 = real_judge(report, report.attempt, 2)
        if d1.decision != d2.decision:
            errors.append(
                f"GLOBAL: judge non-deterministic for clip_id={report.clip_id} "
                f"attempt={report.attempt}: got {d1.decision!r} and {d2.decision!r}"
            )

    # Assert 5: RenderJob rows exist
    if total_rj == 0:
        errors.append("GLOBAL: no RenderJob rows in DB — render ledger was not written")
    else:
        if verbose:
            print(f"  GLOBAL: {total_rj} RenderJob rows in DB (spend ledger present)")

    # Summary
    if errors:
        return False, "\n".join(errors)
    return True, f"All global asserts passed ({len(all_clips)} clips, {total_rj} render jobs)"


# ---------------------------------------------------------------------------
# Run all scenarios
# ---------------------------------------------------------------------------


def run_all_scenarios(
    ctx: HarnessContext,
    verbose: bool = False,
) -> list[tuple[str, bool, str]]:
    """
    Run all 7 scenarios + global asserts.
    Returns list of (label, passed, error_message).
    """
    results: list[tuple[str, bool, str]] = []

    scenario_fns: list[tuple[str, Callable]] = [
        ("Scenario 1: Happy path", lambda: run_scenario_1(ctx, verbose)),
        ("Scenario 2: Correctable → re-render → approved", lambda: run_scenario_2(ctx, verbose)),
        ("Scenario 3: Loop bound → escalate_to_human", lambda: run_scenario_3(ctx, verbose)),
        ("Scenario 4: Safety terminal → rejected", lambda: run_scenario_4(ctx, verbose)),
        ("Scenario 5: Malformed critic → escalated (bug fix)", lambda: run_scenario_5(ctx, verbose)),
        ("Scenario 6: Stage exception → source failed", lambda: run_scenario_6(ctx, verbose)),
        ("Scenario 7: Regression segmentation guards", lambda: run_scenario_7(ctx, verbose)),
    ]

    for label, fn in scenario_fns:
        try:
            fn()
            results.append((label, True, ""))
        except AssertionError as exc:
            results.append((label, False, str(exc)))
        except Exception as exc:
            results.append((label, False, f"{type(exc).__name__}: {exc}"))

    # Global asserts (scenarios 1–6 must have run first to populate the DB)
    try:
        gok, gmsg = run_global_asserts(verbose=verbose)
        results.append(("Global asserts", gok, "" if gok else gmsg))
    except Exception as exc:
        results.append(("Global asserts", False, f"{type(exc).__name__}: {exc}"))

    return results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clip Engine — offline pipeline simulation harness (§8)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-scenario detail",
    )
    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(
            level=logging.WARNING,  # suppress info noise; sim prints its own output
            format="%(levelname)s %(name)s %(message)s",
        )

    print("=" * 72)
    print("CLIP ENGINE — PIPELINE SIMULATION HARNESS (§8)")
    print("FULLY OFFLINE: no network, no LLM, no GPU")
    print("=" * 72)

    ctx = setup_harness()
    try:
        results = run_all_scenarios(ctx, verbose=args.verbose)
    finally:
        teardown_harness(ctx)

    n_total = len(results)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_fail = n_total - n_pass

    print()
    for label, ok, err in results:
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {label}")
        if not ok and err:
            for line in err.splitlines():
                print(f"       {line}")

    print()
    print("-" * 72)
    pass_rate = n_pass / n_total * 100 if n_total else 0
    print(f"Pass rate: {n_pass}/{n_total} ({pass_rate:.0f}%)")

    if n_fail > 0:
        print(f"\nFAIL: {n_fail} scenario(s) did not pass. Exit 1.")
        return 1

    print("\nAll scenarios PASSED. Exit 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
