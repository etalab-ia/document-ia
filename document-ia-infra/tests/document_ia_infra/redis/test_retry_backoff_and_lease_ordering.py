"""Unit and Redis integration tests for retry backoff ZSET and lease-release-before-requeue ordering."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis

from document_ia_infra.exception.retryable_exception import RetryableException
from document_ia_infra.redis.asyncio_consumer import AsyncIoConsumer
from document_ia_infra.redis.base_consumer import InFlightContext
from document_ia_infra.redis.progress_tracker import ProgressTracker
from document_ia_infra.redis.redis_manager import RedisManager
from document_ia_infra.redis.redis_settings import RedisSettings
from document_ia_infra.redis.serializable_message import SerializableMessage
from document_ia_infra.redis.stream_delivery import StreamDelivery
from document_ia_infra.redis.thread_consumer import ThreadConsumer


class RetryTestMessage(SerializableMessage):
    def __init__(self, workflow_execution_id: str, payload: str = "ok"):
        self.workflow_execution_id = workflow_execution_id
        self.payload = payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_execution_id": self.workflow_execution_id,
            "payload": self.payload,
        }

    @staticmethod
    def from_json(data: str) -> "RetryTestMessage":
        parsed = json.loads(data)
        return RetryTestMessage(
            workflow_execution_id=parsed["workflow_execution_id"],
            payload=parsed.get("payload", "ok"),
        )


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
    name = f"test-retry-backoff:{uuid4()}"
    try:
        yield name
    finally:
        await redis_client.delete(
            name,
            f"{name}:dlq",
            f"{name}:retry_delayed",
            f"{name}:retry_delayed:payloads",
        )


def _make_redis_manager(client: Redis) -> RedisManager:
    manager = RedisManager()

    async def _get_connection() -> Redis:
        return client

    manager.get_connection = _get_connection  # type: ignore[method-assign]
    return manager


async def _wait_until(predicate: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            result = predicate()
            if asyncio.iscoroutine(result):
                result = await result
            if result:
                break
            await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Unit tests: Settings & Consumer configuration
# ---------------------------------------------------------------------------


def test_redis_settings_retry_defaults() -> None:
    settings = RedisSettings()
    assert settings.WORKFLOW_MAX_RETRY_NUMBER == 4
    assert settings.WORKFLOW_RETRY_BACKOFF_DELAYS_S == [0.0, 1.0, 10.0]
    assert settings.RETRY_DELAYED_POLL_INTERVAL_S == 0.5


def test_redis_settings_retry_validation() -> None:
    settings_csv = RedisSettings(WORKFLOW_RETRY_BACKOFF_DELAYS_S="0.0, 2.5, 15.0")  # type: ignore[arg-type]
    assert settings_csv.WORKFLOW_RETRY_BACKOFF_DELAYS_S == [0.0, 2.5, 15.0]

    settings_json = RedisSettings(WORKFLOW_RETRY_BACKOFF_DELAYS_S="[0.0, 0.5]")  # type: ignore[arg-type]
    assert settings_json.WORKFLOW_RETRY_BACKOFF_DELAYS_S == [0.0, 0.5]

    with pytest.raises(ValidationError):
        RedisSettings(WORKFLOW_RETRY_BACKOFF_DELAYS_S=[0.0, -1.0])

    with pytest.raises(ValidationError):
        RedisSettings(WORKFLOW_MAX_RETRY_NUMBER=0)

    with pytest.raises(ValidationError):
        RedisSettings(RETRY_DELAYED_POLL_INTERVAL_S=0.0)

    with pytest.raises(ValidationError):
        RedisSettings(RETRY_DELAYED_POLL_INTERVAL_S=31.0)


def test_redis_settings_retry_backoff_env_var_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKFLOW_RETRY_BACKOFF_DELAYS_S", "0.0, 2.5, 15.0")
    assert RedisSettings().WORKFLOW_RETRY_BACKOFF_DELAYS_S == [0.0, 2.5, 15.0]

    monkeypatch.setenv("WORKFLOW_RETRY_BACKOFF_DELAYS_S", "[0.0, 0.5]")
    assert RedisSettings().WORKFLOW_RETRY_BACKOFF_DELAYS_S == [0.0, 0.5]

    monkeypatch.setenv("WORKFLOW_RETRY_BACKOFF_DELAYS_S", "   ")
    assert RedisSettings().WORKFLOW_RETRY_BACKOFF_DELAYS_S == []

    monkeypatch.setenv("WORKFLOW_RETRY_BACKOFF_DELAYS_S", "0.0, -1.0")
    with pytest.raises(ValidationError):
        RedisSettings()

    monkeypatch.setenv("WORKFLOW_RETRY_BACKOFF_DELAYS_S", "0.0, not-a-float")
    with pytest.raises(ValidationError):
        RedisSettings()


def test_consumer_backoff_delay_lookup_and_validation() -> None:
    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c1",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 1.0, 10.0),
        delayed_retry_poll_interval_s=0.2,
    )
    assert consumer._get_retry_backoff_delay_s(1) == 0.0
    assert consumer._get_retry_backoff_delay_s(2) == 1.0
    assert consumer._get_retry_backoff_delay_s(3) == 10.0
    assert consumer._get_retry_backoff_delay_s(4) == 10.0

    consumer_empty = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c2",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        retry_backoff_delays_s=(),
    )
    assert consumer_empty._get_retry_backoff_delay_s(2) == 0.0

    with pytest.raises(ValueError, match="retry_backoff_delays_s"):
        ThreadConsumer[RetryTestMessage](
            consumer_name="c3",
            consumer_group="g1",
            stream_name="s1",
            batch_size=1,
            block_time=10,
            message_class=RetryTestMessage,
            process_message_callable=noop,
            retry_backoff_delays_s=(0.0, -0.5),
        )

    with pytest.raises(ValueError, match="delayed_retry_poll_interval_s"):
        ThreadConsumer[RetryTestMessage](
            consumer_name="c4",
            consumer_group="g1",
            stream_name="s1",
            batch_size=1,
            block_time=10,
            message_class=RetryTestMessage,
            process_message_callable=noop,
            delayed_retry_poll_interval_s=0.0,
        )


# ---------------------------------------------------------------------------
# Unit tests: AC-1 Lease-release-before-requeue strict ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_done_futures_releases_lease_strictly_before_requeue() -> None:
    """AC-1: release_lease must be called strictly before requeue and only once."""
    call_order: list[str] = []

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c1",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        max_retry_number=4,
        use_execution_lease=True,
        retry_backoff_delays_s=(0.0, 1.0, 10.0),
    )

    async def fake_release_lease(_redis: Any, execution_id: str, token: str) -> bool:
        call_order.append(f"release_lease:{execution_id}:{token}")
        return True

    async def fake_requeue(
        _redis: Any, entry_id: str, _fields: dict[str, Any], retries: int
    ) -> bool:
        call_order.append(f"requeue:{entry_id}:{retries}")
        return True

    async def fake_ack(_redis: Any, entry_id: str) -> None:
        call_order.append(f"ack:{entry_id}")

    consumer.delivery.release_lease = fake_release_lease  # type: ignore[method-assign]
    consumer.delivery.requeue = fake_requeue  # type: ignore[method-assign]
    consumer.delivery.ack = fake_ack  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    fut.set_exception(RetryableException("429 Too Many Requests"))

    msg_id = "1700000000000-0"
    exec_id = "exec-order-1"
    token = "c1:tok-1"
    fields = {
        "data": json.dumps({"workflow_execution_id": exec_id}),
        "retries": "0",
    }
    tracker = ProgressTracker(
        msg_id=msg_id,
        execution_id=exec_id,
        lease_token=token,
    )
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=tracker,
        future=fut,
        execution_id=exec_id,
        lease_token=token,
    )
    active_futures: dict[asyncio.Future[Any], tuple[str, dict[str, Any]]] = {
        fut: (msg_id, fields)
    }

    await consumer._finalize_done_futures([fut], active_futures, AsyncMock())

    assert call_order == [
        f"release_lease:{exec_id}:{token}",
        f"requeue:{msg_id}:1",
        f"ack:{msg_id}",
    ]
    assert msg_id not in consumer._in_flight_ids


@pytest.mark.asyncio
async def test_finalize_done_futures_no_double_release_when_handle_raises() -> None:
    """When _handle_finished_future raises, lease is not released a second time."""
    release_calls = 0

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c1",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        use_execution_lease=True,
    )

    async def fake_release_lease(_redis: Any, _eid: str, _tok: str) -> bool:
        nonlocal release_calls
        release_calls += 1
        return True

    async def failing_handle(*_args: Any) -> None:
        raise RuntimeError("boom in handle")

    consumer.delivery.release_lease = fake_release_lease  # type: ignore[method-assign]
    consumer._handle_finished_future = failing_handle  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    fut.set_result(None)

    msg_id = "1700000000000-1"
    fields = {"data": json.dumps({"workflow_execution_id": "exec-2"}), "retries": "0"}
    consumer._in_flight_ids.add(msg_id)
    consumer._in_flight_contexts[msg_id] = InFlightContext(
        msg_id=msg_id,
        fields=fields,
        tracker=ProgressTracker(msg_id=msg_id),
        future=fut,
        execution_id="exec-2",
        lease_token="tok-2",
    )
    active_futures: dict[asyncio.Future[Any], tuple[str, dict[str, Any]]] = {
        fut: (msg_id, fields)
    }

    with pytest.raises(RuntimeError, match="boom in handle"):
        await consumer._finalize_done_futures([fut], active_futures, AsyncMock())

    assert release_calls == 1
    assert msg_id not in consumer._in_flight_ids


@pytest.mark.asyncio
async def test_requeue_once_logs_no_pii_or_presigned_url(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Delayed retry logs must contain only operational metadata, never payload/URLs."""

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c1",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        retry_backoff_delays_s=(0.0, 2.0, 10.0),
    )
    consumer.delivery.schedule_delayed_retry = AsyncMock(return_value=True)  # type: ignore[method-assign]

    secret_url = "https://minio.local/bucket/secret.pdf?X-Amz-Signature=TOPSECRET"
    fields = {
        "data": json.dumps(
            {
                "workflow_execution_id": "exec-log-check",
                "file_url": secret_url,
                "name": "Jean Dupont",
            }
        ),
        "retries": "1",
    }

    with caplog.at_level(logging.INFO):
        await consumer._requeue_once(AsyncMock(), "1700000000000-0", fields, attempts=2)

    combined = "\n".join(caplog.messages)
    assert "exec-log-check" in combined
    assert "1700000000000-0" in combined
    assert "TOPSECRET" not in combined
    assert "Jean Dupont" not in combined
    assert "minio.local" not in combined


