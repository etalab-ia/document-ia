"""Unit tests for qa.runners.load_runner and qa.runners.chaos."""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from qa.client import DocumentIAClient
from qa.config import ResolvedEnvConfig
from qa.fixtures import resolve_fixture
from qa.runners.chaos import (
    ChaosConfigError,
    DockerChaosProvider,
    ManualPauseChaosProvider,
    ScalingoChaosProvider,
    _default_cmd_runner,
    validate_safe_identifier,
)
from qa.runners.load_runner import LoadConfig, compute_latency_stats, run_load_test


def _make_env() -> ResolvedEnvConfig:
    return ResolvedEnvConfig(
        name="local",
        base_url="http://localhost:8000",
        profile="develop",
        api_key_env="dev",
        allow_load=True,
        allow_chaos=True,
        requires_explicit_allow=False,
        default_timeout_s=10.0,
        default_poll_interval_s=0.01,
        default_poll_timeout_s=1.0,
        api_key=SecretStr("dia_dev_2_LOCALKEY_" + "A" * 52 + "_ABCD"),
    )


def test_compute_latency_stats() -> None:
    stats = compute_latency_stats([10.0, 20.0, 30.0, 40.0, 100.0])
    assert stats.count == 5
    assert stats.min_ms == 10.0
    assert stats.max_ms == 100.0
    assert stats.p50_ms == 30.0
    assert stats.avg_ms == 40.0
    assert stats.p95_ms >= 80.0


@pytest.mark.asyncio
async def test_load_runner_zero_drop_with_503_and_429_retries(tmp_path: Path) -> None:
    attempts_by_key: dict[str, int] = {}
    exec_by_key: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.method == "POST":
            idem = req.headers.get("Idempotency-Key") or str(uuid.uuid4())
            count = attempts_by_key.get(idem, 0) + 1
            attempts_by_key[idem] = count
            # First attempt returns 503 on odd requests, 429 on even requests
            if count == 1:
                if len(attempts_by_key) % 2 == 1:
                    return httpx.Response(503, headers={"Retry-After": "0.01"})
                return httpx.Response(429, headers={"Retry-After": "0.01"})
            eid = exec_by_key.setdefault(idem, str(uuid.uuid4()))
            return httpx.Response(200, json={"data": {"execution_id": eid}})
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            eid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={"id": eid, "status": "SUCCESS", "data": {"result": {}}},
            )
        return httpx.Response(404)

    cfg = LoadConfig(
        requests=6,
        concurrency=3,
        workflow_id="document-barcode-extraction-v2",
        files=["synthetic:minimal_pdf", "synthetic:valid_png"],
        use_idempotency_key=True,
        poll_interval_s=0.01,
        max_timeout_s=2.0,
        retry_backoff_scale=0.0,
    )

    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(handler)
    ) as client:
        report = await run_load_test(client, cfg, fixtures_dir=tmp_path)

    assert report.accepted_count == 6
    assert report.success_count == 6
    assert report.backpressure_503_total == 3
    assert report.rate_limit_429_total == 3
    assert report.http_500_total == 0
    assert report.assertions.zero_drop is True
    assert report.assertions.zero_idempotency_duplicates is True
    assert report.assertions.zero_http_500 is True
    assert report.assertions.all_passed is True
    assert report.e2e_latency.count == 6


@pytest.mark.asyncio
async def test_load_runner_detects_dropped_execution_stuck_in_started(
    tmp_path: Path,
) -> None:
    counter = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal counter
        if req.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.method == "POST":
            counter += 1
            return httpx.Response(
                200, json={"data": {"execution_id": f"exec-{counter}"}}
            )
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            eid = req.url.path.rsplit("/", 1)[-1]
            # exec-2 stays stuck in STARTED forever -> DROP!
            if eid == "exec-2":
                return httpx.Response(
                    200, json={"id": eid, "status": "STARTED", "data": {}}
                )
            return httpx.Response(
                200, json={"id": eid, "status": "SUCCESS", "data": {}}
            )
        return httpx.Response(404)

    cfg = LoadConfig(
        requests=3,
        concurrency=3,
        use_idempotency_key=False,
        poll_interval_s=0.01,
        max_timeout_s=0.08,
    )

    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(handler)
    ) as client:
        report = await run_load_test(client, cfg, fixtures_dir=tmp_path)

    assert report.accepted_count == 3
    assert report.success_count == 2
    assert report.timeout_count == 1
    assert report.assertions.zero_drop is False
    assert report.assertions.dropped_execution_ids == ["exec-2"]
    assert report.assertions.all_passed is False


