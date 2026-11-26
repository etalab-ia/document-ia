"""Unit tests for RedisStreamMaintainer (100% offline / deterministic)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from document_ia_infra.redis.redis_settings import RedisSettings, redis_settings
from document_ia_worker.core import redis_stream_maintainer as maintainer_module
from document_ia_worker.core.redis_stream_maintainer import (
    RedisStreamMaintainer,
    StreamMaintenanceSummary,
)


def _make_maintain_result(
    *,
    pending: int = 0,
    lag: int = 0,
    oldest_outstanding_ms: int = 0,
    retained: int = 1,
    dlq: int = 0,
    completed_eligible: int = 0,
    dlq_eligible: int = 0,
) -> dict[str, int]:
    return {
        "pending": pending,
        "lag": lag,
        "oldest_outstanding_ms": oldest_outstanding_ms,
        "retained": retained,
        "dlq": dlq,
        "completed_eligible": completed_eligible,
        "dlq_eligible": dlq_eligible,
    }


def _make_redis_manager(
    *,
    lock_acquired: bool = True,
    connection: Any | None = None,
    return_none: bool = False,
) -> tuple[Any, AsyncMock]:
    conn = connection
    if conn is None and not return_none:
        conn = AsyncMock()
        conn.set = AsyncMock(return_value=lock_acquired)
    rm = AsyncMock()
    rm.get_connection = AsyncMock(return_value=None if return_none else conn)
    return rm, conn


def test_redis_settings_stream_maintenance_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STREAM_MAINTENANCE_ENABLED", raising=False)
    monkeypatch.delenv("STREAM_MAINTENANCE_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("STREAM_MAINTENANCE_MAX_BATCHES_PER_TICK", raising=False)

    settings = RedisSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.STREAM_MAINTENANCE_ENABLED is True
    assert settings.STREAM_MAINTENANCE_INTERVAL_SECONDS == 60.0
    assert settings.STREAM_MAINTENANCE_MAX_BATCHES_PER_TICK == 10


def test_maintainer_constructor_validation() -> None:
    rm, _ = _make_redis_manager()

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        interval_seconds=60.0,
        max_batches_per_tick=5,
        operation_timeout_seconds=3.0,
    )
    assert maintainer.lock_ttl_ms == 54000

    with pytest.raises(ValueError, match="interval_seconds"):
        RedisStreamMaintainer(rm, interval_seconds=0.0)

    with pytest.raises(ValueError, match="interval_seconds"):
        RedisStreamMaintainer(rm, interval_seconds=float("nan"))

    with pytest.raises(ValueError, match="max_batches_per_tick"):
        RedisStreamMaintainer(rm, max_batches_per_tick=0)

    with pytest.raises(ValueError, match="operation_timeout_seconds"):
        RedisStreamMaintainer(rm, operation_timeout_seconds=0.0)


@pytest.mark.asyncio
async def test_maintain_once_disabled_skips_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rm, _ = _make_redis_manager()
    mock_maintain = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(maintainer_module, "maintain", mock_maintain)

    maintainer = RedisStreamMaintainer(rm, enabled=False)
    result = await maintainer.maintain_once()

    assert result is None
    rm.get_connection.assert_not_awaited()
    mock_maintain.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintain_once_skips_when_distributed_lock_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rm, conn = _make_redis_manager(lock_acquired=False)
    mock_maintain = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(maintainer_module, "maintain", mock_maintain)

    maintainer = RedisStreamMaintainer(rm, enabled=True, interval_seconds=60.0)
    result = await maintainer.maintain_once()

    assert result is None
    conn.set.assert_awaited_once()
    _, kwargs = conn.set.call_args
    assert kwargs == {"nx": True, "px": 54000}
    mock_maintain.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintain_once_drains_bounded_batches_and_stops_early(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rm, conn = _make_redis_manager(lock_acquired=True)
    monkeypatch.setattr(redis_settings, "STREAM_CLEANUP_BATCH_SIZE", 100)

    event_responses = [
        _make_maintain_result(retained=150, completed_eligible=100, dlq_eligible=0),
        _make_maintain_result(retained=110, completed_eligible=40, dlq_eligible=2),
    ]
    webhook_responses = [
        _make_maintain_result(retained=5, completed_eligible=0, dlq_eligible=0),
    ]

    async def fake_maintain(
        _redis: Any,
        stream: str,
        *,
        apply: bool = False,
    ) -> dict[str, int]:
        assert _redis is conn
        assert apply is True
        if stream == "event_stream":
            return event_responses.pop(0)
        if stream == "webhook_stream":
            return webhook_responses.pop(0)
        raise AssertionError(f"unexpected stream {stream}")

    monkeypatch.setattr(maintainer_module, "maintain", fake_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        interval_seconds=60.0,
        max_batches_per_tick=5,
        streams=("event_stream", "webhook_stream"),
    )

    with caplog.at_level(logging.INFO, logger=maintainer_module.__name__):
        summaries = await maintainer.maintain_once()

    assert summaries is not None
    assert summaries["event_stream"] == StreamMaintenanceSummary(
        stream="event_stream",
        completed_deleted=140,
        dlq_deleted=2,
        retained=110,
        dlq=0,
        pending=0,
        lag=0,
        oldest_outstanding_ms=0,
        batches_run=2,
    )
    assert summaries["webhook_stream"] == StreamMaintenanceSummary(
        stream="webhook_stream",
        completed_deleted=0,
        dlq_deleted=0,
        retained=5,
        dlq=0,
        pending=0,
        lag=0,
        oldest_outstanding_ms=0,
        batches_run=1,
    )
    assert not event_responses
    assert not webhook_responses
    # INFO logged for stream with deletions, not for idle stream
    assert "stream=event_stream completed_deleted=140 dlq_deleted=2" in caplog.text
    assert "stream=webhook_stream" not in caplog.text


@pytest.mark.asyncio
async def test_maintain_once_respects_max_batches_per_tick_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rm, _ = _make_redis_manager(lock_acquired=True)
    monkeypatch.setattr(redis_settings, "STREAM_CLEANUP_BATCH_SIZE", 50)

    mock_maintain = AsyncMock(
        return_value=_make_maintain_result(
            retained=500, completed_eligible=50, dlq_eligible=0
        )
    )
    monkeypatch.setattr(maintainer_module, "maintain", mock_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        max_batches_per_tick=3,
        streams=("event_stream",),
    )

    summaries = await maintainer.maintain_once()
    assert summaries is not None
    assert summaries["event_stream"].batches_run == 3
    assert summaries["event_stream"].completed_deleted == 150
    assert mock_maintain.await_count == 3


@pytest.mark.asyncio
async def test_maintain_once_isolates_stream_errors_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rm, _ = _make_redis_manager(lock_acquired=True)
    secret_detail = "redis://:super-secret-password@internal:6379/0"

    async def fake_maintain(
        _redis: Any,
        stream: str,
        *,
        apply: bool = False,
    ) -> dict[str, int]:
        assert apply is True
        if stream == "event_stream":
            raise RuntimeError(secret_detail)
        return _make_maintain_result(retained=1, completed_eligible=3)

    monkeypatch.setattr(maintainer_module, "maintain", fake_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        streams=("event_stream", "webhook_stream"),
    )

    with caplog.at_level(logging.WARNING, logger=maintainer_module.__name__):
        summaries = await maintainer.maintain_once()

    assert summaries is not None
    assert "event_stream" not in summaries
    assert summaries["webhook_stream"].completed_deleted == 3
    assert "RuntimeError" in caplog.text
    assert secret_detail not in caplog.text
    assert "super-secret-password" not in caplog.text


@pytest.mark.asyncio
async def test_maintain_once_handles_unavailable_redis_connection_safely(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rm, _ = _make_redis_manager(return_none=True)
    maintainer = RedisStreamMaintainer(rm, enabled=True)

    with caplog.at_level(logging.WARNING, logger=maintainer_module.__name__):
        result = await maintainer.maintain_once()

    assert result is None
    assert "Redis connection unavailable" in caplog.text


@pytest.mark.asyncio
async def test_run_periodic_maintenance_runs_multiple_cycles_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rm, _ = _make_redis_manager(lock_acquired=True)
    call_count = 0
    two_cycles_reached = asyncio.Event()

    async def fake_maintain(
        _redis: Any,
        _stream: str,
        *,
        apply: bool = False,
    ) -> dict[str, int]:
        nonlocal call_count
        assert apply is True
        call_count += 1
        if call_count >= 2:
            two_cycles_reached.set()
        return _make_maintain_result(retained=1)

    monkeypatch.setattr(maintainer_module, "maintain", fake_maintain)

    stop_event = asyncio.Event()
    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        interval_seconds=0.015,
        streams=("event_stream",),
    )

    task = asyncio.create_task(maintainer.run_periodic_maintenance(stop_event))
    await asyncio.wait_for(two_cycles_reached.wait(), timeout=2.0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert call_count >= 2


@pytest.mark.asyncio
async def test_run_periodic_maintenance_returns_immediately_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rm, _ = _make_redis_manager()
    mock_maintain = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(maintainer_module, "maintain", mock_maintain)

    stop_event = asyncio.Event()
    maintainer = RedisStreamMaintainer(rm, enabled=False)
    await asyncio.wait_for(maintainer.run_periodic_maintenance(stop_event), timeout=1.0)
    mock_maintain.assert_not_awaited()