# ---------------------------------------------------------------------------
# Real Redis Integration Tests: AC-1, AC-2, AC-3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_schedule_delayed_retry_idempotence_and_requeue_guard(
    redis_client: Redis, stream: str
) -> None:
    """AC-3: schedule_delayed_retry is idempotent, and requeue skips entries already in ZSET."""
    delivery = StreamDelivery(stream, "grp-idem", "c1")
    entry_id = "1700000000000-0"
    fields = {
        "data": json.dumps({"workflow_execution_id": "exec-idem"}),
        "root_id": entry_id,
    }
    due_at_ms = int(time.time() * 1000) + 60_000

    # 1. Schedule twice with same entry_id -> only 1 entry in ZSET
    assert await delivery.schedule_delayed_retry(
        redis_client, entry_id, fields, retries=2, due_at_ms=due_at_ms
    )
    assert await delivery.schedule_delayed_retry(
        redis_client, entry_id, fields, retries=2, due_at_ms=due_at_ms + 5000
    )
    assert await redis_client.zcard(delivery.delayed_retry_key) == 1

    # 2. Immediate requeue (e.g. from reclaim) must NOT duplicate into main stream while in ZSET
    assert await delivery.requeue(redis_client, entry_id, fields, retries=2)
    assert await redis_client.xlen(stream) == 0

    # 3. Promote when due -> moves from ZSET to main stream exactly once
    promoted = await delivery.promote_due_retries(
        redis_client, now_ms=due_at_ms + 1, batch_size=10
    )
    assert promoted == 1
    assert await redis_client.zcard(delivery.delayed_retry_key) == 0
    assert await redis_client.hlen(delivery.delayed_retry_payloads_key) == 0
    assert await redis_client.xlen(stream) == 1

    # 4. Calling schedule_delayed_retry again after promotion is a no-op (already in stream)
    assert await delivery.schedule_delayed_retry(
        redis_client, entry_id, fields, retries=2, due_at_ms=due_at_ms
    )
    assert await redis_client.zcard(delivery.delayed_retry_key) == 0


