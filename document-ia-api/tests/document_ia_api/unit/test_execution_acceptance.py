"""Contrat d'acceptation des exécutions lorsque la programmation échoue (issue #120).

Une publication Redis en échec ne doit jamais produire une réponse annonçant une
exécution acceptée en `processing`. Les services v1 et v2 doivent répondre `503`
avec `Retry-After` et clôturer l'exécution déjà persistée.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from document_ia_api.application.services.execution_acceptance import (
    S3CompensationGuard,
    schedule_workflow_execution,
)
from document_ia_api.application.services.workflow_service import WorkflowService
from document_ia_api.application.services.workflow_v2_service import WorkflowV2Service
from document_ia_infra.redis.publisher import PublishFailedError, Publisher
from document_ia_infra.redis.redis_settings import redis_settings

pytestmark = pytest.mark.asyncio


V1_WORKFLOW_ID = "document-classification-v1"
V2_WORKFLOW_ID = "document-extraction-v2"


def _publish_failure() -> PublishFailedError:
    return PublishFailedError("event_stream", "no_connection")


def _v2_raw_workflow() -> dict:
    return {
        "id": V2_WORKFLOW_ID,
        "steps": [
            {
                "action": "llm_classify_document",
                "params": {"document_types": {"default": "all"}},
            },
            {
                "action": "llm_extract_data",
                "params": {"model": {"type": "string", "default": "openweight-medium"}},
            },
        ],
    }


def _event_store_recorder() -> MagicMock:
    """Event store factice enregistrant les événements émis, dans l'ordre."""
    recorder = MagicMock()
    recorder.emitted: list[str] = []

    async def _started(**kwargs):
        recorder.emitted.append("started")
        recorder.started_kwargs = kwargs

    async def _failed(**kwargs):
        recorder.emitted.append("failed")
        recorder.failed_kwargs = kwargs

    recorder.emit_workflow_started = AsyncMock(side_effect=_started)
    recorder.emit_workflow_failed = AsyncMock(side_effect=_failed)
    return recorder


def _install_event_store(monkeypatch: pytest.MonkeyPatch, module: str, recorder):
    """Remplace le service d'événements par l'enregistreur.

    Le service de clôture est importé depuis son propre module : il faut aussi
    patcher cette référence, sinon l'exécution resterait ouverte sans que le test
    ne le voie.
    """
    factory = lambda _: recorder  # noqa: E731
    monkeypatch.setattr(f"{module}.EventStoreService", factory)
    monkeypatch.setattr(
        "document_ia_api.application.services.execution_acceptance.EventStoreService",
        factory,
    )


def _assert_scheduling_unavailable(exc: HTTPException, execution_id: str):
    assert exc.status_code == 503
    assert exc.detail["error"] == "execution_scheduling_unavailable"
    assert exc.detail["execution_id"] == execution_id
    # Le client doit pouvoir rejouer sa demande : le délai est borné et explicite.
    assert exc.headers is not None
    assert 1 <= int(exc.headers["Retry-After"]) <= 120


def _assert_execution_closed(recorder, organization_id, workflow_id):
    """L'exécution ne reste pas en `STARTED` sans traitement programmé."""
    assert recorder.emitted == ["started", "failed"]
    failed = recorder.failed_kwargs
    assert failed["organization_id"] == organization_id
    assert failed["workflow_id"] == workflow_id
    assert failed["error_type"] == "execution_scheduling_unavailable"


async def test_v1_publish_failure_returns_503_and_closes_execution(monkeypatch):
    db_session = AsyncMock()
    service = WorkflowService(db_session)
    recorder = _event_store_recorder()
    _install_event_store(
        monkeypatch,
        "document_ia_api.application.services.workflow_service",
        recorder,
    )
    monkeypatch.setattr(
        "document_ia_api.application.services.workflow_service.workflow_repository.get_workflow_by_id",
        AsyncMock(return_value={"id": V1_WORKFLOW_ID}),
    )
    monkeypatch.setattr(
        service.redis_producer,
        "publish_message",
        AsyncMock(side_effect=_publish_failure()),
    )

    organization_id = uuid4()
    with pytest.raises(HTTPException) as exc_info:
        await service.execute_workflow(
            organization_id=organization_id,
            workflow_id=V1_WORKFLOW_ID,
            file=None,
            file_url="https://example.com/document.pdf",
            metadata_json=None,
            request_classification_parameter=None,
            request_extraction_parameter=None,
        )

    execution_id = recorder.started_kwargs["execution_id"]
    _assert_scheduling_unavailable(exc_info.value, execution_id)
    _assert_execution_closed(recorder, organization_id, V1_WORKFLOW_ID)
    # Le client n'a reçu aucune exécution acceptée : sa nouvelle tentative part
    # d'un état connu, sans exécution concurrente en cours de traitement.


