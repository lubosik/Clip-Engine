"""
tests/test_ranker.py — thorough tests of producer.ranker.select_clips.

select_clips is a PURE function: no I/O, no DB, no network.
Tests cover:
  - Basic selection (score filter, length filter, cap)
  - Non-overlap with used_ranges (historical ranges from prior runs)
  - Non-overlap between accepted clips in the same call
  - Greedy best-first selection (higher-score clips win ties)
  - exhaust_source semantics: multiple calls with updated used_ranges
  - Edge cases: empty input, all filtered out, exact boundary lengths
  - max_clips_per_source cap enforcement
"""

from __future__ import annotations

import pytest

from core.config import RankingConfig
from producer.ranker import select_clips, _ranges_overlap, _overlaps_any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cfg(
    clip_length=(20, 60),
    max_clips=8,
    min_score=0.6,
    exhaust_source=False,
) -> RankingConfig:
    return RankingConfig(
        clip_length=list(clip_length),
        max_clips_per_source=max_clips,
        exhaust_source=exhaust_source,
        min_score=min_score,
        ranking_rules="Test rules",
    )


def make_candidate(start, end, score=0.8, hook="Test hook", reason="Test reason") -> dict:
    return {"start": start, "end": end, "score": score, "hook": hook, "reason": reason}


# ---------------------------------------------------------------------------
# Overlap helpers
# ---------------------------------------------------------------------------

class TestOverlapHelpers:
    def test_no_overlap_adjacent(self):
        """Adjacent ranges [0,10] and [10,20] do not overlap."""
        assert not _ranges_overlap(0, 10, 10, 20)

    def test_no_overlap_gap(self):
        assert not _ranges_overlap(0, 10, 15, 25)

    def test_overlap_partial(self):
        assert _ranges_overlap(0, 15, 10, 25)

    def test_overlap_contained(self):
        """Smaller range fully inside larger — overlaps."""
        assert _ranges_overlap(0, 30, 5, 20)

    def test_overlap_identical(self):
        assert _ranges_overlap(10, 20, 10, 20)

    def test_overlaps_any_empty_ranges(self):
        assert not _overlaps_any(0, 10, [])

    def test_overlaps_any_no_match(self):
        assert not _overlaps_any(50, 80, [[0, 30], [90, 120]])

    def test_overlaps_any_one_match(self):
        assert _overlaps_any(25, 55, [[0, 30], [90, 120]])


# ---------------------------------------------------------------------------
# Basic filtering
# ---------------------------------------------------------------------------

class TestBasicFiltering:
    def test_empty_candidates(self):
        cfg = make_cfg()
        result = select_clips([], [], cfg)
        assert result == []

    def test_score_below_min_rejected(self):
        cfg = make_cfg(min_score=0.7)
        candidates = [make_candidate(0, 30, score=0.65)]
        result = select_clips(candidates, [], cfg)
        assert result == []

    def test_score_at_min_accepted(self):
        cfg = make_cfg(min_score=0.6)
        candidates = [make_candidate(0, 30, score=0.6)]
        result = select_clips(candidates, [], cfg)
        assert len(result) == 1

    def test_score_above_min_accepted(self):
        cfg = make_cfg(min_score=0.6)
        candidates = [make_candidate(0, 30, score=0.9)]
        result = select_clips(candidates, [], cfg)
        assert len(result) == 1

    def test_duration_below_min_rejected(self):
        cfg = make_cfg(clip_length=(20, 60))
        candidates = [make_candidate(0, 15, score=0.9)]  # 15s < 20s min
        result = select_clips(candidates, [], cfg)
        assert result == []

    def test_duration_at_min_accepted(self):
        cfg = make_cfg(clip_length=(20, 60))
        candidates = [make_candidate(0, 20, score=0.9)]  # exactly 20s
        result = select_clips(candidates, [], cfg)
        assert len(result) == 1

    def test_duration_above_max_rejected(self):
        cfg = make_cfg(clip_length=(20, 60))
        candidates = [make_candidate(0, 65, score=0.9)]  # 65s > 60s max
        result = select_clips(candidates, [], cfg)
        assert result == []

    def test_duration_at_max_accepted(self):
        cfg = make_cfg(clip_length=(20, 60))
        candidates = [make_candidate(0, 60, score=0.9)]  # exactly 60s
        result = select_clips(candidates, [], cfg)
        assert len(result) == 1

    def test_all_fields_preserved(self):
        cfg = make_cfg()
        c = make_candidate(10, 40, score=0.85, hook="A hook", reason="A reason")
        result = select_clips([c], [], cfg)
        assert len(result) == 1
        assert result[0]["hook"] == "A hook"
        assert result[0]["reason"] == "A reason"
        assert result[0]["start"] == 10
        assert result[0]["end"] == 40


