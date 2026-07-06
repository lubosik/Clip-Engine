"""
producer/comments.py — TikTok comment pull and per-post aggregation.

Per SPEC §4 stage 3: pull comments for selected TikTok sources, store every
comment row with its post_url, and produce a per-post summary string for the
ranker (comment volume + top recurring phrases).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.apify import Apify
    from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

ACTOR_TIKTOK_COMMENTS = "clockworks/tiktok-comments-scraper"

# Number of top phrases to surface in the summary
_TOP_PHRASES = 5
_MIN_WORD_LEN = 4  # ignore short words in phrase extraction


def _extract_phrases(texts: list[str], top_n: int = _TOP_PHRASES) -> list[str]:
    """
    Extract the most common meaningful words/bigrams from comment texts.
    Returns a list of up to top_n phrase strings.
    """
    word_counts: Counter = Counter()
    for text in texts:
        # Lowercase, strip punctuation, split
        words = re.findall(r"[a-z]{%d,}" % _MIN_WORD_LEN, text.lower())
        word_counts.update(words)

    # Exclude very common filler
    stopwords = {
        "that", "this", "with", "from", "have", "just", "they", "when",
        "what", "your", "been", "also", "will", "more", "about", "some",
        "than", "which", "their",
    }
    return [word for word, _ in word_counts.most_common(top_n * 2) if word not in stopwords][:top_n]


def fetch_comments(
    post_url: str,
    apify: "Apify",
    *,
    max_items: int = 200,
) -> list[dict[str, Any]]:
    """
    Fetch comments for a single TikTok video URL.

    Returns list of raw comment dicts from the Apify actor.
    """
    run_input: dict[str, Any] = {
        "postURLs": [post_url],
        "commentsPerPost": max_items,
    }
    try:
        items = apify.run(ACTOR_TIKTOK_COMMENTS, run_input, max_items=max_items)
    except Exception as exc:
        log.error(
            "TikTok comment fetch failed",
            extra={"post_url": post_url, "error": str(exc)},
        )
        return []

    log.info(
        "Fetched TikTok comments",
        extra={"post_url": post_url, "count": len(items)},
    )
    return items


def store_comments(
    session: "Session",
    source_id: str,
    post_url: str,
    raw_comments: list[dict[str, Any]],
) -> int:
    """
    Insert comment rows into the DB (skip duplicates by text+post_url).
    Returns number of new rows inserted.
    """
    from core.models import Comment

    # Build a set of existing (post_url, text) to avoid re-inserting
    existing_texts: set[str] = set()
    existing_rows = (
        session.query(Comment.text)
        .filter_by(source_id=source_id, post_url=post_url)
        .all()
    )
    existing_texts = {row.text for row in existing_rows}

    inserted = 0
    for item in raw_comments:
        text = str(item.get("text") or item.get("commentText") or "").strip()
        if not text or text in existing_texts:
            continue
        likes = int(item.get("diggCount") or item.get("likeCount") or 0)
        comment = Comment(
            source_id=source_id,
            post_url=post_url,
            text=text,
            likes=likes,
        )
        session.add(comment)
        existing_texts.add(text)
        inserted += 1

    if inserted:
        log.info(
            "Stored comments",
            extra={"source_id": source_id, "post_url": post_url, "inserted": inserted},
        )
    return inserted


def aggregate_comment_summary(
    session: "Session",
    source_id: str,
    post_url: str,
) -> str | None:
    """
    Build a short per-post comment summary for the ranker.

    Returns a string like:
        "123 comments. Top themes: protein, gains, recovery, training, supplements"

    Returns None if there are no stored comments.
    """
    from core.models import Comment

    rows = (
        session.query(Comment.text, Comment.likes)
        .filter_by(source_id=source_id, post_url=post_url)
        .all()
    )
    if not rows:
        return None

    texts = [row.text for row in rows]
    phrases = _extract_phrases(texts)

    parts = [f"{len(texts)} comments"]
    if phrases:
        parts.append(f"Top themes: {', '.join(phrases)}")

    summary = ". ".join(parts)
    log.debug(
        "Comment summary",
        extra={"source_id": source_id, "post_url": post_url, "summary": summary},
    )
    return summary


def pull_and_store_comments(
    session: "Session",
    source_id: str,
    post_url: str,
    apify: "Apify",
    *,
    max_items: int = 200,
) -> str | None:
    """
    Convenience wrapper: fetch → store → aggregate.
    Returns the summary string (or None if no comments).
    """
    raw = fetch_comments(post_url, apify, max_items=max_items)
    if raw:
        store_comments(session, source_id, post_url, raw)
        session.flush()
    return aggregate_comment_summary(session, source_id, post_url)
