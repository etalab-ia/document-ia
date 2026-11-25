"""Unit tests for the consumer's terminal-failure callback wiring."""

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from document_ia_infra.redis.model.workflow_execution_message import (
    WorkflowExecutionMessage,
)
from document_ia_worker.workflow import terminal_failure
from document_ia_worker.workflow import workflow_manager as workflow_manager_module
from document_ia_worker.workflow.workflow_manager import WorkflowManager


class _FakeManager:
    instances: list["_FakeManager"] = []

    def __init__(self, message, retry_count: int, is_last_retry: bool = False):
        self.message = message
        self.retry_count = retry_count
        self.is_last_retry = is_last_retry
        self.reasons: list[str] = []
        _FakeManager.instances.append(self)

    async def record_watchdog_terminal_failure(self, reason: str) -> None:
        self.reasons.append(reason)


@pytest.fixture(autouse=True)
def _patch_manager(monkeypatch):
    _FakeManager.instances = []
    monkeypatch.setattr(terminal_failure, "WorkflowManager", _FakeManager)
    yield


async def test_notify_watchdog_terminal_failure_delegates_message_and_retries():
    exec_id = f"exec-{uuid4()}"
    fields = {"data": json.dumps({"workflow_execution_id": exec_id}), "retries": "2"}

    await terminal_failure.notify_watchdog_terminal_failure(
        "1-0", fields, "workflow_timeout"
    )

    assert len(_FakeManager.instances) == 1
    manager = _FakeManager.instances[0]
    assert manager.message.workflow_execution_id == exec_id
    assert manager.retry_count == 2
    assert manager.is_last_retry is True
    assert manager.reasons == ["workflow_timeout"]


async def test_notify_watchdog_terminal_failure_skips_payload_less_message():
    await terminal_failure.notify_watchdog_terminal_failure(
        "1-0", {}, "workflow_timeout"
    )

    assert _FakeManager.instances == []


def _event(
    event_type: str,
    payload: dict[str, Any] | None = None,
    execution_id: str | None = None,
) -> Any:
    return SimpleNamespace(
        event_type=event_type,
        event={} if payload is None else payload,
        execution_id=execution_id if execution_id is not None else str(uuid4()),
        workflow_id=f"wf-{uuid4()}",
        organization_id=uuid4(),
    )


def test_is_terminal_event_distinguishes_retryable_failure():
    """Un échec retryable est intermédiaire : l'échec terminal doit encore être écrit."""
    assert (
        WorkflowManager._is_terminal_event(_event("WorkflowExecutionCompleted")) is True
    )
    assert (
        WorkflowManager._is_terminal_event(
            _event("WorkflowExecutionFailed", {"error_type": "ValueError"})
        )
        is True
    )
    assert (
        WorkflowManager._is_terminal_event(
            _event("WorkflowExecutionFailed", {"error_type": "RetryableException"})
        )
        is False
    )
    assert (
        WorkflowManager._is_terminal_event(_event("WorkflowExecutionStarted")) is False
    )


class _FakeSession:
    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


class _FakeSessionContext:
    async def __aenter__(self) -> Any:
        return _FakeSession()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeDatabaseManager:
    instances: list["_FakeDatabaseManager"] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.disposed = False
        _FakeDatabaseManager.instances.append(self)

    def local_session(self) -> _FakeSessionContext:
        return _FakeSessionContext()

    async def dispose_async(self) -> None:
        self.disposed = True


class _FakeEventStoreService:
    last_event: Any = None
    started_event: Any = None
    emitted: list[dict[str, Any]] = []

    def __init__(self, _session: Any) -> None:
        pass

    async def get_last_event_for_execution_id(self, _execution_id: str) -> Any:
        return _FakeEventStoreService.last_event

    async def get_workflow_started_event(self, _execution_id: str) -> Any:
        return _FakeEventStoreService.started_event

    async def emit_workflow_failed(self, **kwargs: Any) -> None:
        _FakeEventStoreService.emitted.append(kwargs)