@pytest.mark.asyncio
async def test_redis_promote_due_retries_with_non_stream_id_format(
    redis_client: Redis, stream: str
) -> None:
    """QUAL-02: promote_due_retries promotes without error even if entry_id is not '<ms>-<seq>'."""
    delivery = StreamDelivery(stream, "grp-non-stream-id", "c1")
    synthetic_id = "synthetic-entry-id-not-ms-seq"
    fields = {
        "data": json.dumps({"workflow_execution_id": "exec-synthetic"}),
    }
    due_at_ms = int(time.time() * 1000) - 10

    assert await delivery.schedule_delayed_retry(
        redis_client, synthetic_id, fields, retries=2, due_at_ms=due_at_ms
    )
    assert await redis_client.zcard(delivery.delayed_retry_key) == 1

    promoted = await delivery.promote_due_retries(
        redis_client, now_ms=due_at_ms + 20, batch_size=10
    )
    assert promoted == 1
    assert await redis_client.zcard(delivery.delayed_retry_key) == 0
    assert await redis_client.hlen(delivery.delayed_retry_payloads_key) == 0
    assert await redis_client.xlen(stream) == 1


@pytest.mark.asyncio
async def test_redis_promote_due_retries_concurrent_multi_worker_atomicity(
    redis_client: Redis, stream: str
) -> None:
    """AC-3: Concurrent promote_due_retries across multiple workers promotes each message once."""
    d1 = StreamDelivery(stream, "grp-conc", "worker-1")
    d2 = StreamDelivery(stream, "grp-conc", "worker-2")
    now_ms = int(time.time() * 1000)

    count = 15
    for idx in range(count):
        eid = f"{now_ms - 1000}-{idx}"
        await d1.schedule_delayed_retry(
            redis_client,
            eid,
            {"data": json.dumps({"workflow_execution_id": f"exec-{idx}"})},
            retries=2,
            due_at_ms=now_ms - 10,
        )

    p1, p2 = await asyncio.gather(
        d1.promote_due_retries(redis_client, now_ms=now_ms, batch_size=100),
        d2.promote_due_retries(redis_client, now_ms=now_ms, batch_size=100),
    )
    assert p1 + p2 == count
    assert await redis_client.zcard(d1.delayed_retry_key) == 0
    assert await redis_client.xlen(stream) == count


