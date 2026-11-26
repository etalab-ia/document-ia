"""Multi-step protocol scenarios for security, idempotency, rate-limiting and resilience."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from qa.client import (
    IDEMPOTENT_REPLAY_HEADER,
    DocumentIAClient,
    HttpResponse,
    extract_error_code,
    extract_execution_id,
    extract_rate_limit_headers,
)
from qa.fixtures import resolve_fixture
from qa.models import AssertionResult, EnvProfile, ExecutionSnapshot, ScenarioResult
from qa.runners.scenario_runner import matches_tag_filter

ProtocolFunc = Callable[[DocumentIAClient], Awaitable[ScenarioResult]]

FAST_PROTOCOL_WORKFLOW = "document-barcode-extraction-v2"
DEFAULT_MAX_GATEWAY_ATTEMPTS = 4
TRANSIENT_GATEWAY_STATUSES = frozenset({502, 503, 504})


async def _call_with_gateway_retry(
    call: Callable[[], Awaitable[HttpResponse]],
    *,
    max_attempts: int = DEFAULT_MAX_GATEWAY_ATTEMPTS,
    retry_delay_scale: float = 1.0,
) -> HttpResponse:
    """Retry a protocol HTTP call up to max_attempts on transient 502/503/504 or TransportError."""
    attempts = max(1, max_attempts)
    resp: HttpResponse | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = await call()
        except httpx.TransportError:
            if attempt < attempts:
                wait_s = min(1.0 * attempt, 4.0) * retry_delay_scale
                await asyncio.sleep(max(0.0, wait_s))
                continue
            raise

        if resp.status_code in TRANSIENT_GATEWAY_STATUSES and attempt < attempts:
            wait_s = min(1.0 * attempt, 4.0) * retry_delay_scale
            await asyncio.sleep(max(0.0, wait_s))
            continue
        break

    assert resp is not None
    return resp


@dataclass(frozen=True, slots=True)
class ProtocolScenarioSpec:
    """Registry entry describing a multi-step protocol scenario."""

    id: str
    description: str
    tags: tuple[str, ...]
    min_profile: EnvProfile
    opt_in: bool
    runner: ProtocolFunc


def _parse_int_header(resp: HttpResponse, name: str) -> int | None:
    raw = resp.get_header(name)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _build_result(
    spec: ProtocolScenarioSpec,
    start: float,
    assertions: list[AssertionResult],
    *,
    snapshot: ExecutionSnapshot | None = None,
    override_status: str | None = None,
    override_message: str | None = None,
) -> ScenarioResult:
    duration_ms = (time.perf_counter() - start) * 1000.0
    if override_status == "SKIPPED":
        return ScenarioResult(
            id=spec.id,
            description=spec.description,
            kind="protocol",
            tags=list(spec.tags),
            status="SKIPPED",
            duration_ms=duration_ms,
            message=override_message or "",
            assertions=assertions,
            snapshot=snapshot,
        )
    if override_status == "ERROR":
        return ScenarioResult(
            id=spec.id,
            description=spec.description,
            kind="protocol",
            tags=list(spec.tags),
            status="ERROR",
            duration_ms=duration_ms,
            message=override_message or "",
            assertions=assertions,
            snapshot=snapshot,
        )

    passed = all(a.passed for a in assertions) and len(assertions) > 0
    failed_msgs = [a.message for a in assertions if not a.passed and a.message]
    return ScenarioResult(
        id=spec.id,
        description=spec.description,
        kind="protocol",
        tags=list(spec.tags),
        status="PASS" if passed else "FAIL",
        duration_ms=duration_ms,
        message="" if passed else "; ".join(failed_msgs[:4]),
        assertions=assertions,
        snapshot=snapshot,
    )


async def _run_proto_auth_binding(client: DocumentIAClient) -> ScenarioResult:
    """Validate API key structural/env/prefix/version binding (#159) & quota preservation (#250)."""
    spec = PROTOCOL_SCENARIOS["proto-auth-binding"]
    start = time.perf_counter()
    assertions: list[AssertionResult] = []

    if not client.env_config.get_api_key():
        return _build_result(
            spec,
            start,
            assertions,
            override_status="ERROR",
            override_message=f"Missing API key for environment '{client.env_config.name}'.",
        )

    env_tag = client.env_config.api_key_env
    other_env = "prod" if env_tag != "prod" else "staging"

    # Baseline valid call to inspect rate-limit quota before invalid probes
    valid_before = await client.test_auth()
    assertions.append(
        AssertionResult(
            path="auth.valid_before.http_status",
            passed=valid_before.status_code == 200,
            expected=200,
            actual=valid_before.status_code,
            message=(
                ""
                if valid_before.status_code == 200
                else f"Valid API key expected 200, got {valid_before.status_code}"
            ),
        )
    )
    rem_before = _parse_int_header(valid_before, "X-RateLimit-Remaining-Minute")
    reset_before = valid_before.get_header("X-RateLimit-Reset-Minute")

    # 1. Missing API key -> 401
    missing_resp = await client.test_auth(omit_api_key=True)
    assertions.append(
        AssertionResult(
            path="auth.missing_key",
            passed=missing_resp.status_code == 401,
            expected=401,
            actual=missing_resp.status_code,
            message=(
                ""
                if missing_resp.status_code == 401
                else f"Missing key expected 401, got {missing_resp.status_code}"
            ),
        )
    )

    # 2. Invalid random string -> 401
    invalid_resp = await client.test_auth(api_key_override="not-a-valid-api-key")
    assertions.append(
        AssertionResult(
            path="auth.invalid_key",
            passed=invalid_resp.status_code == 401,
            expected=401,
            actual=invalid_resp.status_code,
            message=(
                ""
                if invalid_resp.status_code == 401
                else f"Invalid key expected 401, got {invalid_resp.status_code}"
            ),
        )
    )

    # 3. Wrong <env> binding -> 401
    body_b32 = "A" * 52
    wrong_env_key = f"dia_{other_env}_2_{body_b32[:8]}_{body_b32}_AAAA"
    wrong_env_resp = await client.test_auth(api_key_override=wrong_env_key)
    assertions.append(
        AssertionResult(
            path="auth.wrong_env_key",
            passed=wrong_env_resp.status_code == 401,
            expected=401,
            actual=wrong_env_resp.status_code,
            message=(
                ""
                if wrong_env_resp.status_code == 401
                else f"Wrong-env key expected 401, got {wrong_env_resp.status_code}"
            ),
        )
    )

    # 4. Mutated prefix (prefix != body[:8]) -> 401
    mutated_prefix_key = f"dia_{env_tag}_2_ZZZZZZZZ_{body_b32}_AAAA"
    mutated_prefix_resp = await client.test_auth(api_key_override=mutated_prefix_key)
    assertions.append(
        AssertionResult(
            path="auth.mutated_prefix_key",
            passed=mutated_prefix_resp.status_code == 401,
            expected=401,
            actual=mutated_prefix_resp.status_code,
            message=(
                ""
                if mutated_prefix_resp.status_code == 401
                else f"Mutated-prefix key expected 401, got {mutated_prefix_resp.status_code}"
            ),
        )
    )

    # 5. Version 'v2' (leading 'v' forbidden) -> 401
    v2_prefixed_key = f"dia_{env_tag}_v2_{body_b32[:8]}_{body_b32}_AAAA"
    v2_resp = await client.test_auth(api_key_override=v2_prefixed_key)
    assertions.append(
        AssertionResult(
            path="auth.version_v2_key",
            passed=v2_resp.status_code == 401,
            expected=401,
            actual=v2_resp.status_code,
            message=(
                ""
                if v2_resp.status_code == 401
                else f"Version 'v2' key expected 401, got {v2_resp.status_code}"
            ),
        )
    )

    # Verify the 5 failed 401 attempts did not consume the valid key's minute quota
    valid_after = await client.test_auth()
    rem_after = _parse_int_header(valid_after, "X-RateLimit-Remaining-Minute")
    reset_after = valid_after.get_header("X-RateLimit-Reset-Minute")

    if rem_before is not None and rem_after is not None:
        window_rolled = (
            reset_before is not None
            and reset_after is not None
            and reset_before != reset_after
        )
        # Only valid_after itself should have decremented by 1 (not the 5 x 401 calls)
        quota_preserved = window_rolled or (rem_after >= rem_before - 1)
        assertions.append(
            AssertionResult(
                path="auth.quota_not_consumed_by_401",
                passed=quota_preserved,
                expected=f">= {rem_before - 1} (or window reset)",
                actual=rem_after,
                message=(
                    ""
                    if quota_preserved
                    else f"401 requests consumed valid key quota: before={rem_before}, after={rem_after}"
                ),
            )
        )
    else:
        assertions.append(
            AssertionResult(
                path="auth.quota_not_consumed_by_401",
                passed=False,
                expected="X-RateLimit-Remaining-Minute integer header on valid auth calls",
                actual={"before": rem_before, "after": rem_after},
                message=(
                    "Missing or non-integer 'X-RateLimit-Remaining-Minute' header on "
                    f"valid auth calls (before={rem_before!r}, after={rem_after!r})"
                ),
            )
        )

    snapshot = ExecutionSnapshot(
        http_status=valid_after.status_code,
        rate_limit_headers=extract_rate_limit_headers(valid_after),
    )
    return _build_result(spec, start, assertions, snapshot=snapshot)


async def _run_proto_idempotency(client: DocumentIAClient) -> ScenarioResult:
    """Validate full Idempotency-Key lifecycle (#148) on /api/v2/workflows/{id}/execute."""
    spec = PROTOCOL_SCENARIOS["proto-idempotency"]
    start = time.perf_counter()
    assertions: list[AssertionResult] = []

    if not client.env_config.get_api_key():
        return _build_result(
            spec,
            start,
            assertions,
            override_status="ERROR",
            override_message=f"Missing API key for environment '{client.env_config.name}'.",
        )

    fixture = resolve_fixture("synthetic:minimal_pdf", client.env_config.fixtures_dir)
    idem_key = f"qa-idem-{uuid.uuid4()}"
    metadata_a = {"qa_protocol": "proto-idempotency", "variant": "a"}
    metadata_b = {"qa_protocol": "proto-idempotency", "variant": "b"}

    # 1. First request -> 200 without Idempotent-Replay: true
    first = await client.execute_workflow_v2(
        FAST_PROTOCOL_WORKFLOW,
        fixture=fixture,
        metadata=metadata_a,
        idempotency_key=idem_key,
    )
    exec_1 = extract_execution_id(first.body_json)
    first_replay = (first.get_header(IDEMPOTENT_REPLAY_HEADER) or "").lower()
    assertions.append(
        AssertionResult(
            path="idempotency.first.http_status",
            passed=first.status_code == 200 and exec_1 is not None,
            expected="200 with execution_id",
            actual={"status": first.status_code, "execution_id": exec_1},
            message=(
                ""
                if (first.status_code == 200 and exec_1 is not None)
                else f"First idempotent execute failed: status={first.status_code}, exec_id={exec_1}"
            ),
        )
    )
    assertions.append(
        AssertionResult(
            path="idempotency.first.not_replayed",
            passed=first_replay != "true",
            expected="absent or != 'true'",
            actual=first.get_header(IDEMPOTENT_REPLAY_HEADER),
            message=(
                ""
                if first_replay != "true"
                else "First idempotent request unexpectedly returned Idempotent-Replay: true"
            ),
        )
    )

    # 2. Identical replay -> 200 + Idempotent-Replay: true + same execution_id
    replay = await client.execute_workflow_v2(
        FAST_PROTOCOL_WORKFLOW,
        fixture=fixture,
        metadata=metadata_a,
        idempotency_key=idem_key,
    )
    exec_replay = extract_execution_id(replay.body_json)
    replay_header = (replay.get_header(IDEMPOTENT_REPLAY_HEADER) or "").lower()
    assertions.append(
        AssertionResult(
            path="idempotency.replay.http_status",
            passed=replay.status_code == 200,
            expected=200,
            actual=replay.status_code,
            message=(
                ""
                if replay.status_code == 200
                else f"Idempotent replay expected 200, got {replay.status_code}"
            ),
        )
    )
    assertions.append(
        AssertionResult(
            path="idempotency.replay.header",
            passed=replay_header == "true",
            expected="true",
            actual=replay.get_header(IDEMPOTENT_REPLAY_HEADER),
            message=(
                ""
                if replay_header == "true"
                else f"Expected Idempotent-Replay: true, got {replay.get_header(IDEMPOTENT_REPLAY_HEADER)!r}"
            ),
        )
    )
    assertions.append(
        AssertionResult(
            path="idempotency.replay.same_execution_id",
            passed=exec_1 is not None and exec_replay == exec_1,
            expected=exec_1,
            actual=exec_replay,
            message=(
                ""
                if (exec_1 is not None and exec_replay == exec_1)
                else f"Replay execution_id mismatch: expected {exec_1}, got {exec_replay}"
            ),
        )
    )

    # 3. Conflict payload (same key, different metadata) -> 409 idempotency_conflict
    conflict = await client.execute_workflow_v2(
        FAST_PROTOCOL_WORKFLOW,
        fixture=fixture,
        metadata=metadata_b,
        idempotency_key=idem_key,
    )
    conflict_err = extract_error_code(conflict.body_json)
    assertions.append(
        AssertionResult(
            path="idempotency.conflict",
            passed=conflict.status_code == 409
            and conflict_err == "idempotency_conflict",
            expected={"status": 409, "error": "idempotency_conflict"},
            actual={"status": conflict.status_code, "error": conflict_err},
            message=(
                ""
                if (
                    conflict.status_code == 409
                    and conflict_err == "idempotency_conflict"
                )
                else f"Conflict expected 409/idempotency_conflict, got {conflict.status_code}/{conflict_err}"
            ),
        )
    )

    # 4a. Invalid key (non-ASCII / accented) -> 400 invalid_idempotency_key
    # Note: httpx enforces ASCII header encoding at transport level; send raw latin-1/non-printable
    # control char if unicode cannot be encoded, or test both >256 chars and invalid ASCII.
    # Let's test >256 chars and empty string as well as accented when transport allows.
    invalid_accent_status: int | None = None
    invalid_accent_err: str | None = None
    try:
        resp_accent = await client.execute_workflow_v2(
            FAST_PROTOCOL_WORKFLOW,
            fixture=fixture,
            idempotency_key="qa-clé-é",
        )
        invalid_accent_status = resp_accent.status_code
        invalid_accent_err = extract_error_code(resp_accent.body_json)
    except UnicodeEncodeError:
        # HTTP/1.1 client rejected non-ASCII header locally; test non-printable ASCII (0x1f / empty) instead
        resp_accent = await client.execute_workflow_v2(
            FAST_PROTOCOL_WORKFLOW,
            fixture=fixture,
            idempotency_key="",
        )
        invalid_accent_status = resp_accent.status_code
        invalid_accent_err = extract_error_code(resp_accent.body_json)

    assertions.append(
        AssertionResult(
            path="idempotency.invalid_key_chars_or_empty",
            passed=invalid_accent_status == 400
            and invalid_accent_err == "invalid_idempotency_key",
            expected={"status": 400, "error": "invalid_idempotency_key"},
            actual={"status": invalid_accent_status, "error": invalid_accent_err},
            message=(
                ""
                if (
                    invalid_accent_status == 400
                    and invalid_accent_err == "invalid_idempotency_key"
                )
                else f"Invalid key expected 400/invalid_idempotency_key, got {invalid_accent_status}/{invalid_accent_err}"
            ),
        )
    )

    # 4b. Invalid key (> 256 chars) -> 400 invalid_idempotency_key
    too_long = await client.execute_workflow_v2(
        FAST_PROTOCOL_WORKFLOW,
        fixture=fixture,
        idempotency_key="a" * 257,
    )
    too_long_err = extract_error_code(too_long.body_json)
    assertions.append(
        AssertionResult(
            path="idempotency.invalid_key_too_long",
            passed=too_long.status_code == 400
            and too_long_err == "invalid_idempotency_key",
            expected={"status": 400, "error": "invalid_idempotency_key"},
            actual={"status": too_long.status_code, "error": too_long_err},
            message=(
                ""
                if (
                    too_long.status_code == 400
                    and too_long_err == "invalid_idempotency_key"
                )
                else f"Too-long key expected 400/invalid_idempotency_key, got {too_long.status_code}/{too_long_err}"
            ),
        )
    )

    # 5. Concurrent duplicate with fresh key -> exactly 1 execution_id overall
    conc_key = f"qa-idem-conc-{uuid.uuid4()}"
    conc_resps = await asyncio.gather(
        client.execute_workflow_v2(
            FAST_PROTOCOL_WORKFLOW,
            fixture=fixture,
            metadata=metadata_a,
            idempotency_key=conc_key,
        ),
        client.execute_workflow_v2(
            FAST_PROTOCOL_WORKFLOW,
            fixture=fixture,
            metadata=metadata_a,
            idempotency_key=conc_key,
        ),
    )
    conc_statuses = [r.status_code for r in conc_resps]
    conc_exec_ids = {
        eid
        for r in conc_resps
        if (eid := extract_execution_id(r.body_json)) is not None
    }
    valid_outcomes = all(
        r.status_code == 200
        or (
            r.status_code == 409
            and extract_error_code(r.body_json) == "idempotency_in_progress"
        )
        for r in conc_resps
    )
    has_200 = 200 in conc_statuses
    single_exec = len(conc_exec_ids) == 1
    assertions.append(
        AssertionResult(
            path="idempotency.concurrent_duplicate",
            passed=valid_outcomes and has_200 and single_exec,
            expected="200 + (200 replay or 409 in_progress) with 1 unique execution_id",
            actual={
                "statuses": conc_statuses,
                "execution_ids": sorted(conc_exec_ids),
            },
            message=(
                ""
                if (valid_outcomes and has_200 and single_exec)
                else f"Concurrent duplicate failed: statuses={conc_statuses}, execution_ids={sorted(conc_exec_ids)}"
            ),
        )
    )

    snapshot = ExecutionSnapshot(
        http_status=first.status_code,
        execution_id=exec_1,
        rate_limit_headers=extract_rate_limit_headers(first),
    )
    return _build_result(spec, start, assertions, snapshot=snapshot)


async def _run_proto_ratelimit_headers(client: DocumentIAClient) -> ScenarioResult:
    """Validate X-RateLimit-* header presence and coherent decrementation (#250)."""
    spec = PROTOCOL_SCENARIOS["proto-ratelimit-headers"]
    start = time.perf_counter()
    assertions: list[AssertionResult] = []

    if not client.env_config.get_api_key():
        return _build_result(
            spec,
            start,
            assertions,
            override_status="ERROR",
            override_message=f"Missing API key for environment '{client.env_config.name}'.",
        )

    r1 = await client.test_auth()
    r2 = await client.test_auth()

    assertions.append(
        AssertionResult(
            path="ratelimit.calls_200",
            passed=r1.status_code == 200 and r2.status_code == 200,
            expected=[200, 200],
            actual=[r1.status_code, r2.status_code],
            message=(
                ""
                if (r1.status_code == 200 and r2.status_code == 200)
                else f"Expected 200 on both probes, got {r1.status_code} and {r2.status_code}"
            ),
        )
    )

    required_headers = (
        "X-RateLimit-Remaining-Minute",
        "X-RateLimit-Remaining-Daily",
        "X-RateLimit-Reset-Minute",
        "X-RateLimit-Reset-Daily",
    )
    for hdr in required_headers:
        v1 = r1.get_header(hdr)
        v2 = r2.get_header(hdr)
        present = bool(v1) and bool(v2)
        assertions.append(
            AssertionResult(
                path=f"ratelimit.header.{hdr}",
                passed=present,
                expected="non-empty on both calls",
                actual=[v1, v2],
                message=""
                if present
                else f"Missing header '{hdr}' in rate-limit probe",
            )
        )

    rem_min_1 = _parse_int_header(r1, "X-RateLimit-Remaining-Minute")
    rem_min_2 = _parse_int_header(r2, "X-RateLimit-Remaining-Minute")
    reset_min_1 = r1.get_header("X-RateLimit-Reset-Minute")
    reset_min_2 = r2.get_header("X-RateLimit-Reset-Minute")

    if rem_min_1 is not None and rem_min_2 is not None:
        window_rolled = bool(reset_min_1 and reset_min_2 and reset_min_1 != reset_min_2)
        coherent = window_rolled or (rem_min_2 < rem_min_1)
        assertions.append(
            AssertionResult(
                path="ratelimit.minute_decrement",
                passed=coherent,
                expected=f"< {rem_min_1} (or window reset)",
                actual=rem_min_2,
                message=(
                    ""
                    if coherent
                    else f"Minute remaining did not decrement: first={rem_min_1}, second={rem_min_2}"
                ),
            )
        )

    rem_day_1 = _parse_int_header(r1, "X-RateLimit-Remaining-Daily")
    rem_day_2 = _parse_int_header(r2, "X-RateLimit-Remaining-Daily")
    reset_day_1 = r1.get_header("X-RateLimit-Reset-Daily")
    reset_day_2 = r2.get_header("X-RateLimit-Reset-Daily")

    if rem_day_1 is not None and rem_day_2 is not None:
        day_rolled = bool(reset_day_1 and reset_day_2 and reset_day_1 != reset_day_2)
        coherent_day = day_rolled or (rem_day_2 < rem_day_1)
        assertions.append(
            AssertionResult(
                path="ratelimit.daily_decrement",
                passed=coherent_day,
                expected=f"< {rem_day_1} (or day reset)",
                actual=rem_day_2,
                message=(
                    ""
                    if coherent_day
                    else f"Daily remaining did not decrement: first={rem_day_1}, second={rem_day_2}"
                ),
            )
        )

    snapshot = ExecutionSnapshot(
        http_status=r2.status_code,
        rate_limit_headers=extract_rate_limit_headers(r2),
    )
    return _build_result(spec, start, assertions, snapshot=snapshot)


async def _run_proto_ratelimit_burst(client: DocumentIAClient) -> ScenarioResult:
    """Opt-in burst until 429, verifying Retry-After and daily quota preservation (#250)."""
    spec = PROTOCOL_SCENARIOS["proto-ratelimit-burst"]
    start = time.perf_counter()
    assertions: list[AssertionResult] = []

    if not client.env_config.get_api_key():
        return _build_result(
            spec,
            start,
            assertions,
            override_status="ERROR",
            override_message=f"Missing API key for environment '{client.env_config.name}'.",
        )

    probe = await client.test_auth()
    rem_min_start = _parse_int_header(probe, "X-RateLimit-Remaining-Minute")
    rem_day_start = _parse_int_header(probe, "X-RateLimit-Remaining-Daily")

    max_calls = 200
    if rem_min_start is not None:
        max_calls = min(max(rem_min_start + 5, 10), 250)

    allowed_count = 0
    rejected_429: list[HttpResponse] = []
    last_200_day: int | None = rem_day_start

    for _ in range(max_calls):
        resp = await client.test_auth()
        if resp.status_code == 200:
            allowed_count += 1
            day_val = _parse_int_header(resp, "X-RateLimit-Remaining-Daily")
            if day_val is not None:
                last_200_day = day_val
        elif resp.status_code == 429:
            rejected_429.append(resp)
            if len(rejected_429) >= 3:
                break

    got_429 = len(rejected_429) > 0
    assertions.append(
        AssertionResult(
            path="ratelimit_burst.triggered_429",
            passed=got_429,
            expected="at least one 429 response",
            actual=f"{len(rejected_429)} x 429 after {allowed_count} x 200",
            message=(
                ""
                if got_429
                else f"Burst did not trigger 429 after {max_calls} requests"
            ),
        )
    )

    if got_429:
        first_429 = rejected_429[0]
        retry_after = _parse_int_header(first_429, "Retry-After")
        assertions.append(
            AssertionResult(
                path="ratelimit_burst.retry_after",
                passed=retry_after is not None and retry_after >= 1,
                expected="integer >= 1",
                actual=first_429.get_header("Retry-After"),
                message=(
                    ""
                    if (retry_after is not None and retry_after >= 1)
                    else f"Expected Retry-After >= 1 on 429, got {first_429.get_header('Retry-After')!r}"
                ),
            )
        )

        # Verify rejected 429 responses did not further decrement the daily quota
        last_429_day = _parse_int_header(
            rejected_429[-1], "X-RateLimit-Remaining-Daily"
        )
        if last_200_day is not None and last_429_day is not None:
            preserved = last_429_day >= last_200_day
            assertions.append(
                AssertionResult(
                    path="ratelimit_burst.daily_quota_preserved",
                    passed=preserved,
                    expected=f">= {last_200_day}",
                    actual=last_429_day,
                    message=(
                        ""
                        if preserved
                        else f"429 responses decremented daily quota: last_200={last_200_day}, last_429={last_429_day}"
                    ),
                )
            )

    snapshot = ExecutionSnapshot(
        http_status=rejected_429[0].status_code if got_429 else 200,
        rate_limit_headers=(
            extract_rate_limit_headers(rejected_429[0]) if got_429 else {}
        ),
    )
    return _build_result(spec, start, assertions, snapshot=snapshot)


async def _run_proto_oom_decompression_bomb(
    client: DocumentIAClient,
) -> ScenarioResult:
    """Submit a >100 MP PNG decompression bomb (#258), then verify worker survival."""
    spec = PROTOCOL_SCENARIOS["proto-oom-decompression-bomb"]
    start = time.perf_counter()
    assertions: list[AssertionResult] = []

    if not client.env_config.get_api_key():
        return _build_result(
            spec,
            start,
            assertions,
            override_status="ERROR",
            override_message=f"Missing API key for environment '{client.env_config.name}'.",
        )

    bomb = resolve_fixture(
        "synthetic:decompression_bomb_png", client.env_config.fixtures_dir
    )
    bomb_resp = await client.execute_workflow_v2(
        FAST_PROTOCOL_WORKFLOW,
        fixture=bomb,
        metadata={"qa_protocol": "proto-oom-decompression-bomb"},
    )

    if bomb_resp.status_code in (400, 413, 422):
        assertions.append(
            AssertionResult(
                path="oom_bomb.rejection",
                passed=True,
                expected="HTTP 400/413/422 or async FAILED",
                actual=f"HTTP {bomb_resp.status_code}",
            )
        )
    elif bomb_resp.status_code == 200:
        bomb_exec_id = extract_execution_id(bomb_resp.body_json)
        if not bomb_exec_id:
            assertions.append(
                AssertionResult(
                    path="oom_bomb.execution_id",
                    passed=False,
                    expected="execution_id",
                    actual=None,
                    message="Bomb upload returned 200 without execution_id",
                )
            )
        else:
            poll = await client.poll_execution(bomb_exec_id)
            barcodes: list[object] | None = None
            if poll.last_response and isinstance(poll.last_response.body_json, dict):
                raw_data = poll.last_response.body_json.get("data")
                result_data = (
                    raw_data.get("result", {}) if isinstance(raw_data, dict) else {}
                )
                if isinstance(result_data, dict) and isinstance(
                    result_data.get("barcodes"), list
                ):
                    barcodes = result_data["barcodes"]

            handled_safely = poll.final_status == "FAILED" or (
                poll.final_status == "SUCCESS" and barcodes == []
            )
            assertions.append(
                AssertionResult(
                    path="oom_bomb.execution_status",
                    passed=handled_safely,
                    expected="FAILED or SUCCESS with 0 barcodes (safely skipped by OOM guard)",
                    actual={
                        "status": poll.final_status,
                        "barcodes_count": len(barcodes)
                        if barcodes is not None
                        else None,
                    },
                    message=(
                        ""
                        if handled_safely
                        else f"Decompression bomb not handled safely: status={poll.final_status}, barcodes={barcodes!r}"
                    ),
                )
            )
    else:
        assertions.append(
            AssertionResult(
                path="oom_bomb.http_status",
                passed=False,
                expected="400/413/422 or 200+FAILED",
                actual=bomb_resp.status_code,
                message=f"Unexpected HTTP {bomb_resp.status_code} on decompression bomb",
            )
        )

    # Immediately send a nominal document to verify the worker survived without OOM
    nominal = resolve_fixture("synthetic:minimal_pdf", client.env_config.fixtures_dir)
    followup = await client.execute_workflow_v2(
        FAST_PROTOCOL_WORKFLOW,
        fixture=nominal,
        metadata={"qa_protocol": "proto-oom-decompression-bomb-followup"},
    )
    followup_exec_id = extract_execution_id(followup.body_json)
    assertions.append(
        AssertionResult(
            path="oom_bomb.followup_submit",
            passed=followup.status_code == 200 and followup_exec_id is not None,
            expected="200 with execution_id",
            actual={"status": followup.status_code, "execution_id": followup_exec_id},
            message=(
                ""
                if (followup.status_code == 200 and followup_exec_id is not None)
                else f"Follow-up submit failed after bomb: HTTP {followup.status_code}"
            ),
        )
    )

    followup_status: str | None = None
    if followup.status_code == 200 and followup_exec_id is not None:
        followup_poll = await client.poll_execution(followup_exec_id)
        followup_status = followup_poll.final_status
        assertions.append(
            AssertionResult(
                path="oom_bomb.followup_success",
                passed=followup_status == "SUCCESS",
                expected="SUCCESS",
                actual=followup_status,
                message=(
                    ""
                    if followup_status == "SUCCESS"
                    else f"Worker health follow-up expected SUCCESS, got {followup_status}"
                ),
            )
        )

    snapshot = ExecutionSnapshot(
        http_status=followup.status_code,
        execution_id=followup_exec_id,
        execution_status=followup_status,
    )
    return _build_result(spec, start, assertions, snapshot=snapshot)


async def _run_proto_webhook_isolation(client: DocumentIAClient) -> ScenarioResult:
    """Opt-in admin check for cross-organization webhook DELETE isolation (#160)."""
    spec = PROTOCOL_SCENARIOS["proto-webhook-isolation"]
    start = time.perf_counter()
    assertions: list[AssertionResult] = []

    if not client.env_config.get_admin_api_key():
        return _build_result(
            spec,
            start,
            assertions,
            override_status="SKIPPED",
            override_message=(
                f"Skipped: QA_{client.env_config.name.upper()}_ADMIN_API_KEY not configured."
            ),
        )

    orgs_resp = await client.list_organizations()
    if orgs_resp.status_code != 200 or not isinstance(orgs_resp.body_json, list):
        assertions.append(
            AssertionResult(
                path="webhook.list_organizations",
                passed=False,
                expected="200 with list of organizations",
                actual=orgs_resp.status_code,
                message=f"Failed to list organizations: HTTP {orgs_resp.status_code}",
            )
        )
        return _build_result(spec, start, assertions)

    org_ids = [
        str(o["id"]) for o in orgs_resp.body_json if isinstance(o, dict) and o.get("id")
    ]
    if len(org_ids) < 2:
        return _build_result(
            spec,
            start,
            assertions,
            override_status="SKIPPED",
            override_message="Skipped: need at least 2 organizations to test cross-org webhook isolation.",
        )

    org_a, org_b = org_ids[0], org_ids[1]
    webhook_url = (
        client.env_config.webhook_url
        or f"https://example.com/qa-webhook-isolation-{uuid.uuid4().hex[:8]}"
    )

    create_resp = await client.create_webhook(
        org_a, webhook_url, headers={"X-QA-Test": "isolation"}
    )
    webhook_id = (
        str(create_resp.body_json.get("id"))
        if isinstance(create_resp.body_json, dict) and create_resp.body_json.get("id")
        else None
    )
    assertions.append(
        AssertionResult(
            path="webhook.create_org_a",
            passed=create_resp.status_code == 201 and webhook_id is not None,
            expected="201 with webhook id",
            actual={"status": create_resp.status_code, "id": webhook_id},
            message=(
                ""
                if (create_resp.status_code == 201 and webhook_id is not None)
                else f"Failed to create temp webhook on org A: HTTP {create_resp.status_code}"
            ),
        )
    )

    if webhook_id is None:
        return _build_result(spec, start, assertions)

    try:
        cross_delete = await client.delete_webhook(org_b, webhook_id)
        assertions.append(
            AssertionResult(
                path="webhook.cross_org_delete_404",
                passed=cross_delete.status_code == 404,
                expected=404,
                actual=cross_delete.status_code,
                message=(
                    ""
                    if cross_delete.status_code == 404
                    else f"Cross-org DELETE expected 404, got {cross_delete.status_code}"
                ),
            )
        )
    finally:
        cleanup = await client.delete_webhook(org_a, webhook_id)
        assertions.append(
            AssertionResult(
                path="webhook.cleanup_org_a",
                passed=cleanup.status_code == 204,
                expected=204,
                actual=cleanup.status_code,
                message=(
                    ""
                    if cleanup.status_code == 204
                    else f"Cleanup DELETE on org A expected 204, got {cleanup.status_code}"
                ),
            )
        )

    return _build_result(
        spec,
        start,
        assertions,
        snapshot=ExecutionSnapshot(http_status=204),
    )


PROTOCOL_SCENARIOS: dict[str, ProtocolScenarioSpec] = {
    "proto-auth-binding": ProtocolScenarioSpec(
        id="proto-auth-binding",
        description="API key binding (missing, invalid, wrong env, mutated prefix, v2) + 401 quota preservation (#159/#250)",
        tags=("security", "auth", "synthetic"),
        min_profile="develop",
        opt_in=False,
        runner=_run_proto_auth_binding,
    ),
    "proto-idempotency": ProtocolScenarioSpec(
        id="proto-idempotency",
        description="Idempotency-Key lifecycle: first, replay, conflict 409, invalid key 400, concurrent duplicate (#148)",
        tags=("idempotency", "resilience", "develop", "synthetic"),
        min_profile="develop",
        opt_in=False,
        runner=_run_proto_idempotency,
    ),
    "proto-ratelimit-headers": ProtocolScenarioSpec(
        id="proto-ratelimit-headers",
        description="X-RateLimit-* header presence and coherent minute/daily decrementation (#250)",
        tags=("security", "ratelimit", "develop", "synthetic"),
        min_profile="develop",
        opt_in=False,
        runner=_run_proto_ratelimit_headers,
    ),
    "proto-ratelimit-burst": ProtocolScenarioSpec(
        id="proto-ratelimit-burst",
        description="[Opt-in] Rate-limit burst until 429, Retry-After and daily quota preservation (#250)",
        tags=("ratelimit-burst",),
        min_profile="develop",
        opt_in=True,
        runner=_run_proto_ratelimit_burst,
    ),
    "proto-oom-decompression-bomb": ProtocolScenarioSpec(
        id="proto-oom-decompression-bomb",
        description=">100 MP PNG decompression bomb rejected cleanly + nominal worker survival check (#258)",
        tags=("security", "resilience", "develop", "synthetic"),
        min_profile="develop",
        opt_in=False,
        runner=_run_proto_oom_decompression_bomb,
    ),
    "proto-webhook-isolation": ProtocolScenarioSpec(
        id="proto-webhook-isolation",
        description="[Opt-in/Admin] Cross-organization webhook DELETE returns 404 with cleanup (#160)",
        tags=("webhook",),
        min_profile="develop",
        opt_in=True,
        runner=_run_proto_webhook_isolation,
    ),
}


def filter_protocol_scenarios(
    *,
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
    scenario_ids: set[str] | None = None,
    target_profile: EnvProfile = "develop",
    include_opt_in: bool = False,
) -> list[ProtocolScenarioSpec]:
    """Select protocol scenarios matching CLI filters and environment profile."""
    selected: list[ProtocolScenarioSpec] = []
    for spec in PROTOCOL_SCENARIOS.values():
        if scenario_ids is not None:
            if spec.id not in scenario_ids:
                continue
        else:
            # Opt-in protocols only run when one of their tags is explicitly requested
            if (
                spec.opt_in
                and not include_opt_in
                and (
                    include_tags is None
                    or not ({t.lower() for t in spec.tags} & include_tags)
                )
            ):
                continue
            if not matches_tag_filter(
                list(spec.tags),
                include_tags=include_tags,
                exclude_tags=exclude_tags,
            ):
                continue
            if target_profile == "baseline" and spec.min_profile == "develop":
                continue
        selected.append(spec)
    return selected


async def run_protocol_scenarios(
    client: DocumentIAClient,
    specs: list[ProtocolScenarioSpec],
    *,
    max_gateway_attempts: int = DEFAULT_MAX_GATEWAY_ATTEMPTS,
    retry_delay_scale: float = 1.0,
) -> list[ScenarioResult]:
    """Run selected protocol scenarios sequentially to avoid cross-test rate-limit interference."""
    results: list[ScenarioResult] = []
    original_request = client.request
    had_instance_request = "request" in client.__dict__

    async def _request_with_gateway_retry(
        *args: Any,
        **kwargs: Any,
    ) -> HttpResponse:
        return await _call_with_gateway_retry(
            lambda: original_request(*args, **kwargs),
            max_attempts=max_gateway_attempts,
            retry_delay_scale=retry_delay_scale,
        )

    client.__dict__["request"] = _request_with_gateway_retry
    try:
        for spec in specs:
            if (
                client.env_config.profile == "baseline"
                and spec.min_profile == "develop"
            ):
                results.append(
                    ScenarioResult(
                        id=spec.id,
                        description=spec.description,
                        kind="protocol",
                        tags=list(spec.tags),
                        status="SKIPPED",
                        message="Skipped on baseline profile (requires min_profile=develop).",
                    )
                )
                continue
            start = time.perf_counter()
            try:
                res = await spec.runner(client)
            except httpx.HTTPError as exc:
                res = _build_result(
                    spec,
                    start,
                    [],
                    override_status="ERROR",
                    override_message=f"HTTP transport error: {type(exc).__name__}: {exc}",
                )
            except (ValueError, KeyError, RuntimeError, OSError) as exc:
                res = _build_result(
                    spec,
                    start,
                    [],
                    override_status="ERROR",
                    override_message=f"Unexpected error: {type(exc).__name__}: {exc}",
                )
            results.append(res)
    finally:
        if had_instance_request:
            client.__dict__["request"] = original_request
        else:
            client.__dict__.pop("request", None)
    return results
