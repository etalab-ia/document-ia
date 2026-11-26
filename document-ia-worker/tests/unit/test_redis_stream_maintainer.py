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
    assert maintainer.lock_key_suffix == "maintenance:lock"

    with pytest.raises(ValueError, match="interval_seconds"):
        RedisStreamMaintainer(rm, interval_seconds=0.0)

    with pytest.raises(ValueError, match="interval_seconds"):
        RedisStreamMaintainer(rm, interval_seconds=float("nan"))

    with pytest.raises(ValueError, match="max_batches_per_tick"):
        RedisStreamMaintainer(rm, max_batches_per_tick=0)

    with pytest.raises(ValueError, match="lock_key_suffix"):
        RedisStreamMaintainer(rm, lock_key_suffix="   ")

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
async def test_maintain_once_empty_streams_skips_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rm, _ = _make_redis_manager()
    mock_maintain = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(maintainer_module, "maintain", mock_maintain)

    maintainer = RedisStreamMaintainer(rm, enabled=True, streams=())
    result = await maintainer.maintain_once()

    assert result == {}
    rm.get_connection.assert_not_awaited()
    mock_maintain.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintain_once_skips_when_distributed_lock_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rm, conn = _make_redis_manager(lock_acquired=False)
    mock_maintain = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(maintainer_module, "maintain", mock_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        interval_seconds=60.0,
        streams=("event_stream", "webhook_stream"),
    )
    result = await maintainer.maintain_once()

    assert result == {}
    assert conn.set.await_count == 2
    keys_and_kwargs = [(call.args[0], call.kwargs) for call in conn.set.call_args_list]
    assert keys_and_kwargs == [
        ("event_stream:maintenance:lock", {"nx": True, "px": 54000}),
        ("webhook_stream:maintenance:lock", {"nx": True, "px": 54000}),
    ]
    mock_maintain.assert_not_awaited()


@pytest.mark.asyncio
async def test_maintain_once_uses_per_stream_lock_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = AsyncMock()
    lock_keys_seen: list[str] = []

    async def fake_set(key: str, _value: str, **kwargs: Any) -> bool:
        assert kwargs == {"nx": True, "px": 54000}
        lock_keys_seen.append(key)
        # event_stream lock is already held by another replica; webhook_stream is free
        return key == "webhook_stream:maintenance:lock"

    conn.set = AsyncMock(side_effect=fake_set)
    rm, _ = _make_redis_manager(connection=conn)

    maintained_streams: list[str] = []

    async def fake_maintain(
        _redis: Any,
        stream: str,
        *,
        apply: bool = False,
    ) -> dict[str, int]:
        assert apply is True
        maintained_streams.append(stream)
        return _make_maintain_result(retained=2, completed_eligible=4)

    monkeypatch.setattr(maintainer_module, "maintain", fake_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        interval_seconds=60.0,
        streams=("event_stream", "webhook_stream"),
    )
    summaries = await maintainer.maintain_once()

    assert lock_keys_seen == [
        "event_stream:maintenance:lock",
        "webhook_stream:maintenance:lock",
    ]
    assert maintained_streams == ["webhook_stream"]
    assert summaries is not None
    assert "event_stream" not in summaries
    assert summaries["webhook_stream"].completed_deleted == 4


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
async def test_maintain_once_continues_batches_when_only_dlq_hits_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rm, _ = _make_redis_manager(lock_acquired=True)
    monkeypatch.setattr(redis_settings, "STREAM_CLEANUP_BATCH_SIZE", 50)

    responses = [
        _make_maintain_result(
            retained=120, dlq=70, completed_eligible=0, dlq_eligible=50
        ),
        _make_maintain_result(
            retained=100, dlq=50, completed_eligible=0, dlq_eligible=20
        ),
    ]

    async def fake_maintain(
        _redis: Any,
        _stream: str,
        *,
        apply: bool = False,
    ) -> dict[str, int]:
        assert apply is True
        return responses.pop(0)

    monkeypatch.setattr(maintainer_module, "maintain", fake_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        max_batches_per_tick=5,
        streams=("event_stream",),
    )

    summaries = await maintainer.maintain_once()
    assert summaries is not None
    assert summaries["event_stream"].batches_run == 2
    assert summaries["event_stream"].completed_deleted == 0
    assert summaries["event_stream"].dlq_deleted == 70
    assert not responses


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
async def test_maintain_once_stops_between_batches_when_stop_event_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rm, _ = _make_redis_manager(lock_acquired=True)
    monkeypatch.setattr(redis_settings, "STREAM_CLEANUP_BATCH_SIZE", 50)

    stop_event = asyncio.Event()
    calls = 0

    async def fake_maintain(
        _redis: Any,
        _stream: str,
        *,
        apply: bool = False,
    ) -> dict[str, int]:
        nonlocal calls
        assert apply is True
        calls += 1
        stop_event.set()
        return _make_maintain_result(
            retained=200, completed_eligible=50, dlq_eligible=0
        )

    monkeypatch.setattr(maintainer_module, "maintain", fake_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        max_batches_per_tick=5,
        streams=("event_stream", "webhook_stream"),
    )

    summaries = await maintainer.maintain_once(stop_event=stop_event)
    assert summaries is not None
    assert calls == 1
    assert summaries["event_stream"].batches_run == 1
    assert "webhook_stream" not in summaries


