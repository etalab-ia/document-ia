"""Unit tests for document_ia_worker.main graceful shutdown and crash handling."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _mock_two_ddoc_crl_refresher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent any real network CRL refresh in worker main shutdown unit tests."""
    from document_ia_worker import main as worker_main

    class NoOpCrlRefresher:
        async def startup_refresh(self) -> None:
            return None

        async def run_periodic_refresh(self, stop_event: asyncio.Event) -> None:
            await stop_event.wait()

    monkeypatch.setattr(worker_main, "TwoDDocCrlRefresher", NoOpCrlRefresher)


@pytest.mark.asyncio
async def test_worker_main_sigterm_waits_for_consumer_graceful_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lors de la réception de SIGTERM, main() doit lever _consumer.stop_flag et
    attendre la fin réelle de start_consumer() (vidange + cleanup) avant de rendre
    la main."""
    from document_ia_worker import main as worker_main

    monkeypatch.setattr(worker_main, "_prewarm_models_and_trust_store", lambda: None)

    started = asyncio.Event()
    drain_completed = False

    class FakeConsumer[T]:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.stop_flag = asyncio.Event()
            self.has_running_handlers = False

        async def start_consumer(self) -> None:
            nonlocal drain_completed
            started.set()
            await self.stop_flag.wait()
            # Simule le temps de vidange d'une tâche en vol avant la fin de start_consumer
            await asyncio.sleep(0.08)
            drain_completed = True

    monkeypatch.setattr(worker_main, "ThreadConsumer", FakeConsumer)

    main_task = asyncio.create_task(worker_main.main())
    await asyncio.wait_for(started.wait(), timeout=2.0)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(main_task, timeout=2.0)

    assert drain_completed is True
    assert worker_main._consumer is None


@pytest.mark.asyncio
async def test_worker_main_propagates_consumer_crash_and_cancels_stop_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si run_consumer() échoue de façon inattendue, main() annule stop_wait et
    relève l'exception sans laisser de tâche pendante."""
    from document_ia_worker import main as worker_main

    monkeypatch.setattr(worker_main, "_prewarm_models_and_trust_store", lambda: None)

    class CrashingConsumer[T]:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_flag = asyncio.Event()
            self.has_running_handlers = False

        async def start_consumer(self) -> None:
            raise RuntimeError("boom in start_consumer")

    monkeypatch.setattr(worker_main, "ThreadConsumer", CrashingConsumer)

    with pytest.raises(RuntimeError, match="boom in start_consumer"):
        await asyncio.wait_for(worker_main.main(), timeout=2.0)

    assert worker_main._consumer is None


@pytest.mark.asyncio
async def test_signal_stops_consumer_and_waits_for_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from document_ia_worker import main as worker_main

    monkeypatch.setattr(worker_main, "_prewarm_models_and_trust_store", lambda: None)
    monkeypatch.setattr(worker_main.logger, "disabled", True)

    created_consumers = []

    class FakeConsumer[T]:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_flag = asyncio.Event()
            self.stopped = asyncio.Event()
            self.has_running_handlers = False
            created_consumers.append(self)

        async def start_consumer(self) -> None:
            await self.stop_flag.wait()
            self.stopped.set()

    monkeypatch.setattr(worker_main, "ThreadConsumer", FakeConsumer)
    monkeypatch.setattr(
        worker_main, "_register_signal_handlers", lambda event: event.set()
    )

    await worker_main.main()

    assert len(created_consumers) == 1
    consumer = created_consumers[0]
    assert consumer.stop_flag.is_set()
    assert consumer.stopped.is_set()


@pytest.mark.asyncio
async def test_signal_cancels_consumer_after_shutdown_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from document_ia_worker import main as worker_main

    monkeypatch.setattr(worker_main, "_prewarm_models_and_trust_store", lambda: None)
    monkeypatch.setattr(worker_main.logger, "disabled", True)

    class StuckConsumer[T]:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_flag = asyncio.Event()
            self.has_running_handlers = False

        async def start_consumer(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(worker_main, "ThreadConsumer", StuckConsumer)
    monkeypatch.setattr(
        worker_main, "_register_signal_handlers", lambda event: event.set()
    )
    monkeypatch.setattr(worker_main, "CONSUMER_SHUTDOWN_TIMEOUT_S", 0.01)
    monkeypatch.setattr(worker_main, "CANCELLATION_CLEANUP_TIMEOUT_S", 0.01)
    forced_exit: list[int] = []
    monkeypatch.setattr(worker_main, "_force_exit", lambda: forced_exit.append(1))

    await asyncio.wait_for(worker_main.main(), timeout=1)

    assert forced_exit == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_runtime_closes_owned_redis_manager(monkeypatch, outcome):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from document_ia_worker import main as worker_main

    owner = SimpleNamespace(close=AsyncMock())
    passed_managers = []

    class Consumer:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, **kwargs):
            passed_managers.append(kwargs["current_redis_manager"])

        async def start_consumer(self):
            if outcome == "error":
                raise RuntimeError("handler failed")
            if outcome == "cancel":
                raise asyncio.CancelledError

    monkeypatch.setattr(worker_main, "_consumer", None)
    monkeypatch.setattr(worker_main, "_prewarm_models_and_trust_store", lambda: None)
    monkeypatch.setattr(worker_main, "RedisManager", lambda: owner)
    monkeypatch.setattr(worker_main, "ThreadConsumer", Consumer)
    if outcome == "success":
        await worker_main.run_consumer()
    else:
        exception = RuntimeError if outcome == "error" else asyncio.CancelledError
        with pytest.raises(exception):
            await worker_main.run_consumer()
    assert passed_managers == [owner]
    owner.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_signal_forces_exit_if_handler_thread_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from document_ia_worker import main as worker_main

    monkeypatch.setattr(worker_main, "_prewarm_models_and_trust_store", lambda: None)
    monkeypatch.setattr(worker_main.logger, "disabled", True)

    class RunningHandlerConsumer[T]:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_flag = asyncio.Event()
            self.has_running_handlers = True

        async def start_consumer(self) -> None:
            await self.stop_flag.wait()

    monkeypatch.setattr(worker_main, "ThreadConsumer", RunningHandlerConsumer)
    monkeypatch.setattr(
        worker_main, "_register_signal_handlers", lambda event: event.set()
    )
    forced_exit: list[int] = []
    monkeypatch.setattr(worker_main, "_force_exit", lambda: forced_exit.append(1))

    await worker_main.main()

    assert forced_exit == [1]


