"""Startup and periodic in-memory 2D-Doc CRL refresher for Document IA Worker.

Keeps the in-memory ANTS 2D-Doc PKI revocation lists (CRLs) up to date across
long-running worker lifecycles without writing to disk (compatible with
read-only container filesystems) and without crashing when offline.
"""

from __future__ import annotations

import asyncio
import logging

from fr_2ddoc_parser.crypto.key_resolver import CrlRefreshResult, refresh_default_crls

from document_ia_worker.core.barcode_settings import barcode_settings

logger = logging.getLogger(__name__)


class TwoDDocCrlRefresher:
    """Service managing startup and periodic in-memory 2D-Doc CRL refreshes."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        interval_hours: float | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self.enabled: bool = (
            barcode_settings.TWO_D_DOC_CRL_REFRESH_ENABLED
            if enabled is None
            else enabled
        )
        self.interval_hours: float = (
            float(barcode_settings.TWO_D_DOC_CRL_REFRESH_INTERVAL_HOURS)
            if interval_hours is None
            else float(interval_hours)
        )
        self.timeout_seconds: int = (
            barcode_settings.TWO_D_DOC_CRL_REFRESH_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        if self.interval_hours <= 0:
            raise ValueError("interval_hours must be strictly positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be strictly positive")

    @property
    def interval_seconds(self) -> float:
        """Return the periodic refresh interval converted to seconds."""
        return self.interval_hours * 3600.0

    def refresh_once(self) -> CrlRefreshResult | None:
        """Synchronously refresh in-memory 2D-Doc CRLs for all known CAs.

        Uses ``only_expired=False`` so updated CRLs published ahead of schedule
        (e.g. following an unexpected certificate revocation) are picked up on
        every refresh cycle. Never raises on network or resolver errors.
        """
        if not self.enabled:
            logger.info("[2ddoc-crl] 2D-Doc CRL refresh is disabled; skipping refresh")
            return None

        try:
            result = refresh_default_crls(
                timeout=self.timeout_seconds,
                only_expired=False,
            )
        except Exception as exc:
            logger.warning(
                "[2ddoc-crl] Unexpected error during 2D-Doc CRL refresh (%s); "
                "keeping existing in-memory CRLs",
                type(exc).__name__,
            )
            return None

        logger.info(
            "[2ddoc-crl] In-memory 2D-Doc CRL refresh completed "
            "(refreshed_cas=%s, skipped_fresh_cas=%s, failed_cas=%s, expired_cas=%s)",
            result.refreshed_cas,
            result.skipped_fresh_cas,
            result.failed_cas,
            result.expired_cas,
        )
        if result.failed_cas or result.expired_cas:
            logger.warning(
                "[2ddoc-crl] Some 2D-Doc CAs have failed or expired CRLs after refresh "
                "(failed_cas=%s, expired_cas=%s)",
                result.failed_cas,
                result.expired_cas,
            )
        return result

    async def startup_refresh(self) -> CrlRefreshResult | None:
        """Perform an initial non-blocking CRL refresh at worker startup."""
        return await asyncio.to_thread(self.refresh_once)

    async def run_periodic_refresh(self, stop_event: asyncio.Event) -> None:
        """Periodically refresh 2D-Doc CRLs in the background until stop_event is set."""
        if not self.enabled:
            logger.info(
                "[2ddoc-crl] Periodic 2D-Doc CRL refresh is disabled; exiting task"
            )
            return

        logger.info(
            "[2ddoc-crl] Starting periodic 2D-Doc CRL refresher "
            "(interval=%.2fh / %.1fs, timeout=%ds)",
            self.interval_hours,
            self.interval_seconds,
            self.timeout_seconds,
        )

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.interval_seconds,
                )
                break
            except TimeoutError:
                pass

            await asyncio.to_thread(self.refresh_once)

        logger.info("[2ddoc-crl] Periodic 2D-Doc CRL refresher stopped")