class _FakeRedisManager:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def _patch_workflow_resources(monkeypatch):
    _FakeDatabaseManager.instances = []
    _FakeEventStoreService.emitted = []
    _FakeEventStoreService.last_event = None
    _FakeEventStoreService.started_event = None
    monkeypatch.setattr(
        workflow_manager_module, "DatabaseManager", _FakeDatabaseManager
    )
    monkeypatch.setattr(
        workflow_manager_module, "EventStoreService", _FakeEventStoreService
    )
    monkeypatch.setattr(workflow_manager_module, "RedisManager", _FakeRedisManager)


async def test_terminal_failure_writes_and_releases_resources(
    _patch_workflow_resources,
):
    valid_exec_id = str(uuid4())
    _FakeEventStoreService.last_event = _event(
        "WorkflowExecutionStarted", execution_id=valid_exec_id
    )
    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=valid_exec_id),
        2,
        is_last_retry=True,
    )

    await manager.record_watchdog_terminal_failure("workflow_timeout")

    assert len(_FakeEventStoreService.emitted) == 1
    assert _FakeEventStoreService.emitted[0]["error_type"] == "WatchdogTimeout"
    assert _FakeEventStoreService.emitted[0]["retry_count"] == 2
    assert _FakeDatabaseManager.instances[-1].disposed is True
    assert manager._webhook_redis_manager.closed is True


async def test_terminal_failure_releases_resources_on_terminal_shortcut(
    _patch_workflow_resources,
):
    """Sur une exécution déjà terminée, les ressources sont libérées sans rien écrire."""
    valid_exec_id = str(uuid4())
    _FakeEventStoreService.last_event = _event(
        "WorkflowExecutionCompleted", execution_id=valid_exec_id
    )
    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=valid_exec_id),
        2,
        is_last_retry=True,
    )

    await manager.record_watchdog_terminal_failure("workflow_timeout")

    assert _FakeEventStoreService.emitted == []
    assert _FakeDatabaseManager.instances[-1].disposed is True
    assert manager._webhook_redis_manager.closed is True


async def test_record_watchdog_terminal_failure_deletes_s3_file(
    _patch_workflow_resources, monkeypatch, caplog
):
    import logging
    from pydantic import SecretStr

    raw_s3_key = "sensitive/customer_uploads/document.pdf"
    fake_s3_file_info = SimpleNamespace(
        s3_key=SecretStr(raw_s3_key),
    )
    started_event_obj = SimpleNamespace(
        s3_file_info=fake_s3_file_info,
    )
    last_event_obj = _event("WorkflowExecutionStarted")

    _FakeEventStoreService.last_event = last_event_obj
    _FakeEventStoreService.started_event = (last_event_obj, started_event_obj)

    deleted_keys: list[str] = []

    class _FakeS3Manager:
        def delete_file(self, s3_key: str) -> bool:
            deleted_keys.append(s3_key)
            return True

    monkeypatch.setattr(workflow_manager_module, "S3Manager", _FakeS3Manager)

    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=last_event_obj.execution_id),
        2,
        is_last_retry=True,
    )

    with caplog.at_level(logging.INFO):
        await manager.record_watchdog_terminal_failure("workflow_timeout")

    assert deleted_keys == [raw_s3_key]
    assert raw_s3_key not in caplog.text
    assert "Watchdog terminal failure: S3 file deleted for execution" in caplog.text


async def test_record_watchdog_terminal_failure_still_purges_s3_when_already_terminal(
    _patch_workflow_resources, monkeypatch
):
    from pydantic import SecretStr

    raw_s3_key = "sensitive/completed_execution/document.pdf"
    fake_s3_file_info = SimpleNamespace(
        s3_key=SecretStr(raw_s3_key),
    )
    started_event_obj = SimpleNamespace(
        s3_file_info=fake_s3_file_info,
    )
    last_event_obj = _event("WorkflowExecutionCompleted")

    _FakeEventStoreService.last_event = last_event_obj
    _FakeEventStoreService.started_event = (last_event_obj, started_event_obj)

    deleted_keys: list[str] = []

    class _FakeS3Manager:
        def delete_file(self, s3_key: str) -> bool:
            deleted_keys.append(s3_key)
            return True

    monkeypatch.setattr(workflow_manager_module, "S3Manager", _FakeS3Manager)

    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=last_event_obj.execution_id),
        2,
        is_last_retry=True,
    )

    await manager.record_watchdog_terminal_failure("workflow_timeout")

    assert _FakeEventStoreService.emitted == []
    assert deleted_keys == [raw_s3_key]