@pytest.mark.asyncio
async def test_consumer_crash_forces_exit_if_handler_thread_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from document_ia_worker import main as worker_main

    monkeypatch.setattr(worker_main, "_prewarm_models_and_trust_store", lambda: None)
    monkeypatch.setattr(worker_main.logger, "disabled", True)

    class CrashedConsumer[T]:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_flag = asyncio.Event()
            self.has_running_handlers = True

        async def start_consumer(self) -> None:
            raise RuntimeError("consumer failed")

    monkeypatch.setattr(worker_main, "ThreadConsumer", CrashedConsumer)
    monkeypatch.setattr(worker_main, "_register_signal_handlers", lambda _event: None)
    forced_exit: list[int] = []
    monkeypatch.setattr(worker_main, "_force_exit", lambda: forced_exit.append(1))

    with pytest.raises(RuntimeError, match="consumer failed"):
        await worker_main.main()

    assert forced_exit == [1]


@pytest.mark.asyncio
async def test_run_consumer_runs_crl_startup_refresh_before_consumer_and_cleans_up_periodic_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from document_ia_worker import main as worker_main

    events: list[str] = []
    periodic_started = asyncio.Event()
    periodic_finished = asyncio.Event()

    class TrackingCrlRefresher:
        async def startup_refresh(self) -> None:
            events.append("startup_refresh")

        async def run_periodic_refresh(self, stop_event: asyncio.Event) -> None:
            events.append("periodic_started")
            periodic_started.set()
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                events.append("periodic_cancelled")
                raise
            finally:
                periodic_finished.set()

    class TrackingConsumer[T]:
        def __init__(self, **_kwargs: Any) -> None:
            self.stop_flag = asyncio.Event()
            self.has_running_handlers = False

        async def start_consumer(self) -> None:
            events.append("start_consumer")
            await asyncio.wait_for(periodic_started.wait(), timeout=2.0)

    owner = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(worker_main, "_consumer", None)
    monkeypatch.setattr(worker_main, "_prewarm_models_and_trust_store", lambda: None)
    monkeypatch.setattr(worker_main, "TwoDDocCrlRefresher", TrackingCrlRefresher)
    monkeypatch.setattr(worker_main, "RedisManager", lambda: owner)
    monkeypatch.setattr(worker_main, "ThreadConsumer", TrackingConsumer)

    await asyncio.wait_for(worker_main.run_consumer(), timeout=2.0)

    assert events[:3] == ["startup_refresh", "start_consumer", "periodic_started"]
    assert periodic_finished.is_set()
    owner.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_consumer_passes_reclaim_drain_and_requeue_on_shutdown_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_consumer() doit transmettre reclaim_idle_ms, drain_timeout_s et
    requeue_on_shutdown=True au ThreadConsumer et aligner CONSUMER_SHUTDOWN_TIMEOUT_S."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from document_ia_infra.redis.redis_settings import redis_settings
    from document_ia_worker import main as worker_main

    captured_kwargs: dict[str, Any] = {}

    class CapturingConsumer[T]:
        def __init__(self, **kwargs: Any) -> None:
            captured_kwargs.update(kwargs)
            self.stop_flag = asyncio.Event()
            self.has_running_handlers = False

        async def start_consumer(self) -> None:
            return None

    owner = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(worker_main, "_consumer", None)
    monkeypatch.setattr(worker_main, "_prewarm_models_and_trust_store", lambda: None)
    monkeypatch.setattr(worker_main, "RedisManager", lambda: owner)
    monkeypatch.setattr(worker_main, "ThreadConsumer", CapturingConsumer)

    await worker_main.run_consumer()

    assert (
        worker_main.CONSUMER_SHUTDOWN_TIMEOUT_S
        == redis_settings.WORKER_SHUTDOWN_TIMEOUT_S
    )
    assert (
        captured_kwargs["reclaim_idle_ms"]
        == redis_settings.EVENT_STREAM_RECLAIM_IDLE_MS
    )
    assert captured_kwargs["drain_timeout_s"] == redis_settings.WORKER_DRAIN_TIMEOUT_S
    assert captured_kwargs["requeue_on_shutdown"] is True
