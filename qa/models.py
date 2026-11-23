"""Pydantic v2 data models for the Document IA QA toolkit."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EnvProfile = Literal["baseline", "develop"]
ScenarioStatus = Literal["PASS", "FAIL", "SKIPPED", "ERROR"]
ExecutionMode = Literal["async", "sync", "http"]
ApiVersion = Literal["v1", "v2"]
ChaosMode = Literal["none", "manual", "docker", "scalingo"]


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


class FieldRule(BaseModel):
    """Assertion rule applied to a single scalar, object, or extraction property."""

    model_config = ConfigDict(extra="forbid")

    equals: Any = Field(default=None)
    has_equals: bool = Field(default=False, exclude=True)
    not_equals: Any = Field(default=None)
    has_not_equals: bool = Field(default=False, exclude=True)
    iequals: str | None = Field(default=None)
    contains: str | None = Field(default=None)
    regex: str | None = Field(default=None)
    one_of: list[Any] | None = Field(default=None)
    gte: float | None = Field(default=None)
    lte: float | None = Field(default=None)
    between: tuple[float, float] | None = Field(default=None)
    is_null: bool | None = Field(default=None)
    not_empty: bool | None = Field(default=None)
    exists: bool | None = Field(default=None)
    anomaly_code: str | None = Field(default=None)
    anomaly_raw_value: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _capture_explicit_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return {"equals": data, "has_equals": True}
        normalized = dict(data)
        # Support YAML shorthand `{ value: null, anomaly_code: "invalid_date" }`
        if "value" in normalized:
            val = normalized.pop("value")
            if val is None:
                normalized.setdefault("is_null", True)
            else:
                normalized.setdefault("equals", val)
                normalized["has_equals"] = True
        if "equals" in normalized:
            normalized["has_equals"] = True
        if "not_equals" in normalized:
            normalized["has_not_equals"] = True
        return normalized


class ClassificationExpect(BaseModel):
    """Expectations on execution classification result."""

    model_config = ConfigDict(extra="forbid")

    document_type: str | FieldRule | None = Field(default=None)
    confidence: FieldRule | None = Field(default=None)


class ExtractionExpect(BaseModel):
    """Expectations on execution extraction result."""

    model_config = ConfigDict(extra="forbid")

    type: str | FieldRule | None = Field(default=None)
    properties: dict[str, FieldRule] = Field(default_factory=dict)


class BarcodesExpect(BaseModel):
    """Expectations on extracted barcodes."""

    model_config = ConfigDict(extra="forbid")

    min_count: int | None = Field(default=None, ge=0)
    max_count: int | None = Field(default=None, ge=0)
    contains_type: str | list[str] | None = Field(default=None)


class HeadersExpect(BaseModel):
    """Expectations on HTTP response headers (case-insensitive names)."""

    model_config = ConfigDict(extra="forbid")

    exists: list[str] = Field(default_factory=list)
    not_exists: list[str] = Field(default_factory=list)
    equals: dict[str, str] = Field(default_factory=dict)
    contains: dict[str, str] = Field(default_factory=dict)


class ScenarioExpect(BaseModel):
    """Declarative expectations for a QA scenario."""

    model_config = ConfigDict(extra="forbid")

    http_status: int | list[int] = Field(default=200)
    execution_status: str | list[str] | None = Field(default=None)
    purged: bool | None = Field(default=None)
    classification: ClassificationExpect | None = Field(default=None)
    extraction: ExtractionExpect | None = Field(default=None)
    barcodes: BarcodesExpect | None = Field(default=None)
    headers: HeadersExpect | None = Field(default=None)
    json_path: dict[str, FieldRule] = Field(default_factory=dict)


class ScenarioRequest(BaseModel):
    """Declarative HTTP or workflow request specification."""

    model_config = ConfigDict(extra="forbid")

    mode: ExecutionMode = Field(default="async")
    api_version: ApiVersion = Field(default="v2")
    # Raw HTTP mode fields (smoke tests)
    method: str = Field(default="GET")
    path: str | None = Field(default=None)
    auth: Literal["default", "none", "invalid", "admin"] = Field(default="default")
    # Reserved for synthetic/invalid test keys only; never serialized into reports.
    api_key_override: str | None = Field(default=None)

    # Workflow execution fields
    workflow_id: str | None = Field(default=None)
    file: str | None = Field(default=None)
    file_url: str | None = Field(default=None)
    override: dict[str, Any] | None = Field(default=None)
    raw_override: str | None = Field(default=None)
    metadata: dict[str, Any] | None = Field(default=None)
    raw_metadata: str | None = Field(default=None)
    classification_parameters: dict[str, Any] | None = Field(default=None)
    extraction_parameters: dict[str, Any] | None = Field(default=None)
    headers: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None)
    poll_interval_s: float | None = Field(default=None, gt=0)
    max_timeout_s: float | None = Field(default=None, gt=0)


class ScenarioDefinition(BaseModel):
    """Single YAML scenario definition."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(default="")
    tags: list[str] = Field(default_factory=list)
    min_profile: EnvProfile = Field(default="baseline")
    request: ScenarioRequest
    expect: ScenarioExpect = Field(default_factory=ScenarioExpect)
    source_file: str | None = Field(default=None, exclude=True)


class ScenarioFile(BaseModel):
    """Top-level structure of a scenario YAML file."""

    model_config = ConfigDict(extra="forbid")

    scenarios: list[ScenarioDefinition] = Field(default_factory=list)