@pytest.mark.asyncio
async def test_redis_four_attempts_backoff_schedule_and_dlq(
    redis_client: Redis, stream: str
) -> None:
    """AC-2: max_retry_number=4 with backoff delays executes 4 attempts and routes to DLQ."""
    group = f"grp-4att-{uuid4()}"
    exec_id = str(uuid4())
    attempts_log: list[tuple[int, bool, float]] = []

    async def failing_handler(
        _msg: RetryTestMessage, retry_count: int, is_last_retry: bool
    ) -> None:
        attempts_log.append((retry_count, is_last_retry, time.monotonic()))
        raise RetryableException("HTTP 429")

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="worker-backoff",
        consumer_group=group,
        stream_name=stream,
        batch_size=5,
        block_time=50,
        message_class=RetryTestMessage,
        process_message_callable=failing_handler,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 0.15, 0.25),
        delayed_retry_poll_interval_s=0.05,
        use_execution_lease=True,
        current_redis_manager=_make_redis_manager(redis_client),
        close_redis_on_stop=False,
    )

    await consumer.start()
    try:
        await redis_client.xadd(
            stream,
            {
                "data": json.dumps({"workflow_execution_id": exec_id}),
                "retries": "0",
            },
        )
        await _wait_until(
            lambda: redis_client.xlen(f"{stream}:dlq"),
            timeout=5.0,
        )
    finally:
        await consumer.stop()

    assert [(rc, is_last) for rc, is_last, _ in attempts_log] == [
        (0, False),
        (1, False),
        (2, False),
        (3, True),
    ]
    # Retry #2 waited at least ~0.12s (configured 0.15s) and Retry #3 waited at least ~0.20s (configured 0.25s)
    assert attempts_log[2][2] - attempts_log[1][2] >= 0.12
    assert attempts_log[3][2] - attempts_log[2][2] >= 0.20
    assert await redis_client.xlen(f"{stream}:dlq") == 1
    assert await redis_client.zcard(f"{stream}:retry_delayed") == 0


