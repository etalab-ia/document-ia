"""Process-wide semaphore bounding concurrent PostgreSQL connections opened by a worker."""

import asyncio
import contextlib
import inspect
import threading
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from document_ia_infra.data.data_settings import database_settings
from document_ia_infra.exception.retryable_exception import RetryableException
from document_ia_infra.redis.redis_settings import redis_settings

_lock = threading.Lock()
_semaphore: threading.BoundedSemaphore | None = None
_semaphore_limit: int = 0


def get_effective_worker_db_max_connections() -> int:
    """Return the effective worker DB semaphore limit.

    Uses ``database_settings.WORKER_DB_MAX_CONNECTIONS`` when explicitly set,
    otherwise falls back to ``redis_settings.REDIS_WORKER_NUMBER``.
    """
    if database_settings.WORKER_DB_MAX_CONNECTIONS is not None:
        return max(1, int(database_settings.WORKER_DB_MAX_CONNECTIONS))
    return max(1, int(redis_settings.REDIS_WORKER_NUMBER))


def _get_worker_db_semaphore() -> threading.BoundedSemaphore:
    global _semaphore, _semaphore_limit
    configured_limit = get_effective_worker_db_max_connections()
    with _lock:
        if _semaphore is None or _semaphore_limit != configured_limit:
            _semaphore = threading.BoundedSemaphore(configured_limit)
            _semaphore_limit = configured_limit
        return _semaphore


def reset_worker_db_semaphore() -> None:
    """Reset the worker database semaphore so it is recreated on next acquisition."""
    global _semaphore, _semaphore_limit
    with _lock:
        _semaphore = None
        _semaphore_limit = 0


async def close_session_safely(session: Any) -> None:
    """Close a database session without propagating errors."""
    close_fn = getattr(session, "close", None)
    if callable(close_fn):
        with contextlib.suppress(Exception):
            maybe_coro = close_fn()
            if inspect.isawaitable(maybe_coro):
                await maybe_coro


@asynccontextmanager
async def acquire_worker_db_slot(
    timeout_seconds: float | None = None,
) -> AsyncGenerator[None]:
    """Acquire a process-wide worker database connection slot across all worker threads."""
    sem = _get_worker_db_semaphore()
    if sem.acquire(blocking=False):
        try:
            yield
        finally:
            sem.release()
        return

    effective_timeout = (
        float(database_settings.DB_POOL_TIMEOUT)
        if timeout_seconds is None
        else timeout_seconds
    )
    cancelled = threading.Event()
    state_lock = threading.Lock()
    state_acquired = False

    def _acquire_with_cancel() -> bool:
        nonlocal state_acquired
        deadline = time.monotonic() + effective_timeout
        while not cancelled.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if sem.acquire(timeout=min(0.1, remaining)):
                with state_lock:
                    if cancelled.is_set():
                        sem.release()
                        return False
                    state_acquired = True
                    return True
        return False

    acquired = False
    try:
        acquired = await asyncio.to_thread(_acquire_with_cancel)
    except BaseException:
        with state_lock:
            cancelled.set()
            if state_acquired:
                state_acquired = False
                sem.release()
        raise

    if not acquired:
        raise RetryableException(
            "Timed out waiting for a worker database connection slot"
        )

    try:
        yield
    finally:
        sem.release()
