"""Unit and Redis integration tests for 90s reclaim settings and graceful-shutdown requeue."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis

from document_ia_infra.redis.asyncio_consumer import AsyncIoConsumer
from document_ia_infra.redis.base_consumer import RECLAIM_IDLE_MS, InFlightContext
from document_ia_infra.redis.progress_tracker import (
    ProgressTracker,
    get_current_progress_tracker,
)
from document_ia_infra.redis.redis_manager import RedisManager
from document_ia_infra.redis.redis_settings import RedisSettings
from document_ia_infra.redis.serializable_message import SerializableMessage
from document_ia_infra.redis.thread_consumer import ThreadConsumer


class ShutdownTestMessage(SerializableMessage):
    def __init__(self, workflow_execution_id: str, payload: str = "ok") -> None:
        self.workflow_execution_id = workflow_execution_id
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_execution_id": self.workflow_execution_id,
            "payload": self.payload,
        }

    @staticmethod
    def from_json(data: str) -> ShutdownTestMessage:
        parsed = json.loads(data)
        return ShutdownTestMessage(
            workflow_execution_id=parsed["workflow_execution_id"],
            payload=parsed.get("payload", "ok"),
        )


class LocalRedisManager(RedisManager):
    def __init__(self, client: Redis) -> None:
        super().__init__()
        self._client = client

    async def get_connection(self) -> Redis:
        return self._client

    async def close(self) -> None:
        return None


@pytest.fixture
async def redis_client() -> AsyncGenerator[Redis, None]:
    client = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not available: {exc}")
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def stream(redis_client: Redis) -> AsyncGenerator[str, None]:
    name = f"test-graceful-shutdown:{uuid4()}"
    try:
        yield name
    finally:
        await redis_client.delete(
            name,
            f"{name}:dlq",
            f"{name}:retry_delayed",
            f"{name}:retry_delayed:payloads",
        )


async def _wait_until(predicate: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                break
            await asyncio.sleep(0.02)


def _pending_count(info: Any) -> int:
    return int(info["pending"] if isinstance(info, dict) else info.pending)


# ---------------------------------------------------------------------------
# Unit tests: Settings & Heartbeat cadence
# ---------------------------------------------------------------------------


def test_redis_settings_reclaim_and_worker_shutdown_defaults() -> None:
    settings = RedisSettings()
    assert settings.EVENT_STREAM_RECLAIM_IDLE_MS == 90000
    assert settings.WORKER_DRAIN_TIMEOUT_S == 20.0
    assert settings.WORKER_SHUTDOWN_TIMEOUT_S == 24.0
    # Legacy constant in base_consumer must stay untouched for backward compatibility.
    assert RECLAIM_IDLE_MS == 300000


def test_redis_settings_reclaim_and_worker_shutdown_validation() -> None:
    # Equal drain and shutdown timeouts are allowed.
    equal = RedisSettings(WORKER_DRAIN_TIMEOUT_S=20.0, WORKER_SHUTDOWN_TIMEOUT_S=20.0)
    assert equal.WORKER_DRAIN_TIMEOUT_S == 20.0
    assert equal.WORKER_SHUTDOWN_TIMEOUT_S == 20.0

    # Shutdown timeout strictly less than drain timeout must be rejected.
    with pytest.raises(ValidationError, match="WORKER_SHUTDOWN_TIMEOUT_S"):
        RedisSettings(WORKER_DRAIN_TIMEOUT_S=25.0, WORKER_SHUTDOWN_TIMEOUT_S=24.0)

    # EVENT_STREAM_RECLAIM_IDLE_MS bounds
    with pytest.raises(ValidationError):
        RedisSettings(EVENT_STREAM_RECLAIM_IDLE_MS=999)

    # Non-finite values (NaN / +inf / -inf) must be rejected.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            RedisSettings(WORKER_DRAIN_TIMEOUT_S=bad)
        with pytest.raises(ValidationError):
            RedisSettings(WORKER_SHUTDOWN_TIMEOUT_S=bad)

    # Out-of-range finite values must be rejected.
    with pytest.raises(ValidationError):
        RedisSettings(WORKER_DRAIN_TIMEOUT_S=-1.0)
    with pytest.raises(ValidationError):
        RedisSettings(WORKER_DRAIN_TIMEOUT_S=301.0)
    with pytest.raises(ValidationError):
        RedisSettings(WORKER_SHUTDOWN_TIMEOUT_S=0.5)
    with pytest.raises(ValidationError):
        RedisSettings(WORKER_SHUTDOWN_TIMEOUT_S=301.0)


def test_thread_consumer_heartbeat_interval_stays_30s_with_90s_reclaim() -> None:
    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name="worker-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=10,
        block_time=10000,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        reclaim_idle_ms=90000,
        lease_ttl_ms=60000,
        use_execution_lease=True,
        max_processing_time_s=120.0,
        step_progress_timeout_s=90.0,
        requeue_on_shutdown=True,
    )
    assert consumer.reclaim_idle_ms == 90000
    assert consumer._heartbeat_interval_s == 30.0
    assert consumer.requeue_on_shutdown is True


def test_asyncio_consumer_forwards_requeue_on_shutdown() -> None:
    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    default_consumer = AsyncIoConsumer[ShutdownTestMessage](
        consumer_name="api-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=5,
        block_time=1000,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
    )
    assert default_consumer.requeue_on_shutdown is False

    enabled_consumer = AsyncIoConsumer[ShutdownTestMessage](
        consumer_name="api-2",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=5,
        block_time=1000,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        requeue_on_shutdown=True,
    )
    assert enabled_consumer.requeue_on_shutdown is True


# ---------------------------------------------------------------------------
# Unit tests: _abort_unfinished_futures ordering, fail-closed & compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_retries", [0, 1])
async def test_abort_unfinished_futures_requeues_immediately_preserving_retries(
    initial_retries: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fences first, releases lease, requeues immediately with unchanged retries, then ACKs."""
    events: list[str] = []

    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name="worker-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=1,
        block_time=10,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        use_execution_lease=True,
        requeue_on_shutdown=True,
    )
    consumer.redis = AsyncMock()

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    msg_id = "1700000000000-0"
    exec_id = "exec-shutdown-1"
    token = "worker-1:tok-1"
    secret_payload = "SENSITIVE_PII_DO_NOT_LOG"
    fields = {
        "data": json.dumps(
            {"workflow_execution_id": exec_id, "payload": secret_payload}
        ),
        "retries": str(initial_retries),
    }
    tracker = ProgressTracker(msg_id=msg_id, execution_id=exec_id, lease_token=token)
    ctx = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=tracker,
        future=fut,
        execution_id=exec_id,
        lease_token=token,
    )
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = ctx

    async def fake_release_lease(
        _redis: Any, execution_id: str, lease_token: str
    ) -> bool:
        assert tracker.is_aborted
        assert tracker.abort_reason == "shutdown"
        assert ctx.handled is True
        events.append(f"release_lease:{execution_id}:{lease_token}")
        return True

    async def fake_requeue_once(
        _redis: Any,
        entry_id: str,
        _fields: Any,
        attempts: int,
        *,
        immediate: bool = False,
    ) -> bool:
        events.append(f"requeue:{entry_id}:{attempts}:{immediate}")
        return True

    async def fake_ack(_redis: Any, entry_id: str) -> None:
        events.append(f"ack:{entry_id}")

    consumer.delivery.release_lease = fake_release_lease  # type: ignore[method-assign]
    consumer._requeue_once = fake_requeue_once  # type: ignore[method-assign]
    consumer.delivery.ack = fake_ack  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO):
        await consumer._abort_unfinished_futures([fut])

    assert events == [
        f"release_lease:{exec_id}:{token}",
        f"requeue:{msg_id}:{initial_retries}:True",
        f"ack:{msg_id}",
    ]
    assert msg_id not in consumer._in_flight_ids
    assert secret_payload not in caplog.text
    assert any(
        msg_id in rec.message and f"retries={initial_retries}" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_abort_unfinished_futures_fail_closed_on_requeue_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When requeue fails, ACK must NOT be called and no PII may be logged."""

    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name="worker-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=1,
        block_time=10,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        use_execution_lease=True,
        requeue_on_shutdown=True,
    )
    consumer.redis = AsyncMock()

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    msg_id = "1700000000001-0"
    secret_payload = "SECRET_PII_REQUEUE_ERR"
    fields = {
        "data": json.dumps(
            {"workflow_execution_id": "exec-2", "payload": secret_payload}
        ),
        "retries": "1",
    }
    tracker = ProgressTracker(msg_id=msg_id, execution_id="exec-2", lease_token="tok-2")
    ctx = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=tracker,
        future=fut,
        execution_id="exec-2",
        lease_token="tok-2",
    )
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = ctx

    release_mock = AsyncMock(return_value=True)
    requeue_mock = AsyncMock(side_effect=ConnectionError("redis down"))
    ack_mock = AsyncMock()
    consumer.delivery.release_lease = release_mock  # type: ignore[method-assign]
    consumer._requeue_once = requeue_mock  # type: ignore[method-assign]
    consumer.delivery.ack = ack_mock  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        await consumer._abort_unfinished_futures([fut])

    assert tracker.is_aborted
    assert tracker.abort_reason == "shutdown"
    assert ctx.handled is True
    release_mock.assert_awaited_once()
    requeue_mock.assert_awaited_once()
    ack_mock.assert_not_awaited()
    assert msg_id in consumer._in_flight_ids
    assert secret_payload not in caplog.text
    assert any(
        msg_id in rec.message and "ConnectionError" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_abort_unfinished_futures_fail_closed_on_release_lease_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When release_lease raises, neither requeue nor ACK may run."""

    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name="worker-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=1,
        block_time=10,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        use_execution_lease=True,
        requeue_on_shutdown=True,
    )
    consumer.redis = AsyncMock()

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    msg_id = "1700000000002-0"
    fields = {"data": json.dumps({"workflow_execution_id": "exec-3"}), "retries": "0"}
    tracker = ProgressTracker(msg_id=msg_id, execution_id="exec-3", lease_token="tok-3")
    ctx = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=tracker,
        future=fut,
        execution_id="exec-3",
        lease_token="tok-3",
    )
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = ctx

    release_mock = AsyncMock(side_effect=RuntimeError("lease lua failed"))
    requeue_mock = AsyncMock(return_value=True)
    ack_mock = AsyncMock()
    consumer.delivery.release_lease = release_mock  # type: ignore[method-assign]
    consumer._requeue_once = requeue_mock  # type: ignore[method-assign]
    consumer.delivery.ack = ack_mock  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        await consumer._abort_unfinished_futures([fut])

    assert tracker.is_aborted
    assert tracker.abort_reason == "shutdown"
    assert ctx.handled is True
    release_mock.assert_awaited_once()
    requeue_mock.assert_not_awaited()
    ack_mock.assert_not_awaited()
    assert msg_id in consumer._in_flight_ids
    assert any(
        msg_id in rec.message and "RuntimeError" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_abort_unfinished_futures_fail_closed_when_lease_not_owned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When release_lease returns False (lease no longer owned), neither requeue nor ACK may run."""

    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name="worker-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=1,
        block_time=10,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        use_execution_lease=True,
        requeue_on_shutdown=True,
    )
    consumer.redis = AsyncMock()

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    msg_id = "1700000000002-1"
    secret_payload = "SECRET_PII_LEASE_NOT_OWNED"
    fields = {
        "data": json.dumps(
            {"workflow_execution_id": "exec-3b", "payload": secret_payload}
        ),
        "retries": "0",
    }
    tracker = ProgressTracker(
        msg_id=msg_id, execution_id="exec-3b", lease_token="tok-3b"
    )
    ctx = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=tracker,
        future=fut,
        execution_id="exec-3b",
        lease_token="tok-3b",
    )
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = ctx

    release_mock = AsyncMock(return_value=False)
    requeue_mock = AsyncMock(return_value=True)
    ack_mock = AsyncMock()
    consumer.delivery.release_lease = release_mock  # type: ignore[method-assign]
    consumer._requeue_once = requeue_mock  # type: ignore[method-assign]
    consumer.delivery.ack = ack_mock  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        await consumer._abort_unfinished_futures([fut])

    assert tracker.is_aborted
    assert tracker.abort_reason == "shutdown"
    assert ctx.handled is True
    release_mock.assert_awaited_once()
    requeue_mock.assert_not_awaited()
    ack_mock.assert_not_awaited()
    assert msg_id in consumer._in_flight_ids
    assert secret_payload not in caplog.text
    assert "tok-3b" not in caplog.text
    assert "exec-3b" not in caplog.text
    assert any(
        rec.levelno == logging.WARNING
        and msg_id in rec.message
        and "not owned" in rec.message.lower()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_abort_unfinished_futures_default_false_does_not_requeue() -> None:
    """Default requeue_on_shutdown=False fences and releases lease without requeue/ACK."""

    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name="worker-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=1,
        block_time=10,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        use_execution_lease=True,
    )
    assert consumer.requeue_on_shutdown is False
    consumer.redis = AsyncMock()

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    msg_id = "1700000000003-0"
    fields = {"data": json.dumps({"workflow_execution_id": "exec-4"}), "retries": "0"}
    tracker = ProgressTracker(msg_id=msg_id, execution_id="exec-4", lease_token="tok-4")
    ctx = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=tracker,
        future=fut,
        execution_id="exec-4",
        lease_token="tok-4",
    )
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = ctx

    release_mock = AsyncMock(return_value=True)
    requeue_mock = AsyncMock(return_value=True)
    ack_mock = AsyncMock()
    consumer.delivery.release_lease = release_mock  # type: ignore[method-assign]
    consumer._requeue_once = requeue_mock  # type: ignore[method-assign]
    consumer.delivery.ack = ack_mock  # type: ignore[method-assign]

    await consumer._abort_unfinished_futures([fut])

    assert tracker.is_aborted
    assert ctx.handled is True
    release_mock.assert_awaited_once()
    requeue_mock.assert_not_awaited()
    ack_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_fenced_handler_returning_after_requeue_on_shutdown_does_not_double_ack() -> (
    None
):
    """Once requeue_on_shutdown marks ctx.handled and ACKs, late completion is a no-op."""

    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name="worker-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=1,
        block_time=10,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        use_execution_lease=True,
        requeue_on_shutdown=True,
    )
    fake_redis = AsyncMock()
    consumer.redis = fake_redis

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    msg_id = "1700000000004-0"
    fields = {"data": json.dumps({"workflow_execution_id": "exec-5"}), "retries": "1"}
    tracker = ProgressTracker(msg_id=msg_id, execution_id="exec-5", lease_token="tok-5")
    ctx = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=tracker,
        future=fut,
        execution_id="exec-5",
        lease_token="tok-5",
    )
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = ctx
    consumer._processing_futures.add(fut)
    active_futures: dict[asyncio.Future[Any], tuple[str, dict[str, Any]]] = {
        fut: (msg_id, fields)
    }

    release_mock = AsyncMock(return_value=True)
    requeue_mock = AsyncMock(return_value=True)
    ack_mock = AsyncMock()
    consumer.delivery.release_lease = release_mock  # type: ignore[method-assign]
    consumer._requeue_once = requeue_mock  # type: ignore[method-assign]
    consumer.delivery.ack = ack_mock  # type: ignore[method-assign]

    await consumer._abort_unfinished_futures([fut])
    assert ack_mock.await_count == 1
    assert release_mock.await_count == 1

    # Simulate the fenced handler thread finishing normally afterwards.
    fut.set_result(None)
    await consumer._finalize_done_futures([fut], active_futures, fake_redis)

    assert ack_mock.await_count == 1
    assert release_mock.await_count == 1
    assert msg_id not in consumer._in_flight_contexts
    assert msg_id not in consumer._in_flight_ids
    assert fut not in consumer._processing_futures


# ---------------------------------------------------------------------------
# Redis integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_retries", [0, 1])
async def test_graceful_shutdown_requeues_immediately_and_peer_resumes(
    redis_client: Redis,
    stream: str,
    initial_retries: int,
) -> None:
    """After drain_timeout_s, worker1 fences, releases lease, requeues with unchanged retries,
    ACKs original (pending==0), and worker2 immediately picks up and completes the execution."""
    group = f"group-{uuid4()}"
    exec_id = f"exec-{uuid4()}"
    original_msg_id = await redis_client.xadd(
        stream,
        {
            "data": json.dumps({"workflow_execution_id": exec_id, "payload": "doc"}),
            "retries": str(initial_retries),
        },
    )

    worker1_started = threading.Event()
    worker1_release = threading.Event()
    worker1_saw_abort: list[str | None] = []

    async def worker1_handler(
        _message: ShutdownTestMessage, _retry_count: int, _is_last: bool
    ) -> None:
        worker1_started.set()
        await asyncio.to_thread(worker1_release.wait)
        tracker = get_current_progress_tracker()
        worker1_saw_abort.append(tracker.abort_reason if tracker is not None else None)

    rm1 = LocalRedisManager(redis_client)
    worker1 = ThreadConsumer[ShutdownTestMessage](
        consumer_name=f"worker-1-{uuid4()}",
        consumer_group=group,
        stream_name=stream,
        batch_size=1,
        block_time=20,
        drain_timeout_s=0.15,
        message_class=ShutdownTestMessage,
        process_message_callable=worker1_handler,
        reclaim_idle_ms=90000,
        lease_ttl_ms=60000,
        step_progress_timeout_s=90.0,
        max_processing_time_s=120.0,
        use_execution_lease=True,
        requeue_on_shutdown=True,
        close_redis_on_stop=False,
        current_redis_manager=rm1,
    )
    worker1._heartbeat_interval_s = 0.05

    worker2_received: list[tuple[str, int]] = []
    worker2_done = threading.Event()

    async def worker2_handler(
        message: ShutdownTestMessage, retry_count: int, _is_last: bool
    ) -> None:
        worker2_received.append((message.workflow_execution_id, retry_count))
        worker2_done.set()

    rm2 = LocalRedisManager(redis_client)
    worker2 = ThreadConsumer[ShutdownTestMessage](
        consumer_name=f"worker-2-{uuid4()}",
        consumer_group=group,
        stream_name=stream,
        batch_size=1,
        block_time=20,
        drain_timeout_s=0.5,
        message_class=ShutdownTestMessage,
        process_message_callable=worker2_handler,
        reclaim_idle_ms=90000,
        lease_ttl_ms=60000,
        use_execution_lease=True,
        requeue_on_shutdown=True,
        close_redis_on_stop=False,
        current_redis_manager=rm2,
    )

    stop_task: asyncio.Task[None] | None = None
    try:
        await worker1.start()
        await _wait_until(worker1_started.is_set, timeout=3.0)

        # Verify lease is currently held by worker1.
        lease_key = worker1.delivery.execution_lease_key(exec_id)
        assert await redis_client.get(lease_key) is not None

        # Trigger graceful shutdown on worker1 while handler is still blocked.
        stop_task = asyncio.create_task(worker1.stop())

        # Wait until drain_timeout_s expires and worker1 requeues + ACKs the original message.
        async def requeued_and_acked() -> bool:
            entries = await redis_client.xrange(stream)
            has_copy = any(
                eid != original_msg_id and fields.get("original_id") == original_msg_id
                for eid, fields in entries
            )
            if not has_copy:
                return False
            pending = _pending_count(await redis_client.xpending(stream, group))
            return pending == 0

        await _wait_until(requeued_and_acked, timeout=3.0)

        # Lease must already be released and requeued entry must preserve retries.
        assert await redis_client.get(lease_key) is None
        entries = await redis_client.xrange(stream)
        copies = [
            (eid, fields)
            for eid, fields in entries
            if eid != original_msg_id and fields.get("original_id") == original_msg_id
        ]
        assert len(copies) == 1
        assert copies[0][1]["retries"] == str(initial_retries)

        # worker1.stop() must complete promptly BEFORE the blocked handler thread is released
        # (no extra drain_timeout_s + 5s / heartbeat wait), while preserving has_running_handlers.
        await asyncio.wait_for(stop_task, timeout=2.0)
        stop_task = None
        assert worker1.has_running_handlers is True

        # Release the fenced worker1 thread so it exits normally without double-ACKing or erroring.
        worker1_release.set()
        await _wait_until(
            lambda: (
                not worker1.has_running_handlers and worker1_saw_abort == ["shutdown"]
            ),
            timeout=3.0,
        )
        assert len(worker1._processing_futures) == 0

        # Start worker2: it must pick up the requeued message immediately (reclaim_idle_ms is 90s!).
        await worker2.start()
        await _wait_until(worker2_done.is_set, timeout=3.0)
        assert worker2_received == [(exec_id, initial_retries)]

        await _wait_until(
            lambda: _async_pending_zero(redis_client, stream, group),
            timeout=3.0,
        )
    finally:
        worker1_release.set()
        if stop_task is not None:
            await asyncio.wait_for(stop_task, timeout=5.0)
        await worker2.stop()


async def _async_pending_zero(redis_client: Redis, stream: str, group: str) -> bool:
    return _pending_count(await redis_client.xpending(stream, group)) == 0


@pytest.mark.asyncio
async def test_graceful_shutdown_fail_closed_in_redis_when_requeue_or_lease_release_fails(
    redis_client: Redis,
    stream: str,
) -> None:
    """In Redis, if requeue or lease release fails during shutdown, entry stays pending (pending==1)."""
    for failure_mode in ("requeue", "release_lease"):
        group = f"group-{failure_mode}-{uuid4()}"
        exec_id = f"exec-{failure_mode}-{uuid4()}"
        sub_stream = f"{stream}:{failure_mode}"
        try:
            await redis_client.xadd(
                sub_stream,
                {
                    "data": json.dumps({"workflow_execution_id": exec_id}),
                    "retries": "0",
                },
            )

            started = threading.Event()
            release = threading.Event()

            async def blocking_handler(
                _message: ShutdownTestMessage, _retry_count: int, _is_last: bool
            ) -> None:
                started.set()
                await asyncio.to_thread(release.wait)

            rm = LocalRedisManager(redis_client)
            consumer = ThreadConsumer[ShutdownTestMessage](
                consumer_name=f"worker-{failure_mode}-{uuid4()}",
                consumer_group=group,
                stream_name=sub_stream,
                batch_size=1,
                block_time=20,
                drain_timeout_s=0.15,
                message_class=ShutdownTestMessage,
                process_message_callable=blocking_handler,
                reclaim_idle_ms=90000,
                lease_ttl_ms=60000,
                use_execution_lease=True,
                requeue_on_shutdown=True,
                close_redis_on_stop=False,
                current_redis_manager=rm,
            )
            consumer._heartbeat_interval_s = 0.05

            if failure_mode == "requeue":
                consumer._requeue_once = AsyncMock(  # type: ignore[method-assign]
                    side_effect=ConnectionError("simulated redis requeue failure")
                )
            else:
                consumer.delivery.release_lease = AsyncMock(  # type: ignore[method-assign]
                    side_effect=ConnectionError("simulated redis lease failure")
                )

            stop_task: asyncio.Task[None] | None = None
            try:
                await consumer.start()
                await _wait_until(started.is_set, timeout=3.0)

                stop_task = asyncio.create_task(consumer.stop())
                await asyncio.sleep(0.4)
                release.set()
                await asyncio.wait_for(stop_task, timeout=5.0)
                stop_task = None

                # Original entry must still be pending in the PEL, and no duplicate stream entry added.
                assert (
                    _pending_count(await redis_client.xpending(sub_stream, group)) == 1
                )
                assert len(await redis_client.xrange(sub_stream)) == 1
            finally:
                release.set()
                if stop_task is not None:
                    await asyncio.wait_for(stop_task, timeout=5.0)
                await redis_client.delete(
                    consumer.delivery.execution_lease_key(exec_id)
                )
        finally:
            await redis_client.delete(sub_stream, f"{sub_stream}:dlq")


@pytest.mark.asyncio
async def test_abort_unfinished_futures_reconnects_when_redis_was_invalidated() -> None:
    """If self.redis was invalidated (None) prior to shutdown abort, _ensure_connection() is used."""

    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    reconnected_redis = AsyncMock()
    reconnect_rm = LocalRedisManager(reconnected_redis)

    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name="worker-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=1,
        block_time=10,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        use_execution_lease=True,
        requeue_on_shutdown=True,
        current_redis_manager=reconnect_rm,
    )
    consumer.redis = None

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    msg_id = "1700000000005-0"
    exec_id = "exec-reconnect-1"
    token = "worker-1:tok-reconnect"
    fields = {"data": json.dumps({"workflow_execution_id": exec_id}), "retries": "1"}
    tracker = ProgressTracker(msg_id=msg_id, execution_id=exec_id, lease_token=token)
    ctx = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=tracker,
        future=fut,
        execution_id=exec_id,
        lease_token=token,
    )
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = ctx

    release_mock = AsyncMock(return_value=True)
    requeue_mock = AsyncMock(return_value=True)
    ack_mock = AsyncMock()
    consumer.delivery.release_lease = release_mock  # type: ignore[method-assign]
    consumer._requeue_once = requeue_mock  # type: ignore[method-assign]
    consumer.delivery.ack = ack_mock  # type: ignore[method-assign]

    await consumer._abort_unfinished_futures([fut])

    assert tracker.is_aborted
    assert tracker.abort_reason == "shutdown"
    assert ctx.handled is True
    release_mock.assert_awaited_once_with(reconnected_redis, exec_id, token)
    requeue_mock.assert_awaited_once_with(
        reconnected_redis, msg_id, fields, 1, immediate=True
    )
    ack_mock.assert_awaited_once_with(reconnected_redis, msg_id)
    assert msg_id not in consumer._in_flight_ids


@pytest.mark.asyncio
async def test_abort_unfinished_futures_fail_closed_when_reconnect_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When self.redis is None and _ensure_connection() raises, fail closed without leaking secrets."""

    async def noop(_msg: ShutdownTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    class FailingRedisManager(RedisManager):
        async def get_connection(self) -> Redis:
            raise ConnectionError("redis://user:SECRET_REDIS_PASSWORD@host:6379/0")

    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name="worker-1",
        consumer_group="group-1",
        stream_name="stream-1",
        batch_size=1,
        block_time=10,
        message_class=ShutdownTestMessage,
        process_message_callable=noop,
        use_execution_lease=True,
        requeue_on_shutdown=True,
        current_redis_manager=FailingRedisManager(),
    )
    consumer.redis = None

    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    msg_id = "1700000000006-0"
    secret_payload = "SENSITIVE_PAYLOAD_NO_REDIS"
    fields = {
        "data": json.dumps(
            {"workflow_execution_id": "exec-reconnect-fail", "payload": secret_payload}
        ),
        "retries": "0",
    }
    tracker = ProgressTracker(
        msg_id=msg_id, execution_id="exec-reconnect-fail", lease_token="tok-fail"
    )
    ctx = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=tracker,
        future=fut,
        execution_id="exec-reconnect-fail",
        lease_token="tok-fail",
    )
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = ctx

    release_mock = AsyncMock(return_value=True)
    requeue_mock = AsyncMock(return_value=True)
    ack_mock = AsyncMock()
    consumer.delivery.release_lease = release_mock  # type: ignore[method-assign]
    consumer._requeue_once = requeue_mock  # type: ignore[method-assign]
    consumer.delivery.ack = ack_mock  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        await consumer._abort_unfinished_futures([fut])

    assert tracker.is_aborted
    assert tracker.abort_reason == "shutdown"
    assert ctx.handled is True
    release_mock.assert_not_awaited()
    requeue_mock.assert_not_awaited()
    ack_mock.assert_not_awaited()
    assert msg_id in consumer._in_flight_ids
    assert "SECRET_REDIS_PASSWORD" not in caplog.text
    assert secret_payload not in caplog.text
    assert any(
        rec.levelno == logging.WARNING and "ConnectionError" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_thread_consumer_start_consumer_returns_promptly_after_drain_timeout_while_handler_blocked(
    redis_client: Redis,
    stream: str,
) -> None:
    """ThreadConsumer.start_consumer() (worker path) returns promptly after drain_timeout_s
    even while a fenced handler thread is still blocked, keeping has_running_handlers True."""
    group = f"group-start-consumer-{uuid4()}"
    exec_id = f"exec-start-consumer-{uuid4()}"
    original_msg_id = await redis_client.xadd(
        stream,
        {
            "data": json.dumps({"workflow_execution_id": exec_id, "payload": "doc"}),
            "retries": "0",
        },
    )

    started = threading.Event()
    release = threading.Event()
    saw_abort: list[str | None] = []

    async def blocking_handler(
        _message: ShutdownTestMessage, _retry_count: int, _is_last: bool
    ) -> None:
        started.set()
        await asyncio.to_thread(release.wait)
        tracker = get_current_progress_tracker()
        saw_abort.append(tracker.abort_reason if tracker is not None else None)

    rm = LocalRedisManager(redis_client)
    consumer = ThreadConsumer[ShutdownTestMessage](
        consumer_name=f"worker-sc-{uuid4()}",
        consumer_group=group,
        stream_name=stream,
        batch_size=1,
        block_time=20,
        drain_timeout_s=0.15,
        message_class=ShutdownTestMessage,
        process_message_callable=blocking_handler,
        reclaim_idle_ms=90000,
        lease_ttl_ms=60000,
        step_progress_timeout_s=90.0,
        max_processing_time_s=120.0,
        use_execution_lease=True,
        requeue_on_shutdown=True,
        close_redis_on_stop=False,
        current_redis_manager=rm,
    )
    consumer._heartbeat_interval_s = 0.05

    consumer_task = asyncio.create_task(consumer.start_consumer())
    lease_key = consumer.delivery.execution_lease_key(exec_id)
    try:
        await _wait_until(started.is_set, timeout=3.0)
        assert await redis_client.get(lease_key) is not None

        # Trigger graceful shutdown like document-ia-worker SIGTERM handler.
        consumer.stop_flag.set()

        # Must return well before drain_timeout_s + 5s (no _drain_pending / heartbeat stall).
        await asyncio.wait_for(consumer_task, timeout=2.0)

        assert consumer.has_running_handlers is True
        assert consumer._draining is False
        assert consumer.heartbeat_task is not None and consumer.heartbeat_task.done()
        assert await redis_client.get(lease_key) is None
        assert _pending_count(await redis_client.xpending(stream, group)) == 0
        entries = await redis_client.xrange(stream)
        copies = [
            (eid, fields)
            for eid, fields in entries
            if eid != original_msg_id and fields.get("original_id") == original_msg_id
        ]
        assert len(copies) == 1
        assert copies[0][1]["retries"] == "0"

        release.set()
        await _wait_until(
            lambda: not consumer.has_running_handlers and saw_abort == ["shutdown"],
            timeout=3.0,
        )
        assert len(consumer._processing_futures) == 0
    finally:
        release.set()
        consumer.stop_flag.set()
        if not consumer_task.done():
            await asyncio.wait_for(consumer_task, timeout=5.0)
        await redis_client.delete(lease_key)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_retries", [0, 1])
async def test_asyncio_consumer_requeue_on_shutdown_fences_releases_lease_requeues_and_acks(
    redis_client: Redis,
    stream: str,
    initial_retries: int,
) -> None:
    """AsyncIoConsumer with use_execution_lease=True and requeue_on_shutdown=True fences
    with abort_reason='shutdown', cancels the coroutine, releases lease, requeues immediately
    with unchanged retries, and ACKs the original entry (pending==0)."""
    group = f"group-asyncio-{uuid4()}"
    exec_id = f"exec-asyncio-{uuid4()}"
    original_msg_id = await redis_client.xadd(
        stream,
        {
            "data": json.dumps({"workflow_execution_id": exec_id, "payload": "doc"}),
            "retries": str(initial_retries),
        },
    )

    started = asyncio.Event()
    never_finishes = asyncio.Event()
    observed_in_finally: list[tuple[bool, bool, str | None]] = []

    async def blocking_coro_handler(
        _message: ShutdownTestMessage, _retry_count: int, _is_last: bool
    ) -> None:
        started.set()
        cancelled = False
        try:
            await never_finishes.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            tracker = get_current_progress_tracker()
            observed_in_finally.append(
                (
                    cancelled,
                    tracker.is_aborted if tracker is not None else False,
                    tracker.abort_reason if tracker is not None else None,
                )
            )

    rm = LocalRedisManager(redis_client)
    consumer = AsyncIoConsumer[ShutdownTestMessage](
        consumer_name=f"api-asyncio-{uuid4()}",
        consumer_group=group,
        stream_name=stream,
        batch_size=1,
        block_time=20,
        drain_timeout_s=0.15,
        message_class=ShutdownTestMessage,
        process_message_callable=blocking_coro_handler,
        reclaim_idle_ms=90000,
        lease_ttl_ms=60000,
        use_execution_lease=True,
        requeue_on_shutdown=True,
        close_redis_on_stop=False,
        current_redis_manager=rm,
    )
    consumer._heartbeat_interval_s = 0.05
    lease_key = consumer.delivery.execution_lease_key(exec_id)

    try:
        await consumer.start()
        await asyncio.wait_for(started.wait(), timeout=3.0)
        assert await redis_client.get(lease_key) is not None

        await asyncio.wait_for(consumer.stop(), timeout=2.0)

        assert observed_in_finally == [(True, True, "shutdown")]
        assert consumer.has_running_handlers is False
        assert len(consumer._processing_futures) == 0
        assert await redis_client.get(lease_key) is None
        assert _pending_count(await redis_client.xpending(stream, group)) == 0

        entries = await redis_client.xrange(stream)
        copies = [
            (eid, fields)
            for eid, fields in entries
            if eid != original_msg_id and fields.get("original_id") == original_msg_id
        ]
        assert len(copies) == 1
        assert copies[0][1]["retries"] == str(initial_retries)
    finally:
        never_finishes.set()
        await consumer.stop()
        await redis_client.delete(lease_key)
