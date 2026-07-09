"""
scripts/upload_hero.py — Upload hero video and poster assets to Cloudflare R2.

Uploads the following files to R2 under the 'hero/' prefix:
  assets/hero/hero_loop.mp4           → hero/hero_loop.mp4
  assets/hero/hero_loop_vertical.mp4  → hero/hero_loop_vertical.mp4
  assets/hero/hero_poster_web.jpg     → hero/hero_poster_web.jpg
  assets/hero/hero_poster_mobile.jpg  → hero/hero_poster_mobile.jpg

These R2 keys are referenced by the /api/hero endpoint and the login page.

Requires R2 to be configured (R2_BUCKET, R2_ENDPOINT, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY).

Usage:
    python scripts/upload_hero.py [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("upload_hero")

HERO_FILES = [
    "hero_loop.mp4",
    "hero_loop_vertical.mp4",
    "hero_poster_web.jpg",
    "hero_poster_mobile.jpg",
]

ASSETS_DIR = Path(__file__).parent.parent / "assets" / "hero"


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload hero assets to R2")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview uploads without actually uploading",
    )
    args = parser.parse_args()

    # Load .env best-effort
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v

    # Validate R2 configuration
    required = ["R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error("Missing required env vars: %s", ", ".join(missing))
        sys.exit(1)

    for k in ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]:
        if "CHANGEME" in os.getenv(k, ""):
            log.error("%s is a placeholder — set real R2 credentials", k)
            sys.exit(1)

    results: list[tuple[str, str, bool]] = []

    for filename in HERO_FILES:
        local_path = ASSETS_DIR / filename
        r2_key = f"hero/{filename}"

        if not local_path.exists():
            log.warning("File not found (skipping): %s", local_path)
            results.append((filename, r2_key, False))
            continue

        size_mb = local_path.stat().st_size / 1e6
        if args.dry_run:
            log.info("[DRY RUN] Would upload: %s → %s (%.1f MB)", local_path.name, r2_key, size_mb)
            results.append((filename, r2_key, True))
            continue

        try:
            from core import r2
            r2.upload_file(local_path, r2_key)
            log.info("Uploaded: %s → %s (%.1f MB)", local_path.name, r2_key, size_mb)
            results.append((filename, r2_key, True))
        except Exception as exc:
            log.error("Failed to upload %s: %s", filename, exc)
            results.append((filename, r2_key, False))

    # Summary
    print()
    ok = sum(1 for _, _, success in results if success)
    total = len(results)
    print(f"  Hero upload summary: {ok}/{total} succeeded")
    for filename, key, success in results:
        mark = "✓" if success else "✗"
        print(f"    {mark}  {filename:<35}  hero/{filename}")
    print()

    if ok < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