@pytest.mark.asyncio
async def test_chaos_providers_docker_scalingo_manual_and_validation(
    tmp_path: Path,
) -> None:
    # Safe identifier rejects shell injection
    with pytest.raises(ChaosConfigError):
        validate_safe_identifier("worker; rm -rf /", "chaos_target")
    with pytest.raises(ChaosConfigError):
        validate_safe_identifier("$(whoami)", "scalingo_app")
    with pytest.raises(ChaosConfigError):
        validate_safe_identifier("--force", "chaos_target")
    with pytest.raises(ChaosConfigError):
        validate_safe_identifier("worker\n", "chaos_target")
    with pytest.raises(ChaosConfigError, match="production-named"):
        ScalingoChaosProvider(scalingo_app="document-ia-prod")
    with pytest.raises(ChaosConfigError, match="production-named"):
        ScalingoChaosProvider(scalingo_app="document-ia-production")

    # Docker Redis chaos verifies GET 200 and POST 503 during outage
    executed_cmds: list[list[str]] = []

    def fake_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
        executed_cmds.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    def redis_outage_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/api/v1/executions/exec-existing":
            return httpx.Response(
                200, json={"id": "exec-existing", "status": "SUCCESS"}
            )
        if req.method == "POST":
            return httpx.Response(503, headers={"Retry-After": "5"})
        return httpx.Response(404)

    docker_chaos = DockerChaosProvider(
        target="document-ia-redis",
        downtime_s=0.05,
        cmd_runner=fake_cmd,
    )
    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(redis_outage_handler)
    ) as client:
        event = await docker_chaos.trigger(
            client=client,
            probe_execution_id="exec-existing",
            probe_workflow_id="document-barcode-extraction-v2",
            probe_fixture=resolve_fixture("synthetic:minimal_pdf", tmp_path),
        )

    assert executed_cmds == [
        ["docker", "stop", "document-ia-redis"],
        ["docker", "start", "document-ia-redis"],
    ]
    assert event.redis_read_during_outage_ok is True
    assert event.redis_write_fail_closed_503 is True

    # Scalingo chaos
    scalingo_cmds: list[list[str]] = []
    scalingo_chaos = ScalingoChaosProvider(
        scalingo_app="document-ia-staging",
        target="worker",
        cmd_runner=lambda args: (
            scalingo_cmds.append(args) or subprocess.CompletedProcess(args, 0, "", "")
        ),
    )
    s_event = await scalingo_chaos.trigger()
    assert scalingo_cmds == [
        ["scalingo", "--app", "document-ia-staging", "restart", "worker"]
    ]
    assert s_event.mode == "scalingo"

    # Manual chaos
    prompts: list[str] = []
    manual = ManualPauseChaosProvider(
        target="staging-worker",
        prompt_fn=lambda msg: prompts.append(msg) or "",
    )
    m_event = await manual.trigger()
    assert len(prompts) == 1
    assert m_event.mode == "manual"


@pytest.mark.asyncio
async def test_load_runner_handles_in_progress_409_and_detects_http_500(
    tmp_path: Path,
) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        if req.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.method == "POST":
            calls += 1
            if calls == 1:
                return httpx.Response(
                    409,
                    json={
                        "errors": {
                            "error": "idempotency_in_progress",
                            "execution_id": "exec-in-flight",
                        }
                    },
                )
            return httpx.Response(500, text="Internal Server Error")
        if req.method == "GET" and req.url.path == "/api/v1/executions/exec-in-flight":
            return httpx.Response(
                200, json={"id": "exec-in-flight", "status": "SUCCESS", "data": {}}
            )
        return httpx.Response(404)

    cfg = LoadConfig(
        requests=2,
        concurrency=1,
        use_idempotency_key=False,
        poll_interval_s=0.01,
        max_timeout_s=1.0,
    )
    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(handler)
    ) as client:
        report = await run_load_test(client, cfg, fixtures_dir=tmp_path)

    assert report.accepted_count == 1
    assert report.success_count == 1
    assert report.http_500_total == 1
    assert report.assertions.zero_drop is True
    assert report.assertions.zero_http_500 is False
    assert report.assertions.all_passed is False


