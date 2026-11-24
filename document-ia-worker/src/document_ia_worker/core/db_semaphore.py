"""Process-wide semaphore bounding concurrent PostgreSQL connections opened by a worker."""

import asyncio
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from document_ia_infra.data.data_settings import database_settings
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


@asynccontextmanager
async def acquire_worker_db_slot() -> AsyncGenerator[None]:
    """Acquire a process-wide worker database connection slot across all worker threads."""
    sem = _get_worker_db_semaphore()
    await asyncio.to_thread(sem.acquire)
    try:
        yield
    finally:
        sem.release()
