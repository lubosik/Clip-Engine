"""
core/apify.py — ApifyClient wrapper.

Public interface (per ARCHITECTURE §4):
    class Apify:
        def run(self, actor_id, run_input, *, max_items=None) -> list[dict]

The ApifyClient SDK is imported lazily so that modules importing this file
do not fail in test environments where apify-client is not installed.
"""

from __future__ import annotations

import logging
from typing import Any

from core.settings import get_settings

log = logging.getLogger(__name__)


class Apify:
    """
    Thin wrapper around the Apify Python SDK.

    Instantiation is cheap (no network call). The APIFY_TOKEN is validated
    on first use via settings.require_apify().
    """

    def __init__(self) -> None:
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from apify_client import ApifyClient  # type: ignore[import]
            except ImportError as exc:
                raise ImportError(
                    "apify-client is required for Apify operations. "
                    "Install it with: pip install apify-client"
                ) from exc
            token = get_settings().require_apify()
            self._client = ApifyClient(token)
        return self._client

    def run(
        self,
        actor_id: str,
        run_input: dict[str, Any],
        *,
        max_items: int | None = None,
    ) -> list[dict]:
        """
        Run an Apify actor synchronously and return all result items.

        Items with an `error` or `errorCode` field are logged and skipped —
        they never cause an exception.  The run id and reported usage/cost
        are logged at INFO level.

        Args:
            actor_id:  e.g. "streamers/youtube-scraper"
            run_input: actor input dict
            max_items: optional cap on returned items (passed to iterate_items)

        Returns:
            List of result dicts (error items excluded).
        """
        client = self._get_client()

        log.info("Starting Apify actor run", extra={"actor": actor_id})

        try:
            run = client.actor(actor_id).call(run_input=run_input)
        except Exception as exc:
            log.error(
                "Apify actor run failed",
                extra={"actor": actor_id, "error": str(exc)},
            )
            raise

        run_id = run.get("id", "unknown")
        usage = run.get("usage", {})
        cost = run.get("usageTotalUsd") or usage.get("COMPUTE_UNITS_CHARGED", None)
        log.info(
            "Apify actor run complete",
            extra={
                "actor": actor_id,
                "run_id": run_id,
                "status": run.get("status"),
                "cost_usd": cost,
                "usage": usage,
            },
        )

        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            log.warning(
                "Apify run returned no defaultDatasetId",
                extra={"actor": actor_id, "run_id": run_id},
            )
            return []

        items: list[dict] = []
        kwargs: dict[str, Any] = {}
        if max_items is not None:
            kwargs["limit"] = max_items

        for item in client.dataset(dataset_id).iterate_items(**kwargs):
            if not isinstance(item, dict):
                log.warning(
                    "Apify item is not a dict; skipping",
                    extra={"actor": actor_id, "run_id": run_id, "item_type": type(item).__name__},
                )
                continue

            error = item.get("error") or item.get("errorCode")
            if error:
                log.warning(
                    "Skipping Apify error item",
                    extra={
                        "actor": actor_id,
                        "run_id": run_id,
                        "error": error,
                        "url": item.get("url") or item.get("videoUrl", ""),
                    },
                )
                continue

            items.append(item)

        log.info(
            "Apify dataset collected",
            extra={"actor": actor_id, "run_id": run_id, "items": len(items)},
        )
        return items
