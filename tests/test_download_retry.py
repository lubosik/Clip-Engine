"""Regression tests for the YouTube bot-check retry chain in producer/download.py.

YouTube's "Sign in to confirm you're not a bot" wall targets datacenter IPs on
the default web client; retrying with ios/tv innertube clients usually works.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from producer import download as dl


class _FakeYDL:
    """Records the extractor_args of every attempt; fails per the plan."""

    plan: list[bool] = []      # per-attempt: True = raise bot-check error
    attempts: list[object] = []  # recorded player_client values
    error: str = "ERROR: [youtube] xyz: Sign in to confirm you're not a bot."

    def __init__(self, opts):
        self._opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def download(self, urls):
        i = len(self.attempts)
        clients = (self._opts.get("extractor_args") or {}).get("youtube", {}).get(
            "player_client"
        )
        type(self).attempts.append(clients)
        if type(self).plan[i]:
            raise RuntimeError(type(self).error)
        # success → create the expected output file
        out = Path(self._opts["outtmpl"].replace(".%(ext)s", ".mp4"))
        out.write_bytes(b"fake")


@pytest.fixture
def fake_ytdlp(monkeypatch, tmp_path):
    _FakeYDL.attempts = []
    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = _FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    return tmp_path / "vid"


def test_bot_check_retries_with_alternate_clients(fake_ytdlp):
    _FakeYDL.plan = [True, False]  # web blocked → ios/tv succeeds
    out = dl._download_youtube("https://youtu.be/xyz", fake_ytdlp)
    assert out.suffix == ".mp4"
    assert _FakeYDL.attempts == [None, ["ios", "tv"]]


def test_bot_check_exhausts_chain_then_raises(fake_ytdlp):
    _FakeYDL.plan = [True, True, True]
    with pytest.raises(RuntimeError, match="not a bot"):
        dl._download_youtube("https://youtu.be/xyz", fake_ytdlp)
    assert _FakeYDL.attempts == [None, ["ios", "tv"], ["android"]]


def test_non_botcheck_error_does_not_retry(fake_ytdlp):
    _FakeYDL.plan = [True]
    _FakeYDL.error = "ERROR: [youtube] xyz: Video unavailable"
    try:
        with pytest.raises(RuntimeError, match="unavailable"):
            dl._download_youtube("https://youtu.be/xyz", fake_ytdlp)
        assert _FakeYDL.attempts == [None]  # no second attempt
    finally:
        _FakeYDL.error = "ERROR: [youtube] xyz: Sign in to confirm you're not a bot."


def test_first_attempt_success_uses_default_client(fake_ytdlp):
    _FakeYDL.plan = [False]
    dl._download_youtube("https://youtu.be/xyz", fake_ytdlp)
    assert _FakeYDL.attempts == [None]