@pytest.mark.asyncio
async def test_load_runner_retries_502_504_and_uses_server_processing_ms(
    tmp_path: Path,
) -> None:
    attempts_by_key: dict[str, int] = {}
    exec_by_key: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.method == "POST":
            idem = req.headers.get("Idempotency-Key") or ""
            cnt = attempts_by_key.get(idem, 0) + 1
            attempts_by_key[idem] = cnt
            if cnt == 1:
                status_code = 502 if len(attempts_by_key) % 2 == 1 else 504
                return httpx.Response(status_code, text="Gateway error")
            if idem in exec_by_key:
                return httpx.Response(
                    200,
                    headers={"Idempotent-Replay": "true"},
                    json={"data": {"execution_id": exec_by_key[idem]}},
                )
            eid = str(uuid.uuid4())
            exec_by_key[idem] = eid
            return httpx.Response(200, json={"data": {"execution_id": eid}})
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            eid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "id": eid,
                    "status": "SUCCESS",
                    "data": {"total_processing_time_ms": 420, "result": {}},
                },
            )
        return httpx.Response(404)

    cfg = LoadConfig(
        requests=2,
        concurrency=2,
        use_idempotency_key=True,
        poll_interval_s=0.01,
        max_timeout_s=2.0,
        retry_backoff_scale=0.0,
    )
    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(handler)
    ) as client:
        report = await run_load_test(client, cfg, fixtures_dir=tmp_path)

    assert report.assertions.all_passed is True
    assert report.assertions.zero_idempotency_duplicates is True
    assert any(r.idempotent_replay_count >= 1 for r in report.records)
    assert report.process_latency.min_ms == 420.0
    assert report.process_latency.max_ms == 420.0


@pytest.mark.asyncio
async def test_load_runner_detects_idempotency_duplicate_poll_500_and_redis_chaos_failure(
    tmp_path: Path,
) -> None:
    # 1. Broken idempotency server: replay returns a DIFFERENT execution_id
    post_count = 0

    def broken_idem_handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if req.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.method == "POST":
            post_count += 1
            return httpx.Response(
                200, json={"data": {"execution_id": f"dup-exec-{post_count}"}}
            )
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            eid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={"id": eid, "status": "SUCCESS", "data": {}}
            )
        return httpx.Response(404)

    cfg_dup = LoadConfig(
        requests=2,
        concurrency=2,
        use_idempotency_key=True,
        poll_interval_s=0.01,
        max_timeout_s=1.0,
    )
    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(broken_idem_handler)
    ) as client:
        dup_report = await run_load_test(client, cfg_dup, fixtures_dir=tmp_path)

    assert dup_report.assertions.zero_idempotency_duplicates is False
    assert len(dup_report.assertions.duplicate_idempotency_keys) == 1
    assert dup_report.assertions.all_passed is False

    # 2. HTTP 500 during poll_execution must fail zero_http_500 & all_passed
    poll_attempts = 0

    def poll_500_handler(req: httpx.Request) -> httpx.Response:
        nonlocal poll_attempts
        if req.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.method == "POST":
            return httpx.Response(200, json={"data": {"execution_id": "exec-p500"}})
        if req.method == "GET" and req.url.path == "/api/v1/executions/exec-p500":
            poll_attempts += 1
            if poll_attempts == 1:
                return httpx.Response(500, text="DB glitch")
            return httpx.Response(
                200, json={"id": "exec-p500", "status": "SUCCESS", "data": {}}
            )
        return httpx.Response(404)

    cfg_p500 = LoadConfig(
        requests=1,
        concurrency=1,
        use_idempotency_key=False,
        poll_interval_s=0.01,
        max_timeout_s=1.0,
    )
    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(poll_500_handler)
    ) as client:
        p500_report = await run_load_test(client, cfg_p500, fixtures_dir=tmp_path)

    assert p500_report.http_500_total >= 1
    assert p500_report.assertions.zero_http_500 is False
    assert p500_report.assertions.all_passed is False

    # 3. Redis chaos outage invariant failure must fail all_passed
    def bad_redis_outage_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.method == "POST":
            # Bug: during outage, POST returns 200 instead of fail-closed 503
            return httpx.Response(200, json={"data": {"execution_id": "exec-r1"}})
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            return httpx.Response(
                200, json={"id": "exec-r1", "status": "SUCCESS", "data": {}}
            )
        return httpx.Response(404)

    fake_docker = DockerChaosProvider(
        target="document-ia-redis",
        downtime_s=0.05,
        cmd_runner=lambda args: subprocess.CompletedProcess(args, 0, "", ""),
    )
    cfg_redis = LoadConfig(
        requests=1,
        concurrency=1,
        use_idempotency_key=False,
        chaos_mode="docker",
        chaos_after=1,
        chaos_target="document-ia-redis",
        poll_interval_s=0.01,
        max_timeout_s=1.0,
    )
    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(bad_redis_outage_handler)
    ) as client:
        redis_report = await run_load_test(
            client, cfg_redis, fixtures_dir=tmp_path, chaos_provider=fake_docker
        )

    assert redis_report.chaos_event is not None
    assert redis_report.chaos_event.redis_write_fail_closed_503 is False
    assert redis_report.assertions.all_passed is False


