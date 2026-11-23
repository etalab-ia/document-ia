"""Declarative YAML scenario loader and executor."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import yaml

from qa.client import DocumentIAClient, HttpResponse, extract_execution_id
from qa.fixtures import (
    FixtureNotFoundError,
    FixturePathTraversalError,
    ResolvedFixture,
    resolve_fixture,
)
from qa.matcher import build_execution_snapshot, evaluate_expectations
from qa.models import (
    AssertionResult,
    EnvProfile,
    ScenarioDefinition,
    ScenarioExpect,
    ScenarioFile,
    ScenarioResult,
)

INVALID_DEMO_API_KEY = "_".join(
    ("dia", "staging", "2", "BADPREFX", "INVALIDKEYBODY" + "0" * 28, "0000")
)


class ScenarioLoadError(RuntimeError):
    """Raised when a YAML scenario file is malformed or contains duplicate IDs."""


def load_scenarios(paths: list[Path]) -> list[ScenarioDefinition]:
    """Load and validate all ScenarioDefinition entries from YAML files."""
    loaded: list[ScenarioDefinition] = []
    seen_ids: dict[str, str] = {}

    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, ValueError, OSError) as exc:
            raise ScenarioLoadError(f"Failed to parse YAML '{path}': {exc}") from exc

        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ScenarioLoadError(
                f"Scenario file '{path}' must contain a YAML mapping with key 'scenarios'."
            )

        try:
            parsed = ScenarioFile.model_validate(raw)
        except (ValueError, KeyError, RuntimeError, OSError) as exc:
            raise ScenarioLoadError(
                f"Invalid scenario schema in '{path}': {exc}"
            ) from exc

        for scenario in parsed.scenarios:
            if scenario.id in seen_ids:
                raise ScenarioLoadError(
                    f"Duplicate scenario id '{scenario.id}' found in '{path}' "
                    f"(already defined in '{seen_ids[scenario.id]}')."
                )
            seen_ids[scenario.id] = str(path)
            scenario.source_file = str(path)
            loaded.append(scenario)

    return loaded


def matches_tag_filter(
    item_tags: list[str],
    *,
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
) -> bool:
    """Return True if an item's tags match include/exclude filters."""
    normalized = {t.strip().lower() for t in item_tags if t.strip()}
    if exclude_tags and (normalized & exclude_tags):
        return False
    if include_tags is not None:
        return bool(normalized & include_tags)
    return True


def filter_scenarios(
    scenarios: list[ScenarioDefinition],
    *,
    include_tags: set[str] | None = None,
    exclude_tags: set[str] | None = None,
    scenario_ids: set[str] | None = None,
    target_profile: EnvProfile | None = None,
) -> list[ScenarioDefinition]:
    """Filter scenarios by ID, tags, and optional profile compatibility."""
    filtered: list[ScenarioDefinition] = []
    for scenario in scenarios:
        if scenario_ids is not None:
            if scenario.id not in scenario_ids:
                continue
        else:
            if not matches_tag_filter(
                scenario.tags,
                include_tags=include_tags,
                exclude_tags=exclude_tags,
            ):
                continue
            if target_profile == "baseline" and scenario.min_profile == "develop":
                continue
        filtered.append(scenario)
    return filtered


def _needs_polling(expect: ScenarioExpect, http_response: HttpResponse) -> bool:
    """Decide whether an async execution response should be polled."""
    if http_response.status_code != 200:
        return False
    return (
        expect.execution_status is not None
        or expect.classification is not None
        or expect.extraction is not None
        or expect.barcodes is not None
        or expect.purged is not None
    )


def _is_clean_sync_rejection(
    expect: ScenarioExpect, http_response: HttpResponse
) -> bool:
    """Return True when a negative scenario accepts either 4xx OR 200+FAILED and got 4xx."""
    if http_response.status_code < 400:
        return False
    expected_statuses = (
        [expect.http_status]
        if isinstance(expect.http_status, int)
        else list(expect.http_status)
    )
    return http_response.status_code in expected_statuses and 200 in expected_statuses