async def test_start_purges_s3_on_terminal_prepare_workflow_failure(
    _patch_workflow_resources, monkeypatch
):
    from pydantic import SecretStr
    from document_ia_worker.exception.workflow_not_found_exception import (
        WorkflowNotFoundException,
    )

    raw_s3_key = "sensitive/prepare_failure/document.pdf"
    fake_s3_file_info = SimpleNamespace(
        s3_key=SecretStr(raw_s3_key),
    )
    started_event_obj = SimpleNamespace(
        s3_file_info=fake_s3_file_info,
    )
    started_event_dto = _event("WorkflowExecutionStarted")

    _FakeEventStoreService.started_event = (started_event_dto, started_event_obj)

    deleted_keys: list[str] = []

    class _FakeS3Manager:
        def delete_file(self, s3_key: str) -> bool:
            deleted_keys.append(s3_key)
            return True

    monkeypatch.setattr(workflow_manager_module, "S3Manager", _FakeS3Manager)

    exec_id = started_event_dto.execution_id
    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=exec_id),
        0,
        is_last_retry=False,
    )

    async def failing_prepare(_session):
        raise WorkflowNotFoundException("Workflow wf-test not found")

    monkeypatch.setattr(manager, "_prepare_workflow", failing_prepare)

    with pytest.raises(WorkflowNotFoundException):
        await manager.start()

    assert deleted_keys == [raw_s3_key]


async def test_execute_workflow_cleanup_continues_if_later_step_cleanup_raises(
    _patch_workflow_resources, monkeypatch
):
    from unittest.mock import AsyncMock

    from document_ia_worker.workflow.main_workflow_context import (
        MainWorkflowContext,
    )
    from datetime import datetime, UTC

    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id="exec-cleanup-resilience"),
        0,
        is_last_retry=False,
    )
    manager.main_workflow_context = MainWorkflowContext(
        execution_id="exec-cleanup-resilience",
        organization_id=uuid4(),
        workflow_version=1,
        start_time=datetime.now(UTC),
        steps_metadata=[],
    )

    from unittest.mock import MagicMock
    from document_ia_worker.exception.workflow_step_exception import (
        WorkflowStepException,
    )

    step1 = SimpleNamespace(
        cleanup=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=("res1", None)),
        inject_workflow_context=MagicMock(),
        get_context_result_key=MagicMock(return_value="key1"),
    )
    step2 = SimpleNamespace(
        cleanup=AsyncMock(side_effect=RuntimeError("Step 2 cleanup failed")),
        execute=AsyncMock(side_effect=RuntimeError("Step 2 failed")),
        inject_workflow_context=MagicMock(),
        get_context_result_key=MagicMock(return_value="key2"),
    )

    manager.step_list = [step1, step2]

    with pytest.raises(WorkflowStepException) as exc_info:
        await manager._execute_workflow()

    assert "Step 2 failed" in str(exc_info.value.inner_exception)

    assert step2.cleanup.called is True
    assert step1.cleanup.called is True