# ---------------------------------------------------------------------------
# Non-overlap vs used_ranges
# ---------------------------------------------------------------------------

class TestNonOverlapUsedRanges:
    def test_overlaps_used_range_rejected(self):
        cfg = make_cfg()
        # previously cut [0, 40]; new candidate [30, 60] overlaps
        used = [[0.0, 40.0]]
        candidates = [make_candidate(30, 60, score=0.9)]
        result = select_clips(candidates, used, cfg)
        assert result == []

    def test_non_overlapping_used_range_accepted(self):
        cfg = make_cfg()
        used = [[0.0, 30.0]]
        candidates = [make_candidate(40, 70, score=0.9)]
        result = select_clips(candidates, used, cfg)
        assert len(result) == 1

    def test_adjacent_to_used_range_accepted(self):
        """[0,30] used; candidate [30, 60] — adjacent, not overlapping."""
        cfg = make_cfg()
        used = [[0.0, 30.0]]
        candidates = [make_candidate(30, 60, score=0.9)]
        result = select_clips(candidates, used, cfg)
        assert len(result) == 1

    def test_fully_inside_used_range_rejected(self):
        cfg = make_cfg()
        used = [[0.0, 120.0]]
        candidates = [make_candidate(10, 50, score=0.95)]
        result = select_clips(candidates, used, cfg)
        assert result == []

    def test_multiple_used_ranges(self):
        cfg = make_cfg()
        used = [[0.0, 30.0], [60.0, 90.0]]
        # Candidate [40, 65] overlaps second used range
        overlapping = make_candidate(40, 65, score=0.9)
        # Candidate [32, 58] fits in the gap
        fitting = make_candidate(32, 58, score=0.85)
        result = select_clips([overlapping, fitting], used, cfg)
        assert len(result) == 1
        assert result[0]["start"] == 32


# ---------------------------------------------------------------------------
# Non-overlap between accepted clips in same call
# ---------------------------------------------------------------------------

class TestMutualNonOverlap:
    def test_two_overlapping_candidates_only_one_accepted(self):
        cfg = make_cfg(max_clips=8)
        # Both cover [0, 40] — only one (higher score) accepted
        high = make_candidate(0, 40, score=0.95)
        low = make_candidate(10, 50, score=0.75)
        result = select_clips([low, high], [], cfg)
        assert len(result) == 1
        assert result[0]["score"] == 0.95

    def test_greedy_best_first_selection(self):
        """With overlapping candidates, highest-scored non-overlapping set is selected."""
        cfg = make_cfg(max_clips=3)
        # [0,30] score=0.9, [20,50] score=0.8 (overlaps first), [60,90] score=0.7 (no overlap)
        c1 = make_candidate(0, 30, score=0.9)
        c2 = make_candidate(20, 50, score=0.8)
        c3 = make_candidate(60, 90, score=0.7)
        result = select_clips([c1, c2, c3], [], cfg)
        # c1 accepted, c2 rejected (overlaps c1), c3 accepted
        assert len(result) == 2
        starts = {r["start"] for r in result}
        assert 0 in starts
        assert 60 in starts

    def test_many_non_overlapping_all_accepted(self):
        cfg = make_cfg(max_clips=10)
        candidates = [make_candidate(i * 70, i * 70 + 30, score=0.8) for i in range(5)]
        result = select_clips(candidates, [], cfg)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# max_clips_per_source cap
# ---------------------------------------------------------------------------

class TestMaxClipsCap:
    def test_cap_enforced(self):
        cfg = make_cfg(max_clips=3)
        # 5 non-overlapping candidates
        candidates = [make_candidate(i * 70, i * 70 + 30, score=0.8) for i in range(5)]
        result = select_clips(candidates, [], cfg)
        assert len(result) == 3

    def test_cap_of_one(self):
        cfg = make_cfg(max_clips=1)
        candidates = [make_candidate(i * 70, i * 70 + 30, score=0.9 - i * 0.05) for i in range(4)]
        result = select_clips(candidates, [], cfg)
        assert len(result) == 1
        assert result[0]["score"] == pytest.approx(0.9)

    def test_cap_higher_than_available_clips(self):
        cfg = make_cfg(max_clips=10)
        candidates = [make_candidate(i * 70, i * 70 + 30, score=0.8) for i in range(3)]
        result = select_clips(candidates, [], cfg)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Sorting: best-first
# ---------------------------------------------------------------------------

