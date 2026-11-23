"""Unit tests for 2D-Doc VerificationStatus exposure across API, OpenAPI/ReDoc and webhooks."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fr_2ddoc_parser.model.models import VerificationStatus

from document_ia_api.api.contracts.execution.success import ExecutionSuccessModel
from document_ia_api.application.services.execution_service import ExecutionService
from document_ia_api.infra.webhook_notification_service import (
    WebhookNotificationService,
)
from document_ia_api.main import app
from document_ia_infra.data.event.dto.event_dto import EventDTO
from document_ia_infra.data.event.dto.event_type_enum import EventType
from document_ia_infra.data.event.schema.barcode import Ants2DDoc, BarcodePosition
from document_ia_infra.data.event.schema.workflow.workflow_execution_completed_event import (
    CompletedEventResult,
    WorkflowExecutionCompletedEvent,
)
from document_ia_infra.data.webhook.dto.webhook_dto import WebHookDTO


@pytest.fixture(autouse=True)
def mock_api_key_service_auth() -> None:
    """Unit tests do not exercise API authentication or PostgreSQL."""


DEFAULT_TEST_ORG_ID = UUID("11111111-1111-4111-8111-111111111111")


def _build_completed_event_dto(
    *,
    verification_status: VerificationStatus | None,
    include_verification_status_key: bool = True,
    organization_id: UUID = DEFAULT_TEST_ORG_ID,
    execution_id: str = "exec-2ddoc-test",
) -> EventDTO:
    is_valid = verification_status == VerificationStatus.VERIFIED
    completed = WorkflowExecutionCompletedEvent(
        event_id=uuid4(),
        organization_id=organization_id,
        workflow_id="wf-2ddoc",
        execution_id=execution_id,
        created_at=datetime.now(UTC),
        version=2,
        final_result=CompletedEventResult(
            extraction=None,
            classification=None,
            barcodes=[
                Ants2DDoc(
                    position=BarcodePosition(
                        top_left=(10, 20),
                        top_right=(110, 20),
                        bottom_right=(110, 120),
                        bottom_left=(10, 120),
                    ),
                    page_number=1,
                    is_valid=is_valid,
                    verification_status=verification_status,
                    raw_data={"10": "Alice"},
                    typed_data=[],
                    issue_date=date(2025, 1, 15),
                    ants_type="01",
                    doc_type="01",
                )
            ],
        ),
        total_processing_time_ms=250,
        output_summary={},
        steps_completed=3,
    )
    payload = completed.model_dump(mode="json")
    if not include_verification_status_key:
        del payload["final_result"]["barcodes"][0]["verification_status"]
        payload["final_result"]["barcodes"][0]["is_valid"] = True

    return EventDTO(
        id=uuid4(),
        workflow_id="wf-2ddoc",
        organization_id=organization_id,
        execution_id=execution_id,
        created_at=completed.created_at,
        event_type=EventType.WORKFLOW_EXECUTION_COMPLETED,
        event=payload,
    )


def test_openapi_schema_exposes_verification_status_enum_and_ants_2ddoc_ref() -> None:
    openapi_schema = app.openapi()
    schemas = openapi_schema["components"]["schemas"]

    assert "VerificationStatus" in schemas
    verification_status_schema = schemas["VerificationStatus"]
    assert verification_status_schema["type"] == "string"
    expected_values = [status.value for status in VerificationStatus]
    assert len(expected_values) == 10
    assert verification_status_schema["enum"] == expected_values

    assert "Ants2DDoc" in schemas
    ants_properties = schemas["Ants2DDoc"]["properties"]
    assert "verification_status" in ants_properties
    verification_prop = ants_properties["verification_status"]
    assert {"anyOf"} <= set(verification_prop.keys())
    assert {"$ref": "#/components/schemas/VerificationStatus"} in verification_prop[
        "anyOf"
    ]
    assert {"type": "null"} in verification_prop["anyOf"]

    # Internal VerificationResult fields must never be exposed in API schema.
    for forbidden_field in (
        "detail",
        "ca_id",
        "cert_id",
        "reference_date",
        "tsl_sequence_number",
        "tsl_next_update",
        "tsl_freshness",
    ):
        assert forbidden_field not in ants_properties

    # Sensitive 2D-Doc content fields must retain their masking annotation.
    assert ants_properties["raw_data"].get("x-mask") is True
    assert ants_properties["typed_data"].get("x-mask") is True


@pytest.mark.parametrize("status", list(VerificationStatus))
def test_execution_service_returns_verification_status_for_new_events(
    status: VerificationStatus,
) -> None:
    dto = _build_completed_event_dto(verification_status=status)
    model = ExecutionService().get_event_model(
        dto, execution_id=dto.execution_id, is_debug_mode=False
    )

    assert isinstance(model, ExecutionSuccessModel)
    assert len(model.data.result.barcodes) == 1
    ants = model.data.result.barcodes[0]
    assert isinstance(ants, Ants2DDoc)
    assert ants.verification_status is status
    assert ants.is_valid is (status == VerificationStatus.VERIFIED)

    dumped = model.model_dump(mode="json")
    assert dumped["data"]["result"]["barcodes"][0]["verification_status"] == (
        status.value
    )


def test_execution_service_returns_none_verification_status_for_historical_events() -> (
    None
):
    dto = _build_completed_event_dto(
        verification_status=None,
        include_verification_status_key=False,
    )
    model = ExecutionService().get_event_model(
        dto, execution_id=dto.execution_id, is_debug_mode=False
    )

    assert isinstance(model, ExecutionSuccessModel)
    ants = model.data.result.barcodes[0]
    assert isinstance(ants, Ants2DDoc)
    assert ants.verification_status is None
    assert ants.is_valid is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "include_key", "expected_json_value"),
    [
        (VerificationStatus.VERIFIED, True, "verified"),
        (VerificationStatus.SIGNATURE_INVALID, True, "signature_invalid"),
        (VerificationStatus.RESOLVER_UNAVAILABLE, True, "resolver_unavailable"),
        (None, False, None),
    ],
)
async def test_webhook_notification_serializes_verification_status(
    status: VerificationStatus | None,
    include_key: bool,
    expected_json_value: str | None,
) -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = WebhookNotificationService(
        http_client=client, retry_initial_delay_seconds=0.0
    )
    now = datetime.now(UTC)
    webhook_dto = WebHookDTO(
        id=uuid4(),
        organization_id=DEFAULT_TEST_ORG_ID,
        url="https://example.test/webhook",
        headers={"X-Webhook-Test": "1"},
        created_at=now,
        updated_at=now,
    )
    event_dto = _build_completed_event_dto(
        verification_status=status,
        include_verification_status_key=include_key,
    )

    try:
        await service._call_webhook_notification(webhook_dto, event_dto)
    finally:
        await service.aclose()

    assert len(captured_requests) == 1
    body = json.loads(captured_requests[0].content.decode("utf-8"))
    barcode_payload = body["data"]["result"]["barcodes"][0]
    assert barcode_payload["type"] == "2D_DOC"
    assert barcode_payload["verification_status"] == expected_json_value
    assert "detail" not in barcode_payload