async def test_start_aborted_during_save_failure_event_does_not_purge_s3(
    _patch_workflow_resources, monkeypatch
):
    from datetime import datetime, UTC
    from pydantic import SecretStr

    from document_ia_infra.exception.processing_aborted_exception import (
        ProcessingAbortedException,
    )
    from document_ia_worker.workflow.main_workflow_context import (
        MainWorkflowContext,
    )

    raw_s3_key = "sensitive/aborted/document.pdf"
    started_event_obj = SimpleNamespace(
        s3_file_info=SimpleNamespace(s3_key=SecretStr(raw_s3_key)),
    )
    started_event_dto = _event("WorkflowExecutionStarted")
    _FakeEventStoreService.started_event = (started_event_dto, started_event_obj)

    deleted_keys: list[str] = []

    class _FakeS3Manager:
        def delete_file(self, s3_key: str) -> bool:
            deleted_keys.append(s3_key)
            return True

    monkeypatch.setattr(workflow_manager_module, "S3Manager", _FakeS3Manager)

    fake_tracker = SimpleNamespace(is_aborted=False)
    monkeypatch.setattr(
        workflow_manager_module, "get_current_progress_tracker", lambda: fake_tracker
    )

    exec_id = started_event_dto.execution_id
    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=exec_id),
        0,
        is_last_retry=True,
    )

    async def fake_prepare(_session):
        manager.event_dto = started_event_dto
        manager.main_workflow_context = MainWorkflowContext(
            execution_id=exec_id,
            organization_id=uuid4(),
            workflow_version=1,
            start_time=datetime.now(UTC),
            steps_metadata=[],
        )
        return False

    async def failing_execute():
        raise RuntimeError("Execution failed")

    async def aborted_save_failure(_session, _exc, **_kwargs):
        fake_tracker.is_aborted = True
        raise ProcessingAbortedException("Fenced by watchdog")

    from unittest.mock import MagicMock

    monkeypatch.setattr(manager, "_prepare_workflow", fake_prepare)
    monkeypatch.setattr(manager, "_parse_start_event", MagicMock(return_value=None))
    monkeypatch.setattr(manager, "_prepare_executor", MagicMock(return_value=None))
    monkeypatch.setattr(manager, "_execute_workflow", failing_execute)
    monkeypatch.setattr(manager, "_save_failure_event", aborted_save_failure)

    await manager.start()

    assert deleted_keys == []


async def test_start_purges_s3_when_download_step_cleanup_raises(
    _patch_workflow_resources, monkeypatch
):
    from datetime import datetime, UTC
    from unittest.mock import AsyncMock, MagicMock
    from pydantic import SecretStr

    from document_ia_worker.workflow.main_workflow_context import (
        MainWorkflowContext,
    )

    raw_s3_key = "sensitive/cleanup_raises/document.pdf"
    started_event_obj = SimpleNamespace(
        s3_file_info=SimpleNamespace(s3_key=SecretStr(raw_s3_key)),
    )
    started_event_dto = _event("WorkflowExecutionStarted")
    _FakeEventStoreService.started_event = (started_event_dto, started_event_obj)

    deleted_keys: list[str] = []

    class _FakeS3Manager:
        def delete_file(self, s3_key: str) -> bool:
            deleted_keys.append(s3_key)
            return True

    monkeypatch.setattr(workflow_manager_module, "S3Manager", _FakeS3Manager)
    monkeypatch.setattr(
        workflow_manager_module, "get_current_progress_tracker", lambda: None
    )

    exec_id = started_event_dto.execution_id
    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=exec_id),
        0,
        is_last_retry=True,
    )

    download_step = SimpleNamespace(
        cleanup=AsyncMock(side_effect=RuntimeError("S3 cleanup failed")),
        execute=AsyncMock(side_effect=RuntimeError("Step failed")),
        inject_workflow_context=MagicMock(),
        get_context_result_key=MagicMock(return_value="download_res"),
    )

    async def fake_prepare(_session):
        manager.event_dto = started_event_dto
        manager.main_workflow_context = MainWorkflowContext(
            execution_id=exec_id,
            organization_id=uuid4(),
            workflow_version=1,
            start_time=datetime.now(UTC),
            steps_metadata=[],
        )
        manager.step_list = [download_step]
        return False

    monkeypatch.setattr(manager, "_prepare_workflow", fake_prepare)
    monkeypatch.setattr(manager, "_parse_start_event", MagicMock(return_value=None))
    monkeypatch.setattr(manager, "_prepare_executor", MagicMock(return_value=None))
    monkeypatch.setattr(manager, "_save_failure_event", AsyncMock(return_value=None))
    monkeypatch.setattr(
        manager, "_notify_webhook_execution_finished", AsyncMock(return_value=None)
    )

    with pytest.raises(RuntimeError, match="Step failed"):
        await manager.start()

    assert manager._s3_cleaned_up is False
    assert deleted_keys == [raw_s3_key]


