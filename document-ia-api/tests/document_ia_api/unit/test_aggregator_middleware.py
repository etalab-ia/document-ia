from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from document_ia_api.api.middleware.aggregator_middleware import (
    AggregationMiddleware,
    _sanitize_aggregator_metrics,
    _wrap_response_and_capture,
    get_request_payload_safely,
)


@pytest.mark.asyncio
async def test_aggregator_middleware_logs_on_unhandled_500_exception(monkeypatch):
    logged_entries: list[dict] = []

    mock_logger = MagicMock()
    mock_logger.info.side_effect = lambda msg, extra=None: logged_entries.append(
        json.loads(msg)
    )
    orig_get_logger = logging.getLogger

    def patched_get_logger(name=None):
        if name == "aggregator":
            return mock_logger
        return orig_get_logger(name)

    monkeypatch.setattr(
        "document_ia_api.api.middleware.aggregator_middleware.logging.getLogger",
        patched_get_logger,
    )

    middleware = AggregationMiddleware(app=MagicMock())

    request = MagicMock(spec=Request)
    request.method = "GET"
    request.url.path = "/api/v1/failing-endpoint"
    request.client.host = "127.0.0.1"
    request.headers = Headers({"x-request-id": "req-fail-500"})
    request.query_params = {}
    request.path_params = {}
    request.scope = {"route": MagicMock(path="/api/v1/failing-endpoint")}
    request.state = MagicMock()
    request.state.request_id = "req-fail-500"
    request.state.organization = None
    request.state.aggregator_payload = {
        "query_params": {},
        "path_params": {},
        "request_body": None,
    }

    async def call_next_crashing(req: Request) -> Response:
        raise RuntimeError("Database connection pool exhausted")

    with pytest.raises(RuntimeError, match="Database connection pool exhausted"):
        await middleware.dispatch(request, call_next_crashing)

    assert len(logged_entries) == 1
    entry = logged_entries[0]
    assert entry["request_id"] == "req-fail-500"
    assert entry["status_code"] == 500
    assert entry["path"] == "/api/v1/failing-endpoint"
    assert entry["method"] == "GET"


@pytest.mark.asyncio
async def test_wrap_response_and_capture_strict_allowlist():
    raw_response = JSONResponse(
        content={"data": "test_output", "secret": "SUPER_SECRET_123"},
        status_code=200,
        headers={
            "X-Request-Id": "req-999",
            "Idempotent-Replay": "true",
            "Set-Cookie": "session=secret_token",
            "X-Internal-Token": "secret_internal",
        },
    )

    request = MagicMock(spec=Request)
    returned_resp, meta = await _wrap_response_and_capture(request, raw_response)

    # Status and response object
    assert returned_resp.status_code == 200
    assert meta["status_code"] == 200

    # Headers allowlist check: allowed headers kept, sensitive headers pruned
    headers = meta["response_headers"]
    assert "x-request-id" in headers
    assert "idempotent-replay" in headers
    assert "content-type" in headers
    assert "set-cookie" not in headers
    assert "x-internal-token" not in headers

    # Envelope metrics check
    assert "response_body" in meta
    assert meta["response_body"]["bytes"] > 0
    assert "application/json" in meta["response_body"]["content_type"]

    # Zero body preview
    assert "body_preview" not in meta
    assert "response_body_preview" not in meta


@pytest.mark.asyncio
async def test_get_request_payload_safely_masks_and_filters():
    request = MagicMock(spec=Request)
    request.query_params = {
        "limit": "20",
        "api_key": "SECRET_KEY_abc123",  # gitleaks:allow
        "unknown_query": "sensitive_data",
    }
    request.path_params = {
        "execution_id": "exec-456",
        "unauthorized_path_param": "leaked_param",
    }
    request.headers = Headers(
        {
            "content-type": "application/json",
            "content-length": "42",
            "authorization": "Bearer secret_bearer_token",
        }
    )
    request.state = MagicMock()

    await get_request_payload_safely(request)

    payload = request.state.aggregator_payload
    assert payload is not None

    # Query params: allowed kept, unknown masked
    assert payload["query_params"]["limit"] == "20"
    assert payload["query_params"]["api_key"] == "**********"
    assert payload["query_params"]["unknown_query"] == "**********"

    # Path params: only allowed keys retained
    assert payload["path_params"]["execution_id"] == "exec-456"
    assert "unauthorized_path_param" not in payload["path_params"]

    # Request body metric
    assert payload["request_body"]["content_type"] == "application/json"
    assert payload["request_body"]["bytes"] == 42

    # Zero body preview
    assert "body_preview" not in payload
    assert "request_body_preview" not in payload


