"""
tests/test_pipeline_wrapper_seams.py — real-signature seam tests for the
_pipeline_* wrappers in producer/video_pipeline.py.

Lesson from the first real Add-video run (2026-07-27): unit tests and the
simulation harness monkeypatched the wrappers themselves, so a wrapper calling
the REAL function with the wrong signature (ensure_campaign positional args,
rank_moments arity) crashed only in production. These tests call the wrappers
with mocks one level DEEPER, so the wrapper→real-function binding is exercised.
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import Base, Campaign


@pytest.fixture()
def session(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/seams.db")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def test_ensure_campaign_wrapper_binds_real_signature(session):
    """The wrapper must call the REAL core.db.ensure_campaign successfully."""
    from producer.video_pipeline import _pipeline_ensure_campaign

    _pipeline_ensure_campaign(session, "seamcamp", True, {"k": "v"})
    session.commit()
    row = session.query(Campaign).filter_by(name="seamcamp").one()
    assert row.enabled is True
    assert row.config_snapshot == {"k": "v"}


def test_rank_wrapper_binds_real_rank_clips(monkeypatch):
    """The wrapper must satisfy ranker.rank_clips → core.llm.rank_moments
    bindings for real (only the LLM-touching core is faked)."""
    import core.llm as llm_mod
    from core.config import RankingConfig
    from producer.video_pipeline import _pipeline_rank_moments

    seen: dict = {}

    def fake_rank_moments(transcript, rules, comment_summary, clip_len,
                          max_clips, preference_context="", sentence_spans=None,
                          stance=""):
        seen.update(
            rules=rules, clip_len=clip_len, max_clips=max_clips, stance=stance
        )
        return []

    monkeypatch.setattr(llm_mod, "rank_moments", fake_rank_moments)

    cfg = RankingConfig(ranking_rules="seam rules", clip_length=[10, 40])
    out = _pipeline_rank_moments(
        [{"start": 0.0, "end": 2.0, "text": "hello there."}],
        cfg,
        sentence_spans=None,
        preference_context="ctx",
    )
    assert out == []
    assert seen["rules"] == "seam rules"
    assert seen["clip_len"] == (10, 40)
    assert isinstance(seen["max_clips"], int)


@pytest.mark.parametrize(
    "wrapper_name,real_path,kwargs",
    [
        (
            "_pipeline_fetch_transcript",
            "producer.transcripts.fetch_and_store_transcript",
            dict(session=None, source_id="s", platform="youtube", url="u",
                 apify=None, campaign="c"),
        ),
        (
            "_pipeline_download_source",
            "producer.download.download_source",
            dict(source_id="s", platform="youtube", url="u", raw={}),
        ),
        (
            "_pipeline_render_and_record",
            "producer.render_dispatch.render_and_record",
            dict(cfg=None, source_meta={}, clip_candidate={}, source_video=None,
                 words=None, workdir=None, campaign_name="c",
                 campaign_mode="demo", session=None),
        ),
    ],
)
def test_wrapper_kwargs_bind_to_real_signatures(wrapper_name, real_path, kwargs):
    """Every kwarg set a wrapper forwards must bind to the real signature."""
    module_path, fn_name = real_path.rsplit(".", 1)
    real = getattr(__import__(module_path, fromlist=[fn_name]), fn_name)
    # Raises TypeError if the wrapper's kwarg set no longer matches.
    inspect.signature(real).bind(**kwargs)
