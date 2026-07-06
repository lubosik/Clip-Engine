"""
Postiz REST client — built against verified Postiz public API (2026-07-06).

Reference: POSTIZ_API.md (docs.postiz.com/public-api, issues #717 #1147).

Verified API shape:
  Base:  {POSTIZ_API_URL}/public/v1   (try /api/public/v1 on 404)
  Auth:  Authorization: <raw api key>   — NO "Bearer" prefix

  GET  /integrations
       Response: [{id, name, identifier, picture, disabled, profile, customer}]
       identifier is the platform slug: x | tiktok | instagram | youtube | ...

  POST /upload   (multipart, field "file", MP4 only, max 50MB)
       Response: {id, path, name, organizationId, createdAt, updatedAt}
       IMPORTANT: pass BOTH id AND path when attaching media.
       Do NOT use /upload-from-url (known bug #1147).

  POST /posts
       ALL four top-level fields are required (even for drafts):
       {
         "type": "now"|"schedule"|"draft",
         "date": "<ISO UTC>",
         "shortLink": false,
         "tags": [],
         "posts": [{
           "integration": {"id": "<uuid>"},
           "value": [{"content": "<caption>",
                       "image": [{"id": "<upload id>", "path": "<upload path>"}]}],
           "settings": {"__type": "<identifier>", ...platform fields...}
         }]
       }
       Response: [{"postId": "...", "integration": "..."}]   ← list, not dict

  GET  /posts?startDate=<ISO>&endDate=<ISO>
       Items include id, content, publishDate, releaseURL (when state=PUBLISHED),
       state (QUEUE|PUBLISHED|ERROR|DRAFT), integration.{id, providerIdentifier, name}

  GET  /analytics/:integrationId?date=30
       Platform-level analytics.

Rate limit: ~90 create-post requests/hour (self-hosted default; override with API_LIMIT).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

try:
    from core.settings import settings
except Exception:
    settings = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# X/Twitter character cap.
_X_MAX_CHARS: int = 280

# Platform identifiers that map to X/Twitter.
_X_IDENTIFIERS: frozenset[str] = frozenset({"x", "twitter"})

# Default platform settings per identifier.
# These are passed as posts[].settings and MUST include __type.
_DEFAULT_SETTINGS: dict[str, dict[str, Any]] = {
    "x": {
        "__type": "x",
        "who_can_reply_post": "everyone",
        "community": "",
    },
    "tiktok": {
        "__type": "tiktok",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "duet": False,
        "stitch": False,
        "comment": True,
        "autoAddMusic": "no",
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
        "video_made_with_ai": False,
        "content_posting_method": "DIRECT_POST",
    },
    "instagram": {
        "__type": "instagram",
        "post_type": "reel",
        "is_trial_reel": False,
        "collaborators": [],
    },
    "youtube": {
        "__type": "youtube",
        "title": "",
        "type": "public",
        "selfDeclaredMadeForKids": "no",
        "tags": [],
    },
}


def _default_settings_for(identifier: str) -> dict[str, Any]:
    """Return platform settings dict for the given identifier."""
    return dict(_DEFAULT_SETTINGS.get(identifier, {"__type": identifier}))


# ---------------------------------------------------------------------------
# Caption helpers
# ---------------------------------------------------------------------------

def _truncate_for_x(caption: str) -> str:
    """Truncate caption to fit X's 280-character limit.

    Strategy:
    1. Split off hashtag lines (lines whose stripped form starts with '#').
    2. Drop hashtag lines one-by-one from the end until it fits.
    3. If still too long, hard-truncate the body with an ellipsis.
    """
    if len(caption) <= _X_MAX_CHARS:
        return caption

    lines = caption.splitlines()
    body_lines: list[str] = []
    tag_lines: list[str] = []
    in_tags = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or (in_tags and stripped == ""):
            in_tags = True
            tag_lines.append(line)
        else:
            if in_tags:
                # Non-tag line appeared after hashtags — fold back into body.
                in_tags = False
                body_lines.extend(tag_lines)
                tag_lines = []
            body_lines.append(line)

    while tag_lines and len("\n".join(body_lines + tag_lines)) > _X_MAX_CHARS:
        tag_lines.pop()

    combined = "\n".join(body_lines + tag_lines)
    if len(combined) <= _X_MAX_CHARS:
        return combined

    ellipsis = "…"
    return combined[: _X_MAX_CHARS - len(ellipsis)] + ellipsis


# ---------------------------------------------------------------------------
# Postiz client
# ---------------------------------------------------------------------------

class PostizError(Exception):
    """Non-2xx response or network error from Postiz."""


class Postiz:
    """Thin HTTP client for the Postiz public REST API.

    Args:
        api_url:  Origin of the Postiz instance (e.g. https://postiz.example.com).
                  Trailing slashes are stripped. The /public/v1 prefix is appended
                  automatically.
        api_key:  Raw API key from Postiz Settings → Developers → Public API.
                  Never hard-code; read from POSTIZ_API_KEY.
        timeout:  HTTP timeout in seconds (default 120 to tolerate large uploads).
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        timeout: float = 120.0,
    ) -> None:
        if not api_url:
            raise ValueError("POSTIZ_API_URL must not be empty")
        if not api_key:
            raise ValueError("POSTIZ_API_KEY must not be empty")

        self._origin = api_url.rstrip("/")
        self._base = f"{self._origin}/public/v1"
        self._headers = {
            "Authorization": api_key,   # raw key — no Bearer prefix
            "Accept": "application/json",
        }
        self._timeout = timeout
        self._client = httpx.Client(
            headers=self._headers,
            timeout=self._timeout,
            follow_redirects=True,
        )

        # Integration cache: channel_name_or_handle -> integration dict
        self._integration_cache: dict[str, dict[str, Any]] = {}
        self._cache_lock = Lock()
        self._cache_fetched_at: float = 0.0
        self._cache_ttl: float = 300.0  # seconds

    # ------------------------------------------------------------------
    # Public interface  (matches ARCHITECTURE §4 signature exactly)
    # ------------------------------------------------------------------

    def create_post(
        self,
        channel: str,
        caption: str,
        video_path: Path,
        schedule_at: datetime | None,
        draft: bool,
    ) -> dict[str, Any]:
        """Upload *video_path* and create a Postiz post/draft on *channel*.

        Args:
            channel:     Campaign destination channel name (e.g. ``"tiktok_fitness"``).
                         Resolved to a Postiz integration via GET /integrations.
            caption:     Post caption. Automatically truncated for X.
            video_path:  Absolute path to the rendered mp4 clip.
            schedule_at: UTC datetime to schedule at. ``None`` → now+1h (required
                         by API even for drafts).
            draft:       ``True`` → Postiz draft; ``False`` → schedule/post.

        Returns:
            ``{"id": str, ...}``  — the first item of the Postiz response list,
            normalised so callers always get a ``{"id": ...}`` dict.

        Raises:
            PostizError: on HTTP errors, missing media id, or integration not found.
        """
        integration = self._resolve_integration(channel)
        integration_id: str = integration["id"]
        identifier: str = integration.get("identifier", "")

        if integration.get("disabled"):
            logger.warning(
                "Integration %r (id=%s) is disabled — OAuth may have expired",
                channel,
                integration_id,
            )

        # Platform-specific caption adjustments.
        if identifier in _X_IDENTIFIERS:
            caption = _truncate_for_x(caption)

        logger.info(
            "Uploading video for channel=%s integration_id=%s path=%s",
            channel, integration_id, video_path,
        )
        upload = self._upload_media(video_path)
        logger.info("Upload complete id=%s path=%s", upload["id"], upload["path"])

        body = self._build_post_body(
            integration_id=integration_id,
            identifier=identifier,
            caption=caption,
            upload=upload,
            schedule_at=schedule_at,
            draft=draft,
        )

        raw = self._request("POST", "/posts", json=body)

        # Response is a list: [{"postId": "...", "integration": "..."}]
        result = raw[0] if isinstance(raw, list) and raw else (raw if isinstance(raw, dict) else {})
        # Normalise to always have "id" key (callers depend on it).
        if "postId" in result and "id" not in result:
            result = dict(result, id=result["postId"])

        logger.info(
            "Postiz post created id=%s channel=%s draft=%s",
            result.get("id"),
            channel,
            draft,
        )
        return result

    def list_integrations(self) -> list[dict[str, Any]]:
        """Return the raw list of Postiz integrations."""
        raw = self._request("GET", "/integrations")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("integrations", "data", "items"):
                if key in raw and isinstance(raw[key], list):
                    return raw[key]
        logger.warning("Unexpected /integrations response shape: %s", type(raw))
        return []

    def list_posts(
        self,
        start_date: datetime,
        end_date: datetime,
    ) -> list[dict[str, Any]]:
        """Return posts in the given UTC date range.

        Each item includes: id, content, publishDate, releaseURL, state,
        integration.{id, providerIdentifier, name}.
        """
        params = (
            f"?startDate={start_date.isoformat()}&endDate={end_date.isoformat()}"
        )
        raw = self._request("GET", f"/posts{params}")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("posts", "data", "items"):
                if key in raw and isinstance(raw[key], list):
                    return raw[key]
        return []

    def get_integration_analytics(
        self, integration_id: str, *, days: int = 30
    ) -> dict[str, Any]:
        """Fetch platform analytics for an integration (best-effort).

        Returns empty dict if the endpoint is unavailable.
        """
        try:
            return self._request("GET", f"/analytics/{integration_id}?date={days}")
        except PostizError as exc:
            logger.debug("Analytics endpoint unavailable for %s: %s", integration_id, exc)
            return {}

    def refresh_integration_cache(self) -> None:
        """Force-refresh the integration name/handle-to-record cache."""
        integrations = self.list_integrations()
        mapping: dict[str, dict[str, Any]] = {}
        for item in integrations:
            # Index by multiple keys for flexible lookup.
            for key_field in ("name", "profile", "handle"):
                val: str = item.get(key_field) or ""
                if val:
                    mapping[val] = item
                    mapping[val.lower()] = item
        with self._cache_lock:
            self._integration_cache = mapping
            self._cache_fetched_at = time.monotonic()
        logger.debug("Integration cache refreshed: %d entries", len(mapping))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_integration(self, channel: str) -> dict[str, Any]:
        """Resolve a campaign channel name to a Postiz integration record."""
        self._maybe_refresh_cache()

        integration = self._lookup_channel(channel)
        if integration is not None:
            return integration

        # Force refresh and retry once.
        logger.info("Channel %r not in cache; refreshing from Postiz", channel)
        self.refresh_integration_cache()
        integration = self._lookup_channel(channel)
        if integration is not None:
            return integration

        available = sorted(set(self._integration_cache.keys()))
        raise PostizError(
            f"Channel {channel!r} not found in Postiz integrations. "
            f"Available names/handles: {available}"
        )

    def _lookup_channel(self, channel: str) -> dict[str, Any] | None:
        with self._cache_lock:
            cache = dict(self._integration_cache)

        # Exact match.
        if channel in cache:
            return cache[channel]
        lower = channel.lower()
        if lower in cache:
            return cache[lower]
        # Substring match (e.g. "tiktok_fitness" substring-matches "fitness").
        for key, value in cache.items():
            k = key.lower()
            if lower in k or k in lower:
                return value
        return None

    def _maybe_refresh_cache(self) -> None:
        with self._cache_lock:
            age = time.monotonic() - self._cache_fetched_at
        if age > self._cache_ttl or not self._integration_cache:
            self.refresh_integration_cache()

    def _upload_media(self, video_path: Path) -> dict[str, str]:
        """Upload a video file and return the full upload record {id, path, ...}."""
        if not video_path.exists():
            raise PostizError(f"Video file not found: {video_path}")

        file_size = video_path.stat().st_size
        max_bytes = 50 * 1024 * 1024  # 50 MB
        if file_size > max_bytes:
            raise PostizError(
                f"Video {video_path.name} is {file_size // 1024 // 1024}MB; "
                f"Postiz upload limit is 50MB"
            )

        with video_path.open("rb") as fh:
            response = self._request(
                "POST",
                "/upload",
                files={"file": (video_path.name, fh, "video/mp4")},
            )

        media_id = response.get("id")
        media_path = response.get("path")
        if not media_id or not media_path:
            raise PostizError(
                f"Upload response missing id or path. Keys present: {list(response.keys())}"
            )
        return {"id": str(media_id), "path": str(media_path)}

    @staticmethod
    def _build_post_body(
        *,
        integration_id: str,
        identifier: str,
        caption: str,
        upload: dict[str, str],
        schedule_at: datetime | None,
        draft: bool,
    ) -> dict[str, Any]:
        """Build the verified POST /posts request body.

        All four top-level fields (type, date, shortLink, tags) are always
        included — the API requires them even for drafts.
        """
        if schedule_at is None:
            schedule_at = datetime.now(timezone.utc) + timedelta(hours=1)

        if schedule_at.tzinfo is None:
            schedule_at = schedule_at.replace(tzinfo=timezone.utc)

        post_type = "draft" if draft else "schedule"

        settings_obj = _default_settings_for(identifier)

        return {
            "type": post_type,
            "date": schedule_at.astimezone(timezone.utc).isoformat().replace("+00:00", ".000Z"),
            "shortLink": False,
            "tags": [],
            "posts": [
                {
                    "integration": {"id": integration_id},
                    "value": [
                        {
                            "content": caption,
                            "image": [
                                {
                                    "id": upload["id"],
                                    "path": upload["path"],
                                }
                            ],
                        }
                    ],
                    "settings": settings_obj,
                }
            ],
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        files: dict | None = None,
    ) -> Any:
        """Execute an HTTP request against the Postiz API.

        Raises:
            PostizError: on non-2xx response or JSON decode failure.
        """
        # Allow /public/v1 fallback: if self._base returned 404 before, this
        # could retry against /api/public/v1.  For now use _base directly and
        # document the /api/ fallback in railway.md.
        url = f"{self._base}{path}"
        try:
            if files is not None:
                resp = self._client.request(method, url, files=files)
            else:
                resp = self._client.request(method, url, json=json)
        except httpx.HTTPError as exc:
            raise PostizError(f"HTTP error {method} {url}: {exc}") from exc

        if resp.status_code >= 400:
            raise PostizError(
                f"Postiz returned {resp.status_code} for {method} {url}: "
                f"{resp.text[:500]}"
            )

        try:
            return resp.json()
        except Exception as exc:
            raise PostizError(
                f"Failed to decode JSON from {method} {url}: {exc}"
            ) from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Postiz":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def get_postiz_client() -> Postiz:
    """Construct a Postiz client from environment / core.settings."""
    import os

    api_url: str = ""
    api_key: str = ""

    if settings is not None:
        api_url = getattr(settings, "postiz_api_url", "") or ""
        api_key = getattr(settings, "postiz_api_key", "") or ""

    if not api_url:
        api_url = os.environ.get("POSTIZ_API_URL", "")
    if not api_key:
        api_key = os.environ.get("POSTIZ_API_KEY", "")

    return Postiz(api_url=api_url, api_key=api_key)
