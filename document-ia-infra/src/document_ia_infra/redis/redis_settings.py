import json
import math
from typing import Annotated, Any, Self
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import NoDecode

from document_ia_infra.core.BaseDocumentIaSettings import BaseDocumentIaSettings


class RedisSettings(BaseDocumentIaSettings):
    REDIS_HOST: str = Field(default="localhost")
    REDIS_PORT: int = Field(default=6379)
    REDIS_DB: int = Field(default=0)
    REDIS_PASSWORD: SecretStr = Field(default_factory=lambda: SecretStr("password"))

    REDIS_WORKER_NUMBER: int = Field(default=1)

    REDIS_URL: str | None = Field(default=None)

    EVENT_STREAM_NAME: str = Field(default="event_stream")
    WEBHOOK_STREAM_NAME: str = Field(default="webhook_stream")
    WEBHOOK_CONSUMER_GROUP: str = Field(default="webhook_consumer")
    EVENT_STREAM_EXPIRATION: int = Field(default=300)
    EVENT_STREAM_RECLAIM_IDLE_MS: int = Field(default=90000, ge=1000)
    # Kept for configuration compatibility; publishers no longer trim streams.
    EVENT_STREAM_MAXLEN: int = Field(default=100000)
    DLQ_STREAM_MAXLEN: int = Field(default=100000)
    EVENT_CONSUMER_GROUP: str = Field(default="workflow_execution_consumer")

    # Worker retry & backoff configuration
    WORKFLOW_MAX_RETRY_NUMBER: int = Field(default=4, ge=1)
    WORKFLOW_RETRY_BACKOFF_DELAYS_S: Annotated[list[float], NoDecode] = Field(
        default=[0.0, 1.0, 10.0],
    )
    RETRY_DELAYED_POLL_INTERVAL_S: float = Field(default=0.5, gt=0.0, le=30.0)

    # Worker graceful shutdown timeouts
    WORKER_DRAIN_TIMEOUT_S: float = Field(default=20.0, ge=0.0, le=300.0)
    WORKER_SHUTDOWN_TIMEOUT_S: float = Field(default=24.0, ge=1.0, le=300.0)

    ADMISSION_MAX_OUTSTANDING: int = Field(default=2000, ge=2)
    ADMISSION_RESUME_OUTSTANDING: int = Field(default=1600, ge=0)
    ADMISSION_MAX_AGE_SECONDS: int = Field(default=600, ge=2)
    ADMISSION_RESUME_AGE_SECONDS: int = Field(default=300, ge=0)
    # Includes completed history and DLQ; retries of accepted work may exceed it.
    ADMISSION_MAX_RETAINED: int = Field(default=50000, ge=2)
    # Cleanup preserves at least one last-delivered row for lag tracking.
    ADMISSION_RESUME_RETAINED: int = Field(default=32000, ge=1)
    STREAM_COMPLETED_RETENTION_SECONDS: int = Field(default=86400, ge=1)
    STREAM_DLQ_RETENTION_SECONDS: int = Field(default=604800, ge=1)
    STREAM_CLEANUP_BATCH_SIZE: int = Field(default=100, ge=1, le=1000)
    # Automatic background Redis stream maintenance (worker)
    STREAM_MAINTENANCE_ENABLED: bool = Field(default=True)
    STREAM_MAINTENANCE_INTERVAL_SECONDS: float = Field(default=60.0, gt=0.0, le=86400.0)
    STREAM_MAINTENANCE_MAX_BATCHES_PER_TICK: int = Field(default=10, ge=1, le=100)
    # Operator-enabled, shared across instances; 0 keeps normal throughput.
    STREAM_RECOVERY_INTERVAL_MS: int = Field(default=0, ge=0, le=60000)

    # Watchdog & Execution lease defaults
    DEFAULT_MAX_PROCESSING_TIME_S: float = Field(default=120.0, ge=1.0)
    DEFAULT_STEP_PROGRESS_TIMEOUT_S: float = Field(default=90.0, ge=1.0)
    DEFAULT_LEASE_TTL_MS: int = Field(default=60000, ge=1000)

    @field_validator("WORKFLOW_RETRY_BACKOFF_DELAYS_S", mode="before")
    @classmethod
    def parse_retry_backoff_delays(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [float(part.strip()) for part in stripped.split(",") if part.strip()]
        return value

    @field_validator("WORKFLOW_RETRY_BACKOFF_DELAYS_S")
    @classmethod
    def validate_retry_backoff_delays(cls, value: list[float]) -> list[float]:
        for delay in value:
            if not math.isfinite(delay) or delay < 0.0:
                raise ValueError(
                    "WORKFLOW_RETRY_BACKOFF_DELAYS_S elements must be finite and >= 0.0"
                )
        return value

    @field_validator(
        "RETRY_DELAYED_POLL_INTERVAL_S", "STREAM_MAINTENANCE_INTERVAL_SECONDS"
    )
    @classmethod
    def validate_positive_finite_intervals(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Interval settings must be finite")
        return value

    @field_validator("WORKER_DRAIN_TIMEOUT_S", "WORKER_SHUTDOWN_TIMEOUT_S")
    @classmethod
    def validate_worker_shutdown_timeout_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Worker shutdown timeouts must be finite")
        return value

    @model_validator(mode="after")
    def validate_admission_thresholds(self) -> Self:
        if self.ADMISSION_RESUME_OUTSTANDING >= self.ADMISSION_MAX_OUTSTANDING:
            raise ValueError("Admission resume count must be below the stop count")
        if self.ADMISSION_RESUME_AGE_SECONDS >= self.ADMISSION_MAX_AGE_SECONDS:
            raise ValueError("Admission resume age must be below the stop age")
        if self.ADMISSION_RESUME_RETAINED >= self.ADMISSION_MAX_RETAINED:
            raise ValueError("Admission resume history must be below the stop count")
        if self.EVENT_STREAM_NAME == self.WEBHOOK_STREAM_NAME:
            raise ValueError("Workflow and webhook streams must be distinct")
        return self

    @model_validator(mode="after")
    def validate_worker_shutdown_timeouts(self) -> Self:
        if self.WORKER_SHUTDOWN_TIMEOUT_S < self.WORKER_DRAIN_TIMEOUT_S:
            raise ValueError(
                "WORKER_SHUTDOWN_TIMEOUT_S must be >= WORKER_DRAIN_TIMEOUT_S"
            )
        return self

    # Valeur du header `Retry-After` (en secondes) renvoyé quand une exécution ne
    # peut pas être programmée. Bornée pour ne pas inviter le client à attendre
    # indéfiniment une indisponibilité prolongée.
    PUBLISH_RETRY_AFTER_SECONDS: int = Field(default=5, ge=1, le=120)
    # Bounds the fenced publication, matching the unfenced admission path.
    PUBLISH_TIMEOUT_SECONDS: float = Field(default=5.0, gt=0, le=60)

    # Durée de rétention des clés d'idempotence (12 heures par défaut).
    IDEMPOTENCY_KEY_TTL_SECONDS: int = Field(default=43200, ge=1)
    # Durée de verrouillage temporaire pendant le traitement de la requête (60 secondes).
    IDEMPOTENCY_LOCK_TTL_SECONDS: int = Field(default=60, ge=1, le=600)

    def get_redis_url(self) -> str:
        if self.REDIS_URL:
            return self.REDIS_URL
        password = quote(self.REDIS_PASSWORD.get_secret_value(), safe="")
        return (
            f"redis://:{password}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        )

    def get_redis_url_without_credentials(self) -> str:
        """Effective Redis URL with any userinfo removed.

        Used when the server answers `AUTH` with "without any password
        configured": the fallback must keep the host, port, database, scheme and
        connection options selected by `get_redis_url()`, and drop only the
        credentials. Rebuilding the URL from `REDIS_HOST`/`REDIS_PORT`/`REDIS_DB`
        would silently move the connection to another server whenever
        `REDIS_URL` is the source of the endpoint.
        """
        parts = urlsplit(self.get_redis_url())
        netloc = parts.hostname or ""
        if parts.hostname and ":" in parts.hostname:
            # IPv6 literals must keep their brackets.
            netloc = f"[{parts.hostname}]"
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit(
            (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
        )


redis_settings = RedisSettings()
