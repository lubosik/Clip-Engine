"""
scripts/rebuild_profile.py — CLI tool to rebuild a campaign preference profile.

Usage:
    python -m scripts.rebuild_profile <campaign_name>

Connects to the database, collects the last 100 operator decisions for the
campaign, makes one LLM call to distil measurable selection rules, and inserts
a new PreferenceProfile row.

Prints the resulting profile to stdout as JSON, or an error message on failure.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python -m scripts.rebuild_profile <campaign_name>",
            file=sys.stderr,
        )
        sys.exit(1)

    campaign = sys.argv[1].strip()
    if not campaign:
        print("Error: campaign name must be non-empty", file=sys.stderr)
        sys.exit(1)

    # Configure logging to see what's happening
    from core.logging import configure_logging

    configure_logging()

    try:
        from core.db import get_session
        from core.preferences import build_profile
    except Exception as exc:
        print(f"Error: failed to import core modules: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Building preference profile for campaign: {campaign!r}", file=sys.stderr)

    try:
        with get_session() as session:
            profile = build_profile(session, campaign, min_decisions=1)
    except Exception as exc:
        print(f"Error: database error: {exc}", file=sys.stderr)
        sys.exit(1)

    if profile is None:
        print(
            f"Profile build returned None — check logs for details "
            f"(LLM error or insufficient decisions for campaign {campaign!r})",
            file=sys.stderr,
        )
        sys.exit(1)

    from datetime import datetime, timezone

    created_at = profile.created_at
    if isinstance(created_at, datetime) and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    result = {
        "campaign": profile.campaign,
        "version": profile.version,
        "rules": profile.rules or [],
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else None,
        "meta": profile.meta or {},
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