@pytest.mark.asyncio
async def test_redis_cross_worker_lease_handoff_on_immediate_retry(
    redis_client: Redis, stream: str
) -> None:
    """AC-1: Immediate retry (0.0s) after release_lease is immediately picked up with zero lease rejection."""
    group = f"grp-handoff-{uuid4()}"
    exec_id = str(uuid4())
    completed = asyncio.Event()
    call_count = 0

    async def fail_once_then_succeed(
        _msg: RetryTestMessage, retry_count: int, _is_last: bool
    ) -> None:
        nonlocal call_count
        call_count += 1
        if retry_count == 0:
            raise RetryableException("transient 429")
        completed.set()

    c1 = AsyncIoConsumer[RetryTestMessage](
        consumer_name="worker-1",
        consumer_group=group,
        stream_name=stream,
        batch_size=1,
        block_time=50,
        message_class=RetryTestMessage,
        process_message_callable=fail_once_then_succeed,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 0.2, 0.5),
        delayed_retry_poll_interval_s=0.05,
        use_execution_lease=True,
        current_redis_manager=_make_redis_manager(redis_client),
        close_redis_on_stop=False,
    )
    c2 = AsyncIoConsumer[RetryTestMessage](
        consumer_name="worker-2",
        consumer_group=group,
        stream_name=stream,
        batch_size=1,
        block_time=50,
        message_class=RetryTestMessage,
        process_message_callable=fail_once_then_succeed,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 0.2, 0.5),
        delayed_retry_poll_interval_s=0.05,
        use_execution_lease=True,
        current_redis_manager=_make_redis_manager(redis_client),
        close_redis_on_stop=False,
    )

    await c1.start()
    await c2.start()
    try:
        await redis_client.xadd(
            stream,
            {
                "data": json.dumps({"workflow_execution_id": exec_id}),
                "retries": "0",
            },
        )
        await asyncio.wait_for(completed.wait(), timeout=3.0)
        # Wait briefly for final ACK to settle
        await _wait_until(
            lambda: _pending_count_zero(redis_client, stream, group),
            timeout=3.0,
        )
    finally:
        await c1.stop()
        await c2.stop()

    assert call_count == 2
    assert await redis_client.get(StreamDelivery.execution_lease_key(exec_id)) is None


async def _pending_count_zero(redis: Redis, stream: str, group: str) -> bool:
    info = await redis.xpending(stream, group)
    pending = info["pending"] if isinstance(info, dict) else info.pending
    return pending == 0


@pytest.mark.asyncio
async def test_redis_delayed_retry_survives_worker_stop_and_restart(
    redis_client: Redis, stream: str
) -> None:
    """AC-3: A delayed retry scheduled in Redis ZSET survives worker 1 shutdown and is completed by worker 2."""
    group = f"grp-survive-{uuid4()}"
    exec_id = str(uuid4())
    scheduled_in_zset = asyncio.Event()
    completed_by_w2 = asyncio.Event()

    async def w1_handler(
        _msg: RetryTestMessage, retry_count: int, _is_last: bool
    ) -> None:
        if retry_count >= 1:
            scheduled_in_zset.set()
        raise RetryableException("transient")

    async def w2_handler(
        _msg: RetryTestMessage, retry_count: int, _is_last: bool
    ) -> None:
        assert retry_count == 2
        completed_by_w2.set()

    c1 = AsyncIoConsumer[RetryTestMessage](
        consumer_name="worker-1",
        consumer_group=group,
        stream_name=stream,
        batch_size=1,
        block_time=50,
        message_class=RetryTestMessage,
        process_message_callable=w1_handler,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 0.3, 1.0),
        delayed_retry_poll_interval_s=0.05,
        use_execution_lease=True,
        current_redis_manager=_make_redis_manager(redis_client),
        close_redis_on_stop=False,
    )

    await c1.start()
    await redis_client.xadd(
        stream,
        {"data": json.dumps({"workflow_execution_id": exec_id}), "retries": "0"},
    )
    await asyncio.wait_for(scheduled_in_zset.wait(), timeout=3.0)
    await _wait_until(
        lambda: redis_client.zcard(f"{stream}:retry_delayed"),
        timeout=3.0,
    )
    # Stop worker 1 while retry #2 is still waiting in ZSET
    await c1.stop()
    assert await redis_client.zcard(f"{stream}:retry_delayed") == 1

    # Start worker 2 -> promotes due retry from ZSET and completes it
    c2 = AsyncIoConsumer[RetryTestMessage](
        consumer_name="worker-2",
        consumer_group=group,
        stream_name=stream,
        batch_size=1,
        block_time=50,
        message_class=RetryTestMessage,
        process_message_callable=w2_handler,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 0.3, 1.0),
        delayed_retry_poll_interval_s=0.05,
        use_execution_lease=True,
        current_redis_manager=_make_redis_manager(redis_client),
        close_redis_on_stop=False,
    )
    await c2.start()
    try:
        await asyncio.wait_for(completed_by_w2.wait(), timeout=3.0)
    finally:
        await c2.stop()

    assert await redis_client.zcard(f"{stream}:retry_delayed") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_batch_size", [0, -1, -100])