@pytest.mark.asyncio
async def test_maintain_once_isolates_stream_errors_without_leaking_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rm, _ = _make_redis_manager(lock_acquired=True)
    monkeypatch.setattr(redis_settings, "STREAM_CLEANUP_BATCH_SIZE", 10)
    secret_detail = "redis://:super-secret-password@internal:6379/0"
    event_calls = 0

    async def fake_maintain(
        _redis: Any,
        stream: str,
        *,
        apply: bool = False,
    ) -> dict[str, int]:
        nonlocal event_calls
        assert apply is True
        if stream == "event_stream":
            event_calls += 1
            if event_calls == 1:
                return _make_maintain_result(
                    retained=20, completed_eligible=10, dlq_eligible=1
                )
            raise RuntimeError(secret_detail)
        return _make_maintain_result(retained=1, completed_eligible=3)

    monkeypatch.setattr(maintainer_module, "maintain", fake_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        max_batches_per_tick=3,
        streams=("event_stream", "webhook_stream"),
    )

    with caplog.at_level(logging.WARNING, logger=maintainer_module.__name__):
        summaries = await maintainer.maintain_once()

    assert summaries is not None
    assert "event_stream" not in summaries
    assert summaries["webhook_stream"].completed_deleted == 3
    assert "RuntimeError" in caplog.text
    assert "after batches=1 completed_deleted=10 dlq_deleted=1" in caplog.text
    assert secret_detail not in caplog.text
    assert "super-secret-password" not in caplog.text


@pytest.mark.asyncio
async def test_maintain_once_handles_connection_exception_without_leaking_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_detail = "redis://:super-secret-password@internal:6379/0"
    rm = AsyncMock()
    rm.get_connection = AsyncMock(side_effect=RuntimeError(secret_detail))

    maintainer = RedisStreamMaintainer(rm, enabled=True)

    with caplog.at_level(logging.WARNING, logger=maintainer_module.__name__):
        result = await maintainer.maintain_once()

    assert result is None
    assert "RuntimeError" in caplog.text
    assert secret_detail not in caplog.text
    assert "super-secret-password" not in caplog.text


@pytest.mark.asyncio
async def test_maintain_once_handles_lock_acquisition_timeout_safely(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = AsyncMock()

    async def slow_set(*_args: Any, **_kwargs: Any) -> bool:
        await asyncio.sleep(1.0)
        return True

    conn.set = AsyncMock(side_effect=slow_set)
    rm, _ = _make_redis_manager(connection=conn)
    mock_maintain = AsyncMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(maintainer_module, "maintain", mock_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        streams=("event_stream",),
        operation_timeout_seconds=0.02,
    )

    with caplog.at_level(logging.WARNING, logger=maintainer_module.__name__):
        result = await maintainer.maintain_once()

    assert result == {}
    mock_maintain.assert_not_awaited()
    assert "TimeoutError" in caplog.text


@pytest.mark.asyncio
async def test_maintain_once_handles_maintain_timeout_and_isolates_streams(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rm, _ = _make_redis_manager(lock_acquired=True)

    async def fake_maintain(
        _redis: Any,
        stream: str,
        *,
        apply: bool = False,
    ) -> dict[str, int]:
        assert apply is True
        if stream == "event_stream":
            await asyncio.sleep(1.0)
        return _make_maintain_result(retained=2, completed_eligible=5)

    monkeypatch.setattr(maintainer_module, "maintain", fake_maintain)

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        streams=("event_stream", "webhook_stream"),
        operation_timeout_seconds=0.02,
    )

    with caplog.at_level(logging.WARNING, logger=maintainer_module.__name__):
        summaries = await maintainer.maintain_once()

    assert summaries is not None
    assert "event_stream" not in summaries
    assert summaries["webhook_stream"].completed_deleted == 5
    assert "TimeoutError" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["get_connection", "lock_set", "maintain"])
async def test_maintain_once_propagates_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    rm, conn = _make_redis_manager(lock_acquired=True)

    if stage == "get_connection":
        rm.get_connection = AsyncMock(side_effect=asyncio.CancelledError())
    elif stage == "lock_set":
        conn.set = AsyncMock(side_effect=asyncio.CancelledError())
    elif stage == "maintain":
        monkeypatch.setattr(
            maintainer_module,
            "maintain",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )

    maintainer = RedisStreamMaintainer(
        rm,
        enabled=True,
        streams=("event_stream",),
    )

    with pytest.raises(asyncio.CancelledError):
        await maintainer.maintain_once()


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
