"""Unit tests for qa.runners.scenario_runner using httpx.MockTransport."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from qa.client import DocumentIAClient
from qa.config import ResolvedEnvConfig
from qa.fixtures import DEFAULT_SCENARIOS_DIR, discover_scenario_files
from qa.models import ScenarioDefinition
from qa.runners.scenario_runner import ScenarioLoadError, load_scenarios, run_scenario


def _make_env(profile: str = "develop") -> ResolvedEnvConfig:
    return ResolvedEnvConfig(
        name="staging" if profile == "develop" else "sandbox",
        base_url="https://api.test.local",
        profile=profile,  # type: ignore[arg-type]
        api_key_env="staging",
        allow_load=True,
        allow_chaos=True,
        requires_explicit_allow=False,
        default_timeout_s=10.0,
        default_poll_interval_s=0.01,
        default_poll_timeout_s=2.0,
        api_key=SecretStr("dia_staging_2_ABCDEFGH_" + "A" * 52 + "_ABCD"),
    )


def test_all_builtin_yaml_scenarios_validate_cleanly(tmp_path: Path) -> None:
    files = discover_scenario_files(
        fixtures_dir=tmp_path,
        default_scenarios_dir=DEFAULT_SCENARIOS_DIR,
    )
    assert len(files) >= 4
    scenarios = load_scenarios(files)
    ids = {s.id for s in scenarios}
    assert "smoke-health" in ids
    assert "func-v2-invalid-date-anomaly" in ids
    assert "func-v1-2ddoc-compat" in ids
    assert "sec-ssrf-cloud-metadata" in ids


@pytest.mark.asyncio
async def test_async_v2_scenario_passes_with_transient_poll_503(tmp_path: Path) -> None:
    poll_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        if (
            request.method == "POST"
            and request.url.path
            == "/api/v2/workflows/document-barcode-extraction-v2/execute"
        ):
            assert request.headers.get("X-API-KEY")
            return httpx.Response(
                200,
                json={"status": "success", "data": {"execution_id": "exec-42"}},
            )
        if request.method == "GET" and request.url.path == "/api/v1/executions/exec-42":
            poll_calls += 1
            if poll_calls == 1:
                return httpx.Response(503, headers={"Retry-After": "0.01"})
            if poll_calls == 2:
                return httpx.Response(
                    200, json={"id": "exec-42", "status": "STARTED", "data": {}}
                )
            return httpx.Response(
                200,
                json={
                    "id": "exec-42",
                    "status": "SUCCESS",
                    "purged": False,
                    "data": {
                        "total_processing_time_ms": 320,
                        "result": {"barcodes": []},
                    },
                },
            )
        return httpx.Response(404)

    scenario = ScenarioDefinition.model_validate(
        {
            "id": "test-async-v2",
            "tags": ["baseline", "synthetic"],
            "request": {
                "api_version": "v2",
                "mode": "async",
                "workflow_id": "document-barcode-extraction-v2",
                "file": "synthetic:minimal_pdf",
                "poll_interval_s": 0.01,
                "max_timeout_s": 2.0,
            },
            "expect": {
                "http_status": 200,
                "execution_status": "SUCCESS",
                "purged": False,
                "barcodes": {"min_count": 0},
            },
        }
    )

    async with DocumentIAClient(
        _make_env("develop"), transport=httpx.MockTransport(handler)
    ) as client:
        result = await run_scenario(client, scenario, fixtures_dir=tmp_path)

    assert result.status == "PASS"
    assert poll_calls == 3
    assert result.snapshot is not None
    assert result.snapshot.execution_id == "exec-42"
    assert result.snapshot.total_processing_time_ms == 320


@pytest.mark.asyncio
async def test_missing_local_fixture_skipped_or_failed_when_strict(
    tmp_path: Path,
) -> None:
    scenario = ScenarioDefinition.model_validate(
        {
            "id": "test-missing-real-doc",
            "tags": ["real"],
            "request": {
                "api_version": "v2",
                "mode": "async",
                "workflow_id": "document-classification-extraction-v2",
                "file": "cni/does_not_exist.pdf",
            },
            "expect": {"http_status": 200, "execution_status": "SUCCESS"},
        }
    )

    async with DocumentIAClient(
        _make_env("develop"),
        transport=httpx.MockTransport(lambda req: httpx.Response(500)),
    ) as client:
        skipped = await run_scenario(
            client, scenario, fixtures_dir=tmp_path, strict_fixtures=False
        )
        failed = await run_scenario(
            client, scenario, fixtures_dir=tmp_path, strict_fixtures=True
        )

    assert skipped.status == "SKIPPED"
    assert "not found" in skipped.message.lower()
    assert failed.status == "FAIL"


@pytest.mark.asyncio
async def test_negative_scenario_accepts_sync_400_and_async_failed(
    tmp_path: Path,
) -> None:
    scenario = ScenarioDefinition.model_validate(
        {
            "id": "neg-ssrf-test",
            "tags": ["security"],
            "request": {
                "api_version": "v2",
                "mode": "async",
                "workflow_id": "document-barcode-extraction-v2",
                "file_url": "https://127.0.0.1/secret.pdf",
                "poll_interval_s": 0.01,
            },
            "expect": {
                "http_status": [200, 400],
                "execution_status": "FAILED",
            },
        }
    )

    # Case 1: API rejects synchronously with 400
    async with DocumentIAClient(
        _make_env("develop"),
        transport=httpx.MockTransport(
            lambda req: httpx.Response(400, json={"detail": "SSRF blocked"})
        ),
    ) as client_400:
        res_400 = await run_scenario(client_400, scenario, fixtures_dir=tmp_path)
    assert res_400.status == "PASS"

    # Case 2: API returns 200 and worker marks FAILED
    def async_fail_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json={"data": {"execution_id": "exec-ssrf"}})
        return httpx.Response(
            200, json={"id": "exec-ssrf", "status": "FAILED", "data": {}}
        )

    async with DocumentIAClient(
        _make_env("develop"),
        transport=httpx.MockTransport(async_fail_handler),
    ) as client_async:
        res_async = await run_scenario(client_async, scenario, fixtures_dir=tmp_path)
    assert res_async.status == "PASS"


@pytest.mark.asyncio
async def test_develop_scenario_is_skipped_on_baseline_profile(tmp_path: Path) -> None:
    scenario = ScenarioDefinition.model_validate(
        {
            "id": "dev-only",
            "min_profile": "develop",
            "request": {
                "mode": "http",
                "method": "GET",
                "path": "/api/v1/health",
                "auth": "none",
            },
            "expect": {"http_status": 200},
        }
    )

    async with DocumentIAClient(
        _make_env("baseline"),
        transport=httpx.MockTransport(lambda req: httpx.Response(200)),
    ) as client:
        res = await run_scenario(client, scenario, fixtures_dir=tmp_path)
    assert res.status == "SKIPPED"


@pytest.mark.asyncio
async def test_v1_params_and_v2_sync_execution(tmp_path: Path) -> None:
    seen_paths: list[str] = []
    seen_bodies: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_paths.append(req.url.path)
        seen_bodies.append(req.read())
        if req.url.path == "/api/v1/workflows/document-extraction-mistral-v1/execute":
            return httpx.Response(200, json={"data": {"execution_id": "exec-v1"}})
        if req.url.path == "/api/v1/executions/exec-v1":
            return httpx.Response(
                200,
                json={
                    "id": "exec-v1",
                    "status": "SUCCESS",
                    "purged": False,
                    "data": {
                        "total_processing_time_ms": 210,
                        "result": {"barcodes": []},
                    },
                },
            )
        if (
            req.url.path
            == "/api/v2/workflows/document-barcode-extraction-v2/execute-sync"
        ):
            return httpx.Response(
                200,
                json={
                    "id": "exec-sync",
                    "status": "SUCCESS",
                    "purged": False,
                    "data": {
                        "total_processing_time_ms": 95,
                        "result": {"barcodes": []},
                    },
                },
            )
        return httpx.Response(404)

    sc_v1 = ScenarioDefinition.model_validate(
        {
            "id": "test-v1-params",
            "request": {
                "api_version": "v1",
                "mode": "async",
                "workflow_id": "document-extraction-mistral-v1",
                "file": "synthetic:cni_pdf",
                "classification_parameters": {"llm-model": "albert-small"},
                "extraction_parameters": {"document-type": "cni"},
                "poll_interval_s": 0.01,
            },
            "expect": {"http_status": 200, "execution_status": "SUCCESS"},
        }
    )
    sc_sync = ScenarioDefinition.model_validate(
        {
            "id": "test-v2-sync",
            "request": {
                "api_version": "v2",
                "mode": "sync",
                "workflow_id": "document-barcode-extraction-v2",
                "file": "synthetic:valid_png",
            },
            "expect": {
                "http_status": 200,
                "execution_status": "SUCCESS",
                "purged": False,
            },
        }
    )

    async with DocumentIAClient(
        _make_env("develop"), transport=httpx.MockTransport(handler)
    ) as client:
        r_v1 = await run_scenario(client, sc_v1, fixtures_dir=tmp_path)
        r_sync = await run_scenario(client, sc_sync, fixtures_dir=tmp_path)

    assert r_v1.status == "PASS", r_v1.message
    assert r_sync.status == "PASS", r_sync.message
    assert b'name="classification-parameters"' in seen_bodies[0]
    assert b'name="extraction-parameters"' in seen_bodies[0]
    assert "/api/v2/workflows/document-barcode-extraction-v2/execute-sync" in seen_paths


def test_load_scenarios_malformed_yaml_raises_scenario_load_error(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "broken.yaml"
    bad.write_text("scenarios:\n  - id: [unclosed\n", encoding="utf-8")
    with pytest.raises(ScenarioLoadError, match="Failed to parse YAML"):
        load_scenarios([bad])


@pytest.mark.asyncio
async def test_http_mode_authenticated_scenario_errors_when_api_key_missing(
    tmp_path: Path,
) -> None:
    env_no_key = _make_env("develop").model_copy(update={"api_key": None})
    scenario = ScenarioDefinition.model_validate(
        {
            "id": "smoke-workflows-auth-required",
            "request": {
                "mode": "http",
                "method": "GET",
                "path": "/api/v2/workflows/",
                "auth": "default",
            },
            "expect": {"http_status": 200},
        }
    )
    async with DocumentIAClient(
        env_no_key, transport=httpx.MockTransport(lambda req: httpx.Response(401))
    ) as client:
        res = await run_scenario(client, scenario, fixtures_dir=tmp_path)
    assert res.status == "ERROR"
    assert "API key missing" in res.message
