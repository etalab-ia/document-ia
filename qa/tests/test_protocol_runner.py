"""Unit tests for qa.runners.protocol_runner using httpx.MockTransport."""

from __future__ import annotations

import uuid

import httpx
import pytest
from pydantic import SecretStr

from qa.client import DocumentIAClient
from qa.config import ResolvedEnvConfig
from qa.runners.protocol_runner import PROTOCOL_SCENARIOS, run_protocol_scenarios


def _make_env(*, with_admin: bool = True) -> ResolvedEnvConfig:
    return ResolvedEnvConfig(
        name="staging",
        base_url="https://api.test.local",
        profile="develop",
        api_key_env="staging",
        allow_load=True,
        allow_chaos=True,
        requires_explicit_allow=False,
        default_timeout_s=10.0,
        default_poll_interval_s=0.01,
        default_poll_timeout_s=2.0,
        api_key=SecretStr("dia_staging_2_VALIDKEY_" + "A" * 52 + "_ABCD"),
        admin_api_key=(
            SecretStr("dia_staging_2_ADMINKEY_" + "B" * 52 + "_WXYZ")
            if with_admin
            else None
        ),
    )


@pytest.mark.asyncio
async def test_proto_auth_binding_passes_and_preserves_quota() -> None:
    env = _make_env()
    valid_key = env.get_api_key()
    rem_min = 60

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal rem_min
        if req.url.path == "/api/test":
            key = req.headers.get("X-API-KEY")
            if key != valid_key:
                return httpx.Response(401, json={"detail": "Unauthorized"})
            rem_min -= 1
            return httpx.Response(
                200,
                json={"status": "success"},
                headers={
                    "X-RateLimit-Remaining-Minute": str(rem_min),
                    "X-RateLimit-Remaining-Daily": "999",
                    "X-RateLimit-Reset-Minute": "2026-11-23T15:00:00",
                    "X-RateLimit-Reset-Daily": "2026-11-24T00:00:00",
                },
            )
        return httpx.Response(404)

    async with DocumentIAClient(env, transport=httpx.MockTransport(handler)) as client:
        res = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-auth-binding"]]
            )
        )[0]

    assert res.status == "PASS", res.message
    assert len(res.assertions) == 7


@pytest.mark.asyncio
async def test_proto_idempotency_full_lifecycle() -> None:
    env = _make_env()
    store: dict[str, tuple[str, bytes]] = {}
    conc_seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        key = req.headers.get("Idempotency-Key")
        if key is None:
            return httpx.Response(
                200, json={"data": {"execution_id": str(uuid.uuid4())}}
            )

        if (
            len(key) == 0
            or len(key) > 256
            or not key.isascii()
            or not all(32 <= ord(c) <= 126 for c in key)
        ):
            return httpx.Response(
                400,
                json={
                    "errors": {
                        "error": "invalid_idempotency_key",
                        "message": "Invalid key",
                    }
                },
            )

        body_bytes = req.read()
        # Simulate concurrent in-progress collision for qa-idem-conc-*
        if key.startswith("qa-idem-conc-"):
            if key not in conc_seen:
                eid = str(uuid.uuid4())
                conc_seen[key] = eid
                return httpx.Response(200, json={"data": {"execution_id": eid}})
            return httpx.Response(
                409,
                json={
                    "errors": {
                        "error": "idempotency_in_progress",
                        "execution_id": conc_seen[key],
                    }
                },
            )

        if key in store:
            saved_eid, saved_body = store[key]
            # Compare metadata snippet inside multipart body
            if (b'"variant": "b"' in body_bytes) != (b'"variant": "b"' in saved_body):
                return httpx.Response(
                    409,
                    json={"errors": {"error": "idempotency_conflict"}},
                )
            return httpx.Response(
                200,
                headers={"Idempotent-Replay": "true"},
                json={"data": {"execution_id": saved_eid}},
            )

        eid = str(uuid.uuid4())
        store[key] = (eid, body_bytes)
        return httpx.Response(200, json={"data": {"execution_id": eid}})

    async with DocumentIAClient(env, transport=httpx.MockTransport(handler)) as client:
        res = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-idempotency"]]
            )
        )[0]

    assert res.status == "PASS", res.message
    assert all(a.passed for a in res.assertions)


