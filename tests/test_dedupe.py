"""
tests/test_dedupe.py — deduplication logic tests.

Tests the pure utility functions in producer.dedupe:
  - compute_source_id
  - is_duplicate
  - filter_new_candidates
  - filter_done_sources
  - sort_by_engagement

DB-bound functions are not tested here (they require a live DB).
"""

from __future__ import annotations

import pytest

from producer.dedupe import (
    compute_source_id,
    is_duplicate,
    filter_new_candidates,
    filter_done_sources,
    sort_by_engagement,
)


def make_candidate(platform: str, native_id: str, view_count: int = 1000) -> dict:
    sid = compute_source_id(platform, native_id)
    return {
        "platform": platform,
        "native_id": native_id,
        "source_id": sid,
        "url": f"https://example.com/{native_id}",
        "title": f"Video {native_id}",
        "view_count": view_count,
    }


# ---------------------------------------------------------------------------
# compute_source_id
# ---------------------------------------------------------------------------

class TestComputeSourceId:
    def test_youtube_format(self):
        assert compute_source_id("youtube", "abc123") == "youtube:abc123"

    def test_tiktok_format(self):
        assert compute_source_id("tiktok", "7123456789") == "tiktok:7123456789"

    def test_instagram_format(self):
        assert compute_source_id("instagram", "Cx_shortcode") == "instagram:Cx_shortcode"

    def test_separator_is_colon(self):
        result = compute_source_id("youtube", "vid_001")
        platform, native_id = result.split(":", 1)
        assert platform == "youtube"
        assert native_id == "vid_001"

    def test_empty_platform_raises(self):
        with pytest.raises(ValueError, match="platform"):
            compute_source_id("", "abc")

    def test_empty_native_id_raises(self):
        with pytest.raises(ValueError, match="native_id"):
            compute_source_id("youtube", "")

    def test_native_id_with_colon(self):
        """native_id containing colons should be preserved verbatim after the first colon."""
        result = compute_source_id("youtube", "ab:cd:ef")
        assert result == "youtube:ab:cd:ef"

    def test_case_preserved(self):
        """Platform and native_id cases should be preserved."""
        assert compute_source_id("YouTube", "MyVID") == "YouTube:MyVID"

    def test_deterministic(self):
        """Same inputs always produce the same output."""
        a = compute_source_id("tiktok", "123")
        b = compute_source_id("tiktok", "123")
        assert a == b

    def test_different_inputs_different_outputs(self):
        a = compute_source_id("youtube", "abc")
        b = compute_source_id("tiktok", "abc")
        c = compute_source_id("youtube", "xyz")
        assert a != b
        assert a != c
        assert b != c


# ---------------------------------------------------------------------------
# is_duplicate
# ---------------------------------------------------------------------------

class TestIsDuplicate:
    def test_found(self):
        assert is_duplicate("youtube:abc", {"youtube:abc", "tiktok:xyz"})

    def test_not_found(self):
        assert not is_duplicate("youtube:new", {"youtube:abc", "tiktok:xyz"})

    def test_empty_set(self):
        assert not is_duplicate("youtube:abc", set())

    def test_exact_match_only(self):
        """Must be an exact match, not a substring."""
        assert not is_duplicate("youtube:ab", {"youtube:abc"})


# ---------------------------------------------------------------------------
# filter_new_candidates
# ---------------------------------------------------------------------------