@pytest.mark.asyncio
async def test_manual_chaos_pauses_subsequent_submissions_until_operator_confirms(
    tmp_path: Path,
) -> None:
    post_count = 0
    posts_seen_during_manual_pause: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if req.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.method == "POST":
            post_count += 1
            return httpx.Response(
                200, json={"data": {"execution_id": f"exec-manual-{post_count}"}}
            )
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            eid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={"id": eid, "status": "SUCCESS", "data": {}}
            )
        return httpx.Response(404)

    def operator_prompt(_msg: str) -> str:
        # Sleep briefly in worker thread to give the event loop a chance to run any unblocked tasks
        time.sleep(0.05)
        posts_seen_during_manual_pause.append(post_count)
        return ""

    manual_provider = ManualPauseChaosProvider(
        target="staging-worker",
        prompt_fn=operator_prompt,
    )
    cfg = LoadConfig(
        requests=6,
        concurrency=5,  # intentionally higher than chaos_after=2
        use_idempotency_key=False,
        chaos_mode="manual",
        chaos_after=2,
        poll_interval_s=0.01,
        max_timeout_s=1.0,
    )

    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(handler)
    ) as client:
        report = await run_load_test(
            client,
            cfg,
            fixtures_dir=tmp_path,
            chaos_provider=manual_provider,
        )

    # While operator was prompted, only the first `chaos_after` (2) submissions were sent
    assert posts_seen_during_manual_pause == [2]
    # After operator confirmed, all 6 requests completed
    assert post_count == 6
    assert report.accepted_count == 6
    assert report.success_count == 6
    assert report.assertions.all_passed is True


def test_default_cmd_runner_wraps_subprocess_errors_with_stderr() -> None:
    # 1. CalledProcessError includes stderr
    with (
        patch(
            "qa.runners.chaos.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                returncode=1,
                cmd=["docker", "restart", "missing-container"],
                stderr="Error response from daemon: No such container: missing-container\n",
            ),
        ),
        pytest.raises(
            ChaosConfigError,
            match="No such container: missing-container",
        ),
    ):
        _default_cmd_runner(["docker", "restart", "missing-container"])

    # 2. TimeoutExpired includes timeout and stderr
    with (
        patch(
            "qa.runners.chaos.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=["scalingo", "--app", "doc-ia-staging", "restart", "worker"],
                timeout=60.0,
                stderr="API gateway timeout\n",
            ),
        ),
        pytest.raises(
            ChaosConfigError,
            match="timed out after 60.0s.*API gateway timeout",
        ),
    ):
        _default_cmd_runner(
            ["scalingo", "--app", "doc-ia-staging", "restart", "worker"]
        )

    # 3. FileNotFoundError mentions missing binary
    with (
        patch(
            "qa.runners.chaos.subprocess.run",
            side_effect=FileNotFoundError("No such file or directory: 'scalingo'"),
        ),
        pytest.raises(
            ChaosConfigError,
            match="binary 'scalingo' not found in PATH",
        ),
    ):
        _default_cmd_runner(
            ["scalingo", "--app", "doc-ia-staging", "restart", "worker"]
        )


@pytest.mark.asyncio
async def test_load_runner_emits_realtime_progress_logs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if req.url.path == "/api/v1/health":
            return httpx.Response(200, json={"status": "healthy"})
        if req.method == "POST":
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "0.01"})
            return httpx.Response(
                200, json={"data": {"execution_id": f"exec-prog-{attempts}"}}
            )
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            eid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={"id": eid, "status": "SUCCESS", "data": {}}
            )
        return httpx.Response(404)

    cfg = LoadConfig(
        requests=2,
        concurrency=1,
        use_idempotency_key=False,
        poll_interval_s=0.01,
        max_timeout_s=1.0,
        retry_backoff_scale=0.0,
        show_progress=True,
    )
    async with DocumentIAClient(
        _make_env(), transport=httpx.MockTransport(handler)
    ) as client:
        await run_load_test(client, cfg, fixtures_dir=tmp_path)

    captured = capsys.readouterr().out
    assert "[LOAD] Phase 1/2" in captured
    assert "[SUBMIT-RETRY]" in captured
    assert "[SUBMIT 1/2]" in captured
    assert "[SUBMIT 2/2]" in captured
    assert "[LOAD] Phase 1 terminée : 2/2 exécutions acceptées" in captured
    assert "[POLL [" in captured
    assert "[PENDING (1)]" in captured