async def test_promote_due_retries_rejects_non_positive_batch_size(
    invalid_batch_size: int,
) -> None:
    delivery = StreamDelivery("s-bounds", "g-bounds", "c-bounds")
    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        await delivery.promote_due_retries(
            AsyncMock(), now_ms=1000, batch_size=invalid_batch_size
        )


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_retry_backoff_rejects_non_finite_values(bad_value: float) -> None:
    with pytest.raises(ValidationError):
        RedisSettings(WORKFLOW_RETRY_BACKOFF_DELAYS_S=[0.0, bad_value])

    with pytest.raises(ValidationError):
        RedisSettings(RETRY_DELAYED_POLL_INTERVAL_S=bad_value)

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    with pytest.raises(ValueError, match="retry_backoff_delays_s"):
        AsyncIoConsumer[RetryTestMessage](
            consumer_name="c-nan",
            consumer_group="g-nan",
            stream_name="s-nan",
            batch_size=1,
            block_time=10,
            message_class=RetryTestMessage,
            process_message_callable=noop,
            retry_backoff_delays_s=(0.0, bad_value),
        )

    with pytest.raises(ValueError, match="delayed_retry_poll_interval_s"):
        AsyncIoConsumer[RetryTestMessage](
            consumer_name="c-nan2",
            consumer_group="g-nan",
            stream_name="s-nan",
            batch_size=1,
            block_time=10,
            message_class=RetryTestMessage,
            process_message_callable=noop,
            delayed_retry_poll_interval_s=bad_value,
        )


@pytest.mark.asyncio
async def test_retry_scheduling_failure_notifies_terminal_failure_after_dlq_retention() -> (
    None
):
    """When retry scheduling fails and message is retained in DLQ, on_terminal_failure must run."""
    notifications: list[tuple[str, dict[str, Any], str]] = []

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    async def on_terminal_failure(
        msg_id: str, fields: dict[str, Any], reason: str
    ) -> None:
        notifications.append((msg_id, dict(fields), reason))

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c-sched-fail",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 1.0, 10.0),
        on_terminal_failure=on_terminal_failure,
    )
    consumer.delivery.schedule_delayed_retry = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("Redis ZSET write failed")
    )
    consumer._send_to_dlq = AsyncMock(return_value=True)  # type: ignore[method-assign]
    consumer.delivery.ack = AsyncMock(return_value=None)  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    fut.set_exception(RetryableException("HTTP 429"))

    msg_id = "1700000000100-0"
    fields = {
        "data": json.dumps({"workflow_execution_id": "exec-sched-fail-1"}),
        "retries": "1",
    }
    await consumer._handle_finished_future(fut, msg_id, fields, AsyncMock())
    await consumer._drain_terminal_failure_tasks()

    assert len(notifications) == 1
    assert notifications[0][0] == msg_id
    assert notifications[0][2] == "error_requeueing_message"
    assert consumer.delivery.ack.await_count == 1


@pytest.mark.asyncio
async def test_retry_scheduling_failure_notifies_terminal_failure_even_when_ack_fails() -> (
    None
):
    """Once DLQ retention succeeds, reclaim will only ACK (never requeue), so finalization must still run."""
    notifications: list[tuple[str, dict[str, Any], str]] = []

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    async def on_terminal_failure(
        msg_id: str, fields: dict[str, Any], reason: str
    ) -> None:
        notifications.append((msg_id, dict(fields), reason))

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c-sched-fail-ack",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 1.0, 10.0),
        on_terminal_failure=on_terminal_failure,
    )
    consumer.delivery.requeue = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("Redis XADD failed")
    )
    consumer._send_to_dlq = AsyncMock(return_value=True)  # type: ignore[method-assign]
    consumer.delivery.ack = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("Redis XACK failed")
    )

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    fut.set_exception(RetryableException("HTTP 429"))

    msg_id = "1700000000101-0"
    fields = {
        "data": json.dumps({"workflow_execution_id": "exec-sched-fail-2"}),
        "retries": "0",
    }
    await consumer._handle_finished_future(fut, msg_id, fields, AsyncMock())
    await consumer._drain_terminal_failure_tasks()

    assert len(notifications) == 1
    assert notifications[0][0] == msg_id
    assert notifications[0][2] == "error_requeueing_message"


