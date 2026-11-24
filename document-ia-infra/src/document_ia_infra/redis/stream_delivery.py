"""Redis delivery primitives, deliberately independent of handler/error policy.

Clients are borrowed: the application/RedisManager that created them closes them.
Copy-before-ACK is atomic and idempotent even when a successful reply is lost.
Source entries and their copies must not be trimmed before pending recovery.
"""

import asyncio
import json
from collections.abc import Awaitable
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from document_ia_infra.redis.delivery_metrics import delivery_metric
from document_ia_infra.redis.redis_settings import redis_settings

_COPY_ONCE_LUA = """
local target = KEYS[1]
local orig = ARGV[1]
local existing = redis.call('XRANGE', target, ARGV[2], '+')
for _, e in ipairs(existing) do
  local f = e[2]
  for i = 1, #f, 2 do
    if f[i] == 'original_id' and f[i+1] == orig then
      return {0, e[1]}
    end
  end
end
if #KEYS >= 2 then
  if redis.call('ZSCORE', KEYS[2], orig) ~= false then
    return {0, orig}
  end
end
local args = {}
for i = 3, #ARGV do table.insert(args, ARGV[i]) end
local id = redis.call('XADD', target, '*', unpack(args))
return {1, id}
"""

_SCHEDULE_DELAYED_RETRY_LUA = """
local delayed_key = KEYS[1]
local stream = KEYS[2]
local payloads_key = KEYS[3]
local orig = ARGV[1]
local minimum = ARGV[2]
local due_at_ms = tonumber(ARGV[3])
local payload_json = ARGV[4]

local min_id = string.match(minimum, '^%d+%-%d+$') and minimum or '-'
local existing = redis.call('XRANGE', stream, min_id, '+')
for _, e in ipairs(existing) do
  local f = e[2]
  for i = 1, #f, 2 do
    if f[i] == 'original_id' and f[i+1] == orig then
      return {0, e[1]}
    end
  end
end

if redis.call('ZSCORE', delayed_key, orig) ~= false then
  return {0, orig}
end

redis.call('HSET', payloads_key, orig, payload_json)
redis.call('ZADD', delayed_key, due_at_ms, orig)
return {1, orig}
"""

_PROMOTE_DUE_RETRIES_LUA = """
local delayed_key = KEYS[1]
local stream = KEYS[2]
local payloads_key = KEYS[3]
local now_ms = ARGV[1]
local batch_size = tonumber(ARGV[2])

local due = redis.call('ZRANGEBYSCORE', delayed_key, '-inf', now_ms, 'LIMIT', 0, batch_size)
local promoted = 0

for _, orig_id in ipairs(due) do
  local removed = redis.call('ZREM', delayed_key, orig_id)
  if removed == 1 then
    local payload_json = redis.call('HGET', payloads_key, orig_id)
    redis.call('HDEL', payloads_key, orig_id)
    if payload_json then
      local ok, decoded = pcall(cjson.decode, payload_json)
      if ok and type(decoded) == 'table' then
        local already_in_stream = false
        local min_id = string.match(orig_id, '^%d+%-%d+$') and orig_id or '-'
        local existing = redis.call('XRANGE', stream, min_id, '+')
        for _, e in ipairs(existing) do
          local f = e[2]
          for i = 1, #f, 2 do
            if f[i] == 'original_id' and f[i+1] == orig_id then
              already_in_stream = true
              break
            end
          end
          if already_in_stream then
            break
          end
        end
        if not already_in_stream then
          local args = {}
          for k, v in pairs(decoded) do
            table.insert(args, tostring(k))
            table.insert(args, tostring(v))
          end
          if #args > 0 then
            redis.call('XADD', stream, '*', unpack(args))
            promoted = promoted + 1
          end
        end
      end
    end
  end
end

return promoted
"""

_RENEW_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
  return 0
end
"""

_RELEASE_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"""