class TestSortOrder:
    def test_higher_score_wins_when_overlapping(self):
        cfg = make_cfg(max_clips=1)
        low = make_candidate(0, 30, score=0.65)
        high = make_candidate(0, 30, score=0.92)
        result = select_clips([low, high], [], cfg)
        assert result[0]["score"] == pytest.approx(0.92)

    def test_returned_in_score_desc_order(self):
        cfg = make_cfg(max_clips=4)
        candidates = [make_candidate(i * 70, i * 70 + 30, score=round(0.65 + i * 0.07, 2)) for i in range(4)]
        result = select_clips(candidates, [], cfg)
        scores = [r["score"] for r in result]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# exhaust_source semantics (caller loops; select_clips is called multiple times)
# ---------------------------------------------------------------------------

class TestExhaustSource:
    def test_second_call_with_updated_used_ranges_finds_more_clips(self):
        """
        Simulate exhaust_source loop: first call picks [0,30]; second call
        with [0,30] as used_ranges picks [60,90].
        """
        cfg = make_cfg(max_clips=1)
        candidates_all = [
            make_candidate(0, 30, score=0.9),
            make_candidate(60, 90, score=0.85),
        ]

        # First call — no history
        first_batch = select_clips(candidates_all, [], cfg)
        assert len(first_batch) == 1
        assert first_batch[0]["start"] == 0

        # Simulate updating used_ranges
        used_after_first = [[0.0, 30.0]]

        # Second call — first range is now used
        second_batch = select_clips(candidates_all, used_after_first, cfg)
        assert len(second_batch) == 1
        assert second_batch[0]["start"] == 60

    def test_exhaustion_stops_when_no_valid_candidates(self):
        """When all candidates overlap used_ranges, select_clips returns empty."""
        cfg = make_cfg(max_clips=4)
        candidates = [make_candidate(0, 30, score=0.9)]
        used = [[0.0, 30.0]]
        result = select_clips(candidates, used, cfg)
        assert result == []

    def test_multi_round_exhaustion(self):
        """Multiple rounds of select_clips+used_ranges update correctly exhaust a source."""
        cfg = make_cfg(max_clips=2)
        # 6 non-overlapping 30-second segments in a 420-second video
        candidates = [make_candidate(i * 70, i * 70 + 30, score=0.9 - i * 0.02) for i in range(6)]

        all_selected = []
        used: list[list[float]] = []

        # Keep calling until nothing is returned
        for _ in range(10):  # safety cap
            batch = select_clips(candidates, used, cfg)
            if not batch:
                break
            all_selected.extend(batch)
            used.extend([[c["start"], c["end"]] for c in batch])

        # All 6 candidates should have been selected across multiple rounds
        assert len(all_selected) == 6
        starts = sorted(r["start"] for r in all_selected)
        assert starts == [i * 70 for i in range(6)]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_all_candidates_overlap_used(self):
        cfg = make_cfg()
        # One big used range covers everything
        used = [[0.0, 10000.0]]
        candidates = [make_candidate(100, 130, score=0.9)]
        result = select_clips(candidates, used, cfg)
        assert result == []

    def test_all_candidates_too_short(self):
        cfg = make_cfg(clip_length=(30, 60))
        candidates = [make_candidate(i * 100, i * 100 + 10, score=0.9) for i in range(5)]
        result = select_clips(candidates, [], cfg)
        assert result == []

    def test_all_candidates_below_min_score(self):
        cfg = make_cfg(min_score=0.8)
        candidates = [make_candidate(i * 70, i * 70 + 30, score=0.5) for i in range(5)]
        result = select_clips(candidates, [], cfg)
        assert result == []

    def test_floating_point_timestamps(self):
        """Select clips should handle float timestamps precisely."""
        cfg = make_cfg(clip_length=(15, 60))
        # 23.7 seconds long — within [15, 60]
        c = make_candidate(12.3, 36.0, score=0.85)
        result = select_clips([c], [], cfg)
        assert len(result) == 1
        assert result[0]["start"] == pytest.approx(12.3)
        assert result[0]["end"] == pytest.approx(36.0)

    def test_zero_used_ranges(self):
        cfg = make_cfg()
        candidates = [make_candidate(0, 30, score=0.9)]
        result = select_clips(candidates, [], cfg)
        assert len(result) == 1

    def test_large_used_ranges_list_performance(self):
        """select_clips should handle a large used_ranges list without error."""
        cfg = make_cfg(max_clips=1)
        # 1000 used ranges, candidate is in a gap
        used = [[i * 60.0, i * 60.0 + 30.0] for i in range(1000)]
        # Gap at the very end
        candidates = [make_candidate(70000, 70040, score=0.9)]
        result = select_clips(candidates, used, cfg)
        assert len(result) == 1
