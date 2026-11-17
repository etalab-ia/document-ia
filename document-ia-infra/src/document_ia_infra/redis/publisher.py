"""
Class Publisher for Redis Stream
"""

import asyncio
import json
import logging
from collections.abc import Awaitable
from datetime import datetime
from typing import Any, TypeVar, Generic, cast

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import ResponseError as RedisReplyError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.typing import EncodableT

from document_ia_infra.redis.redis_manager import redis_manager, RedisManager
from document_ia_infra.redis.redis_settings import redis_settings
from document_ia_infra.redis.serializable_message import SerializableMessage
from document_ia_infra.redis.admission import admit_execution
from document_ia_infra.redis.idempotent_publish import IDEMPOTENT_PUBLISH_SCRIPT

logger = logging.getLogger(__name__)

_EXECUTION_LOOKUP_PAGE_SIZE = 1000
_EXECUTION_LOOKUP_MAX_PAGES = 100

T = TypeVar("T", bound=SerializableMessage)


class PublishFailedError(Exception):
    """The message could not be scheduled on the Redis stream.

    Raised instead of returning ``None`` so callers can distinguish a scheduling
    failure from a successful publication and must decide the resulting state.
    """

    def __init__(self, stream_name: str, reason: str):
        self.stream_name = stream_name
        self.reason = reason
        super().__init__(f"Publication on Redis stream {stream_name} failed: {reason}")


class ReservationLostError(Exception):
    """The idempotency reservation was lost before the publication.

    Raised by the fenced publication when the ownership check inside the same
    Redis operation failed: another request owns the key, so nothing was
    written. Publishing here would duplicate the execution.
    """


class PublicationOutcomeUnknownError(PublishFailedError):
    """The publication may or may not have been applied.

    Raised when a publication reply is lost (timeout, dropped connection).
    Redis may have committed the XADD; the fenced script may also have marked
    the reservation COMPLETED. The caller must reconcile the outcome before
    reporting a scheduling failure or deleting an uploaded input document.
    """

    def __init__(self, stream_name: str, reason: str) -> None:
        super().__init__(stream_name, reason)
        self.execution_id: str | None = None


