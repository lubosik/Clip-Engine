"""
tests/test_rank_chunking.py — chunked ranking for long transcripts
(2026-07-29: a 168-min lecture yielded 4 raw candidates through one giant
prompt while a 130-min podcast yielded 19 — attention dilution; long
transcripts are now mined in ~45-min windows).
"""

from __future__ import annotations

import pytest

import producer.video_pipeline as vp


def _segs(duration_s: float, step: float = 10.0) -> list[dict]:
    out = []
    t = 0.0
    while t < duration_s:
        out.append({"start": t, "end": min(t + step, duration_s), "text": f"seg {t}"})
        t += step
    return out


class _Cfg:
    max_clips_per_source = 30


def test_short_transcript_single_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        vp, "_pipeline_rank_moments",
        lambda segs, cfg, sentence_spans=None, preference_context="": (
            calls.append(len(segs)) or []
        ),
    )
    vp._rank_in_chunks(_segs(60 * 60), _Cfg(), sentence_spans=None)
    assert len(calls) == 1  # under 75-min threshold → one call


def test_long_transcript_chunks_and_merges(monkeypatch):
    calls = []

    def fake_rank(segs, cfg, sentence_spans=None, preference_context=""):
        calls.append((segs[0]["start"], segs[-1]["end"]))
        s = segs[0]["start"]
        # One candidate per window at its start (plus a duplicate of the
        # previous window's candidate inside the overlap for de-dup testing).
        return [{"start": s + 10, "end": s + 40, "score": 0.8, "hook": "h", "reason": ""}]

    monkeypatch.setattr(vp, "_pipeline_rank_moments", fake_rank)
    out = vp._rank_in_chunks(_segs(168 * 60), _Cfg(), sentence_spans=None)
    # 168 min at 45-min windows with 3-min overlap → 4 windows
    assert len(calls) == 4
    assert len(out) == 4
    assert out == sorted(out, key=lambda c: c["start"])


def test_overlap_dedup_keeps_higher_score(monkeypatch):
    windows = [
        [{"start": 100.0, "end": 140.0, "score": 0.6, "hook": "a", "reason": ""}],
        [{"start": 102.0, "end": 141.0, "score": 0.9, "hook": "b", "reason": ""}],
        [], [],
    ]
    it = iter(windows)
    monkeypatch.setattr(
        vp, "_pipeline_rank_moments",
        lambda *a, **k: next(it),
    )
    out = vp._rank_in_chunks(_segs(168 * 60), _Cfg(), sentence_spans=None)
    assert len(out) == 1
    assert out[0]["score"] == 0.9


def test_partial_chunk_failure_degrades(monkeypatch):
    from core.llm import RankingUnavailable

    n = {"i": 0}

    def fake_rank(*a, **k):
        n["i"] += 1
        if n["i"] == 2:
            raise RankingUnavailable("boom")
        return [{"start": n["i"] * 3000.0, "end": n["i"] * 3000.0 + 30,
                 "score": 0.7, "hook": "h", "reason": ""}]

    monkeypatch.setattr(vp, "_pipeline_rank_moments", fake_rank)
    out = vp._rank_in_chunks(_segs(168 * 60), _Cfg(), sentence_spans=None)
    assert len(out) == 3  # 4 windows, 1 failed, others kept


def test_all_chunks_fail_reraises(monkeypatch):
    from core.llm import RankingUnavailable

    monkeypatch.setattr(
        vp, "_pipeline_rank_moments",
        lambda *a, **k: (_ for _ in ()).throw(RankingUnavailable("boom")),
    )
    with pytest.raises(RankingUnavailable):
        vp._rank_in_chunks(_segs(168 * 60), _Cfg(), sentence_spans=None)


def test_emit_called_per_window(monkeypatch):
    monkeypatch.setattr(vp, "_pipeline_rank_moments", lambda *a, **k: [])
    details = []
    vp._rank_in_chunks(_segs(168 * 60), _Cfg(), sentence_spans=None,
                       emit=details.append)
    assert len(details) == 4
    assert details[0].startswith("Scanning section 1 of 4")
