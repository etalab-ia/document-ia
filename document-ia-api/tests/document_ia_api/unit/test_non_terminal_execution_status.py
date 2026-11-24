"""Unit tests for non-terminal failure projection (is_terminal=False -> STARTED) in API."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from document_ia_api.api.contracts.execution.failed import ExecutionFailedModel
from document_ia_api.api.contracts.execution.started import ExecutionStartedModel
from document_ia_api.api.contracts.execution.success import ExecutionSuccessModel
from document_ia_api.api.contracts.execution.types import ExecutionStatus
from document_ia_api.application.services.execution_service import ExecutionService
from document_ia_api.application.services.workflow_service import WorkflowService
from document_ia_infra.data.event.dto.anonymization_enum import AnonymizationStatus
from document_ia_infra.data.event.dto.event_dto import EventDTO
from document_ia_infra.data.event.dto.event_type_enum import EventType
from document_ia_infra.data.event.schema.mappers.event_mapper import decode_event_dto
from document_ia_infra.data.event.schema.workflow.workflow_execution_started_event import (
    WorkflowExecutionStartedEvent,
)
from document_ia_infra.exception.retryable_exception import RetryableException


def _started_event_dto(
    execution_id: str,
    organization_id: UUID,
    workflow_id: str = "wf-1",
    anonymization_status: AnonymizationStatus = AnonymizationStatus.PENDING,
) -> EventDTO:
    now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=UTC)
    return EventDTO(
        id=uuid4(),
        workflow_id=workflow_id,
        execution_id=execution_id,
        organization_id=organization_id,
        created_at=now,
        event_type=EventType.WORKFLOW_EXECUTION_STARTED,
        event={
            "workflow_id": workflow_id,
            "execution_id": execution_id,
            "organization_id": str(organization_id),
            "created_at": now.isoformat(),
            "version": 1,
            "event_type": EventType.WORKFLOW_EXECUTION_STARTED.value,
            "s3_file_info": {
                "filename": "invoice.pdf",
                "s3_key": "uploads/invoice.pdf",
                "size": 1024,
                "content_type": "application/pdf",
                "uploaded_at": now.isoformat(),
                "presigned_url": "https://minio.local/presigned/invoice",
            },
            "file_url": None,
            "metadata": {},
            "classification_parameters": {},
            "extraction_parameters": {},
        },
        anonymization_status=anonymization_status,
    )


def _failed_event_dto(
    execution_id: str,
    organization_id: UUID,
    *,
    is_terminal: bool | None = True,
    workflow_id: str = "wf-1",
    retry_count: int = 0,
) -> EventDTO:
    now = datetime(2026, 11, 24, 12, 0, 5, tzinfo=UTC)
    payload: dict[str, object] = {
        "workflow_id": workflow_id,
        "execution_id": execution_id,
        "organization_id": str(organization_id),
        "created_at": now.isoformat(),
        "version": 1,
        "event_type": EventType.WORKFLOW_EXECUTION_FAILED.value,
        "error_type": "RetryableException",
        "error_message": "HTTP 429 Too Many Requests",
        "failed_step": "ocr",
        "retry_count": retry_count,
    }
    if is_terminal is not None:
        payload["is_terminal"] = is_terminal
    return EventDTO(
        id=uuid4(),
        workflow_id=workflow_id,
        execution_id=execution_id,
        organization_id=organization_id,
        created_at=now,
        event_type=EventType.WORKFLOW_EXECUTION_FAILED,
        event=payload,
        anonymization_status=AnonymizationStatus.PENDING,
    )


def _completed_event_dto(
    execution_id: str, organization_id: UUID, workflow_id: str = "wf-1"
) -> EventDTO:
    now = datetime(2026, 11, 24, 12, 0, 10, tzinfo=UTC)
    return EventDTO(
        id=uuid4(),
        workflow_id=workflow_id,
        execution_id=execution_id,
        organization_id=organization_id,
        created_at=now,
        event_type=EventType.WORKFLOW_EXECUTION_COMPLETED,
        event={
            "workflow_id": workflow_id,
            "execution_id": execution_id,
            "organization_id": str(organization_id),
            "created_at": now.isoformat(),
            "version": 1,
            "event_type": EventType.WORKFLOW_EXECUTION_COMPLETED.value,
            "final_result": {
                "classification": None,
                "extraction": None,
                "barcodes": [],
            },
            "total_processing_time_ms": 500,
            "output_summary": {},
            "steps_completed": 1,
        },
        anonymization_status=AnonymizationStatus.PENDING,
    )


def test_execution_service_non_terminal_failed_with_started_event_returns_started() -> (
    None
):
    service = ExecutionService()
    org_id = uuid4()
    exec_id = "exec-non-terminal-1"
    started_dto = _started_event_dto(exec_id, org_id)
    failed_dto = _failed_event_dto(exec_id, org_id, is_terminal=False)

    model = service.get_event_model(
        failed_dto,
        exec_id,
        is_debug_mode=False,
        started_event_dto=started_dto,
    )

    assert isinstance(model, ExecutionStartedModel)
    assert model.status == ExecutionStatus.STARTED
    assert model.id == exec_id
    assert model.data.created_at == started_dto.created_at
    assert model.data.s3_file_info is not None
    assert model.data.s3_file_info.file_name == "invoice.pdf"
    assert model.purged is False


def test_execution_service_non_terminal_failed_propagates_started_event_purged_flag() -> (
    None
):
    service = ExecutionService()
    org_id = uuid4()
    exec_id = "exec-non-terminal-purged"
    started_dto = _started_event_dto(
        exec_id,
        org_id,
        anonymization_status=AnonymizationStatus.DONE,
    )
    failed_dto = _failed_event_dto(exec_id, org_id, is_terminal=False)

    model = service.get_event_model(
        failed_dto,
        exec_id,
        is_debug_mode=False,
        started_event_dto=started_dto,
    )

    assert isinstance(model, ExecutionStartedModel)
    assert model.status == ExecutionStatus.STARTED
    assert model.purged is True


def test_execution_service_non_terminal_failed_without_started_event_fallback() -> None:
    service = ExecutionService()
    org_id = uuid4()
    exec_id = "exec-non-terminal-2"
    failed_dto = _failed_event_dto(exec_id, org_id, is_terminal=False)

    model = service.get_event_model(
        failed_dto,
        exec_id,
        is_debug_mode=False,
        started_event_dto=None,
    )

    assert isinstance(model, ExecutionStartedModel)
    assert model.status == ExecutionStatus.STARTED
    assert model.data.created_at == failed_dto.created_at
    assert model.data.s3_file_info is None
    assert model.data.file_url is None


@pytest.mark.parametrize("is_terminal", [True, None])
def test_execution_service_terminal_or_legacy_failed_returns_failed(
    is_terminal: bool | None,
) -> None:
    service = ExecutionService()
    org_id = uuid4()
    exec_id = "exec-terminal-1"
    failed_dto = _failed_event_dto(exec_id, org_id, is_terminal=is_terminal)

    model = service.get_event_model(failed_dto, exec_id, is_debug_mode=False)

    assert isinstance(model, ExecutionFailedModel)
    assert model.status == ExecutionStatus.FAILED
    assert model.data.error_type == "RetryableException"


def test_get_execution_route_projects_non_terminal_failure_as_started(
    monkeypatch: pytest.MonkeyPatch,
    client_with_api_key_standard,
    standard_api_key_value: str,
    organization_id: UUID,
) -> None:
    exec_id = "exec-route-non-terminal"
    started_dto = _started_event_dto(exec_id, organization_id)
    started_decoded = decode_event_dto(started_dto)
    assert isinstance(started_decoded, WorkflowExecutionStartedEvent)
    failed_dto = _failed_event_dto(exec_id, organization_id, is_terminal=False)

    monkeypatch.setattr(
        "document_ia_api.api.routes.v1.executions.EventStoreService.get_last_event_for_execution_id",
        AsyncMock(return_value=failed_dto),
    )
    monkeypatch.setattr(
        "document_ia_api.api.routes.v1.executions.EventStoreService.get_workflow_started_event",
        AsyncMock(return_value=(started_dto, started_decoded)),
    )
    monkeypatch.setattr(
        "document_ia_api.api.routes.v1.executions.workflow_repository.get_workflow_by_id",
        AsyncMock(return_value={"id": "wf-1"}),
    )

    response = client_with_api_key_standard.get(
        f"/api/v1/executions/{exec_id}",
        headers={"X-API-KEY": standard_api_key_value},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "STARTED"
    assert body["id"] == exec_id
    assert body["data"]["s3_file_info"]["file_name"] == "invoice.pdf"


def test_get_execution_route_returns_503_when_started_event_lookup_fails_transiently(
    monkeypatch: pytest.MonkeyPatch,
    client_with_api_key_standard,
    standard_api_key_value: str,
    organization_id: UUID,
) -> None:
    exec_id = "exec-route-503-started-lookup"
    failed_dto = _failed_event_dto(exec_id, organization_id, is_terminal=False)

    monkeypatch.setattr(
        "document_ia_api.api.routes.v1.executions.EventStoreService.get_last_event_for_execution_id",
        AsyncMock(return_value=failed_dto),
    )
    monkeypatch.setattr(
        "document_ia_api.api.routes.v1.executions.EventStoreService.get_workflow_started_event",
        AsyncMock(side_effect=RetryableException("DB pool exhausted")),
    )
    monkeypatch.setattr(
        "document_ia_api.api.routes.v1.executions.workflow_repository.get_workflow_by_id",
        AsyncMock(return_value={"id": "wf-1"}),
    )

    response = client_with_api_key_standard.get(
        f"/api/v1/executions/{exec_id}",
        headers={"X-API-KEY": standard_api_key_value},
    )
    assert response.status_code == 503
    body = response.json()
    assert body.get("errors", {}).get("code") == "database_unavailable"


def test_get_execution_route_returns_failed_on_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
    client_with_api_key_standard,
    standard_api_key_value: str,
    organization_id: UUID,
) -> None:
    exec_id = "exec-route-terminal"
    failed_dto = _failed_event_dto(
        exec_id, organization_id, is_terminal=True, retry_count=3
    )

    monkeypatch.setattr(
        "document_ia_api.api.routes.v1.executions.EventStoreService.get_last_event_for_execution_id",
        AsyncMock(return_value=failed_dto),
    )
    monkeypatch.setattr(
        "document_ia_api.api.routes.v1.executions.workflow_repository.get_workflow_by_id",
        AsyncMock(return_value={"id": "wf-1"}),
    )

    response = client_with_api_key_standard.get(
        f"/api/v1/executions/{exec_id}",
        headers={"X-API-KEY": standard_api_key_value},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["data"]["retry_count"] == 3


@pytest.mark.asyncio
async def test_wait_for_execution_result_continues_polling_on_non_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    exec_id = "exec-sync-poll"
    started_dto = _started_event_dto(exec_id, org_id)
    started_decoded = decode_event_dto(started_dto)
    assert isinstance(started_decoded, WorkflowExecutionStartedEvent)
    non_terminal_failed = _failed_event_dto(exec_id, org_id, is_terminal=False)
    completed_dto = _completed_event_dto(exec_id, org_id)

    events_sequence = [started_dto, non_terminal_failed, completed_dto]
    get_last_mock = AsyncMock(side_effect=events_sequence)
    get_started_mock = AsyncMock(return_value=(started_dto, started_decoded))

    monkeypatch.setattr(
        "document_ia_api.application.services.workflow_service.EventStoreService.get_last_event_for_execution_id",
        get_last_mock,
    )
    monkeypatch.setattr(
        "document_ia_api.application.services.workflow_service.EventStoreService.get_workflow_started_event",
        get_started_mock,
    )

    service = WorkflowService(MagicMock())
    result = await service.wait_for_execution_result(
        execution_id=exec_id,
        organization_id=org_id,
        timeout_seconds=5,
        poll_interval_ms=50,
    )

    assert isinstance(result, ExecutionSuccessModel)
    assert result.status == ExecutionStatus.SUCCESS
    assert get_last_mock.await_count == 3
    assert get_started_mock.await_count == 0


@pytest.mark.asyncio
async def test_wait_for_execution_result_stops_immediately_on_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = uuid4()
    exec_id = "exec-sync-terminal"
    terminal_failed = _failed_event_dto(
        exec_id, org_id, is_terminal=True, retry_count=3
    )

    get_last_mock = AsyncMock(return_value=terminal_failed)
    monkeypatch.setattr(
        "document_ia_api.application.services.workflow_service.EventStoreService.get_last_event_for_execution_id",
        get_last_mock,
    )

    service = WorkflowService(MagicMock())
    result = await service.wait_for_execution_result(
        execution_id=exec_id,
        organization_id=org_id,
        timeout_seconds=5,
        poll_interval_ms=50,
    )

    assert isinstance(result, ExecutionFailedModel)
    assert result.status == ExecutionStatus.FAILED
    assert get_last_mock.await_count == 1