@pytest.mark.asyncio
async def test_retry_scheduling_failure_does_not_notify_when_dlq_retention_fails() -> (
    None
):
    """If DLQ retention also fails, the entry stays pending in PEL for reclaim recovery."""
    notifications: list[tuple[str, dict[str, Any], str]] = []

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    async def on_terminal_failure(
        msg_id: str, fields: dict[str, Any], reason: str
    ) -> None:
        notifications.append((msg_id, dict(fields), reason))

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c-sched-fail-no-dlq",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 1.0, 10.0),
        on_terminal_failure=on_terminal_failure,
    )
    consumer.delivery.requeue = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("Redis down")
    )
    consumer._send_to_dlq = AsyncMock(return_value=False)  # type: ignore[method-assign]
    consumer.delivery.ack = AsyncMock(return_value=None)  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    fut.set_exception(RetryableException("HTTP 429"))

    msg_id = "1700000000102-0"
    fields = {
        "data": json.dumps({"workflow_execution_id": "exec-sched-fail-3"}),
        "retries": "0",
    }
    await consumer._handle_finished_future(fut, msg_id, fields, AsyncMock())
    await consumer._drain_terminal_failure_tasks()

    assert notifications == []
    assert consumer.delivery.ack.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "retries"),
    [
        (RetryableException("HTTP 429"), "3"),  # max_retries_exceeded
        (ValueError("bad input"), "0"),  # not_retryable_error
    ],
)
async def test_handler_finalized_errors_do_not_double_notify_terminal_failure(
    exc: Exception, retries: str
) -> None:
    """When the handler already recorded terminal failure, WorkerErrorPolicy must not re-notify."""
    notifications: list[tuple[str, dict[str, Any], str]] = []

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    async def on_terminal_failure(
        msg_id: str, fields: dict[str, Any], reason: str
    ) -> None:
        notifications.append((msg_id, dict(fields), reason))

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c-no-dup",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        max_retry_number=4,
        on_terminal_failure=on_terminal_failure,
    )
    consumer._send_to_dlq = AsyncMock(return_value=True)  # type: ignore[method-assign]
    consumer.delivery.ack = AsyncMock(return_value=None)  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    fut.set_exception(exc)

    msg_id = "1700000000103-0"
    fields = {
        "data": json.dumps({"workflow_execution_id": "exec-no-dup"}),
        "retries": retries,
    }
    await consumer._handle_finished_future(fut, msg_id, fields, AsyncMock())
    await consumer._drain_terminal_failure_tasks()

    assert notifications == []
    assert consumer.delivery.ack.await_count == 1


