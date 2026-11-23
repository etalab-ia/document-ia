"""Typed async HTTP client for Document IA API (v1, v2, executions, admin)."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Self

import httpx

from qa.config import ResolvedEnvConfig, sanitize_headers
from qa.fixtures import ResolvedFixture

API_KEY_HEADER = "X-API-KEY"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENT_REPLAY_HEADER = "Idempotent-Replay"

RATE_LIMIT_HEADER_NAMES = (
    "X-RateLimit-Remaining-Minute",
    "X-RateLimit-Remaining-Daily",
    "X-RateLimit-Reset-Minute",
    "X-RateLimit-Reset-Daily",
    "Retry-After",
)

TRANSIENT_POLL_STATUSES = frozenset({429, 502, 503, 504})
NON_TERMINAL_EXECUTION_STATUSES = frozenset({"STARTED", "PROCESSING", "PENDING"})


@dataclass(slots=True)
class HttpResponse:
    """Normalized HTTP response wrapper with safe header lookup."""

    status_code: int
    headers: dict[str, str]
    body_json: Any | None
    body_text: str
    elapsed_ms: float

    def get_header(self, name: str) -> str | None:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None

    def sanitized_headers(self) -> dict[str, str]:
        return sanitize_headers(self.headers)


@dataclass(slots=True)
class PollOutcome:
    """Result of polling GET /api/v1/executions/{execution_id}."""

    execution_id: str
    final_status: str
    elapsed_ms: float
    polls: int
    last_response: HttpResponse | None
    transient_errors: int = 0
    http_500_errors: int = 0


def extract_execution_id(payload: Any) -> str | None:
    """Extract execution_id from v1/v2 execute responses or ProblemDetail errors."""
    if not isinstance(payload, dict):
        return None

    data_field = payload.get("data")
    if isinstance(data_field, dict):
        if data_field.get("execution_id"):
            return str(data_field["execution_id"])
        if data_field.get("id"):
            return str(data_field["id"])

    errors_field = payload.get("errors")
    if isinstance(errors_field, dict) and errors_field.get("execution_id"):
        return str(errors_field["execution_id"])

    if payload.get("execution_id"):
        return str(payload["execution_id"])
    if payload.get("id"):
        return str(payload["id"])

    return None


def extract_error_code(payload: Any) -> str | None:
    """Extract a structured error identifier from ProblemDetail payloads."""
    if not isinstance(payload, dict):
        return None
    errors_field = payload.get("errors")
    if isinstance(errors_field, dict) and isinstance(errors_field.get("error"), str):
        return errors_field["error"]
    if isinstance(payload.get("code"), str):
        return payload["code"]
    if isinstance(payload.get("detail"), str):
        return payload["detail"]
    return None


def extract_rate_limit_headers(response: HttpResponse) -> dict[str, str]:
    """Extract standard rate-limit and retry headers from an HTTP response."""
    extracted: dict[str, str] = {}
    for header_name in RATE_LIMIT_HEADER_NAMES:
        val = response.get_header(header_name)
        if val is not None:
            extracted[header_name] = val
    return extracted


def parse_retry_after(response: HttpResponse, default_s: float = 2.0) -> float:
    """Parse Retry-After header as seconds (bounded safely)."""
    raw = response.get_header("Retry-After")
    if raw is None:
        return default_s
    try:
        parsed = float(raw.strip())
        if parsed > 0:
            return min(parsed, 60.0)
    except ValueError:
        pass
    return default_s


def _wrap_response(raw: httpx.Response, elapsed_ms: float) -> HttpResponse:
    parsed_json: Any | None = None
    body_text = raw.text
    if body_text:
        try:
            parsed_json = raw.json()
        except ValueError:
            parsed_json = None
    return HttpResponse(
        status_code=raw.status_code,
        headers=dict(raw.headers),
        body_json=parsed_json,
        body_text=body_text,
        elapsed_ms=elapsed_ms,
    )


class DocumentIAClient:
    """Async HTTP client wrapping Document IA API endpoints."""

    def __init__(
        self,
        env_config: ResolvedEnvConfig,
        *,
        concurrency: int = 10,
        timeout_s: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.env_config = env_config
        self.base_url = env_config.base_url.rstrip("/")
        effective_timeout = (
            timeout_s if timeout_s is not None else env_config.default_timeout_s
        )
        limits = httpx.Limits(
            max_connections=max(20, concurrency * 2),
            max_keepalive_connections=max(10, concurrency * 2),
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            limits=limits,
            timeout=httpx.Timeout(effective_timeout, connect=15.0),
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_headers(
        self,
        *,
        use_admin_key: bool = False,
        api_key_override: str | None = None,
        omit_api_key: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if not omit_api_key:
            key: str | None
            if api_key_override is not None:
                key = api_key_override
            elif use_admin_key:
                key = self.env_config.get_admin_api_key()
            else:
                key = self.env_config.get_api_key()
            if key:
                headers[API_KEY_HEADER] = key

        if extra_headers:
            headers.update(extra_headers)
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        use_admin_key: bool = False,
        api_key_override: str | None = None,
        omit_api_key: bool = False,
        extra_headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> HttpResponse:
        headers = self._build_headers(
            use_admin_key=use_admin_key,
            api_key_override=api_key_override,
            omit_api_key=omit_api_key,
            extra_headers=extra_headers,
        )
        start = time.perf_counter()
        raw = await self._client.request(
            method=method.upper(),
            url=path,
            params=params,
            json=json_body,
            data=data,
            files=files,
            headers=headers,
            timeout=timeout_s if timeout_s is not None else httpx.USE_CLIENT_DEFAULT,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return _wrap_response(raw, elapsed_ms)

    async def health(self) -> HttpResponse:
        return await self.request("GET", "/api/v1/health", omit_api_key=True)

    async def test_auth(
        self,
        *,
        api_key_override: str | None = None,
        omit_api_key: bool = False,
    ) -> HttpResponse:
        return await self.request(
            "GET",
            "/api/test",
            api_key_override=api_key_override,
            omit_api_key=omit_api_key,
        )

    async def list_workflows_v2(
        self,
        *,
        api_key_override: str | None = None,
        omit_api_key: bool = False,
    ) -> HttpResponse:
        return await self.request(
            "GET",
            "/api/v2/workflows/",
            api_key_override=api_key_override,
            omit_api_key=omit_api_key,
        )

    async def list_extraction_schemas(self) -> HttpResponse:
        return await self.request(
            "GET", "/api/v1/extraction-schemas", omit_api_key=True
        )

    async def execute_workflow_v2(
        self,
        workflow_id: str,
        *,
        fixture: ResolvedFixture | None = None,
        file_url: str | None = None,
        override: dict[str, Any] | None = None,
        raw_override: str | None = None,
        metadata: dict[str, Any] | None = None,
        raw_metadata: str | None = None,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        api_key_override: str | None = None,
        omit_api_key: bool = False,
        sync: bool = False,
        timeout_s: float | None = None,
    ) -> HttpResponse:
        suffix = "execute-sync" if sync else "execute"
        path = f"/api/v2/workflows/{workflow_id}/{suffix}"

        form_data: dict[str, str] = {}
        if file_url is not None:
            form_data["file_url"] = file_url
        if raw_override is not None:
            form_data["override"] = raw_override
        elif override is not None:
            form_data["override"] = json.dumps(override, ensure_ascii=False)
        if raw_metadata is not None:
            form_data["metadata"] = raw_metadata
        elif metadata is not None:
            form_data["metadata"] = json.dumps(metadata, ensure_ascii=False)

        files: dict[str, tuple[str, bytes, str]] | None = None
        if fixture is not None:
            files = {"file": (fixture.name, fixture.content, fixture.content_type)}

        merged_headers = dict(extra_headers or {})
        if idempotency_key is not None:
            merged_headers[IDEMPOTENCY_KEY_HEADER] = idempotency_key

        return await self.request(
            "POST",
            path,
            data=form_data or None,
            files=files,
            extra_headers=merged_headers,
            api_key_override=api_key_override,
            omit_api_key=omit_api_key,
            timeout_s=timeout_s,
        )

    async def execute_workflow_v1(
        self,
        workflow_id: str,
        *,
        fixture: ResolvedFixture | None = None,
        file_url: str | None = None,
        classification_parameters: dict[str, Any] | None = None,
        extraction_parameters: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        raw_metadata: str | None = None,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        api_key_override: str | None = None,
        omit_api_key: bool = False,
        sync: bool = False,
        timeout_s: float | None = None,
    ) -> HttpResponse:
        suffix = "execute-sync" if sync else "execute"
        path = f"/api/v1/workflows/{workflow_id}/{suffix}"

        form_data: dict[str, str] = {}
        if file_url is not None:
            form_data["file_url"] = file_url
        if classification_parameters is not None:
            form_data["classification-parameters"] = json.dumps(
                classification_parameters, ensure_ascii=False
            )
        if extraction_parameters is not None:
            form_data["extraction-parameters"] = json.dumps(
                extraction_parameters, ensure_ascii=False
            )
        if raw_metadata is not None:
            form_data["metadata"] = raw_metadata
        elif metadata is not None:
            form_data["metadata"] = json.dumps(metadata, ensure_ascii=False)

        files: dict[str, tuple[str, bytes, str]] | None = None
        if fixture is not None:
            files = {"file": (fixture.name, fixture.content, fixture.content_type)}

        merged_headers = dict(extra_headers or {})
        if idempotency_key is not None:
            merged_headers[IDEMPOTENCY_KEY_HEADER] = idempotency_key

        return await self.request(
            "POST",
            path,
            data=form_data or None,
            files=files,
            extra_headers=merged_headers,
            api_key_override=api_key_override,
            omit_api_key=omit_api_key,
            timeout_s=timeout_s,
        )

    async def get_execution(
        self,
        execution_id: str,
        *,
        is_debug_mode: bool = False,
        api_key_override: str | None = None,
        omit_api_key: bool = False,
    ) -> HttpResponse:
        params = {"is_debug_mode": "true"} if is_debug_mode else None
        return await self.request(
            "GET",
            f"/api/v1/executions/{execution_id}",
            params=params,
            api_key_override=api_key_override,
            omit_api_key=omit_api_key,
        )

    async def poll_execution(
        self,
        execution_id: str,
        *,
        poll_interval_s: float | None = None,
        max_timeout_s: float | None = None,
        stagger_s: float = 0.0,
    ) -> PollOutcome:
        """Poll GET /api/v1/executions/{id} until terminal status or timeout.

        Resilient to transient 429/502/503/504 responses and httpx transport errors.
        """
        interval = (
            poll_interval_s
            if poll_interval_s is not None
            else self.env_config.default_poll_interval_s
        )
        timeout = (
            max_timeout_s
            if max_timeout_s is not None
            else self.env_config.default_poll_timeout_s
        )

        start = time.perf_counter()
        if stagger_s > 0:
            await asyncio.sleep(stagger_s)

        polls = 0
        transient_errors = 0
        http_500_errors = 0
        last_response: HttpResponse | None = None

        while True:
            elapsed_s = time.perf_counter() - start
            if elapsed_s >= timeout:
                return PollOutcome(
                    execution_id=execution_id,
                    final_status="TIMEOUT",
                    elapsed_ms=elapsed_s * 1000.0,
                    polls=polls,
                    last_response=last_response,
                    transient_errors=transient_errors,
                    http_500_errors=http_500_errors,
                )

            polls += 1
            try:
                resp = await self.get_execution(execution_id)
                last_response = resp
                if resp.status_code == 200 and isinstance(resp.body_json, dict):
                    status = str(resp.body_json.get("status", "")).upper()
                    if status and status not in NON_TERMINAL_EXECUTION_STATUSES:
                        return PollOutcome(
                            execution_id=execution_id,
                            final_status=status,
                            elapsed_ms=(time.perf_counter() - start) * 1000.0,
                            polls=polls,
                            last_response=resp,
                            transient_errors=transient_errors,
                            http_500_errors=http_500_errors,
                        )
                elif resp.status_code == 500:
                    http_500_errors += 1
                elif resp.status_code in TRANSIENT_POLL_STATUSES:
                    transient_errors += 1
                    wait_s = parse_retry_after(resp, default_s=max(interval, 1.0))
                    remaining = timeout - (time.perf_counter() - start)
                    if remaining <= 0:
                        continue
                    await asyncio.sleep(min(wait_s, remaining))
                    continue
                elif resp.status_code in (401, 403, 404):
                    return PollOutcome(
                        execution_id=execution_id,
                        final_status=f"HTTP_{resp.status_code}",
                        elapsed_ms=(time.perf_counter() - start) * 1000.0,
                        polls=polls,
                        last_response=resp,
                        transient_errors=transient_errors,
                        http_500_errors=http_500_errors,
                    )
            except httpx.HTTPError:
                transient_errors += 1

            remaining = timeout - (time.perf_counter() - start)
            if remaining <= 0:
                continue
            await asyncio.sleep(min(interval, remaining))

    # --- Admin routes (require QA_<ENV>_ADMIN_API_KEY) ---

    async def list_organizations(self) -> HttpResponse:
        return await self.request(
            "GET", "/api/v1/admin/organizations", use_admin_key=True
        )

    async def create_webhook(
        self,
        organization_id: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        payload: dict[str, Any] = {"url": url, "headers": headers or {}}
        return await self.request(
            "POST",
            f"/api/v1/admin/organizations/{organization_id}/webhooks",
            json_body=payload,
            use_admin_key=True,
        )

    async def delete_webhook(
        self, organization_id: str, webhook_id: str
    ) -> HttpResponse:
        return await self.request(
            "DELETE",
            f"/api/v1/admin/organizations/{organization_id}/webhooks/{webhook_id}",
            use_admin_key=True,
        )