class StreamDelivery:
    """Concrete operations for one stream/group, with no scheduling or retries."""

    def __init__(self, stream: str, group: str, consumer: str):
        self.stream = stream
        self.group = group
        self.consumer = consumer

    @property
    def delayed_retry_key(self) -> str:
        return f"{self.stream}:retry_delayed"

    @property
    def delayed_retry_payloads_key(self) -> str:
        return f"{self.stream}:retry_delayed:payloads"

    @staticmethod
    def _normalize_fields(fields: dict[Any, Any]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in fields.items():
            if value is None:
                continue
            str_key = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            str_val = value.decode("utf-8") if isinstance(value, bytes) else str(value)
            normalized[str_key] = str_val
        return normalized

    async def ensure_group(self, redis: Redis, start: str) -> None:
        try:
            await redis.xgroup_create(
                name=self.stream, groupname=self.group, id=start, mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read(self, redis: Redis, count: int, block: int) -> Any:
        if not await self._recovery_slot(redis, "read"):
            await asyncio.sleep(
                min(redis_settings.STREAM_RECOVERY_INTERVAL_MS / 1000, 1)
            )
            return []
        if redis_settings.STREAM_RECOVERY_INTERVAL_MS:
            count = 1
            block = min(block, 1000)
        return await redis.xreadgroup(
            self.group,
            self.consumer,
            streams={self.stream: ">"},
            count=count,
            block=block,
        )

    async def claim(self, redis: Redis, idle_ms: int, start: str, count: int) -> Any:
        if not await self._recovery_slot(redis, "claim"):
            return start, [], []
        if redis_settings.STREAM_RECOVERY_INTERVAL_MS:
            count = 1
        result = await redis.xautoclaim(
            self.stream,
            self.group,
            self.consumer,
            idle_ms,
            start,
            count=count,
        )
        delivery_metric(self.stream, "reclaim", "success", len(result[1]))
        return result

    async def _recovery_slot(self, redis: Redis, operation: str) -> bool:
        interval = redis_settings.STREAM_RECOVERY_INTERVAL_MS
        if not interval:
            return True
        # Shared per stream and operation, not multiplied by instance count.
        return bool(
            await redis.set(
                f"{self.stream}:recovery:{operation}", "1", nx=True, px=interval
            )
        )

    async def ack(self, redis: Redis, entry_id: str) -> None:
        try:
            await redis.xack(self.stream, self.group, entry_id)
        except Exception:
            delivery_metric(self.stream, "ack", "error")
            raise
        delivery_metric(self.stream, "ack", "success")

    async def refresh(self, redis: Redis, ids: set[Any]) -> None:
        if ids:
            await redis.xclaim(
                self.stream,
                self.group,
                self.consumer,
                min_idle_time=0,
                message_ids=list(ids),
                justid=True,
            )

    async def copy_once(
        self,
        redis: Redis,
        target: str,
        entry_id: str,
        fields: dict[str, Any],
        *,
        minimum: str = "0-0",
        delayed_key: str | None = None,
    ) -> bool:
        payload = {**self._normalize_fields(fields), "original_id": str(entry_id)}
        argv = [str(entry_id), minimum]
        for key, value in payload.items():
            argv.extend((key, value))
        operation = "dlq" if target == f"{self.stream}:dlq" else "retry"
        keys = [target] if delayed_key is None else [target, delayed_key]
        try:
            result = await redis.eval(_COPY_ONCE_LUA, len(keys), *keys, *argv)
        except Exception:
            delivery_metric(self.stream, operation, "error")
            raise
        delivery_metric(self.stream, operation, "created" if result[0] else "existing")
        return True

    async def requeue(
        self, redis: Redis, entry_id: str, fields: dict[str, Any], retries: int
    ) -> bool:
        return await self.copy_once(
            redis,
            self.stream,
            entry_id,
            {**fields, "retries": str(retries)},
            minimum=str(entry_id),
            delayed_key=self.delayed_retry_key,
        )

    async def schedule_delayed_retry(
        self,
        redis: Redis,
        entry_id: str,
        fields: dict[str, Any],
        retries: int,
        due_at_ms: int,
    ) -> bool:
        payload = {
            **self._normalize_fields(fields),
            "retries": str(retries),
            "original_id": str(entry_id),
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        try:
            result = await redis.eval(
                _SCHEDULE_DELAYED_RETRY_LUA,
                3,
                self.delayed_retry_key,
                self.stream,
                self.delayed_retry_payloads_key,
                str(entry_id),
                str(entry_id),
                str(due_at_ms),
                serialized,
            )
        except Exception:
            delivery_metric(self.stream, "retry_delayed", "error")
            raise
        delivery_metric(
            self.stream, "retry_delayed", "created" if result[0] else "existing"
        )
        return True

    async def promote_due_retries(
        self,
        redis: Redis,
        now_ms: int,
        batch_size: int = 100,
    ) -> int:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        try:
            promoted = await redis.eval(
                _PROMOTE_DUE_RETRIES_LUA,
                3,
                self.delayed_retry_key,
                self.stream,
                self.delayed_retry_payloads_key,
                str(now_ms),
                str(batch_size),
            )
            promoted_count = int(promoted or 0)
        except Exception:
            delivery_metric(self.stream, "retry_promoted", "error")
            raise
        if promoted_count > 0:
            delivery_metric(self.stream, "retry_promoted", "success", promoted_count)
        return promoted_count

    async def retain(self, redis: Redis, entry_id: str, fields: dict[str, Any]) -> bool:
        return await self.copy_once(redis, f"{self.stream}:dlq", entry_id, fields)

    @staticmethod
    def execution_lease_key(execution_id: str) -> str:
        return f"lock:workflow_execution:{execution_id}"

    async def acquire_lease(
        self, redis: Redis, execution_id: str, token: str, ttl_ms: int
    ) -> bool:
        key = self.execution_lease_key(execution_id)
        try:
            result = await redis.set(key, token, nx=True, px=ttl_ms)
            acquired = bool(result)
        except Exception:
            delivery_metric(self.stream, "lease_acquire", "error")
            raise
        delivery_metric(
            self.stream, "lease_acquire", "success" if acquired else "rejected"
        )
        return acquired

    async def renew_lease(
        self, redis: Redis, execution_id: str, token: str, ttl_ms: int
    ) -> bool:
        key = self.execution_lease_key(execution_id)
        try:
            result = await cast(
                Awaitable[Any], redis.eval(_RENEW_LEASE_LUA, 1, key, token, str(ttl_ms))
            )
            renewed = bool(result == 1)
        except Exception:
            delivery_metric(self.stream, "lease_renew", "error")
            raise
        delivery_metric(self.stream, "lease_renew", "success" if renewed else "lost")
        return renewed

    async def release_lease(self, redis: Redis, execution_id: str, token: str) -> bool:
        key = self.execution_lease_key(execution_id)
        try:
            result = await cast(
                Awaitable[Any], redis.eval(_RELEASE_LEASE_LUA, 1, key, token)
            )
            released = bool(result == 1)
        except Exception:
            delivery_metric(self.stream, "lease_release", "error")
            raise
        delivery_metric(
            self.stream, "lease_release", "success" if released else "not_owned"
        )
        return released