async def test_v2_publish_failure_returns_503_and_closes_execution(monkeypatch):
    db_session = AsyncMock()
    service = WorkflowV2Service(db_session)
    recorder = _event_store_recorder()
    _install_event_store(
        monkeypatch,
        "document_ia_api.application.services.workflow_v2_service",
        recorder,
    )
    monkeypatch.setattr(
        "document_ia_api.application.services.workflow_v2_service.workflow_v2_repository.get_raw_workflow_by_id",
        lambda _: _v2_raw_workflow(),
    )
    monkeypatch.setattr(
        service.redis_producer,
        "publish_message",
        AsyncMock(side_effect=_publish_failure()),
    )

    organization_id = uuid4()
    with pytest.raises(HTTPException) as exc_info:
        await service.execute_workflow(
            organization_id=organization_id,
            workflow_id=V2_WORKFLOW_ID,
            file=None,
            file_url="https://example.com/document.pdf",
            metadata_json=None,
            override_payload=None,
        )

    execution_id = recorder.started_kwargs["execution_id"]
    _assert_scheduling_unavailable(exc_info.value, execution_id)
    _assert_execution_closed(recorder, organization_id, V2_WORKFLOW_ID)


async def test_v1_publish_success_keeps_processing_contract(monkeypatch):
    """Le chemin de succès v1 conserve sa réponse `processing` et aucun échec."""
    db_session = AsyncMock()
    service = WorkflowService(db_session)
    recorder = _event_store_recorder()
    _install_event_store(
        monkeypatch,
        "document_ia_api.application.services.workflow_service",
        recorder,
    )
    monkeypatch.setattr(
        "document_ia_api.application.services.workflow_service.workflow_repository.get_workflow_by_id",
        AsyncMock(return_value={"id": V1_WORKFLOW_ID}),
    )
    monkeypatch.setattr(
        service.redis_producer, "publish_message", AsyncMock(return_value="1-0")
    )

    result = await service.execute_workflow(
        organization_id=uuid4(),
        workflow_id=V1_WORKFLOW_ID,
        file=None,
        file_url="https://example.com/document.pdf",
        metadata_json=None,
        request_classification_parameter=None,
        request_extraction_parameter=None,
    )

    assert result.status == "processing"
    assert recorder.emitted == ["started"]


async def test_v2_publish_success_keeps_processing_contract(monkeypatch):
    """Le chemin de succès v2 conserve sa réponse `processing` et aucun échec."""
    db_session = AsyncMock()
    service = WorkflowV2Service(db_session)
    recorder = _event_store_recorder()
    _install_event_store(
        monkeypatch,
        "document_ia_api.application.services.workflow_v2_service",
        recorder,
    )
    monkeypatch.setattr(
        "document_ia_api.application.services.workflow_v2_service.workflow_v2_repository.get_raw_workflow_by_id",
        lambda _: _v2_raw_workflow(),
    )
    monkeypatch.setattr(
        service.redis_producer, "publish_message", AsyncMock(return_value="1-0")
    )

    result = await service.execute_workflow(
        organization_id=uuid4(),
        workflow_id=V2_WORKFLOW_ID,
        file=None,
        file_url="https://example.com/document.pdf",
        metadata_json=None,
        override_payload=None,
    )

    assert result.status == "processing"
    assert recorder.emitted == ["started"]


