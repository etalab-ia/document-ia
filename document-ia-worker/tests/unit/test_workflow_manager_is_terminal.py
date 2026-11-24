"""Unit tests for WorkflowManager is_terminal propagation and _is_terminal_event logic."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from document_ia_infra.data.event.dto.anonymization_enum import AnonymizationStatus
from document_ia_infra.data.event.dto.event_dto import EventDTO
from document_ia_infra.data.event.dto.event_type_enum import EventType
from document_ia_infra.exception.retryable_exception import RetryableException
from document_ia_infra.redis.model.workflow_execution_message import (
    WorkflowExecutionMessage,
)
from document_ia_worker.exception.workflow_step_exception import WorkflowStepException
from document_ia_worker.workflow import workflow_manager as wm_module
from document_ia_worker.workflow.workflow_manager import WorkflowManager


def _make_event_dto(
    event_type: EventType, payload: dict[str, object], execution_id: str | None = None
) -> EventDTO:
    return EventDTO(
        id=uuid4(),
        workflow_id="wf-1",
        execution_id=execution_id or str(uuid4()),
        organization_id=uuid4(),
        created_at=datetime.now(UTC),
        event_type=event_type,
        event=payload,
        anonymization_status=AnonymizationStatus.PENDING,
    )


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    [
        (EventType.WORKFLOW_EXECUTION_COMPLETED, {}, True),
        (EventType.WORKFLOW_EXECUTION_STARTED, {}, False),
        (
            EventType.WORKFLOW_EXECUTION_FAILED,
            {"error_type": "RetryableException", "is_terminal": False},
            False,
        ),
        (
            EventType.WORKFLOW_EXECUTION_FAILED,
            {"error_type": "RetryableException", "is_terminal": True},
            True,
        ),
        (
            EventType.WORKFLOW_EXECUTION_FAILED,
            {"error_type": "ValueError", "is_terminal": True},
            True,
        ),
        # Legacy payloads without is_terminal
        (
            EventType.WORKFLOW_EXECUTION_FAILED,
            {"error_type": "RetryableException"},
            False,
        ),
        (
            EventType.WORKFLOW_EXECUTION_FAILED,
            {"error_type": "ValueError"},
            True,
        ),
    ],
)
def test_is_terminal_event_handles_explicit_and_legacy_payloads(
    event_type: EventType, payload: dict[str, object], expected: bool
) -> None:
    dto = _make_event_dto(event_type, payload)
    assert WorkflowManager._is_terminal_event(dto) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "is_last_retry", "expected_is_terminal"),
    [
        (
            WorkflowStepException("ocr", RetryableException("HTTP 429")),
            False,
            False,
        ),
        (
            WorkflowStepException("ocr", RetryableException("HTTP 429")),
            True,
            True,
        ),
        (
            WorkflowStepException("extract", ValueError("invalid document")),
            False,
            True,
        ),
    ],
)
async def test_workflow_manager_start_emits_expected_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    exc: Exception,
    is_last_retry: bool,
    expected_is_terminal: bool,
) -> None:
    exec_id = str(uuid4())
    started_dto = _make_event_dto(
        EventType.WORKFLOW_EXECUTION_STARTED,
        {"version": 1},
        execution_id=exec_id,
    )

    mock_session = AsyncMock()
    mock_db_cm = AsyncMock()
    mock_db_cm.__aenter__.return_value = mock_session
    mock_db_cm.__aexit__.return_value = None

    mock_db_manager = MagicMock()
    mock_db_manager.local_session.return_value = mock_db_cm
    mock_db_manager.dispose_async = AsyncMock()

    emit_failed_mock = AsyncMock()

    class _FakeEventStoreService:
        def __init__(self, _session: object) -> None:
            self.session = _session

        emit_workflow_failed = emit_failed_mock

        async def get_workflow_started_event(self, _eid: str) -> None:
            return None

    monkeypatch.setattr(
        wm_module, "_create_worker_database_manager", lambda: mock_db_manager
    )
    monkeypatch.setattr(wm_module, "EventStoreService", _FakeEventStoreService)
    monkeypatch.setattr(wm_module, "setup_logging_worker", lambda: None)
    monkeypatch.setattr(wm_module, "handle_finish_execution", lambda *_a, **_kw: None)

    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=exec_id),
        retry_count=3 if is_last_retry else 0,
        is_last_retry=is_last_retry,
    )

    async def fake_prepare(_session: object) -> bool:
        manager.event_dto = started_dto
        return False

    async def failing_execute() -> None:
        raise exc

    with (
        patch.object(manager, "_prepare_workflow", side_effect=fake_prepare),
        patch.object(
            manager,
            "_parse_start_event",
            return_value=SimpleNamespace(
                workflow_id="wf-1", workflow_configuration=None
            ),
        ),
        patch.object(manager, "_prepare_executor", return_value=None),
        patch.object(manager, "_execute_workflow", side_effect=failing_execute),
        patch.object(
            manager, "_notify_webhook_execution_finished", new_callable=AsyncMock
        ) as mock_notify,
        patch.object(
            manager, "_purge_s3_file_for_execution", new_callable=AsyncMock
        ) as mock_purge_s3,
    ):
        with pytest.raises((RetryableException, ValueError)):
            await manager.start()

    emit_failed_mock.assert_awaited_once()
    assert emit_failed_mock.await_args is not None
    assert emit_failed_mock.await_args.kwargs["is_terminal"] is expected_is_terminal

    if expected_is_terminal:
        mock_purge_s3.assert_awaited_once()
        mock_notify.assert_awaited_once()
    else:
        mock_purge_s3.assert_not_awaited()
        mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_watchdog_terminal_failure_emits_is_terminal_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_id = str(uuid4())
    non_terminal_failed_dto = _make_event_dto(
        EventType.WORKFLOW_EXECUTION_FAILED,
        {
            "version": 1,
            "error_type": RetryableException.__name__,
            "is_terminal": False,
        },
        execution_id=exec_id,
    )

    mock_session = AsyncMock()
    mock_db_cm = AsyncMock()
    mock_db_cm.__aenter__.return_value = mock_session
    mock_db_cm.__aexit__.return_value = None

    mock_db_manager = MagicMock()
    mock_db_manager.local_session.return_value = mock_db_cm
    mock_db_manager.dispose_async = AsyncMock()

    emit_failed_mock = AsyncMock()

    class _FakeEventStoreService:
        def __init__(self, _session: object) -> None:
            self.session = _session

        async def get_last_event_for_execution_id(self, _eid: str) -> EventDTO:
            return non_terminal_failed_dto

        async def get_workflow_started_event(self, _eid: str) -> None:
            return None

        emit_workflow_failed = emit_failed_mock

    monkeypatch.setattr(
        wm_module, "_create_worker_database_manager", lambda: mock_db_manager
    )
    monkeypatch.setattr(wm_module, "EventStoreService", _FakeEventStoreService)
    monkeypatch.setattr(wm_module, "setup_logging_worker", lambda: None)

    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=exec_id),
        retry_count=3,
        is_last_retry=True,
    )
    with (
        patch.object(
            manager, "_notify_webhook_execution_finished", new_callable=AsyncMock
        ),
        patch.object(manager, "_purge_s3_file_for_execution", new_callable=AsyncMock),
    ):
        await manager.record_watchdog_terminal_failure("max_processing_time")

    emit_failed_mock.assert_awaited_once()
    assert emit_failed_mock.await_args is not None
    assert emit_failed_mock.await_args.kwargs["is_terminal"] is True
    assert emit_failed_mock.await_args.kwargs["error_type"] == "WatchdogTimeout"
