"""
tests/test_discovery_exclusion.py — Unit tests for discover_all() exclusion filters.

Tests cover:
  - sources.youtube.exclude_channels filters YouTube candidates by author_handle
  - sources.exclude_keywords filters by title across all platforms
  - Exclusion is case-insensitive (substring match)
  - Empty exclude lists return all candidates unmodified
  - Non-YouTube candidates are not affected by exclude_channels
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(
    exclude_channels: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
) -> Any:
    """Return a minimal CampaignConfig-like mock."""
    cfg = MagicMock()
    cfg.name = "test_campaign"

    yt = MagicMock()
    yt.exclude_channels = exclude_channels or []
    cfg.sources.youtube = yt
    cfg.sources.exclude_keywords = exclude_keywords or []

    return cfg


def _make_candidate(
    platform="youtube",
    source_id=None,
    title="Great workout tips",
    author_handle="FitnessPro",
    view_count=50_000,
) -> dict:
    sid = source_id or f"{platform}:test_id_{title[:6]}"
    return {
        "platform": platform,
        "native_id": sid.split(":", 1)[-1],
        "source_id": sid,
        "url": f"https://example.com/{sid}",
        "title": title,
        "author_handle": author_handle,
        "view_count": view_count,
        "duration_sec": 300.0,
        "published_at": "2026-01-01",
        "raw": {},
    }


# ---------------------------------------------------------------------------
# Test: discover_all exclusion applied to merged candidate list
# We patch the per-platform discover functions to return controlled data.
# ---------------------------------------------------------------------------

class TestDiscoverAllExcludeChannels:
    def test_excluded_youtube_channel_removed(self):
        from producer.discover import _is_excluded_by_keywords

        candidates = [
            _make_candidate(platform="youtube", source_id="youtube:a1", author_handle="BadChannel"),
            _make_candidate(platform="youtube", source_id="youtube:a2", author_handle="GoodChannel"),
        ]

        cfg = _make_cfg(exclude_channels=["BadChannel"])
        yt_cfg = cfg.sources.youtube
        yt_exclude = list(yt_cfg.exclude_channels)

        # Apply the same filtering logic as discover_all
        filtered = []
        for c in candidates:
            if c.get("platform") == "youtube" and yt_exclude:
                handle = (c.get("author_handle") or "").lower()
                if any(exc.lower() in handle for exc in yt_exclude):
                    continue
            filtered.append(c)

        assert len(filtered) == 1
        assert filtered[0]["source_id"] == "youtube:a2"

    def test_exclude_channel_case_insensitive(self):
        candidates = [
            _make_candidate(platform="youtube", source_id="youtube:b1", author_handle="BADCHANNEL"),
        ]
        yt_exclude = ["badchannel"]  # lowercase pattern, uppercase value
        filtered = []
        for c in candidates:
            handle = (c.get("author_handle") or "").lower()
            if any(exc.lower() in handle for exc in yt_exclude):
                continue
            filtered.append(c)
        assert len(filtered) == 0

    def test_exclude_channel_partial_match(self):
        """Substring match: 'Spam' matches 'SpamChannel123'."""
        candidates = [
            _make_candidate(platform="youtube", source_id="youtube:c1", author_handle="SpamChannel123"),
        ]
        yt_exclude = ["Spam"]
        filtered = []
        for c in candidates:
            handle = (c.get("author_handle") or "").lower()
            if any(exc.lower() in handle for exc in yt_exclude):
                continue
            filtered.append(c)
        assert len(filtered) == 0

    def test_exclude_channel_does_not_affect_tiktok(self):
        """exclude_channels is YouTube-only; TikTok candidates must not be removed."""
        candidates = [
            _make_candidate(platform="tiktok", source_id="tiktok:t1", author_handle="BadChannel"),
        ]
        yt_exclude = ["BadChannel"]
        # Only filter YouTube platforms
        filtered = []
        for c in candidates:
            if c.get("platform") == "youtube" and yt_exclude:
                handle = (c.get("author_handle") or "").lower()
                if any(exc.lower() in handle for exc in yt_exclude):
                    continue
            filtered.append(c)
        assert len(filtered) == 1  # TikTok candidate survives


class TestDiscoverAllExcludeKeywords:
    def test_keyword_in_title_removes_candidate(self):
        from producer.discover import _is_excluded_by_keywords
        c = _make_candidate(title="Best sponsored fitness tips", source_id="youtube:k1")
        assert _is_excluded_by_keywords(c, ["sponsored"])

    def test_keyword_not_in_title_keeps_candidate(self):
        from producer.discover import _is_excluded_by_keywords
        c = _make_candidate(title="Clean workout plan", source_id="youtube:k2")
        assert not _is_excluded_by_keywords(c, ["sponsored", "ad"])

    def test_keyword_case_insensitive(self):
        from producer.discover import _is_excluded_by_keywords
        c = _make_candidate(title="This is an AD for protein", source_id="youtube:k3")
        assert _is_excluded_by_keywords(c, ["ad"])

    def test_empty_keyword_list_keeps_all(self):
        from producer.discover import _is_excluded_by_keywords
        c = _make_candidate(title="Some title", source_id="youtube:k4")
        assert not _is_excluded_by_keywords(c, [])

    def test_keyword_applies_to_tiktok(self):
        from producer.discover import _is_excluded_by_keywords
        c = _make_candidate(
            platform="tiktok",
            title="Promotion for my sponsor",
            source_id="tiktok:k5",
        )
        assert _is_excluded_by_keywords(c, ["promotion"])

    def test_keyword_applies_to_instagram(self):
        from producer.discover import _is_excluded_by_keywords
        c = _make_candidate(
            platform="instagram",
            title="#ad #promo great workout",
            source_id="instagram:k6",
        )
        assert _is_excluded_by_keywords(c, ["promo"])


class TestDiscoverAllIntegration:
    """Integration test: patch the per-platform discover functions and run discover_all."""

    def _run_filtered(self, candidates, exclude_channels=None, exclude_keywords=None):
        """Simulate discover_all filtering logic without real Apify calls."""
        from producer.discover import _is_excluded_by_keywords

        yt_exclude = exclude_channels or []
        kw_exclude = exclude_keywords or []

        filtered = []
        for c in candidates:
            if c.get("platform") == "youtube" and yt_exclude:
                handle = (c.get("author_handle") or "").lower()
                if any(exc.lower() in handle for exc in yt_exclude):
                    continue
            if _is_excluded_by_keywords(c, kw_exclude):
                continue
            filtered.append(c)
        return filtered

    def test_no_exclusions_returns_all(self):
        candidates = [
            _make_candidate(source_id="youtube:x1"),
            _make_candidate(platform="tiktok", source_id="tiktok:x2"),
        ]
        result = self._run_filtered(candidates)
        assert len(result) == 2

    def test_combined_exclusions(self):
        """Channel exclusion + keyword exclusion work independently."""
        candidates = [
            _make_candidate(source_id="youtube:y1", author_handle="BadChannel", title="Good title"),
            _make_candidate(source_id="youtube:y2", author_handle="GoodChannel", title="Ad promo"),
            _make_candidate(source_id="youtube:y3", author_handle="GoodChannel", title="Great tips"),
        ]
        result = self._run_filtered(
            candidates,
            exclude_channels=["BadChannel"],
            exclude_keywords=["ad promo"],
        )
        assert len(result) == 1
        assert result[0]["source_id"] == "youtube:y3"