async def test_start_purges_s3_when_download_step_delete_returns_false(
    _patch_workflow_resources, monkeypatch
):
    from datetime import datetime, UTC
    from unittest.mock import AsyncMock, MagicMock
    from pydantic import SecretStr

    from document_ia_infra.core.model.file_info import FileInfo
    from document_ia_worker.workflow.main_workflow_context import (
        MainWorkflowContext,
    )
    from document_ia_worker.workflow.step.download_file import (
        download_file as download_file_module,
    )
    from document_ia_worker.workflow.step.download_file.download_file import (
        DownloadFileStep,
    )

    raw_s3_key = "sensitive/delete_returns_false/document.pdf"
    file_info = FileInfo(
        filename="document.pdf",
        s3_key=SecretStr(raw_s3_key),
        size=1024,
        content_type="application/pdf",
        uploaded_at=datetime.now(UTC).isoformat(),
        presigned_url=SecretStr("https://s3.example.com/presigned"),
    )
    started_event_obj = SimpleNamespace(s3_file_info=file_info)
    started_event_dto = _event("WorkflowExecutionStarted")
    _FakeEventStoreService.started_event = (started_event_dto, started_event_obj)

    deleted_keys: list[str] = []

    class _FakeS3Manager:
        def delete_file(self, s3_key: str) -> bool:
            deleted_keys.append(s3_key)
            return False

    monkeypatch.setattr(workflow_manager_module, "S3Manager", _FakeS3Manager)
    monkeypatch.setattr(download_file_module, "S3Manager", _FakeS3Manager)
    monkeypatch.setattr(
        workflow_manager_module, "get_current_progress_tracker", lambda: None
    )

    exec_id = str(uuid4())
    started_event_dto.execution_id = exec_id
    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=exec_id),
        0,
        is_last_retry=True,
    )
    ctx = MainWorkflowContext(
        execution_id=exec_id,
        organization_id=uuid4(),
        workflow_version=1,
        start_time=datetime.now(UTC),
        steps_metadata=[],
    )
    download_step = DownloadFileStep(main_workflow_context=ctx, file_info=file_info)
    monkeypatch.setattr(
        download_step,
        "execute",
        AsyncMock(side_effect=RuntimeError("Step failed")),
    )

    async def fake_prepare(_session):
        manager.event_dto = started_event_dto
        manager.main_workflow_context = ctx
        manager.step_list = [download_step]
        return False

    monkeypatch.setattr(manager, "_prepare_workflow", fake_prepare)
    monkeypatch.setattr(manager, "_parse_start_event", MagicMock(return_value=None))
    monkeypatch.setattr(manager, "_prepare_executor", MagicMock(return_value=None))
    monkeypatch.setattr(manager, "_save_failure_event", AsyncMock(return_value=None))
    monkeypatch.setattr(
        manager, "_notify_webhook_execution_finished", AsyncMock(return_value=None)
    )

    with pytest.raises(RuntimeError, match="Step failed"):
        await manager.start()

    assert download_step.s3_cleanup_succeeded is False
    assert manager._s3_cleaned_up is False
    assert deleted_keys == [raw_s3_key, raw_s3_key]


