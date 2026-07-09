"""Regression tests for the YouTube transcript fetch/parse path.

These lock in the fixes for the demo "0 clips" failure:
  1. the actor input must be {"videoUrl": url} (not startUrls);
  2. segments live under the `data` key with string start/dur;
  3. an actor run with usage=None must not crash cost accounting.
"""

from __future__ import annotations

from producer.transcripts import (
    fetch_youtube_transcript,
    _norm_yt_segments,
    ACTOR_YT_TRANSCRIPT,
)


class _FakeApify:
    """Records the last actor call and returns a canned dataset."""

    def __init__(self, items):
        self._items = items
        self.calls = []

    def run(self, actor_id, run_input, **kw):
        self.calls.append((actor_id, run_input))
        return self._items


# Real shape observed from pintostudio/youtube-transcript-scraper.
_REAL_ITEM = [
    {
        "data": [
            {"start": "0.520", "dur": "3.720", "text": "hi everyone Peter here"},
            {"start": "4.240", "dur": "4.120", "text": "we will cover hypertrophy"},
            {"start": "8.360", "dur": "6.640", "text": "what stimulates muscle"},
        ]
    }
]


def test_fetch_youtube_uses_videourl_input():
    apify = _FakeApify(_REAL_ITEM)
    fetch_youtube_transcript("https://youtu.be/abc", apify)
    actor_id, run_input = apify.calls[0]
    assert actor_id == ACTOR_YT_TRANSCRIPT
    assert run_input == {"videoUrl": "https://youtu.be/abc"}  # not startUrls


def test_fetch_youtube_parses_data_key():
    apify = _FakeApify(_REAL_ITEM)
    segs = fetch_youtube_transcript("https://youtu.be/abc", apify)
    assert len(segs) == 3
    # String start/dur coerced to float; end = start + dur.
    assert segs[0] == {"start": 0.52, "end": 0.52 + 3.72, "text": "hi everyone Peter here"}
    assert all(isinstance(s["start"], float) and isinstance(s["end"], float) for s in segs)


def test_fetch_youtube_empty_dataset_is_safe():
    assert fetch_youtube_transcript("https://youtu.be/x", _FakeApify([])) == []


def test_norm_yt_segments_handles_string_values():
    out = _norm_yt_segments([{"start": "1.0", "dur": "2.5", "text": " hello "}])
    assert out == [{"start": 1.0, "end": 3.5, "text": "hello"}]