@pytest.mark.asyncio
async def test_ambiguous_requeue_failure_with_committed_retry_skips_dlq_and_terminal_failure(
    redis_client: Redis, stream: str
) -> None:
    """If Redis committed the retry (ZSET or stream) before the reply was lost, do not DLQ or finalize."""
    notifications: list[tuple[str, dict[str, Any], str]] = []

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    async def on_terminal_failure(
        msg_id: str, fields: dict[str, Any], reason: str
    ) -> None:
        notifications.append((msg_id, dict(fields), reason))

    group = "g-ambiguous"
    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c-ambiguous",
        consumer_group=group,
        stream_name=stream,
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 1.0, 10.0),
        on_terminal_failure=on_terminal_failure,
    )
    await consumer.delivery.ensure_group(redis_client, "0-0")

    # Case 1: Delayed retry committed to ZSET before TimeoutError (lost reply)
    orig_id = await redis_client.xadd(
        stream,
        {
            "data": json.dumps({"workflow_execution_id": "exec-lost-zset"}),
            "retries": "1",
        },
    )
    await redis_client.xreadgroup(group, "c-ambiguous", {stream: ">"}, count=1)

    real_schedule = consumer.delivery.schedule_delayed_retry

    async def commit_then_timeout(
        redis: Redis,
        entry_id: str,
        fields: dict[str, Any],
        retries: int,
        due_at_ms: int,
    ) -> bool:
        await real_schedule(redis, entry_id, fields, retries, due_at_ms)
        raise TimeoutError("lost reply after ZSET commit")

    consumer.delivery.schedule_delayed_retry = commit_then_timeout  # type: ignore[method-assign]
    consumer._send_to_dlq = AsyncMock(return_value=True)  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    fut.set_exception(RetryableException("HTTP 429"))

    fields = {
        "data": json.dumps({"workflow_execution_id": "exec-lost-zset"}),
        "retries": "1",
    }
    await consumer._handle_finished_future(fut, orig_id, fields, redis_client)
    await consumer._drain_terminal_failure_tasks()

    assert consumer._send_to_dlq.await_count == 0
    assert notifications == []
    assert await redis_client.zcard(consumer.delivery.delayed_retry_key) == 1
    assert (await redis_client.xpending(stream, group))["pending"] == 0

    # Case 2: Immediate requeue committed to main stream before TimeoutError (lost reply)
    orig_id_2 = await redis_client.xadd(
        stream,
        {
            "data": json.dumps({"workflow_execution_id": "exec-lost-stream"}),
            "retries": "0",
        },
    )
    await redis_client.xreadgroup(group, "c-ambiguous", {stream: ">"}, count=1)

    real_requeue = consumer.delivery.requeue

    async def requeue_commit_then_timeout(
        redis: Redis, entry_id: str, fields: dict[str, Any], retries: int
    ) -> bool:
        await real_requeue(redis, entry_id, fields, retries)
        raise TimeoutError("lost reply after XADD commit")

    consumer.delivery.requeue = requeue_commit_then_timeout  # type: ignore[method-assign]

    fut2: asyncio.Future[Any] = loop.create_future()
    fut2.set_exception(RetryableException("HTTP 429"))
    fields2 = {
        "data": json.dumps({"workflow_execution_id": "exec-lost-stream"}),
        "retries": "0",
    }
    await consumer._handle_finished_future(fut2, orig_id_2, fields2, redis_client)
    await consumer._drain_terminal_failure_tasks()

    assert consumer._send_to_dlq.await_count == 0
    assert notifications == []
    assert (await redis_client.xpending(stream, group))["pending"] == 0


@pytest.mark.asyncio
async def test_ambiguous_requeue_failure_when_verification_fails_leaves_entry_pending() -> (
    None
):
    """If Redis is unreachable during retry-state verification, leave entry pending in PEL."""
    notifications: list[tuple[str, dict[str, Any], str]] = []

    async def noop(_msg: RetryTestMessage, _rc: int, _is_last: bool) -> None:
        return None

    async def on_terminal_failure(
        msg_id: str, fields: dict[str, Any], reason: str
    ) -> None:
        notifications.append((msg_id, dict(fields), reason))

    consumer = AsyncIoConsumer[RetryTestMessage](
        consumer_name="c-ambiguous-down",
        consumer_group="g1",
        stream_name="s1",
        batch_size=1,
        block_time=10,
        message_class=RetryTestMessage,
        process_message_callable=noop,
        max_retry_number=4,
        retry_backoff_delays_s=(0.0, 1.0, 10.0),
        on_terminal_failure=on_terminal_failure,
    )
    consumer.delivery.schedule_delayed_retry = AsyncMock(  # type: ignore[method-assign]
        side_effect=ConnectionError("Redis disconnected")
    )
    consumer.delivery.has_scheduled_retry = AsyncMock(  # type: ignore[method-assign]
        side_effect=ConnectionError("Redis still disconnected")
    )
    consumer._send_to_dlq = AsyncMock(return_value=True)  # type: ignore[method-assign]
    consumer.delivery.ack = AsyncMock(return_value=None)  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    fut.set_exception(RetryableException("HTTP 429"))

    msg_id = "1700000000104-0"
    fields = {
        "data": json.dumps({"workflow_execution_id": "exec-redis-down"}),
        "retries": "1",
    }
    await consumer._handle_finished_future(fut, msg_id, fields, AsyncMock())
    await consumer._drain_terminal_failure_tasks()

    assert consumer._send_to_dlq.await_count == 0
    assert consumer.delivery.ack.await_count == 0
    assert notifications == []