class TestFilterNewCandidates:
    def test_all_new(self):
        candidates = [
            make_candidate("youtube", "v1"),
            make_candidate("youtube", "v2"),
        ]
        result = filter_new_candidates(candidates, set())
        assert len(result) == 2

    def test_all_existing(self):
        candidates = [
            make_candidate("youtube", "v1"),
            make_candidate("youtube", "v2"),
        ]
        existing = {c["source_id"] for c in candidates}
        result = filter_new_candidates(candidates, existing)
        assert result == []

    def test_mixed(self):
        c1 = make_candidate("youtube", "v1")
        c2 = make_candidate("youtube", "v2")
        c3 = make_candidate("tiktok", "t1")
        existing = {c1["source_id"]}
        result = filter_new_candidates([c1, c2, c3], existing)
        assert len(result) == 2
        ids = {c["source_id"] for c in result}
        assert c1["source_id"] not in ids
        assert c2["source_id"] in ids
        assert c3["source_id"] in ids

    def test_candidate_missing_source_id_skipped(self):
        """Candidates without source_id should be skipped with a warning."""
        bad = {"platform": "youtube", "url": "https://example.com"}
        good = make_candidate("youtube", "v1")
        result = filter_new_candidates([bad, good], set())
        assert len(result) == 1
        assert result[0]["source_id"] == good["source_id"]

    def test_order_preserved(self):
        """filter_new_candidates preserves the order of new candidates."""
        candidates = [make_candidate("youtube", f"v{i}") for i in range(5)]
        # Remove every other one as "existing"
        existing = {candidates[i]["source_id"] for i in range(0, 5, 2)}
        result = filter_new_candidates(candidates, existing)
        # Should be v1, v3 in that order
        assert [r["native_id"] for r in result] == ["v1", "v3"]

    def test_empty_candidates(self):
        result = filter_new_candidates([], {"youtube:abc"})
        assert result == []


# ---------------------------------------------------------------------------
# filter_done_sources
# ---------------------------------------------------------------------------

class TestFilterDoneSources:
    def test_all_done_filtered(self):
        candidates = [
            make_candidate("youtube", "v1"),
            make_candidate("youtube", "v2"),
        ]
        done = {c["source_id"] for c in candidates}
        result = filter_done_sources(candidates, done)
        assert result == []

    def test_partially_done_kept(self):
        """Sources with status=partially_done should NOT be in done_ids — they should pass."""
        c1 = make_candidate("youtube", "v1")  # done
        c2 = make_candidate("youtube", "v2")  # partially_done — not in done_ids
        done = {c1["source_id"]}
        result = filter_done_sources([c1, c2], done)
        assert len(result) == 1
        assert result[0]["source_id"] == c2["source_id"]

    def test_none_done(self):
        candidates = [make_candidate("youtube", f"v{i}") for i in range(3)]
        result = filter_done_sources(candidates, set())
        assert len(result) == 3

    def test_empty_candidates(self):
        result = filter_done_sources([], {"youtube:v1"})
        assert result == []


# ---------------------------------------------------------------------------
# sort_by_engagement
# ---------------------------------------------------------------------------

class TestSortByEngagement:
    def test_sorted_desc(self):
        candidates = [
            make_candidate("youtube", "v1", view_count=100),
            make_candidate("youtube", "v2", view_count=5000),
            make_candidate("youtube", "v3", view_count=300),
        ]
        result = sort_by_engagement(candidates)
        view_counts = [c["view_count"] for c in result]
        assert view_counts == [5000, 300, 100]

    def test_tie_stable_order(self):
        """Equal view counts should not raise errors."""
        candidates = [
            make_candidate("youtube", "v1", view_count=1000),
            make_candidate("youtube", "v2", view_count=1000),
        ]
        result = sort_by_engagement(candidates)
        assert len(result) == 2

    def test_empty_list(self):
        assert sort_by_engagement([]) == []

    def test_missing_view_count_treated_as_zero(self):
        """Candidates without view_count should be treated as 0 (sorted last)."""
        no_count = {"platform": "tiktok", "source_id": "tiktok:x", "url": "http://x.com"}
        with_count = make_candidate("tiktok", "v1", view_count=500)
        result = sort_by_engagement([no_count, with_count])
        assert result[0]["source_id"] == with_count["source_id"]
        assert result[-1]["source_id"] == "tiktok:x"

    def test_original_list_not_mutated(self):
        """sort_by_engagement must not mutate the input list."""
        candidates = [
            make_candidate("youtube", "v1", view_count=100),
            make_candidate("youtube", "v2", view_count=5000),
        ]
        original_order = [c["source_id"] for c in candidates]
        sort_by_engagement(candidates)
        assert [c["source_id"] for c in candidates] == original_order

    def test_single_item(self):
        c = make_candidate("youtube", "v1", view_count=999)
        result = sort_by_engagement([c])
        assert result == [c]