class Publisher(Generic[T]):
    def __init__(
        self, stream_name: str, current_redis_manager: RedisManager = redis_manager
    ):
        """Initialise the Redis connection and the publisher"""
        self.stream_name = stream_name
        self.current_redis_manager = current_redis_manager

    async def publish_message(self, message: T):
        """Publie une tâche dans le stream Redis.

        Returns the Redis message id on success.

        Raises:
            PublishFailedError: when no connection is available or Redis refuses
                the write. The exception message never embeds the published
                payload.
            PublicationOutcomeUnknownError: when the reply is lost and Redis may
                already have committed the write. The caller must reconcile the
                execution id before treating publication as failed.

        New workflow messages allocate capacity atomically against both queues.
        Other publications can drain accepted work. Never trim unfinished work.
        """
        try:
            connection = await self.current_redis_manager.get_connection()
        except RedisError as e:
            logger.error("❌ Redis connection failed: %s", e)
            raise PublishFailedError(self.stream_name, type(e).__name__) from e

        if connection is None:
            logger.error("❌ No Redis connection available")
            raise PublishFailedError(self.stream_name, "no_connection")

        try:
            # Convertir les données en format approprié pour Redis
            stream_data: dict[EncodableT, EncodableT] = {
                "data": json.dumps(message.to_dict()),
                "timestamp": datetime.now().isoformat(),
                "retries": 0,
            }

            if self.stream_name == redis_settings.EVENT_STREAM_NAME:
                message_id = await admit_execution(
                    connection,
                    data=str(stream_data["data"]),
                    timestamp=str(stream_data["timestamp"]),
                )
            else:
                message_id = await connection.xadd(self.stream_name, stream_data)

            logger.info(f"✅ Message published in Redis stream: {message_id}")

            return message_id
        except RedisReplyError as e:
            # Redis answered and rejected the write, so it definitively did not
            # happen. Capacity and command failures stay confirmed failures.
            logger.error(f"❌ Publication rejected by Redis: {e}")
            raise PublishFailedError(self.stream_name, type(e).__name__) from e
        except (
            asyncio.TimeoutError,
            ConnectionError,
            RedisConnectionError,
            RedisTimeoutError,
        ) as e:
            # The command may have committed before its reply was lost. Let the
            # caller reconcile the execution id before reporting a failure or
            # deleting an uploaded input document.
            logger.error(f"❌ Publication outcome unknown: {e}")
            raise PublicationOutcomeUnknownError(
                self.stream_name, type(e).__name__
            ) from e
        except Exception as e:
            logger.error(f"❌ Error publishing message in Redis stream: {e}")
            raise PublishFailedError(self.stream_name, type(e).__name__) from e

    async def publish_message_fenced(
        self,
        message: T,
        *,
        reservation_key: str,
        reservation_execution_id: str,
        reservation_ttl_seconds: int,
    ) -> str:
        """Publish only while this request still owns its idempotency reservation.

        Ownership, capacity admission and the stream write happen in one Redis
        script, so the key cannot expire or be re-acquired between the check and
        the XADD. A separate pre-check is not enough: the publisher awaits its
        connection in between, and a stale request would then publish a second
        execution.

        Raises:
            ReservationLostError: the reservation is no longer ours; nothing was
                written.
            PublishFailedError: no connection, or capacity was exhausted.
        """
        try:
            connection = await self.current_redis_manager.get_connection()
        except RedisError as e:
            logger.error("❌ Redis connection failed: %s", e)
            raise PublishFailedError(self.stream_name, type(e).__name__) from e

        if connection is None:
            logger.error("❌ No Redis connection available")
            raise PublishFailedError(self.stream_name, "no_connection")

        stream_data = {
            "data": json.dumps(message.to_dict()),
            "timestamp": datetime.now().isoformat(),
        }
        args = [
            reservation_execution_id,
            str(reservation_ttl_seconds),
            redis_settings.EVENT_CONSUMER_GROUP,
            redis_settings.WEBHOOK_CONSUMER_GROUP,
            str(redis_settings.ADMISSION_MAX_OUTSTANDING),
            str(redis_settings.ADMISSION_RESUME_OUTSTANDING),
            str(redis_settings.ADMISSION_MAX_AGE_SECONDS),
            str(redis_settings.ADMISSION_RESUME_AGE_SECONDS),
            "publish",
            stream_data["data"],
            stream_data["timestamp"],
            str(redis_settings.ADMISSION_MAX_RETAINED),
            str(redis_settings.ADMISSION_RESUME_RETAINED),
        ]
        keys = [
            reservation_key,
            redis_settings.EVENT_STREAM_NAME,
            redis_settings.WEBHOOK_STREAM_NAME,
            f"{redis_settings.EVENT_STREAM_NAME}:admission",
            f"{redis_settings.WEBHOOK_STREAM_NAME}:admission",
        ]
        # Bounded like the unfenced path (`admit_execution`): without a deadline a
        # stalled Redis holds the request and its pooled connection open.
        try:
            result = await asyncio.wait_for(
                cast(
                    Awaitable[list[object]],
                    connection.eval(  # type: ignore[UnknownMemberType]
                        IDEMPOTENT_PUBLISH_SCRIPT, len(keys), *keys, *args
                    ),
                ),
                timeout=redis_settings.PUBLISH_TIMEOUT_SECONDS,
            )
        except RedisReplyError as e:
            # Redis answered and rejected the command (OOM, READONLY after a
            # failover, a Lua error). The write definitively did not happen, so
            # this is a confirmed rejection: reporting it as unknown would let
            # the caller mark an unpublished execution as accepted.
            logger.error("❌ Fenced publication rejected by Redis: %s", e)
            raise PublishFailedError(self.stream_name, type(e).__name__) from e
        except (
            asyncio.TimeoutError,
            ConnectionError,
            RedisConnectionError,
            RedisTimeoutError,
        ) as e:
            # No reply arrived. The script may have committed, so the outcome is
            # genuinely unknown and must not be reported as a failure.
            logger.error("❌ Publication outcome unknown: %s", e)
            unknown = PublicationOutcomeUnknownError(self.stream_name, type(e).__name__)
            unknown.execution_id = reservation_execution_id
            raise unknown from e
        except Exception as e:
            logger.error("❌ Error publishing fenced message: %s", e)
            raise PublishFailedError(self.stream_name, type(e).__name__) from e

        if not result or result[0] == 0:
            raise ReservationLostError()
        if result[0] != 1:
            raise PublishFailedError(self.stream_name, "capacity_exhausted")
        message_id = result[1]
        if not isinstance(message_id, str):
            raise PublishFailedError(self.stream_name, "invalid_publication_response")
        return message_id

    async def execution_is_queued(self, execution_id: str) -> bool | None:
        """Report whether a message for `execution_id` is on this stream.

        Resolves an unknown publication outcome: the reply may be lost after
        Redis committed, so presence on the stream is the evidence that the work
        was actually scheduled.

        Paginates through the stream within the global publication timeout
        budget and a page ceiling. Returns None when the stream could not be
        read exhaustively (connection error, timeout, or truncated scan). That
        must not be confused with absence: the outcome is still unresolved.
        """
        try:
            connection = await self.current_redis_manager.get_connection()
        except Exception:
            return None
        if connection is None:
            return None

        loop = asyncio.get_running_loop()
        deadline = loop.time() + redis_settings.PUBLISH_TIMEOUT_SECONDS
        cursor = "-"
        page_size = _EXECUTION_LOOKUP_PAGE_SIZE

        for _ in range(_EXECUTION_LOOKUP_MAX_PAGES):
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "Timed out scanning stream %s to reconcile execution %s; "
                    "outcome remains unresolved",
                    self.stream_name,
                    execution_id,
                )
                return None

            try:
                # Bounded by the remaining publication budget: a stalled Redis
                # would otherwise hold the request, its pooled connection and
                # the reservation renewal open on this read.
                entries = await asyncio.wait_for(
                    cast(
                        Awaitable[Any],
                        connection.xrange(  # type: ignore[UnknownMemberType]
                            self.stream_name, min=cursor, max="+", count=page_size
                        ),
                    ),
                    timeout=remaining,
                )
            except Exception as exc:
                logger.warning(
                    "Unable to inspect stream %s to reconcile execution %s: %s; "
                    "outcome remains unresolved",
                    self.stream_name,
                    execution_id,
                    exc,
                )
                return None

            if not entries:
                return False

            for _entry_id, fields in entries:
                raw = fields.get("data") or fields.get(b"data")
                if not raw:
                    continue
                try:
                    if json.loads(raw).get("workflow_execution_id") == execution_id:
                        return True
                except ValueError, AttributeError:
                    continue

            if len(entries) < page_size:
                return False

            last_id = entries[-1][0]
            last_id_str = (
                last_id.decode("utf-8") if isinstance(last_id, bytes) else str(last_id)
            )
            cursor = f"({last_id_str}"

        logger.warning(
            "Exceeded max lookup pages (%d) scanning stream %s to reconcile "
            "execution %s; outcome remains unresolved",
            _EXECUTION_LOOKUP_MAX_PAGES,
            self.stream_name,
            execution_id,
        )
        return None
