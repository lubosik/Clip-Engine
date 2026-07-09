"""
scripts/healthcheck.py — System healthcheck for Clip Engine.

Checks:
  1. Postgres   — SELECT 1 via SQLAlchemy
  2. R2         — put / get / delete a tiny test object (core.r2.healthcheck)
  3. Apify      — GET /v2/users/me with APIFY_TOKEN
  4. Postiz     — GET /api/integrations with POSTIZ_API_KEY
  5. Modal      — token present in env or ~/.modal.toml + app list contains
                  'clip-engine-render'

Prints an aligned PASS/FAIL table and exits 1 if any check fails.
Degrades gracefully: missing env vars → FAIL with a hint, not a traceback.

Usage:
    python scripts/healthcheck.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# ── width of the service name column ────────────────────────────────────────
_COL = 12


def _row(name: str, passed: bool, hint: str = "") -> str:
    status = "PASS" if passed else "FAIL"
    marker = "✓" if passed else "✗"
    base = f"  {marker}  {name:<{_COL}}  {status}"
    if not passed and hint:
        base += f"  — {hint}"
    return base


def check_postgres() -> tuple[bool, str]:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return False, "DATABASE_URL is not set"
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True, ""
    except Exception as exc:
        return False, str(exc)[:120]


def check_r2() -> tuple[bool, str]:
    bucket = os.getenv("R2_BUCKET", "")
    endpoint = os.getenv("R2_ENDPOINT", "")
    key_id = os.getenv("R2_ACCESS_KEY_ID", "")
    secret = os.getenv("R2_SECRET_ACCESS_KEY", "")

    if not all([bucket, endpoint, key_id, secret]):
        missing = [k for k, v in {
            "R2_BUCKET": bucket,
            "R2_ENDPOINT": endpoint,
            "R2_ACCESS_KEY_ID": key_id,
            "R2_SECRET_ACCESS_KEY": secret,
        }.items() if not v]
        return False, f"Missing env vars: {', '.join(missing)}"

    if "CHANGEME" in key_id or "CHANGEME" in secret:
        return False, "R2_ACCESS_KEY_ID or R2_SECRET_ACCESS_KEY is CHANGEME — set real keys"

    try:
        from core.r2 import healthcheck
        result = healthcheck()
        if result["ok"]:
            return True, ""
        return False, str(result.get("error", ""))[:120]
    except Exception as exc:
        return False, str(exc)[:120]


def check_apify() -> tuple[bool, str]:
    token = os.getenv("APIFY_TOKEN", "")
    if not token:
        return False, "APIFY_TOKEN is not set"
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.apify.com/v2/users/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True, ""
            return False, f"HTTP {resp.status}"
    except Exception as exc:
        return False, str(exc)[:120]


def check_postiz() -> tuple[bool, str]:
    api_url = os.getenv("POSTIZ_API_URL", "")
    api_key = os.getenv("POSTIZ_API_KEY", "")
    if not api_url:
        return False, "POSTIZ_API_URL is not set"
    if not api_key:
        return False, "POSTIZ_API_KEY is not set"
    try:
        import urllib.request
        url = api_url.rstrip("/") + "/api/integrations"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 401):  # 401 = key wrong but server is up
                return resp.status == 200, ("" if resp.status == 200 else "API key rejected (HTTP 401)")
            return False, f"HTTP {resp.status}"
    except Exception as exc:
        return False, str(exc)[:120]


def check_modal() -> tuple[bool, str]:
    token_id = os.getenv("MODAL_TOKEN_ID", "")
    token_secret = os.getenv("MODAL_TOKEN_SECRET", "")
    toml_path = Path("~/.modal.toml").expanduser()

    has_env = bool(token_id and token_secret)
    has_toml = toml_path.exists()

    if not (has_env or has_toml):
        return False, (
            "No Modal credentials found — set MODAL_TOKEN_ID + MODAL_TOKEN_SECRET "
            "or run 'modal token new'"
        )

    # Check that clip-engine-render is deployed
    try:
        result = subprocess.run(
            [sys.executable, "-m", "modal", "app", "list"],
            capture_output=True, text=True, timeout=20,
            env={**os.environ} if has_env else None,
        )
        if result.returncode != 0:
            return False, f"modal app list failed: {result.stderr[:80]}"
        if "clip-engine-render" in result.stdout:
            return True, ""
        # Try resolving the function directly
        try:
            import modal  # type: ignore[import-untyped]
            fn = modal.Function.from_name("clip-engine-render", "render_clip")
            fn.hydrate()
            return True, ""
        except Exception:
            pass
        return False, (
            "clip-engine-render not found in deployed apps — run 'make deploy-modal'"
        )
    except FileNotFoundError:
        return False, "Modal CLI not found — install with: pip install modal"
    except Exception as exc:
        return False, str(exc)[:120]


def main() -> None:
    # Load .env if present (best-effort; no hard dependency on python-dotenv)
    env_file = Path(".env")
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

    checks = [
        ("Postgres", check_postgres),
        ("R2", check_r2),
        ("Apify", check_apify),
        ("Postiz", check_postiz),
        ("Modal", check_modal),
    ]

    print()
    print("  Clip Engine — System Healthcheck")
    print("  " + "─" * 50)

    any_failed = False
    for name, fn in checks:
        try:
            passed, hint = fn()
        except Exception as exc:
            passed, hint = False, f"Unexpected error: {exc}"
        print(_row(name, passed, hint))
        if not passed:
            any_failed = True

    print("  " + "─" * 50)
    if any_failed:
        print("  Result: FAIL — fix the issues above before running the pipeline\n")
        sys.exit(1)
    else:
        print("  Result: PASS — all systems ready\n")


if __name__ == "__main__":
    main()
