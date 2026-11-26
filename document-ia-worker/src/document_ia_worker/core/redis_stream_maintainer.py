"""Periodic background Redis stream retention maintainer for Document IA Worker.

Automatically drains expired completed and DLQ entries from both workflow and
webhook streams using the completion-aware Lua retention script so retained
counts stay below the API admission ceiling without manual cron intervention.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from uuid import uuid4

from redis.asyncio import Redis

from document_ia_infra.redis.maintenance import maintain
from document_ia_infra.redis.redis_manager import RedisManager
from document_ia_infra.redis.redis_settings import redis_settings

logger = logging.getLogger(__name__)

DEFAULT_MAINTENANCE_LOCK_SUFFIX = "maintenance:lock"
DEFAULT_OPERATION_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class StreamMaintenanceSummary:
    """Aggregated outcome of one maintenance tick for a single stream."""

    stream: str
    completed_deleted: int
    dlq_deleted: int
    retained: int
    dlq: int
    pending: int
    lag: int
    oldest_outstanding_ms: int
    batches_run: int


class RedisStreamMaintainer:
    """Periodic background service applying completion-aware Redis stream retention.

    Coordinates across multiple worker replicas with a short-lived per-stream
    Redis lock (`<stream>:maintenance:lock`) so that at most one instance runs
    maintenance per stream and interval. Never raises on Redis errors and never
    logs document payloads or raw exception details.
    """

    def __init__(
        self,
        redis_manager: RedisManager,
        *,
        enabled: bool | None = None,
        interval_seconds: float | None = None,
        max_batches_per_tick: int | None = None,
        streams: tuple[str, ...] | None = None,
        lock_key_suffix: str = DEFAULT_MAINTENANCE_LOCK_SUFFIX,
        operation_timeout_seconds: float = DEFAULT_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self.redis_manager: RedisManager = redis_manager
        self.enabled: bool = (
            redis_settings.STREAM_MAINTENANCE_ENABLED if enabled is None else enabled
        )
        self.interval_seconds: float = (
            float(redis_settings.STREAM_MAINTENANCE_INTERVAL_SECONDS)
            if interval_seconds is None
            else float(interval_seconds)
        )
        self.max_batches_per_tick: int = (
            redis_settings.STREAM_MAINTENANCE_MAX_BATCHES_PER_TICK
            if max_batches_per_tick is None
            else max_batches_per_tick
        )
        self.streams: tuple[str, ...] = (
            (
                redis_settings.EVENT_STREAM_NAME,
                redis_settings.WEBHOOK_STREAM_NAME,
            )
            if streams is None
            else streams
        )
        self.lock_key_suffix: str = lock_key_suffix.strip()
        self.operation_timeout_seconds: float = float(operation_timeout_seconds)

        if not math.isfinite(self.interval_seconds) or self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and strictly positive")
        if self.max_batches_per_tick < 1:
            raise ValueError("max_batches_per_tick must be at least 1")
        if not self.lock_key_suffix:
            raise ValueError("lock_key_suffix must be a non-empty string")
        if (
            not math.isfinite(self.operation_timeout_seconds)
            or self.operation_timeout_seconds <= 0
        ):
            raise ValueError(
                "operation_timeout_seconds must be finite and strictly positive"
            )

    @property
    def lock_ttl_ms(self) -> int:
        """Distributed slot duration in milliseconds (90% of the maintenance interval)."""
        return max(1, int(self.interval_seconds * 900.0))

    def _lock_key_for_stream(self, stream: str) -> str:
        """Build the Redis lock key scoped to a single stream."""
        return f"{stream}:{self.lock_key_suffix}"

    async def _acquire_maintenance_slot(self, conn: Redis, stream: str) -> bool:
        """Acquire the cluster-wide maintenance slot for one stream."""
        acquired = await asyncio.wait_for(
            conn.set(
                self._lock_key_for_stream(stream),
                uuid4().hex,
                nx=True,
                px=self.lock_ttl_ms,
            ),
            timeout=self.operation_timeout_seconds,
        )
        return bool(acquired)

    async def _maintain_stream(
        self,
        conn: Redis,
        stream: str,
        *,
        stop_event: asyncio.Event | None = None,
        deadline: float | None = None,
    ) -> StreamMaintenanceSummary | None:
        """Run bounded completion-aware retention batches on a single stream."""
        batch_size = redis_settings.STREAM_CLEANUP_BATCH_SIZE
        completed_deleted = 0
        dlq_deleted = 0
        batches_run = 0
        last_result: dict[str, int] | None = None

        try:
            for _ in range(self.max_batches_per_tick):
                if stop_event is not None and stop_event.is_set():
                    break
                if (
                    deadline is not None
                    and batches_run > 0
                    and time.monotonic() >= deadline
                ):
                    break
                result = await asyncio.wait_for(
                    maintain(conn, stream, apply=True),
                    timeout=self.operation_timeout_seconds,
                )
                batches_run += 1
                last_result = result
                batch_completed = result["completed_eligible"]
                batch_dlq = result["dlq_eligible"]
                completed_deleted += batch_completed
                dlq_deleted += batch_dlq

                if batch_completed < batch_size and batch_dlq < batch_size:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[stream-maintenance] Retention pass failed for stream=%s (%s) "
                "after batches=%d completed_deleted=%d dlq_deleted=%d",
                stream,
                type(exc).__name__,
                batches_run,
                completed_deleted,
                dlq_deleted,
            )
            return None

        if last_result is None:
            return None

        summary = StreamMaintenanceSummary(
            stream=stream,
            completed_deleted=completed_deleted,
            dlq_deleted=dlq_deleted,
            retained=last_result["retained"],
            dlq=last_result["dlq"],
            pending=last_result["pending"],
            lag=last_result["lag"],
            oldest_outstanding_ms=last_result["oldest_outstanding_ms"],
            batches_run=batches_run,
        )

        if completed_deleted > 0 or dlq_deleted > 0:
            logger.info(
                "[stream-maintenance] stream=%s completed_deleted=%d dlq_deleted=%d "
                "retained=%d dlq=%d pending=%d lag=%d oldest_outstanding_ms=%d batches=%d",
                summary.stream,
                summary.completed_deleted,
                summary.dlq_deleted,
                summary.retained,
                summary.dlq,
                summary.pending,
                summary.lag,
                summary.oldest_outstanding_ms,
                summary.batches_run,
            )
        else:
            logger.debug(
                "[stream-maintenance] stream=%s completed_deleted=0 dlq_deleted=0 "
                "retained=%d dlq=%d pending=%d lag=%d oldest_outstanding_ms=%d",
                summary.stream,
                summary.retained,
                summary.dlq,
                summary.pending,
                summary.lag,
                summary.oldest_outstanding_ms,
            )

        return summary

    async def maintain_once(
        self,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> dict[str, StreamMaintenanceSummary] | None:
        """Execute one cluster-coordinated maintenance pass across configured streams."""
        if not self.enabled:
            logger.debug(
                "[stream-maintenance] Automatic Redis stream maintenance is disabled; skipping"
            )
            return None

        if not self.streams:
            return {}

        try:
            conn = await self.redis_manager.get_connection()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[stream-maintenance] Failed to obtain Redis connection (%s)",
                type(exc).__name__,
            )
            return None

        if conn is None:
            logger.warning(
                "[stream-maintenance] Redis connection unavailable; skipping maintenance pass"
            )
            return None

        summaries: dict[str, StreamMaintenanceSummary] = {}
        for stream in self.streams:
            if stop_event is not None and stop_event.is_set():
                break

            try:
                acquired = await self._acquire_maintenance_slot(conn, stream)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[stream-maintenance] Failed to acquire maintenance lock for stream=%s (%s)",
                    stream,
                    type(exc).__name__,
                )
                continue

            if not acquired:
                logger.debug(
                    "[stream-maintenance] Maintenance slot already held for stream=%s; skipping",
                    stream,
                )
                continue

            deadline = time.monotonic() + (self.lock_ttl_ms / 1000.0)
            summary = await self._maintain_stream(
                conn,
                stream,
                stop_event=stop_event,
                deadline=deadline,
            )
            if summary is not None:
                summaries[stream] = summary
        return summaries

    async def run_periodic_maintenance(self, stop_event: asyncio.Event) -> None:
        """Run periodic Redis stream maintenance until stop_event is set."""
        if not self.enabled:
            logger.info(
                "[stream-maintenance] Automatic Redis stream maintenance is disabled; exiting task"
            )
            return

        logger.info(
            "[stream-maintenance] Starting periodic Redis stream maintenance "
            "(streams=%s, interval=%.1fs, batch_size=%d, max_batches_per_tick=%d)",
            ",".join(self.streams),
            self.interval_seconds,
            redis_settings.STREAM_CLEANUP_BATCH_SIZE,
            self.max_batches_per_tick,
        )

        try:
            while not stop_event.is_set():
                await self.maintain_once(stop_event=stop_event)
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self.interval_seconds,
                    )
                    break
                except TimeoutError:
                    pass
        finally:
            logger.info(
                "[stream-maintenance] Periodic Redis stream maintenance stopped"
            )
