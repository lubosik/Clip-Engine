"""
tests/test_apify_download_fallback.py — yt-dlp → Apify downloader fallback
(operator decision 2026-07-29: api-ninja/youtube-video-downloader as the
reliable path when the host IP is bot-walled).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import producer.download as dl


class _FakeApify:
    def __init__(self, items):
        self._items = items
        self.calls = []

    def run(self, actor_id, run_input, *, campaign=None, kind="other", max_items=None):
        self.calls.append({"actor_id": actor_id, "run_input": run_input,
                           "campaign": campaign, "kind": kind})
        return self._items


@pytest.fixture()
def apify_env(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "apify_api_test")


def _patch_apify(monkeypatch, items):
    fake = _FakeApify(items)
    import core.apify as apify_mod
    monkeypatch.setattr(apify_mod, "Apify", lambda: fake)
    return fake


def _patch_stream(monkeypatch, payload=b"x" * 4096):
    class _Resp:
        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size=None):
            yield payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(dl.httpx, "stream", lambda *a, **k: _Resp())


def test_fallback_used_when_ytdlp_fails(monkeypatch, tmp_path, apify_env):
    monkeypatch.setattr(dl, "raw_path", lambda sid: tmp_path / "vid")
    monkeypatch.setattr(
        dl, "_download_youtube",
        lambda url, dest: (_ for _ in ()).throw(RuntimeError("Sign in to confirm")),
    )
    fake = _patch_apify(monkeypatch, [
        {"status": "completed", "downloadUrl": "https://x/tmp.mp4", "fileSizeMB": "4"}
    ])
    _patch_stream(monkeypatch)

    out = dl.download_source("youtube:abc", "youtube", "https://youtu.be/abc12345678",
                             {}, campaign="peptides")
    assert Path(out).exists() and Path(out).suffix == ".mp4"
    assert fake.calls[0]["actor_id"] == dl._APIFY_DOWNLOADER_ACTOR
    assert fake.calls[0]["kind"] == "download"
    assert fake.calls[0]["campaign"] == "peptides"
    assert fake.calls[0]["run_input"]["format"] == "1080"
    assert fake.calls[0]["run_input"]["ttl"] == "none"


def test_no_fallback_without_token(monkeypatch, tmp_path):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(dl, "raw_path", lambda sid: tmp_path / "vid")
    monkeypatch.setattr(
        dl, "_download_youtube",
        lambda url, dest: (_ for _ in ()).throw(RuntimeError("Sign in to confirm")),
    )
    with pytest.raises(RuntimeError, match="Sign in"):
        dl.download_source("youtube:abc", "youtube", "https://youtu.be/abc12345678", {})


def test_fallback_raises_on_failed_item(monkeypatch, tmp_path, apify_env):
    monkeypatch.setattr(dl, "raw_path", lambda sid: tmp_path / "vid")
    monkeypatch.setattr(
        dl, "_download_youtube",
        lambda url, dest: (_ for _ in ()).throw(RuntimeError("bot")),
    )
    _patch_apify(monkeypatch, [{"status": "failed", "error": "nope"}])
    with pytest.raises(RuntimeError, match="no completed item"):
        dl.download_source("youtube:abc", "youtube", "https://youtu.be/abc12345678", {})


def test_probe_soft_passes_with_token(monkeypatch, apify_env):
    monkeypatch.setattr(
        dl, "_try_ytdlp_chain",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Sign in to confirm")),
    )
    dl.probe_youtube("https://youtu.be/abc12345678")  # must NOT raise


def test_probe_still_raises_without_token(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(
        dl, "_try_ytdlp_chain",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Sign in to confirm")),
    )
    with pytest.raises(RuntimeError):
        dl.probe_youtube("https://youtu.be/abc12345678")
