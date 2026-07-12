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

# Fallback per-item rates (USD, BRONZE/Starter tier, verified 2026-07-12 via
# the Apify store API — see docs/APIFY_COSTS.md). Pay-per-event actors often
# report usageTotalUsd=null at run-completion time (charges settle async);
# when the re-fetch below still has no figure, estimate items * rate so the
# spend ledger never under-reports to $0.
_FALLBACK_ITEM_RATES: dict[str, float] = {
    "streamers/youtube-scraper": 0.003,
    "pintostudio/youtube-transcript-scraper": 0.01,
    "clockworks/free-tiktok-scraper": 0.002,
    "agentx/tiktok-transcript": 0.38,
    "apify/instagram-scraper": 0.0023,
    "apify/instagram-reel-scraper": 0.0023,
}


class Apify:
    """
    Thin wrapper around the Apify Python SDK.

    Instantiation is cheap (no network call). The APIFY_TOKEN is validated
    on first use via settings.require_apify().
    """

    def __init__(self) -> None:
        self._client: Any = None
        # Real billed spend (usageTotalUsd) accumulated across this instance's
        # runs.  Runs that report no cost contribute $0 here but still bump
        # runs_count.  The producer's --max-apify-spend guard reads this.
        self.total_cost_usd: float = 0.0
        self.runs_count: int = 0

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
        campaign: str | None = None,
        kind: str = "other",
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
            campaign:  campaign name for the spend ledger (apify_runs row)
            kind:      spend ledger category: discovery | transcript |
                       comments | analytics | other

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

        if run is None:
            log.warning("Apify actor run returned None", extra={"actor": actor_id})
            return []
        # apify-client >= 3 returns a typed pydantic Run model; 2.x returned a
        # camelCase dict. Normalise to the dict shape.
        if not isinstance(run, dict):
            run = run.model_dump(by_alias=True)

        run_id = run.get("id", "unknown")
        # `run["usage"]` can be present-but-null (some actors), so a plain
        # .get(..., {}) default won't protect us — coerce None to {} explicitly.
        usage = run.get("usage") or {}
        cost = run.get("usageTotalUsd") or usage.get("COMPUTE_UNITS_CHARGED", None)
        # Real billed spend. Pay-per-event actors settle charges asynchronously,
        # so usageTotalUsd is often null in the call() response — re-fetch the
        # run record once (charges are usually visible seconds later).
        cost_usd: float | None = None
        try:
            raw_usd = run.get("usageTotalUsd")
            if raw_usd is None and run_id != "unknown":
                try:
                    refreshed = client.run(run_id).get()
                    if refreshed is not None:
                        if not isinstance(refreshed, dict):
                            refreshed = refreshed.model_dump(by_alias=True)
                        raw_usd = refreshed.get("usageTotalUsd")
                except Exception:  # noqa: BLE001 - refresh is best-effort
                    pass
            if raw_usd is not None:
                cost_usd = float(raw_usd)
        except (TypeError, ValueError):
            cost_usd = None
        self.runs_count += 1
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

        # Fall back to the published per-item rate when the API never reported
        # a figure — an unrecorded $0 would silently under-count real spend.
        status = run.get("status")
        if cost_usd is None:
            rate = _FALLBACK_ITEM_RATES.get(actor_id)
            if rate is not None:
                cost_usd = round(len(items) * rate, 6)
                status = f"{status or 'OK'} (est)"
        if cost_usd is not None:
            self.total_cost_usd += cost_usd

        self._record_ledger(
            run_id=run_id,
            actor_id=actor_id,
            campaign=campaign,
            kind=kind,
            items=len(items),
            cost_usd=cost_usd,
            status=status,
        )
        return items

    @staticmethod
    def _record_ledger(
        *,
        run_id: str,
        actor_id: str,
        campaign: str | None,
        kind: str,
        items: int,
        cost_usd: float | None,
        status: str | None,
    ) -> None:
        """Persist one apify_runs ledger row (best-effort, never raises).

        A DB failure here must never break a producer run — the run itself
        already succeeded and its results are in hand.
        """
        try:
            from core.db import get_session
            from core.models import ApifyRun

            with get_session() as session:
                session.add(
                    ApifyRun(
                        run_id=run_id or "unknown",
                        actor_id=actor_id,
                        campaign=campaign,
                        kind=kind,
                        items=items,
                        cost_usd=cost_usd,
                        status=status,
                    )
                )
                session.commit()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "Failed to record apify_runs ledger row (run continues)",
                extra={"actor": actor_id, "error": str(exc)},
            )