class AssertionResult(BaseModel):
    """Outcome of one individual assertion."""

    path: str
    passed: bool
    expected: Any = None
    actual: Any = None
    message: str = ""


class ExecutionSnapshot(BaseModel):
    """Sanitized, comparable snapshot of an API / workflow execution result."""

    http_status: int | None = None
    execution_id: str | None = None
    execution_status: str | None = None
    purged: bool | None = None
    total_processing_time_ms: int | None = None
    classification: dict[str, Any] | None = None
    extraction_type: str | None = None
    extraction_properties: dict[str, Any] = Field(default_factory=dict)
    barcodes_count: int | None = None
    barcode_types: list[str] = Field(default_factory=list)
    rate_limit_headers: dict[str, str] = Field(default_factory=dict)
    error_code: str | None = None


class ScenarioResult(BaseModel):
    """Result of executing a single declarative or protocol scenario."""

    id: str
    description: str = ""
    kind: Literal["scenario", "protocol"] = "scenario"
    tags: list[str] = Field(default_factory=list)
    status: ScenarioStatus
    duration_ms: float = 0.0
    message: str = ""
    assertions: list[AssertionResult] = Field(default_factory=list)
    snapshot: ExecutionSnapshot | None = None


class RunSummary(BaseModel):
    """Aggregated counts for a QA suite run."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0


class RunReport(BaseModel):
    """Complete serializable QA run report (no secrets)."""

    schema_version: str = Field(default="1.0")
    env: str
    profile: EnvProfile
    base_url: str
    started_at: str = Field(default_factory=utc_now_iso)
    finished_at: str = Field(default_factory=utc_now_iso)
    duration_s: float = 0.0
    tags_filter: list[str] = Field(default_factory=list)
    summary: RunSummary = Field(default_factory=RunSummary)
    results: list[ScenarioResult] = Field(default_factory=list)


class LatencyStats(BaseModel):
    """Latency distribution in milliseconds."""

    count: int = 0
    min_ms: float = 0.0
    avg_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0


class LoadRequestRecord(BaseModel):
    """Ledger entry for a single request in a load/chaos test."""

    request_index: int
    fixture_name: str
    idempotency_key: str | None = None
    execution_id: str | None = None
    attempts: int = 0
    http_status: int | None = None
    final_status: str = "UNSUBMITTED"
    backpressure_503_count: int = 0
    rate_limit_429_count: int = 0
    idempotent_replay_count: int = 0
    network_error_count: int = 0
    submit_ms: float = 0.0
    process_ms: float = 0.0
    total_ms: float = 0.0
    error_detail: str | None = None


class ChaosEvent(BaseModel):
    """Audit record for a chaos injection action."""

    mode: ChaosMode
    target: str | None = None
    triggered_at: str = Field(default_factory=utc_now_iso)
    recovered_at: str | None = None
    downtime_s: float = 0.0
    details: str = ""
    redis_read_during_outage_ok: bool | None = None
    redis_write_fail_closed_503: bool | None = None


class LoadAssertions(BaseModel):
    """Reliability invariants evaluated after a load/chaos run."""

    zero_drop: bool
    dropped_execution_ids: list[str] = Field(default_factory=list)
    zero_idempotency_duplicates: bool
    duplicate_idempotency_keys: list[str] = Field(default_factory=list)
    zero_http_500: bool
    http_500_count: int = 0
    success_rate_pct: float = 0.0
    all_passed: bool = False


class LoadReport(BaseModel):
    """Complete serializable report for a load / chaos test."""

    schema_version: str = Field(default="1.0")
    env: str
    profile: EnvProfile
    base_url: str
    workflow_id: str
    started_at: str = Field(default_factory=utc_now_iso)
    finished_at: str = Field(default_factory=utc_now_iso)
    duration_s: float = 0.0
    total_requests: int
    concurrency: int
    use_idempotency_key: bool
    chaos_mode: ChaosMode = "none"
    chaos_event: ChaosEvent | None = None
    accepted_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    unsubmitted_count: int = 0
    backpressure_503_total: int = 0
    rate_limit_429_total: int = 0
    http_500_total: int = 0
    submit_latency: LatencyStats = Field(default_factory=LatencyStats)
    process_latency: LatencyStats = Field(default_factory=LatencyStats)
    e2e_latency: LatencyStats = Field(default_factory=LatencyStats)
    assertions: LoadAssertions
    records: list[LoadRequestRecord] = Field(default_factory=list)


class ComparisonDiffItem(BaseModel):
    """Comparison outcome for a single scenario between baseline and target."""

    scenario_id: str
    baseline_status: ScenarioStatus | None = None
    target_status: ScenarioStatus | None = None
    verdict: Literal["REGRESSION", "IMPROVEMENT", "UNCHANGED", "ADDED", "REMOVED"]
    functional_diffs: list[str] = Field(default_factory=list)
    baseline_processing_ms: int | None = None
    target_processing_ms: int | None = None
    latency_delta_ms: int | None = None
    latency_delta_pct: float | None = None


class ComparisonReport(BaseModel):
    """Diff report comparing baseline (e.g. sandbox) and target (e.g. staging)."""

    baseline_env: str
    target_env: str
    baseline_file: str
    target_file: str
    regressions: int = 0
    improvements: int = 0
    functional_diffs_count: int = 0
    unchanged: int = 0
    added: int = 0
    removed: int = 0
    has_regressions: bool = False
    items: list[ComparisonDiffItem] = Field(default_factory=list)