@pytest.mark.asyncio
async def test_aggregation_middleware_zero_body_preview_in_entry(monkeypatch):
    logged_entries: list[dict] = []

    mock_logger = MagicMock()
    mock_logger.info.side_effect = lambda msg, extra=None: logged_entries.append(
        json.loads(msg)
    )
    orig_get_logger = logging.getLogger

    def patched_get_logger(name=None):
        if name == "aggregator":
            return mock_logger
        return orig_get_logger(name)

    monkeypatch.setattr(
        "document_ia_api.api.middleware.aggregator_middleware.logging.getLogger",
        patched_get_logger,
    )

    app = MagicMock()
    middleware = AggregationMiddleware(app)

    request = MagicMock(spec=Request)
    request.method = "POST"
    request.url.path = "/api/v1/workflows/wf-1/execute"
    request.client.host = "127.0.0.1"
    request.headers = Headers(
        {
            "content-type": "application/json",
            "x-request-id": "req-100",
            "authorization": "Bearer SECRET_JWT_TOKEN",
            "cookie": "session_id=SECRET_COOKIE",
            "x-custom-header": "SECRET_HEADER",
        }
    )
    request.query_params = {
        "limit": "10",
        "token": "SECRET_TOKEN_IN_QUERY",
    }
    request.path_params = {
        "workflow_id": "wf-1",
        "internal_id": "INTERNAL_SECRET",
    }
    request.scope = {"route": MagicMock(path="/api/v1/workflows/{workflow_id}/execute")}
    request.state = MagicMock()
    request.state.request_id = "req-100"
    request.state.organization = MagicMock(id="org-1")

    # Dependency captured payload
    await get_request_payload_safely(request)

    # Next endpoint call returns a response with sensitive data in body
    secret_body_marker = "SECRET_SYNTHETIC_MARKER_9999"  # gitleaks:allow

    async def call_next(req: Request) -> Response:
        return JSONResponse(
            content={"message": "ok", "secret": secret_body_marker},
            status_code=201,
            headers={
                "X-Request-Id": "req-100",
                "Set-Cookie": "sensitive_cookie=value",
            },
        )

    response = await middleware.dispatch(request, call_next)
    assert response.status_code == 201

    assert len(logged_entries) == 1
    entry = logged_entries[0]

    # Verify no raw body previews exist anywhere in the entry
    assert "request_body_preview" not in entry
    assert "response_body_preview" not in entry
    assert "body_preview" not in entry

    # Verify sensitive marker is NEVER present in serialized entry
    entry_json_str = json.dumps(entry)
    assert secret_body_marker not in entry_json_str
    assert "SECRET_JWT_TOKEN" not in entry_json_str
    assert "SECRET_COOKIE" not in entry_json_str
    assert "SECRET_HEADER" not in entry_json_str
    assert "SECRET_TOKEN_IN_QUERY" not in entry_json_str
    assert "INTERNAL_SECRET" not in entry_json_str

    # Verify allowed metadata is correctly logged
    assert entry["request_id"] == "req-100"
    assert entry["method"] == "POST"
    assert entry["headers"] == {
        "content-type": "application/json",
        "x-request-id": "req-100",
    }
    assert entry["query_params"]["limit"] == "10"
    assert entry["query_params"]["token"] == "**********"
    assert entry["path_params"] == {"workflow_id": "wf-1"}
    assert entry["request_body"]["content_type"] == "application/json"
    assert entry["response_body"]["bytes"] > 0


def test_sanitize_aggregator_metrics_allows_only_safe_scalars_and_keys():
    secret_marker = "SECRET_SYNTHETIC_METRICS_VALUE_42"  # gitleaks:allow

    assert _sanitize_aggregator_metrics(None) == {}
    assert _sanitize_aggregator_metrics(["not", "a", "dict"]) == {}
    assert _sanitize_aggregator_metrics(secret_marker) == {}

    sanitized = _sanitize_aggregator_metrics(
        {
            "count_int": 12,
            "ratio_float": 0.75,
            "is_healthy": True,
            "optional_none": None,
            "forbidden_str": secret_marker,
            "forbidden_bytes": b"secret_bytes",
            "forbidden_dict": {"nested": secret_marker},
            "forbidden_list": [1, 2, 3],
            "forbidden_obj": object(),
            "forbidden_nan": float("nan"),
            "forbidden_inf": float("inf"),
            "forbidden_neg_inf": float("-inf"),
            "": 1,
            "   ": 2,
            "bad\nkey": 3,
            "bad\rkey": 4,
            "bad\x00key": 5,
            123: 6,
        }
    )

    assert sanitized == {
        "count_int": 12,
        "ratio_float": 0.75,
        "is_healthy": True,
        "optional_none": None,
    }