@pytest.mark.asyncio
async def test_proto_ratelimit_headers_and_burst() -> None:
    env = _make_env()
    rem_min = 5
    rem_day = 100

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal rem_min, rem_day
        if rem_min <= 0:
            return httpx.Response(
                429,
                headers={
                    "Retry-After": "30",
                    "X-RateLimit-Remaining-Minute": "0",
                    "X-RateLimit-Remaining-Daily": str(rem_day),
                    "X-RateLimit-Reset-Minute": "2026-11-23T15:01:00",
                    "X-RateLimit-Reset-Daily": "2026-11-24T00:00:00",
                },
            )
        rem_min -= 1
        rem_day -= 1
        return httpx.Response(
            200,
            json={"status": "success"},
            headers={
                "X-RateLimit-Remaining-Minute": str(rem_min),
                "X-RateLimit-Remaining-Daily": str(rem_day),
                "X-RateLimit-Reset-Minute": "2026-11-23T15:01:00",
                "X-RateLimit-Reset-Daily": "2026-11-24T00:00:00",
            },
        )

    async with DocumentIAClient(env, transport=httpx.MockTransport(handler)) as client:
        hdr_res = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-ratelimit-headers"]]
            )
        )[0]
        burst_res = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-ratelimit-burst"]]
            )
        )[0]

    assert hdr_res.status == "PASS", hdr_res.message
    assert burst_res.status == "PASS", burst_res.message


@pytest.mark.asyncio
async def test_proto_oom_decompression_bomb() -> None:
    env = _make_env()
    exec_statuses: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body = req.read()
            eid = str(uuid.uuid4())
            if b"decompression_bomb.png" in body:
                exec_statuses[eid] = "FAILED"
            else:
                exec_statuses[eid] = "SUCCESS"
            return httpx.Response(200, json={"data": {"execution_id": eid}})
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            eid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={"id": eid, "status": exec_statuses[eid], "data": {}}
            )
        return httpx.Response(404)

    async with DocumentIAClient(env, transport=httpx.MockTransport(handler)) as client:
        res = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-oom-decompression-bomb"]]
            )
        )[0]

    assert res.status == "PASS", res.message


@pytest.mark.asyncio
async def test_proto_webhook_isolation_and_cleanup() -> None:
    env = _make_env(with_admin=True)
    org_a = "11111111-1111-1111-1111-111111111111"
    org_b = "22222222-2222-2222-2222-222222222222"
    webhooks: dict[str, str] = {}  # wh_id -> org_id

    def handler(req: httpx.Request) -> httpx.Response:
        if req.headers.get("X-API-KEY") != env.get_admin_api_key():
            return httpx.Response(403)
        if req.method == "GET" and req.url.path == "/api/v1/admin/organizations":
            return httpx.Response(200, json=[{"id": org_a}, {"id": org_b}])
        if (
            req.method == "POST"
            and req.url.path == f"/api/v1/admin/organizations/{org_a}/webhooks"
        ):
            wh_id = "99999999-9999-9999-9999-999999999999"
            webhooks[wh_id] = org_a
            return httpx.Response(
                201,
                json={
                    "id": wh_id,
                    "organization_id": org_a,
                    "url": "https://example.com",
                },
            )
        if req.method == "DELETE" and "/webhooks/" in req.url.path:
            parts = req.url.path.split("/")
            target_org = parts[5]
            wh_id = parts[7]
            if webhooks.get(wh_id) != target_org:
                return httpx.Response(404)
            del webhooks[wh_id]
            return httpx.Response(204)
        return httpx.Response(404)

    async with DocumentIAClient(env, transport=httpx.MockTransport(handler)) as client:
        res = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-webhook-isolation"]]
            )
        )[0]

    assert res.status == "PASS", res.message
    assert webhooks == {}  # Cleaned up!

    # Without admin key -> SKIPPED
    async with DocumentIAClient(
        _make_env(with_admin=False), transport=httpx.MockTransport(handler)
    ) as client_no_admin:
        skipped = (
            await run_protocol_scenarios(
                client_no_admin, [PROTOCOL_SCENARIOS["proto-webhook-isolation"]]
            )
        )[0]
    assert skipped.status == "SKIPPED"