async def test_save_failure_event_preserves_workflow_version_v2(
    _patch_workflow_resources, monkeypatch
):
    from datetime import datetime, UTC
    from document_ia_worker.exception.workflow_step_exception import (
        WorkflowStepException,
    )
    from document_ia_worker.workflow.main_workflow_context import (
        MainWorkflowContext,
    )

    exec_id = str(uuid4())
    started_v2_event = _event(
        "WorkflowExecutionStarted",
        payload={"version": 2},
        execution_id=exec_id,
    )
    started_v2_event.workflow_id = "document-barcode-extraction-v2"

    class _FakeEventRepo:
        def __init__(self, _session: Any) -> None:
            pass

        async def get_created_event_if_execution_not_completed_or_failed(
            self, _exec_id: str
        ) -> Any:
            return started_v2_event

    monkeypatch.setattr(workflow_manager_module, "EventRepository", _FakeEventRepo)
    monkeypatch.setattr(
        workflow_manager_module.workflow_v2_repository,
        "get_raw_workflow_by_id",
        lambda _wf_id: {"id": "document-barcode-extraction-v2"},
    )

    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=exec_id),
        0,
        is_last_retry=True,
    )
    manager.main_workflow_context = MainWorkflowContext(
        execution_id=exec_id,
        organization_id=None,
        start_time=datetime.now(UTC),
        steps_metadata=[],
    )
    assert manager.main_workflow_context.workflow_version is None

    session = _FakeSession()

    # Case A: before _prepare_workflow populates main_workflow_context.workflow_version,
    # _save_failure_event falls back to self.event_dto.event["version"].
    manager.event_dto = started_v2_event
    await manager._save_failure_event(
        session,  # type: ignore[arg-type]
        WorkflowStepException("prepare_workflow", ValueError("Pre-prepare failure")),
    )
    assert len(_FakeEventStoreService.emitted) == 1
    assert _FakeEventStoreService.emitted[0]["workflow_version"] == 2
    assert _FakeEventStoreService.emitted[0]["failed_step"] == "prepare_workflow"

    # Case B: after _prepare_workflow populates main_workflow_context.workflow_version == 2
    _FakeEventStoreService.emitted.clear()
    is_dup = await manager._prepare_workflow(session)  # type: ignore[arg-type]
    assert is_dup is False
    assert manager.main_workflow_context.workflow_version == 2

    await manager._save_failure_event(
        session,  # type: ignore[arg-type]
        WorkflowStepException("DownloadFileStep", ValueError("SSRF blocked")),
    )

    assert len(_FakeEventStoreService.emitted) == 1
    emitted = _FakeEventStoreService.emitted[0]
    assert emitted["workflow_id"] == "document-barcode-extraction-v2"
    assert emitted["execution_id"] == exec_id
    assert emitted["error_type"] == "ValueError"
    assert emitted["failed_step"] == "DownloadFileStep"
    assert emitted["workflow_version"] == 2


async def test_record_terminal_failure_finalizes_non_terminal_retry_scheduling_error(
    _patch_workflow_resources, monkeypatch
):
    """When retry scheduling fails after a non-terminal failure, finalize DB event, purge S3, and notify webhook."""
    from unittest.mock import AsyncMock
    from pydantic import SecretStr

    exec_id = str(uuid4())
    raw_s3_key = "sensitive/requeue_error/document.pdf"
    started_event_obj = SimpleNamespace(
        s3_file_info=SimpleNamespace(s3_key=SecretStr(raw_s3_key)),
    )
    non_terminal_failed_event = _event(
        "WorkflowExecutionFailed",
        payload={
            "error_type": "RetryableException",
            "failed_step": "ocr",
            "is_terminal": False,
            "version": 2,
        },
        execution_id=exec_id,
    )
    _FakeEventStoreService.last_event = non_terminal_failed_event
    _FakeEventStoreService.started_event = (
        non_terminal_failed_event,
        started_event_obj,
    )

    deleted_keys: list[str] = []

    class _FakeS3Manager:
        def delete_file(self, s3_key: str) -> bool:
            deleted_keys.append(s3_key)
            return True

    monkeypatch.setattr(workflow_manager_module, "S3Manager", _FakeS3Manager)

    manager = WorkflowManager(
        WorkflowExecutionMessage(workflow_execution_id=exec_id),
        retry_count=1,
        is_last_retry=True,
    )
    webhook_mock = AsyncMock()
    monkeypatch.setattr(manager, "_notify_webhook_execution_finished", webhook_mock)

    await manager.record_watchdog_terminal_failure("error_requeueing_message")

    assert len(_FakeEventStoreService.emitted) == 1
    emitted = _FakeEventStoreService.emitted[0]
    assert emitted["is_terminal"] is True
    assert emitted["error_type"] == "RetrySchedulingError"
    assert emitted["failed_step"] == "ocr"
    assert emitted["workflow_version"] == 2
    assert emitted["retry_count"] == 1
    assert "error_requeueing_message" in emitted["error_message"]
    assert deleted_keys == [raw_s3_key]
    assert webhook_mock.await_count == 1
    assert _FakeDatabaseManager.instances[-1].disposed is True
    assert manager._webhook_redis_manager.closed is True
