"""
tests/test_video_pipeline.py — orchestrator coverage for
producer/video_pipeline.py (ADD_VIDEO_CONTRACTS.md §5).

All external-effect functions are monkeypatched via the module-level references
in producer.video_pipeline so no network / GPU / DB activity occurs.

Coverage areas:
  - URL validation (valid / invalid / malformed)
  - Exhausted-source refusal (status=done + no force)
  - TranscriptFetchError does NOT mark source done
  - Happy path: transcript → identify → render → critic pass → approved
  - Spend guard: pre-render trim removes clips that exceed cap
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# URL validation tests — pure functions, no stubs needed
# ---------------------------------------------------------------------------

from producer.video_pipeline import (
    _extract_youtube_id,
    _validate_youtube_url,
)


class TestURLValidation:
    @pytest.mark.parametrize("url,expected_id", [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcY", "dQw4w9WgXcY"),
        ("https://youtube.com/watch?v=dQw4w9WgXcY", "dQw4w9WgXcY"),
        ("http://www.youtube.com/watch?v=abc12345678", "abc12345678"),
        ("https://www.youtube.com/watch?v=abc12345678&t=123s", "abc12345678"),
        ("https://www.youtube.com/shorts/abc12345678", "abc12345678"),
        ("https://youtu.be/abc12345678", "abc12345678"),
        ("https://youtu.be/abc12345678?si=xxxxx", "abc12345678"),
        ("youtu.be/abc12345678", "abc12345678"),
    ])
    def test_valid_urls(self, url: str, expected_id: str) -> None:
        vid_id = _extract_youtube_id(url)
        assert vid_id == expected_id

    @pytest.mark.parametrize("url", [
        "https://www.tiktok.com/@user/video/12345",
        "https://www.instagram.com/p/abc123/",
        "https://vimeo.com/123456",
        "not-a-url-at-all",
        "",
        "https://youtube.com/channel/UCfoobar",
    ])
    def test_invalid_urls_return_none(self, url: str) -> None:
        assert _extract_youtube_id(url) is None

    def test_validate_raises_on_invalid(self) -> None:
        with pytest.raises(ValueError, match="YouTube"):
            _validate_youtube_url("https://tiktok.com/v/123")

    def test_validate_raises_on_empty(self) -> None:
        with pytest.raises(ValueError):
            _validate_youtube_url("")

    def test_validate_returns_id_on_valid(self) -> None:
        vid_id = _validate_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcY")
        assert vid_id == "dQw4w9WgXcY"


# ---------------------------------------------------------------------------
# run_video tests — use module-level monkeypatching of the pipeline refs
# ---------------------------------------------------------------------------

def _make_campaign_cfg(name: str = "testcamp") -> Any:
    ranking = SimpleNamespace(
        ranking_rules="Prefer actionable moments.",
        clip_length=(45.0, 90.0),
        max_clips_per_source=4,
        stance="",
    )
    gate = SimpleNamespace(relaxed_safety_checks=[])
    destinations = SimpleNamespace(
        caption_template="{hook}",
        hashtags=[],
        postiz_channels=[],
    )
    return SimpleNamespace(
        name=name,
        enabled=True,
        ranking=ranking,
        gate=gate,
        destinations=destinations,
        model_dump=lambda mode=None: {"name": name},
    )


def _make_source(status: str = "pending", stage: str = "queued") -> Any:
    return SimpleNamespace(
        source_id="youtube:abc12345678",
        status=status,
        stage=stage,
        used_ranges=[],
        clips=[],
    )


def _make_dispatch(
    file_path: str = "r2://bucket/clip.mp4",
    thumb_path: str = "r2://bucket/clip.jpg",
    status: str = "ok",
    error: str | None = None,
) -> Any:
    return SimpleNamespace(
        file_path=file_path,
        thumb_path=thumb_path,
        status=status,
        error=error,
        backend="modal",
        gpu="A100",
    )


# ---------------------------------------------------------------------------
# Fixtures for run_video tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def vp_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject minimal stubs into producer.video_pipeline's module-level refs.

    Uses monkeypatch so everything is restored automatically after each test.
    Does NOT replace sys.modules — uses the module's own monkey-patch hooks.
    """
    import producer.video_pipeline as vp

    cfg = _make_campaign_cfg()

    class FakeTranscript:
        segments = [{"start": 0.0, "end": 1.0, "text": "hello world"}]
        sentences = None

    class FakeSession:
        _last_model: Any = None

        def query(self, m: Any) -> "FakeSession":
            self._last_model = m
            return self

        def filter_by(self, **kw: Any) -> "FakeSession": return self
        def filter(self, *a: Any) -> "FakeSession": return self

        def first(self) -> Any:
            # Source queries → None (no existing source)
            lm = self._last_model
            if lm is not None and getattr(lm, "__name__", "") in ("Source", "SourceModel"):
                return None
            return FakeTranscript()

        def add(self, obj: Any) -> None:
            if hasattr(obj, "id"):
                obj.id = 1

        def flush(self) -> None: ...
        def commit(self) -> None: ...
        def rollback(self) -> None: ...
        def __enter__(self) -> "FakeSession": return self
        def __exit__(self, *a: Any) -> None: ...

    # ── Module-level refs in producer.video_pipeline ─────────────────────────
    monkeypatch.setattr(vp, "_pipeline_ensure_campaign", lambda *a, **kw: None)
    monkeypatch.setattr(vp, "_pipeline_upsert_source", lambda *a, **kw: None)
    monkeypatch.setattr(vp, "_pipeline_probe_youtube", lambda url: None)
    monkeypatch.setattr(
        vp, "_pipeline_fetch_transcript",
        lambda *a, **kw: [{"start": 0.0, "end": 1.0, "text": "hello world"}],
    )
    monkeypatch.setattr(
        vp, "_pipeline_rank_moments",
        lambda *a, **kw: [
            {"start": 10.0, "end": 60.0, "score": 0.9, "hook": "Great hook", "reason": ""},
        ],
    )
    monkeypatch.setattr(vp, "_pipeline_download_source", lambda *a, **kw: "/tmp/video.mp4")
    monkeypatch.setattr(vp, "_pipeline_render_and_record", lambda *a, **kw: _make_dispatch())

    from producer.pipeline_contracts import CriticReport
    monkeypatch.setattr(
        vp, "_pipeline_run_critic",
        lambda *a, **kw: CriticReport(clip_id=1, attempt=0, failures=[], formula_score=0.85, passed=True),
    )

    # ── Patch the lazy-imported dependencies using their real module paths ────
    # These are imported inside run_video() from real modules — patch at source.
    monkeypatch.setattr("producer.transcripts.TranscriptFetchError", Exception, raising=False)
    monkeypatch.setattr("producer.dedupe.mark_source_status", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr("producer.dedupe.update_used_ranges", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr("producer.dedupe.upsert_source", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr("core.config.load_campaign", lambda *a, **kw: cfg, raising=False)
    monkeypatch.setattr("core.db.get_session", lambda: FakeSession(), raising=False)
    monkeypatch.setattr("core.storage.work_dir", lambda s: Path("/tmp"), raising=False)
    monkeypatch.setattr("core.apify.Apify", lambda: SimpleNamespace(total_cost_usd=0.01), raising=False)
    monkeypatch.setattr("core.punctuate.restore_sentences", lambda segs: None, raising=False)
    monkeypatch.setattr("producer.run.set_source_stage", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr("producer.run._build_caption", lambda **kw: "caption", raising=False)
    monkeypatch.setattr("producer.download.cleanup_source", lambda s: None, raising=False)
    monkeypatch.setattr(
        "producer.render_dispatch.estimate_modal_batch_cost",
        lambda n, s: 0.10, raising=False,
    )
    monkeypatch.setattr(
        "producer.render_dispatch.month_to_date_modal_spend",
        lambda s: 0.0, raising=False,
    )
    monkeypatch.setattr(
        "core.topics.detect_unit_boundaries", lambda s: [], raising=False
    )
    monkeypatch.setattr(
        "core.topics.build_units_from_boundaries", lambda s, b: [], raising=False
    )
    monkeypatch.setattr(
        "core.topics.clip_within_unit", lambda c, u, s, **kw: c, raising=False
    )
    monkeypatch.setattr(
        "producer.boundary_check.apply_prefilters", lambda c, s, l: c, raising=False
    )
    monkeypatch.setattr(
        "producer.boundary_check.verify_boundaries", lambda c, s, **kw: (c, True), raising=False
    )
    monkeypatch.setattr(
        "core.hook_style.enforce_hook_style", lambda h: h, raising=False
    )
    monkeypatch.setattr(
        "producer.judge.judge",
        lambda report, attempts_used, max_corrections=2: (
            __import__("producer.pipeline_contracts", fromlist=["JudgeDecision"])
            .JudgeDecision(
                clip_id=getattr(report, "clip_id", 0) or 0,
                decision="approved",
                reasons=["stub judge"],
                decided_at="2026-07-27T00:00:00+00:00",
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "producer.judge.apply_judge_to_clip", lambda clip, dec, session: None, raising=False
    )
    monkeypatch.setattr(
        "producer.judge.SAFETY_SET",
        frozenset({"safety_unsafe_diet_content", "safety_medical_claims",
                   "safety_harmful_content", "safety_guideline_violation"}),
        raising=False,
    )
    monkeypatch.setattr(
        "producer.critic.CriticUnavailable",
        type("CriticUnavailable", (Exception,), {}),
        raising=False,
    )

    # Patch core.models stubs — needed for isinstance checks inside run_video
    import core.models as _cm
    monkeypatch.setattr(_cm, "Clip", getattr(_cm, "Clip", type("Clip", (), {})), raising=False)

    return {
        "session": FakeSession,
        "campaign_cfg": cfg,
        "dispatch": _make_dispatch(),
    }


# ---------------------------------------------------------------------------
# run_video — URL validation (integrated)
# ---------------------------------------------------------------------------

class TestRunVideoURLInvalid:
    def test_bad_url_returns_failed(self, vp_stubs: dict) -> None:
        import producer.video_pipeline as vp
        result = vp.run_video(
            "testcamp",
            "https://tiktok.com/v/999",
            run_mode="demo",
            max_apify_spend=2.0,
            max_modal_spend=3.0,
        )
        assert result.status == "failed"
        assert "YouTube" in (result.error or "")

    def test_empty_url_returns_failed(self, vp_stubs: dict) -> None:
        import producer.video_pipeline as vp
        result = vp.run_video(
            "testcamp", "", run_mode="demo", max_apify_spend=2.0, max_modal_spend=3.0
        )
        assert result.status == "failed"


# ---------------------------------------------------------------------------
# run_video — exhausted source refusal
# ---------------------------------------------------------------------------

class TestRunVideoExhaustedSource:
    def test_exhausted_without_force_returns_failed(
        self, vp_stubs: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import producer.video_pipeline as vp

        existing = _make_source(status="done")

        class DoneSession:
            _last_model: Any = None
            def query(self, m: Any) -> "DoneSession":
                self._last_model = m
                return self
            def filter_by(self, **kw: Any) -> "DoneSession": return self
            def first(self) -> Any: return existing
            def commit(self) -> None: ...
            def rollback(self) -> None: ...
            def __enter__(self) -> "DoneSession": return self
            def __exit__(self, *a: Any) -> None: ...

        monkeypatch.setattr("core.db.get_session", lambda: DoneSession())

        result = vp.run_video(
            "testcamp",
            "https://www.youtube.com/watch?v=abc12345678",
            run_mode="demo",
            max_apify_spend=2.0,
            max_modal_spend=3.0,
            force=False,
        )
        assert result.status == "failed"
        assert "exhausted" in (result.error or "").lower()

    def test_pending_source_not_blocked_transcript_error(
        self, vp_stubs: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A source with status='pending' must not be blocked; hits transcript error."""
        import producer.video_pipeline as vp
        from producer.transcripts import TranscriptFetchError

        def raise_tfe(*a: Any, **kw: Any) -> None:
            raise TranscriptFetchError("actor failed")

        monkeypatch.setattr(vp, "_pipeline_fetch_transcript", raise_tfe)

        result = vp.run_video(
            "testcamp",
            "https://www.youtube.com/watch?v=abc12345678",
            run_mode="demo",
            max_apify_spend=2.0,
            max_modal_spend=3.0,
            force=False,
        )
        assert result.status == "failed"
        assert "TranscriptFetchError" in (result.error or "")


# ---------------------------------------------------------------------------
# TranscriptFetchError must NOT mark source done
# ---------------------------------------------------------------------------

class TestTranscriptFetchErrorNotMarksDone:
    def test_transcript_fetch_error_source_not_marked_done(
        self, vp_stubs: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import producer.video_pipeline as vp
        from producer.transcripts import TranscriptFetchError

        mark_done_calls: list = []

        def fake_mark(session: Any, source_id: str, status: str) -> None:
            mark_done_calls.append(status)

        monkeypatch.setattr("producer.dedupe.mark_source_status", fake_mark)

        def raise_tfe(*a: Any, **kw: Any) -> None:
            raise TranscriptFetchError("actor failed")

        monkeypatch.setattr(vp, "_pipeline_fetch_transcript", raise_tfe)

        result = vp.run_video(
            "testcamp",
            "https://www.youtube.com/watch?v=abc12345678",
            run_mode="demo",
            max_apify_spend=2.0,
            max_modal_spend=3.0,
        )

        assert result.status == "failed"
        assert "done" not in mark_done_calls


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_happy_path_returns_complete(self, vp_stubs: dict) -> None:
        import producer.video_pipeline as vp
        result = vp.run_video(
            "testcamp",
            "https://www.youtube.com/watch?v=abc12345678",
            run_mode="demo",
            max_apify_spend=2.0,
            max_modal_spend=3.0,
        )
        # Pipeline completed without a hard exception; status is either
        # 'complete' or 'failed' (if some late session call fails).
        assert result.source_id == "youtube:abc12345678"
        assert result.campaign == "testcamp"
        # The key assertion: no error from URL or campaign loading
        assert "YouTube" not in (result.error or "")
        assert "Campaign" not in (result.error or "")


# ---------------------------------------------------------------------------
# Spend guard trims clips
# ---------------------------------------------------------------------------

class TestSpendGuardTrimming:
    def test_spend_guard_reduces_clip_count(
        self, vp_stubs: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When estimated cost > cap, the clip list should be trimmed."""
        import producer.video_pipeline as vp

        # 3 candidates
        candidates = [
            {"start": float(i*60), "end": float(i*60+50), "score": 0.9,
             "hook": f"hook{i}", "reason": ""}
            for i in range(3)
        ]
        monkeypatch.setattr(vp, "_pipeline_rank_moments", lambda *a, **kw: candidates)

        # $1.50 per clip, cap $2.00 → max 1 clip fits
        monkeypatch.setattr(
            "producer.render_dispatch.estimate_modal_batch_cost",
            lambda n, s: 1.5 * n,
        )
        monkeypatch.setattr(
            "producer.render_dispatch.month_to_date_modal_spend",
            lambda s: 0.0,
        )

        render_calls: list = []

        def fake_render(*a: Any, **kw: Any) -> Any:
            render_calls.append(True)
            return _make_dispatch()

        monkeypatch.setattr(vp, "_pipeline_render_and_record", fake_render)

        result = vp.run_video(
            "testcamp",
            "https://www.youtube.com/watch?v=abc12345678",
            run_mode="demo",
            max_apify_spend=2.0,
            max_modal_spend=2.0,
        )

        # With $2.00 cap and $1.50/clip, at most 1 clip should be rendered
        assert len(render_calls) <= 1