async def run_scenario(
    client: DocumentIAClient,
    scenario: ScenarioDefinition,
    *,
    fixtures_dir: Path,
    strict_fixtures: bool = False,
) -> ScenarioResult:
    """Execute a single declarative scenario and evaluate all expectations."""
    start = time.perf_counter()
    target_profile = client.env_config.profile

    if target_profile == "baseline" and scenario.min_profile == "develop":
        return ScenarioResult(
            id=scenario.id,
            description=scenario.description,
            kind="scenario",
            tags=list(scenario.tags),
            status="SKIPPED",
            duration_ms=(time.perf_counter() - start) * 1000.0,
            message="Skipped on baseline profile (requires min_profile=develop).",
        )

    req = scenario.request
    fixture: ResolvedFixture | None = None
    if req.file is not None:
        try:
            fixture = resolve_fixture(req.file, fixtures_dir)
        except FixtureNotFoundError as exc:
            status = "FAIL" if strict_fixtures else "SKIPPED"
            return ScenarioResult(
                id=scenario.id,
                description=scenario.description,
                kind="scenario",
                tags=list(scenario.tags),
                status=status,
                duration_ms=(time.perf_counter() - start) * 1000.0,
                message=str(exc),
            )
        except FixturePathTraversalError as exc:
            return ScenarioResult(
                id=scenario.id,
                description=scenario.description,
                kind="scenario",
                tags=list(scenario.tags),
                status="FAIL",
                duration_ms=(time.perf_counter() - start) * 1000.0,
                message=str(exc),
            )

    omit_api_key = req.auth == "none"
    use_admin_key = req.auth == "admin"
    api_key_override = req.api_key_override
    if req.auth == "invalid" and api_key_override is None:
        api_key_override = INVALID_DEMO_API_KEY

    if use_admin_key and not client.env_config.get_admin_api_key():
        return ScenarioResult(
            id=scenario.id,
            description=scenario.description,
            kind="scenario",
            tags=list(scenario.tags),
            status="SKIPPED",
            duration_ms=(time.perf_counter() - start) * 1000.0,
            message="Skipped: admin API key is not configured for this environment.",
        )

    if (
        req.auth == "default"
        and api_key_override is None
        and not client.env_config.get_api_key()
    ):
        return ScenarioResult(
            id=scenario.id,
            description=scenario.description,
            kind="scenario",
            tags=list(scenario.tags),
            status="ERROR",
            duration_ms=(time.perf_counter() - start) * 1000.0,
            message=(
                f"API key missing for environment '{client.env_config.name}'. "
                f"Set QA_{client.env_config.name.upper()}_API_KEY in environment or .env.qa."
            ),
        )

    try:
        http_response: HttpResponse
        execution_response: HttpResponse | None = None
        execution_status_override: str | None = None

        if req.mode == "http":
            if not req.path:
                return ScenarioResult(
                    id=scenario.id,
                    description=scenario.description,
                    kind="scenario",
                    tags=list(scenario.tags),
                    status="ERROR",
                    duration_ms=(time.perf_counter() - start) * 1000.0,
                    message="Scenario with mode='http' must define request.path.",
                )
            http_response = await client.request(
                req.method,
                req.path,
                use_admin_key=use_admin_key,
                api_key_override=api_key_override,
                omit_api_key=omit_api_key,
                extra_headers=req.headers or None,
            )
        else:
            if not req.workflow_id:
                return ScenarioResult(
                    id=scenario.id,
                    description=scenario.description,
                    kind="scenario",
                    tags=list(scenario.tags),
                    status="ERROR",
                    duration_ms=(time.perf_counter() - start) * 1000.0,
                    message="Workflow scenario must define request.workflow_id.",
                )
            is_sync = req.mode == "sync"
            if req.api_version == "v2":
                http_response = await client.execute_workflow_v2(
                    req.workflow_id,
                    fixture=fixture,
                    file_url=req.file_url,
                    override=req.override,
                    raw_override=req.raw_override,
                    metadata=req.metadata,
                    raw_metadata=req.raw_metadata,
                    idempotency_key=req.idempotency_key,
                    extra_headers=req.headers or None,
                    api_key_override=api_key_override,
                    omit_api_key=omit_api_key,
                    sync=is_sync,
                    timeout_s=req.max_timeout_s if is_sync else None,
                )
            else:
                http_response = await client.execute_workflow_v1(
                    req.workflow_id,
                    fixture=fixture,
                    file_url=req.file_url,
                    classification_parameters=req.classification_parameters,
                    extraction_parameters=req.extraction_parameters,
                    metadata=req.metadata,
                    raw_metadata=req.raw_metadata,
                    idempotency_key=req.idempotency_key,
                    extra_headers=req.headers or None,
                    api_key_override=api_key_override,
                    omit_api_key=omit_api_key,
                    sync=is_sync,
                    timeout_s=req.max_timeout_s if is_sync else None,
                )

            if is_sync:
                execution_response = http_response
            elif _needs_polling(scenario.expect, http_response):
                execution_id = extract_execution_id(http_response.body_json)
                if not execution_id:
                    return ScenarioResult(
                        id=scenario.id,
                        description=scenario.description,
                        kind="scenario",
                        tags=list(scenario.tags),
                        status="FAIL",
                        duration_ms=(time.perf_counter() - start) * 1000.0,
                        message="Async execute returned 200 without an execution_id.",
                        assertions=[
                            AssertionResult(
                                path="execution_id",
                                passed=False,
                                expected="non-empty string",
                                actual=None,
                                message="Missing execution_id in execute response",
                            )
                        ],
                        snapshot=build_execution_snapshot(http_response),
                    )
                poll_outcome = await client.poll_execution(
                    execution_id,
                    poll_interval_s=req.poll_interval_s,
                    max_timeout_s=req.max_timeout_s,
                )
                execution_response = poll_outcome.last_response
                execution_status_override = poll_outcome.final_status

        effective_expect = scenario.expect
        if _is_clean_sync_rejection(scenario.expect, http_response):
            # Synchronous 4xx rejection satisfied the negative scenario; skip execution-level checks.
            effective_expect = scenario.expect.model_copy(
                update={
                    "execution_status": None,
                    "purged": None,
                    "classification": None,
                    "extraction": None,
                    "barcodes": None,
                }
            )

        assertions = evaluate_expectations(
            effective_expect,
            http_response=http_response,
            execution_response=execution_response,
            execution_status_override=execution_status_override,
            target_profile=target_profile,
        )
        all_passed = all(a.passed for a in assertions)
        failed_messages = [a.message for a in assertions if not a.passed and a.message]
        snapshot = build_execution_snapshot(http_response, execution_response)

        return ScenarioResult(
            id=scenario.id,
            description=scenario.description,
            kind="scenario",
            tags=list(scenario.tags),
            status="PASS" if all_passed else "FAIL",
            duration_ms=(time.perf_counter() - start) * 1000.0,
            message="" if all_passed else "; ".join(failed_messages[:3]),
            assertions=assertions,
            snapshot=snapshot,
        )
    except httpx.HTTPError as exc:
        return ScenarioResult(
            id=scenario.id,
            description=scenario.description,
            kind="scenario",
            tags=list(scenario.tags),
            status="ERROR",
            duration_ms=(time.perf_counter() - start) * 1000.0,
            message=f"HTTP transport error: {type(exc).__name__}: {exc}",
        )
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        return ScenarioResult(
            id=scenario.id,
            description=scenario.description,
            kind="scenario",
            tags=list(scenario.tags),
            status="ERROR",
            duration_ms=(time.perf_counter() - start) * 1000.0,
            message=f"Unexpected error: {type(exc).__name__}: {exc}",
        )


async def run_scenarios(
    client: DocumentIAClient,
    scenarios: list[ScenarioDefinition],
    *,
    fixtures_dir: Path,
    strict_fixtures: bool = False,
    concurrency: int = 4,
) -> list[ScenarioResult]:
    """Execute a list of declarative scenarios with bounded concurrency."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(sc: ScenarioDefinition) -> ScenarioResult:
        async with semaphore:
            return await run_scenario(
                client,
                sc,
                fixtures_dir=fixtures_dir,
                strict_fixtures=strict_fixtures,
            )

    tasks = [_bounded(sc) for sc in scenarios]
    return list(await asyncio.gather(*tasks))