@pytest.mark.asyncio
async def test_proto_auth_binding_fails_when_401_consumes_quota() -> None:
    env = _make_env()
    valid_key = env.get_api_key()
    rem_min = 60

    def buggy_handler(req: httpx.Request) -> httpx.Response:
        nonlocal rem_min
        rem_min -= 1  # Bug: decrements even on 401!
        if req.headers.get("X-API-KEY") != valid_key:
            return httpx.Response(401, json={"detail": "Unauthorized"})
        return httpx.Response(
            200,
            json={"status": "success"},
            headers={
                "X-RateLimit-Remaining-Minute": str(rem_min),
                "X-RateLimit-Reset-Minute": "2026-11-23T15:00:00",
            },
        )

    async with DocumentIAClient(
        env, transport=httpx.MockTransport(buggy_handler)
    ) as client:
        res = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-auth-binding"]]
            )
        )[0]

    assert res.status == "FAIL"
    assert any(
        a.path == "auth.quota_not_consumed_by_401" and not a.passed
        for a in res.assertions
    )


@pytest.mark.asyncio
async def test_proto_oom_decompression_bomb_accepts_safe_success_empty_barcodes() -> (
    None
):
    env = _make_env()

    # Case 1: Real worker behavior — SUCCESS with barcodes=[] + follow-up SUCCESS -> PASS
    exec_payloads: dict[str, dict[str, object]] = {}

    def safe_skip_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body = req.read()
            eid = str(uuid.uuid4())
            if b"decompression_bomb.png" in body:
                exec_payloads[eid] = {
                    "id": eid,
                    "status": "SUCCESS",
                    "data": {"result": {"barcodes": []}},
                }
            else:
                exec_payloads[eid] = {
                    "id": eid,
                    "status": "SUCCESS",
                    "data": {"result": {"barcodes": []}},
                }
            return httpx.Response(200, json={"data": {"execution_id": eid}})
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            eid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=exec_payloads[eid])
        return httpx.Response(404)

    async with DocumentIAClient(
        env, transport=httpx.MockTransport(safe_skip_handler)
    ) as client:
        res_pass = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-oom-decompression-bomb"]]
            )
        )[0]
    assert res_pass.status == "PASS", res_pass.message

    # Case 2: Bomb unexpectedly produces non-empty barcodes -> FAIL
    def bad_barcodes_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body = req.read()
            eid = str(uuid.uuid4())
            barcodes = [{"type": "QR"}] if b"decompression_bomb.png" in body else []
            exec_payloads[eid] = {
                "id": eid,
                "status": "SUCCESS",
                "data": {"result": {"barcodes": barcodes}},
            }
            return httpx.Response(200, json={"data": {"execution_id": eid}})
        if req.method == "GET" and req.url.path.startswith("/api/v1/executions/"):
            eid = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=exec_payloads[eid])
        return httpx.Response(404)

    async with DocumentIAClient(
        env, transport=httpx.MockTransport(bad_barcodes_handler)
    ) as client:
        res_bad = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-oom-decompression-bomb"]]
            )
        )[0]
    assert res_bad.status == "FAIL"
    assert any(
        a.path == "oom_bomb.execution_status" and not a.passed
        for a in res_bad.assertions
    )

    # Case 3: Bomb times out (worker hung/OOM) -> FAIL
    short_env = env.model_copy(update={"default_poll_timeout_s": 0.05})

    def timeout_handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body = req.read()
            eid = "bomb-stuck" if b"decompression_bomb.png" in body else "nominal-ok"
            return httpx.Response(200, json={"data": {"execution_id": eid}})
        if req.method == "GET" and req.url.path == "/api/v1/executions/bomb-stuck":
            return httpx.Response(200, json={"id": "bomb-stuck", "status": "STARTED"})
        if req.method == "GET" and req.url.path == "/api/v1/executions/nominal-ok":
            return httpx.Response(200, json={"id": "nominal-ok", "status": "SUCCESS"})
        return httpx.Response(404)

    async with DocumentIAClient(
        short_env, transport=httpx.MockTransport(timeout_handler)
    ) as client:
        res_timeout = (
            await run_protocol_scenarios(
                client, [PROTOCOL_SCENARIOS["proto-oom-decompression-bomb"]]
            )
        )[0]
    assert res_timeout.status == "FAIL"
    assert any(
        a.path == "oom_bomb.execution_status" and not a.passed
        for a in res_timeout.assertions
    )
