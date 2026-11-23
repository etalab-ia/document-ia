"""Concurrent load & chaos runner with 0-drop ledger and latency percentiles."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from qa.client import (
    IDEMPOTENT_REPLAY_HEADER,
    DocumentIAClient,
    extract_error_code,
    extract_execution_id,
    parse_retry_after,
)
from qa.fixtures import ResolvedFixture, resolve_fixture
from qa.models import (
    ChaosEvent,
    ChaosMode,
    LatencyStats,
    LoadAssertions,
    LoadReport,
    LoadRequestRecord,
    utc_now_iso,
)
from qa.runners.chaos import ChaosProvider, build_chaos_provider

TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILED"})


@dataclass(slots=True)
class LoadConfig:
    """Parameters controlling a load / chaos test execution."""

    requests: int = 10
    concurrency: int = 5
    workflow_id: str = "document-classification-extraction-v2"
    files: list[str] = field(default_factory=lambda: ["synthetic:minimal_pdf"])
    override: dict[str, Any] | None = None
    use_idempotency_key: bool = True
    poll_interval_s: float = 2.0
    max_timeout_s: float = 180.0
    chaos_mode: ChaosMode = "none"
    chaos_after: int = 3
    chaos_target: str | None = None
    scalingo_app: str | None = None
    chaos_downtime_s: float = 5.0
    max_retries_per_request: int = 6
    retry_backoff_scale: float = 1.0
    show_progress: bool = True


def _format_progress_bar(completed: int, total: int, width: int = 20) -> str:
    """Build an ASCII progress bar like `[##########..........]`."""
    if total <= 0:
        return f"[{'#' * width}]"
    ratio = min(1.0, max(0.0, completed / total))
    filled = round(ratio * width)
    return f"[{'#' * filled}{'.' * (width - filled)}]"


def compute_latency_stats(samples_ms: list[float]) -> LatencyStats:
    """Compute min/avg/p50/p95/p99/max latency statistics in milliseconds."""
    if not samples_ms:
        return LatencyStats()

    ordered = sorted(samples_ms)
    n = len(ordered)

    def _percentile(pct: float) -> float:
        if n == 1:
            return round(ordered[0], 2)
        rank = (pct / 100.0) * (n - 1)
        lower = math.floor(rank)
        upper = math.ceil(rank)
        if lower == upper:
            return round(ordered[lower], 2)
        frac = rank - lower
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * frac, 2)

    return LatencyStats(
        count=n,
        min_ms=round(ordered[0], 2),
        avg_ms=round(sum(ordered) / n, 2),
        p50_ms=_percentile(50.0),
        p95_ms=_percentile(95.0),
        p99_ms=_percentile(99.0),
        max_ms=round(ordered[-1], 2),
    )


def _extract_server_processing_ms(body_json: Any) -> float | None:
    """Read data.total_processing_time_ms from execution payload when available."""
    if not isinstance(body_json, dict):
        return None
    data = body_json.get("data")
    if not isinstance(data, dict):
        return None
    raw = data.get("total_processing_time_ms")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0:
        return float(raw)
    return None


async def run_load_test(
    client: DocumentIAClient,
    config: LoadConfig,
    *,
    fixtures_dir: Path,
    chaos_provider: ChaosProvider | None = None,
) -> LoadReport:
    """Run a concurrent load test with optional chaos injection and 0-drop verification."""
    if config.requests < 1:
        raise ValueError("requests must be >= 1")
    if config.concurrency < 1:
        raise ValueError("concurrency must be >= 1")

    def _log_progress(msg: str) -> None:
        if config.show_progress:
            print(msg, flush=True)

    resolved_fixtures: list[ResolvedFixture] = [
        resolve_fixture(spec, fixtures_dir)
        for spec in (config.files or ["synthetic:minimal_pdf"])
    ]
    provider = chaos_provider or build_chaos_provider(
        mode=config.chaos_mode,
        target=config.chaos_target,
        scalingo_app=config.scalingo_app,
        downtime_s=config.chaos_downtime_s,
    )

    started_at = utc_now_iso()
    global_start = time.perf_counter()
    run_id = uuid.uuid4().hex[:10]

    # Pre-flight health check (non-blocking if unreachable)
    try:
        await client.health()
    except httpx.HTTPError:
        pass

    records: list[LoadRequestRecord] = [
        LoadRequestRecord(
            request_index=i + 1,
            fixture_name=resolved_fixtures[i % len(resolved_fixtures)].name,
            idempotency_key=(
                f"qa-load-{run_id}-{i + 1:04d}" if config.use_idempotency_key else None
            ),
        )
        for i in range(config.requests)
    ]

    # Track unique execution_id per Idempotency-Key across submissions & replay checks
    key_to_exec_ids: dict[str, set[str]] = {}
    req_start_by_index: dict[int, float] = {}
    submitted_at_by_index: dict[int, float] = {}
    accepted_counter = 0
    first_accepted_exec_id: str | None = None
    chaos_lock = asyncio.Lock()
    chaos_task: asyncio.Task[ChaosEvent] | None = None
    chaos_triggered = False
    chaos_threshold = max(1, min(config.chaos_after, config.requests))
    chaos_gate = asyncio.Event()
    chaos_gate.set()
    manual_pre_chaos_sem = asyncio.Semaphore(chaos_threshold)

    async def _run_chaos_trigger() -> ChaosEvent:
        try:
            return await provider.trigger(
                client=client,
                probe_execution_id=first_accepted_exec_id,
                probe_workflow_id=config.workflow_id,
                probe_fixture=resolved_fixtures[0],
            )
        finally:
            chaos_gate.set()
            if provider.mode == "manual":
                for _ in range(config.requests):
                    manual_pre_chaos_sem.release()

    async def _maybe_trigger_chaos() -> None:
        nonlocal chaos_task, chaos_triggered
        if provider.mode == "none" or chaos_triggered:
            return
        async with chaos_lock:
            if chaos_triggered:
                return
            if accepted_counter >= chaos_threshold:
                chaos_triggered = True
                if provider.mode == "manual":
                    chaos_gate.clear()
                chaos_task = asyncio.create_task(_run_chaos_trigger())

    submit_semaphore = asyncio.Semaphore(config.concurrency)

    async def _submit_one(record: LoadRequestRecord, fixture: ResolvedFixture) -> None:
        nonlocal accepted_counter, first_accepted_exec_id
        acquired_pre_chaos = False
        accepted_this_request = False
        if provider.mode == "manual":
            await manual_pre_chaos_sem.acquire()
            acquired_pre_chaos = True
        try:
            await chaos_gate.wait()
            async with submit_semaphore:
                await chaos_gate.wait()
                req_start = time.perf_counter()
                req_start_by_index[record.request_index] = req_start
                max_attempts = max(1, config.max_retries_per_request + 1)

                for attempt in range(1, max_attempts + 1):
                    await chaos_gate.wait()
                    record.attempts = attempt
                    try:
                        resp = await client.execute_workflow_v2(
                            config.workflow_id,
                            fixture=fixture,
                            override=config.override,
                            metadata={
                                "qa_load_run": run_id,
                                "request_index": record.request_index,
                            },
                            idempotency_key=record.idempotency_key,
                        )
                        record.http_status = resp.status_code

                        if resp.status_code == 200:
                            exec_id = extract_execution_id(resp.body_json)
                            if (
                                resp.get_header(IDEMPOTENT_REPLAY_HEADER) or ""
                            ).lower() == "true":
                                record.idempotent_replay_count += 1
                            if exec_id:
                                now = time.perf_counter()
                                record.execution_id = exec_id
                                record.final_status = "STARTED"
                                record.submit_ms = round((now - req_start) * 1000.0, 2)
                                submitted_at_by_index[record.request_index] = now
                                if record.idempotency_key:
                                    key_to_exec_ids.setdefault(
                                        record.idempotency_key, set()
                                    ).add(exec_id)
                                accepted_counter += 1
                                accepted_this_request = True
                                if first_accepted_exec_id is None:
                                    first_accepted_exec_id = exec_id
                                _log_progress(
                                    f"[SUBMIT {accepted_counter}/{config.requests}] "
                                    f"req=#{record.request_index:03d} "
                                    f"execution_id={record.execution_id} "
                                    f"fixture={record.fixture_name} "
                                    f"({record.submit_ms:.0f}ms)"
                                )
                                await _maybe_trigger_chaos()
                                return
                            record.final_status = "NO_EXECUTION_ID"
                            record.error_detail = "HTTP 200 without execution_id"
                            record.submit_ms = round(
                                (time.perf_counter() - req_start) * 1000.0, 2
                            )
                            return

                        if resp.status_code == 409:
                            err_code = extract_error_code(resp.body_json)
                            existing_id = extract_execution_id(resp.body_json)
                            if err_code == "idempotency_in_progress" and existing_id:
                                now = time.perf_counter()
                                record.execution_id = existing_id
                                record.final_status = "STARTED"
                                record.submit_ms = round((now - req_start) * 1000.0, 2)
                                submitted_at_by_index[record.request_index] = now
                                if record.idempotency_key:
                                    key_to_exec_ids.setdefault(
                                        record.idempotency_key, set()
                                    ).add(existing_id)
                                accepted_counter += 1
                                accepted_this_request = True
                                if first_accepted_exec_id is None:
                                    first_accepted_exec_id = existing_id
                                _log_progress(
                                    f"[SUBMIT {accepted_counter}/{config.requests}] "
                                    f"req=#{record.request_index:03d} "
                                    f"execution_id={record.execution_id} "
                                    f"fixture={record.fixture_name} "
                                    f"({record.submit_ms:.0f}ms)"
                                )
                                await _maybe_trigger_chaos()
                                return
                            record.final_status = "HTTP_409"
                            record.error_detail = f"Conflict: {err_code}"
                            return

                        if resp.status_code in (503, 429):
                            if resp.status_code == 503:
                                record.backpressure_503_count += 1
                            else:
                                record.rate_limit_429_count += 1
                            if attempt < max_attempts:
                                wait_s = (
                                    parse_retry_after(resp, default_s=1.0)
                                    * config.retry_backoff_scale
                                )
                                _log_progress(
                                    f"[SUBMIT-RETRY] req=#{record.request_index:03d} "
                                    f"attempt={attempt} status={resp.status_code} "
                                    f"wait={wait_s:.1f}s"
                                )
                                await asyncio.sleep(max(0.0, wait_s))
                                continue
                            record.final_status = f"HTTP_{resp.status_code}"
                            record.error_detail = (
                                f"Exhausted retries after HTTP {resp.status_code}"
                            )
                            record.submit_ms = round(
                                (time.perf_counter() - req_start) * 1000.0, 2
                            )
                            return

                        # Under chaos/proxy restarts, 502/504 are safe to retry with an Idempotency-Key
                        if (
                            resp.status_code in (502, 504)
                            and config.use_idempotency_key
                            and attempt < max_attempts
                        ):
                            record.network_error_count += 1
                            backoff = (
                                min(1.0 * attempt, 5.0) * config.retry_backoff_scale
                            )
                            await asyncio.sleep(max(0.0, backoff))
                            continue

                        if resp.status_code >= 500:
                            record.final_status = f"HTTP_{resp.status_code}"
                            record.error_detail = f"Server error HTTP {resp.status_code}: {resp.body_text[:160]}"
                            record.submit_ms = round(
                                (time.perf_counter() - req_start) * 1000.0, 2
                            )
                            return

                        # Non-retryable 4xx
                        record.final_status = f"HTTP_{resp.status_code}"
                        record.error_detail = f"Client error HTTP {resp.status_code}: {resp.body_text[:160]}"
                        record.submit_ms = round(
                            (time.perf_counter() - req_start) * 1000.0, 2
                        )
                        return

                    except httpx.HTTPError as exc:
                        record.network_error_count += 1
                        record.error_detail = f"{type(exc).__name__}: {exc}"
                        if attempt < max_attempts:
                            backoff = (
                                min(1.0 * attempt, 5.0) * config.retry_backoff_scale
                            )
                            await asyncio.sleep(max(0.0, backoff))
                            continue
                        record.final_status = "NETWORK_ERROR"
                        record.submit_ms = round(
                            (time.perf_counter() - req_start) * 1000.0, 2
                        )
                        return
        finally:
            if acquired_pre_chaos and not accepted_this_request:
                manual_pre_chaos_sem.release()

    # Phase 1: Submit all requests concurrently
    _log_progress(
        f"[LOAD] Phase 1/2 — Envoi de {config.requests} requêtes "
        f"(concurrency={config.concurrency}, workflow={config.workflow_id})..."
    )
    submit_tasks = [
        _submit_one(records[i], resolved_fixtures[i % len(resolved_fixtures)])
        for i in range(config.requests)
    ]
    await asyncio.gather(*submit_tasks)
    _log_progress(
        f"[LOAD] Phase 1 terminée : {accepted_counter}/{config.requests} "
        "exécutions acceptées. Démarrage Phase 2 (polling)..."
    )

    # If chaos was requested but accepted_counter never reached chaos_after (e.g. small N), trigger once now
    if provider.mode != "none" and not chaos_triggered:
        chaos_triggered = True
        chaos_task = asyncio.create_task(_run_chaos_trigger())

    chaos_event: ChaosEvent | None = None
    if chaos_task is not None:
        chaos_event = await chaos_task

    # Phase 1b: Active idempotency replay probe (verifies no duplicate execution_id is created)
    replay_http_500_count = 0
    if config.use_idempotency_key:
        sample_rec = next(
            (r for r in records if r.execution_id and r.idempotency_key),
            None,
        )
        if sample_rec is not None and sample_rec.idempotency_key is not None:
            sample_fixture = resolved_fixtures[
                (sample_rec.request_index - 1) % len(resolved_fixtures)
            ]
            try:
                replay_resp = await client.execute_workflow_v2(
                    config.workflow_id,
                    fixture=sample_fixture,
                    override=config.override,
                    metadata={
                        "qa_load_run": run_id,
                        "request_index": sample_rec.request_index,
                    },
                    idempotency_key=sample_rec.idempotency_key,
                )
                if replay_resp.status_code == 200:
                    replayed_id = extract_execution_id(replay_resp.body_json)
                    if (
                        replay_resp.get_header(IDEMPOTENT_REPLAY_HEADER) or ""
                    ).lower() == "true":
                        sample_rec.idempotent_replay_count += 1
                    if replayed_id:
                        key_to_exec_ids.setdefault(
                            sample_rec.idempotency_key, set()
                        ).add(replayed_id)
                elif replay_resp.status_code == 409:
                    replayed_id = extract_execution_id(replay_resp.body_json)
                    if replayed_id:
                        key_to_exec_ids.setdefault(
                            sample_rec.idempotency_key, set()
                        ).add(replayed_id)
                elif replay_resp.status_code == 500:
                    replay_http_500_count += 1
            except httpx.HTTPError:
                pass

    # Phase 2: Poll all accepted executions until terminal status or timeout
    poll_semaphore = asyncio.Semaphore(max(config.concurrency, 10))
    poll_http_500_count = 0
    records_to_poll = [rec for rec in records if rec.execution_id is not None]
    total_to_poll = len(records_to_poll)
    pending_by_index: dict[int, LoadRequestRecord] = {
        rec.request_index: rec for rec in records_to_poll
    }
    completed_polls = 0
    poll_phase_start = time.perf_counter()

    async def _poll_one(record: LoadRequestRecord) -> None:
        nonlocal poll_http_500_count, completed_polls
        if not record.execution_id:
            return
        async with poll_semaphore:
            stagger = ((record.request_index - 1) % 5) * min(
                0.2, config.poll_interval_s / 5.0
            )
            outcome = await client.poll_execution(
                record.execution_id,
                poll_interval_s=config.poll_interval_s,
                max_timeout_s=config.max_timeout_s,
                stagger_s=stagger,
            )
            poll_http_500_count += outcome.http_500_errors
            record.final_status = outcome.final_status

            now = time.perf_counter()
            server_proc_ms = (
                _extract_server_processing_ms(outcome.last_response.body_json)
                if outcome.last_response is not None
                else None
            )
            submitted_at = submitted_at_by_index.get(record.request_index)
            req_start = req_start_by_index.get(record.request_index)

            if server_proc_ms is not None:
                record.process_ms = round(server_proc_ms, 2)
            elif submitted_at is not None:
                record.process_ms = round((now - submitted_at) * 1000.0, 2)
            else:
                record.process_ms = round(outcome.elapsed_ms, 2)

            if req_start is not None:
                record.total_ms = round((now - req_start) * 1000.0, 2)
            else:
                record.total_ms = round(record.submit_ms + record.process_ms, 2)

            if outcome.final_status == "TIMEOUT":
                record.error_detail = (
                    f"Timed out after {config.max_timeout_s:.1f}s (still non-terminal)"
                )

            completed_polls += 1
            pending_by_index.pop(record.request_index, None)
            bar = _format_progress_bar(completed_polls, total_to_poll)
            pct = (
                round((completed_polls / total_to_poll) * 100)
                if total_to_poll > 0
                else 100
            )
            _log_progress(
                f"[POLL {bar} {completed_polls}/{total_to_poll} ({pct}%)] "
                f"req=#{record.request_index:03d} execution_id={record.execution_id} "
                f"-> {record.final_status} "
                f"(process={record.process_ms:.0f}ms, total={record.total_ms:.0f}ms) "
                f"| restant={len(pending_by_index)}"
            )
            if 0 < len(pending_by_index) <= 10:
                pending_summary = ", ".join(
                    f"#{r.request_index:03d}:{r.execution_id}({r.fixture_name})"
                    for r in pending_by_index.values()
                )
                _log_progress(f"[PENDING ({len(pending_by_index)})] {pending_summary}")

    heartbeat_interval_s = max(5.0, min(config.poll_interval_s, 15.0))

    async def _progress_monitor() -> None:
        try:
            while True:
                await asyncio.sleep(heartbeat_interval_s)
                if not pending_by_index:
                    break
                elapsed = time.perf_counter() - poll_phase_start
                pending_list = list(pending_by_index.values())
                _log_progress(
                    f"[WAIT {elapsed:.0f}s] {completed_polls}/{total_to_poll} terminées, "
                    f"{len(pending_list)} encore en cours (STARTED)..."
                )
                if len(pending_list) <= 15:
                    items_str = ", ".join(
                        f"req=#{r.request_index:03d} execution_id={r.execution_id} fixture={r.fixture_name}"
                        for r in pending_list
                    )
                else:
                    first_ten = ", ".join(
                        f"req=#{r.request_index:03d} execution_id={r.execution_id} fixture={r.fixture_name}"
                        for r in pending_list[:10]
                    )
                    items_str = f"{first_ten} ... (+{len(pending_list) - 10} autres)"
                _log_progress(f"[WAIT-PENDING] {items_str}")
        except asyncio.CancelledError:
            return

    poll_tasks = [_poll_one(rec) for rec in records_to_poll]
    if poll_tasks:
        monitor_task = asyncio.create_task(_progress_monitor())
        try:
            await asyncio.gather(*poll_tasks)
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)

    total_duration_s = round(time.perf_counter() - global_start, 3)

    # Phase 3: Compute ledger & reliability invariants
    accepted_records = [r for r in records if r.execution_id is not None]
    success_records = [r for r in accepted_records if r.final_status == "SUCCESS"]
    failed_records = [r for r in accepted_records if r.final_status == "FAILED"]
    timeout_records = [r for r in accepted_records if r.final_status == "TIMEOUT"]
    dropped_records = [
        r for r in accepted_records if r.final_status not in TERMINAL_STATUSES
    ]
    unsubmitted_records = [r for r in records if r.execution_id is None]

    http_500_count = (
        sum(1 for r in records if r.http_status == 500)
        + poll_http_500_count
        + replay_http_500_count
    )

    # Check both: (a) 1 key -> >1 execution_ids, and (b) >1 distinct keys -> same execution_id
    duplicate_keys_set: set[str] = {
        k for k, exec_ids in key_to_exec_ids.items() if len(exec_ids) > 1
    }
    exec_id_to_keys: dict[str, set[str]] = {}
    for k, exec_ids in key_to_exec_ids.items():
        for eid in exec_ids:
            exec_id_to_keys.setdefault(eid, set()).add(k)
    for keys_for_exec in exec_id_to_keys.values():
        if len(keys_for_exec) > 1:
            duplicate_keys_set.update(keys_for_exec)
    duplicate_keys = sorted(duplicate_keys_set)

    zero_drop = len(dropped_records) == 0
    zero_idempotency_duplicates = len(duplicate_keys) == 0
    zero_http_500 = http_500_count == 0
    chaos_checks_ok = chaos_event is None or (
        chaos_event.redis_read_during_outage_ok is not False
        and chaos_event.redis_write_fail_closed_503 is not False
    )
    success_rate_pct = (
        round((len(success_records) / len(accepted_records)) * 100.0, 2)
        if accepted_records
        else 0.0
    )

    all_passed = (
        zero_drop
        and zero_idempotency_duplicates
        and zero_http_500
        and chaos_checks_ok
        and len(accepted_records) > 0
        and len(unsubmitted_records) == 0
        and len(failed_records) == 0
    )

    assertions = LoadAssertions(
        zero_drop=zero_drop,
        dropped_execution_ids=[
            r.execution_id for r in dropped_records if r.execution_id is not None
        ],
        zero_idempotency_duplicates=zero_idempotency_duplicates,
        duplicate_idempotency_keys=duplicate_keys,
        zero_http_500=zero_http_500,
        http_500_count=http_500_count,
        success_rate_pct=success_rate_pct,
        all_passed=all_passed,
    )

    submit_samples = [r.submit_ms for r in accepted_records if r.submit_ms > 0]
    process_samples = [r.process_ms for r in success_records if r.process_ms > 0]
    e2e_samples = [r.total_ms for r in success_records if r.total_ms > 0]

    return LoadReport(
        env=client.env_config.name,
        profile=client.env_config.profile,
        base_url=client.env_config.base_url,
        workflow_id=config.workflow_id,
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_s=total_duration_s,
        total_requests=config.requests,
        concurrency=config.concurrency,
        use_idempotency_key=config.use_idempotency_key,
        chaos_mode=provider.mode,
        chaos_event=chaos_event,
        accepted_count=len(accepted_records),
        success_count=len(success_records),
        failed_count=len(failed_records),
        timeout_count=len(timeout_records),
        unsubmitted_count=len(unsubmitted_records),
        backpressure_503_total=sum(r.backpressure_503_count for r in records),
        rate_limit_429_total=sum(r.rate_limit_429_count for r in records),
        http_500_total=http_500_count,
        submit_latency=compute_latency_stats(submit_samples),
        process_latency=compute_latency_stats(process_samples),
        e2e_latency=compute_latency_stats(e2e_samples),
        assertions=assertions,
        records=records,
    )
