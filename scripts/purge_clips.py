"""Purge all clips for a campaign: R2 objects (video + thumb) AND Postgres rows.

Usage: DATABASE_URL=... python scripts/purge_clips.py <campaign> [--yes]

render_jobs.clip_id is ON DELETE SET NULL (spend ledger preserved);
analytics rows cascade. Sources/transcripts are kept — dedupe still applies.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    campaign = sys.argv[1]
    confirmed = "--yes" in sys.argv

    from sqlalchemy import create_engine, text

    eng = create_engine(os.environ["DATABASE_URL"], connect_args={"connect_timeout": 10})
    with eng.connect() as c:
        rows = c.execute(
            text("SELECT id, file_path, thumb_path FROM clips WHERE campaign = :c"),
            {"c": campaign},
        ).all()

    print(f"{len(rows)} clip(s) found for campaign {campaign!r}")
    r2_keys = []
    for _id, fp, tp in rows:
        for p in (fp, tp):
            if p and str(p).startswith("r2://"):
                r2_keys.append(str(p)[len("r2://"):])
    print(f"{len(r2_keys)} R2 object(s) to delete")

    if not confirmed:
        print("Dry run (pass --yes to execute).")
        return

    if r2_keys:
        import boto3

        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        bucket = os.environ["R2_BUCKET"]
        for key in r2_keys:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
                print(f"R2 deleted: {key}")
            except Exception as exc:  # keep purging; report at the end
                print(f"R2 delete FAILED for {key}: {exc}")

    with eng.begin() as c:
        n = c.execute(
            text("DELETE FROM clips WHERE campaign = :c"), {"c": campaign}
        ).rowcount
    print(f"Deleted {n} clip row(s). Done.")


if __name__ == "__main__":
    main()