async def test_v1_close_failure_does_not_mask_the_503(monkeypatch):
    """Si la clôture de l'exécution échoue, la réponse reste un 503 explicite."""
    db_session = AsyncMock()
    service = WorkflowService(db_session)
    recorder = _event_store_recorder()
    recorder.emit_workflow_failed = AsyncMock(side_effect=RuntimeError("db down"))
    _install_event_store(
        monkeypatch,
        "document_ia_api.application.services.workflow_service",
        recorder,
    )
    monkeypatch.setattr(
        "document_ia_api.application.services.workflow_service.workflow_repository.get_workflow_by_id",
        AsyncMock(return_value={"id": V1_WORKFLOW_ID}),
    )
    monkeypatch.setattr(
        service.redis_producer,
        "publish_message",
        AsyncMock(side_effect=_publish_failure()),
    )

    with pytest.raises(HTTPException) as exc_info:
        await service.execute_workflow(
            organization_id=uuid4(),
            workflow_id=V1_WORKFLOW_ID,
            file=None,
            file_url="https://example.com/document.pdf",
            metadata_json=None,
            request_classification_parameter=None,
            request_extraction_parameter=None,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["error"] == "execution_scheduling_unavailable"


def _publisher_with_lost_reply(execution_id: str, queued: bool | None):
    """Build a real Publisher whose Redis reply is lost after a possible commit."""
    connection = AsyncMock()
    stream_entries = []

    async def publish_then_lose_reply(*args, **kwargs):
        if queued is not False:
            stream_entries.append(
                (
                    "1-0",
                    {"data": json.dumps({"workflow_execution_id": execution_id})},
                )
            )
        raise ConnectionError("Redis reply lost")

    connection.eval.side_effect = publish_then_lose_reply
    if queued is None:
        connection.xrange.side_effect = ConnectionError("Redis still unreadable")
    else:
        connection.xrange.side_effect = lambda *args, **kwargs: list(stream_entries)

    manager = AsyncMock()
    manager.get_connection.return_value = connection
    publisher = Publisher(
        redis_settings.EVENT_STREAM_NAME,
        current_redis_manager=manager,
    )
    return publisher, connection


async def test_unfenced_lost_reply_reconciles_queued_message_and_preserves_s3():
    execution_id = str(uuid4())
    publisher, connection = _publisher_with_lost_reply(execution_id, queued=True)
    guard = S3CompensationGuard(execution_id=execution_id)
    guard.arm("uploads/queued-document.pdf")

    with (
        patch(
            "document_ia_api.application.services.execution_acceptance.fail_unscheduled_execution",
            new=AsyncMock(),
        ) as fail_unscheduled,
        patch(
            "document_ia_api.application.services.execution_acceptance.compensate_s3_upload",
            new=AsyncMock(),
        ) as compensate,
    ):
        await schedule_workflow_execution(
            AsyncMock(),
            redis_producer=publisher,
            execution_id=execution_id,
            workflow_id=V1_WORKFLOW_ID,
            organization_id=uuid4(),
            compensation_guard=guard,
        )
        await guard.compensate_if_needed()

    assert connection.xrange.await_count == 1
    fail_unscheduled.assert_not_awaited()
    compensate.assert_not_awaited()


async def test_unfenced_lost_reply_deletes_s3_only_when_absence_is_confirmed():
    execution_id = str(uuid4())
    publisher, connection = _publisher_with_lost_reply(execution_id, queued=False)
    guard = S3CompensationGuard(execution_id=execution_id)
    guard.arm("uploads/unpublished-document.pdf")

    with (
        patch(
            "document_ia_api.application.services.execution_acceptance.fail_unscheduled_execution",
            new=AsyncMock(),
        ) as fail_unscheduled,
        patch(
            "document_ia_api.application.services.execution_acceptance.compensate_s3_upload",
            new=AsyncMock(),
        ) as compensate,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await schedule_workflow_execution(
                AsyncMock(),
                redis_producer=publisher,
                execution_id=execution_id,
                workflow_id=V1_WORKFLOW_ID,
                organization_id=uuid4(),
                compensation_guard=guard,
            )
        await guard.compensate_if_needed()

    assert exc_info.value.status_code == 503
    assert connection.xrange.await_count == 1
    fail_unscheduled.assert_awaited_once()
    compensate.assert_awaited_once_with(
        "uploads/unpublished-document.pdf", execution_id
    )


async def test_unfenced_unresolved_outcome_preserves_s3_without_failure_event():
    execution_id = str(uuid4())
    publisher, connection = _publisher_with_lost_reply(execution_id, queued=None)
    guard = S3CompensationGuard(execution_id=execution_id)
    guard.arm("uploads/unresolved-document.pdf")

    with (
        patch(
            "document_ia_api.application.services.execution_acceptance.fail_unscheduled_execution",
            new=AsyncMock(),
        ) as fail_unscheduled,
        patch(
            "document_ia_api.application.services.execution_acceptance.compensate_s3_upload",
            new=AsyncMock(),
        ) as compensate,
    ):
        await schedule_workflow_execution(
            AsyncMock(),
            redis_producer=publisher,
            execution_id=execution_id,
            workflow_id=V1_WORKFLOW_ID,
            organization_id=uuid4(),
            compensation_guard=guard,
        )
        await guard.compensate_if_needed()

    assert connection.xrange.await_count == 1
    fail_unscheduled.assert_not_awaited()
    compensate.assert_not_awaited()