@pytest.mark.asyncio
async def test_aggregation_middleware_emits_sanitized_metrics_only(monkeypatch):
    logged_entries: list[dict] = []

    mock_logger = MagicMock()
    mock_logger.info.side_effect = lambda msg, extra=None: logged_entries.append(
        json.loads(msg)
    )
    orig_get_logger = logging.getLogger

    def patched_get_logger(name=None):
        if name == "aggregator":
            return mock_logger
        return orig_get_logger(name)

    monkeypatch.setattr(
        "document_ia_api.api.middleware.aggregator_middleware.logging.getLogger",
        patched_get_logger,
    )

    middleware = AggregationMiddleware(app=MagicMock())
    secret_marker = "SECRET_SYNTHETIC_METRIC_LEAK_999"  # gitleaks:allow

    request = MagicMock(spec=Request)
    request.method = "GET"
    request.url.path = "/api/v1/health"
    request.client.host = "127.0.0.1"
    request.headers = Headers({"x-request-id": "req-metrics-1"})
    request.query_params = {}
    request.path_params = {}
    request.scope = {"route": MagicMock(path="/api/v1/health")}
    request.state = SimpleNamespace(
        request_id="req-metrics-1",
        organization=None,
        aggregator_payload={
            "query_params": {},
            "path_params": {},
            "request_body": None,
        },
    )

    async def call_next(req: Request) -> Response:
        req.state.aggregator_metrics = {
            "nb_execution_to_process": 10,
            "queue_ratio": 0.5,
            "redis_healthy": True,
            "nullable_counter": None,
            "secret_str": secret_marker,
            "secret_dict": {"token": secret_marker},
            "secret_list": [secret_marker],
            "secret_bytes": secret_marker.encode(),
            "nan_value": float("nan"),
            "inf_value": float("inf"),
            "bad\nkey": 99,
            "": 1,
        }
        return JSONResponse(content={"status": "healthy"}, status_code=200)

    response = await middleware.dispatch(request, call_next)
    assert response.status_code == 200
    assert len(logged_entries) == 1

    entry = logged_entries[0]
    assert entry["metrics"] == {
        "nb_execution_to_process": 10,
        "queue_ratio": 0.5,
        "redis_healthy": True,
        "nullable_counter": None,
    }
    assert secret_marker not in json.dumps(entry)


@pytest.mark.asyncio
async def test_aggregation_middleware_omits_metrics_when_absent_or_all_rejected(
    monkeypatch,
):
    logged_entries: list[dict] = []

    mock_logger = MagicMock()
    mock_logger.info.side_effect = lambda msg, extra=None: logged_entries.append(
        json.loads(msg)
    )
    orig_get_logger = logging.getLogger

    def patched_get_logger(name=None):
        if name == "aggregator":
            return mock_logger
        return orig_get_logger(name)

    monkeypatch.setattr(
        "document_ia_api.api.middleware.aggregator_middleware.logging.getLogger",
        patched_get_logger,
    )

    middleware = AggregationMiddleware(app=MagicMock())

    def _make_request(request_id: str) -> Request:
        req = MagicMock(spec=Request)
        req.method = "GET"
        req.url.path = "/api/v1/health"
        req.client.host = "127.0.0.1"
        req.headers = Headers({"x-request-id": request_id})
        req.query_params = {}
        req.path_params = {}
        req.scope = {"route": MagicMock(path="/api/v1/health")}
        req.state = SimpleNamespace(
            request_id=request_id,
            organization=None,
            aggregator_payload={
                "query_params": {},
                "path_params": {},
                "request_body": None,
            },
        )
        return req

    # Case 1: request.state.aggregator_metrics not set at all
    async def call_next_without_metrics(_: Request) -> Response:
        return JSONResponse(content={"status": "ok"}, status_code=200)

    await middleware.dispatch(
        _make_request("req-no-metrics"), call_next_without_metrics
    )

    # Case 2: request.state.aggregator_metrics contains only rejected keys/values
    async def call_next_rejected_metrics(req: Request) -> Response:
        req.state.aggregator_metrics = {
            "secret": "SECRET_VALUE_MUST_BE_DROPPED",  # gitleaks:allow
            "nested": {"a": 1},
            "bad\rkey": 42,
            "nan_val": float("nan"),
        }
        return JSONResponse(content={"status": "ok"}, status_code=200)

    await middleware.dispatch(
        _make_request("req-rejected-metrics"), call_next_rejected_metrics
    )

    assert len(logged_entries) == 2
    assert "metrics" not in logged_entries[0]
    assert "metrics" not in logged_entries[1]
    assert "SECRET_VALUE_MUST_BE_DROPPED" not in json.dumps(logged_entries[1])
