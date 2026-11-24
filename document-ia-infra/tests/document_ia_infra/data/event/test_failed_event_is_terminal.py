"""Unit tests for WorkflowExecutionFailedEvent.is_terminal, EventStoreService, and EventRepository."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from document_ia_infra.data.event.dto.anonymization_enum import AnonymizationStatus
from document_ia_infra.data.event.dto.event_type_enum import EventType
from document_ia_infra.data.event.repository.event import EventRepository
from document_ia_infra.data.event.schema.workflow.workflow_execution_failed_event import (
    WorkflowExecutionFailedEvent,
)
from document_ia_infra.exception.retryable_exception import RetryableException
from document_ia_infra.service.event_store_service import EventStoreService


def test_failed_event_is_terminal_defaults_to_true() -> None:
    event = WorkflowExecutionFailedEvent(
        workflow_id="wf-1",
        execution_id="exec-1",
        organization_id=uuid4(),
        created_at=datetime.now(UTC),
        version=1,
        error_type="ValueError",
        error_message="boom",
    )
    assert event.is_terminal is True


def test_failed_event_is_terminal_false_roundtrip() -> None:
    event = WorkflowExecutionFailedEvent(
        workflow_id="wf-1",
        execution_id="exec-1",
        organization_id=uuid4(),
        created_at=datetime.now(UTC),
        version=1,
        error_type=RetryableException.__name__,
        error_message="HTTP 429",
        retry_count=0,
        is_terminal=False,
    )
    dumped = event.model_dump(mode="json")
    assert dumped["is_terminal"] is False
    restored = WorkflowExecutionFailedEvent.model_validate(dumped)
    assert restored.is_terminal is False


def test_failed_event_backward_compatibility_without_is_terminal() -> None:
    legacy_payload = {
        "workflow_id": "wf-1",
        "execution_id": "exec-legacy",
        "organization_id": str(uuid4()),
        "created_at": datetime.now(UTC).isoformat(),
        "version": 1,
        "event_type": EventType.WORKFLOW_EXECUTION_FAILED.value,
        "error_type": "RuntimeError",
        "error_message": "legacy failure",
        "failed_step": "ocr",
        "retry_count": 2,
    }
    restored = WorkflowExecutionFailedEvent.model_validate(legacy_payload)
    assert restored.is_terminal is True


@pytest.mark.asyncio
async def test_event_store_service_propagates_is_terminal() -> None:
    session = AsyncMock()
    service = EventStoreService(session)

    default_event = service.create_workflow_failed_event(
        workflow_id="wf-1",
        execution_id="exec-1",
        organization_id=uuid4(),
        error_type="ValueError",
        error_message="fatal",
    )
    assert default_event.is_terminal is True

    non_terminal_event = service.create_workflow_failed_event(
        workflow_id="wf-1",
        execution_id="exec-1",
        organization_id=uuid4(),
        error_type=RetryableException.__name__,
        error_message="429",
        is_terminal=False,
    )
    assert non_terminal_event.is_terminal is False

    service.store_event = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    await service.emit_workflow_failed(
        workflow_id="wf-1",
        execution_id="exec-1",
        organization_id=uuid4(),
        error_type=RetryableException.__name__,
        error_message="429",
        is_terminal=False,
    )
    assert service.store_event.await_args is not None
    stored_arg = service.store_event.await_args.args[0]
    assert isinstance(stored_arg, WorkflowExecutionFailedEvent)
    assert stored_arg.is_terminal is False


def _make_entity(
    event_type: EventType, event_payload: dict[str, object]
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workflow_id="wf-1",
        execution_id="exec-repo",
        organization_id=uuid4(),
        created_at=datetime.now(UTC),
        event_type=event_type.value,
        event=event_payload,
        anonymization_status=AnonymizationStatus.PENDING.value,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failed_payload", "should_resume"),
    [
        # Explicit is_terminal=False -> resumable
        ({"error_type": "RetryableException", "is_terminal": False}, True),
        # Explicit is_terminal=True -> NOT resumable even for RetryableException
        ({"error_type": "RetryableException", "is_terminal": True}, False),
        # Explicit is_terminal=True non-retryable -> NOT resumable
        ({"error_type": "ValueError", "is_terminal": True}, False),
        # Legacy without is_terminal + RetryableException -> resumable
        ({"error_type": "RetryableException"}, True),
        # Null is_terminal + RetryableException -> resumable (aligned with WorkflowManager._is_terminal_event)
        ({"error_type": "RetryableException", "is_terminal": None}, True),
        # Legacy without is_terminal + non-retryable -> NOT resumable
        ({"error_type": "ValueError"}, False),
    ],
)
async def test_repository_resume_logic_with_is_terminal(
    failed_payload: dict[str, object], should_resume: bool
) -> None:
    started_entity = _make_entity(EventType.WORKFLOW_EXECUTION_STARTED, {"version": 1})
    failed_entity = _make_entity(EventType.WORKFLOW_EXECUTION_FAILED, failed_payload)

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [failed_entity, started_entity]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock

    session = AsyncMock()
    session.execute = AsyncMock(return_value=result_mock)

    repo = EventRepository(session)
    dto = await repo.get_created_event_if_execution_not_completed_or_failed("exec-repo")

    if should_resume:
        assert dto is not None
        assert dto.event_type == EventType.WORKFLOW_EXECUTION_STARTED
    else:
        assert dto is None
