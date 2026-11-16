"""Contrat d'acceptation partagé entre les exécutions de workflows v1 et v2.

Une exécution n'est annoncée comme acceptée que si son traitement est programmé
de façon récupérable. Lorsque la publication sur le stream Redis échoue, l'appelant
obtient une erreur `503` assortie d'un header `Retry-After`, et l'exécution déjà
créée est clôturée explicitement au lieu de rester indéfiniment en `STARTED`.

Voir l'issue #120 : ce module ne cherche pas à rendre atomiques Redis, PostgreSQL
et le stockage objet. Il garantit un état et une réponse cohérents avec la
programmation récupérable du traitement.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from document_ia_api.core.idempotency.exceptions import (
    IdempotencyReservationLostException,
)
from document_ia_api.infra.s3_service import s3_service
from document_ia_infra.redis.model.workflow_execution_message import (
    WorkflowExecutionMessage,
)
from document_ia_infra.redis.publisher import (
    PublicationOutcomeUnknownError,
    PublishFailedError,
    ReservationLostError,
)
from document_ia_infra.redis.redis_settings import redis_settings
from document_ia_infra.service.event_store_service import EventStoreService

logger = logging.getLogger(__name__)

# Code d'erreur fonctionnel exposé dans le corps RFC 7807 de la réponse 503.
EXECUTION_SCHEDULING_ERROR = "execution_scheduling_unavailable"
# Code d'erreur utilisé lorsque la réservation d'idempotence est perdue avant
# la publication : l'exécution démarrée doit être clôturée, jamais laissée en
# `STARTED` sans message capable de la faire progresser.
EXECUTION_RESERVATION_LOST_ERROR = "idempotency_reservation_lost"


def scheduling_unavailable(execution_id: str) -> HTTPException:
    """Erreur HTTP renvoyée quand le traitement n'a pas pu être programmé."""
    return HTTPException(
        status_code=503,
        detail={
            "error": EXECUTION_SCHEDULING_ERROR,
            "message": (
                "The execution could not be scheduled. "
                "No processing is pending; retry the request."
            ),
            "execution_id": execution_id,
        },
        headers={
            "Retry-After": str(redis_settings.PUBLISH_RETRY_AFTER_SECONDS),
        },
    )


async def fail_unscheduled_execution(
    db_session: AsyncSession,
    *,
    execution_id: str,
    workflow_id: str,
    organization_id: UUID,
    error: PublishFailedError,
    workflow_version: int = 1,
) -> None:
    """Clôturer une exécution dont le traitement n'a pas pu être programmé.

    L'événement de démarrage ayant déjà été persisté, on écrit un événement
    d'échec terminal : les lectures d'exécution doivent refléter l'absence de
    traitement programmé plutôt qu'un `STARTED` qui ne progressera jamais.

    `workflow_version` reprend la version de l'événement de démarrage : la
    consultation d'une exécution sélectionne le dépôt de définitions de
    workflows d'après la version du dernier événement. Une clôture annoncée en
    v1 pour une exécution v2 la rendrait illisible.

    L'échec de cette écriture est journalisé et ne masque pas l'erreur de
    programmation initiale : la réponse `503` reste la vérité du contrat.
    """
    try:
        event_store_service = EventStoreService(db_session)
        await event_store_service.emit_workflow_failed(
            workflow_id=workflow_id,
            execution_id=execution_id,
            organization_id=organization_id,
            error_type=EXECUTION_SCHEDULING_ERROR,
            error_message=(
                "Execution could not be scheduled on the processing stream; "
                "no processing is pending."
            ),
            failed_step="schedule",
            workflow_version=workflow_version,
        )
    except Exception:
        logger.exception(
            "Execution %s: unable to record the scheduling failure event",
            execution_id,
        )


async def fail_abandoned_execution(
    db_session: AsyncSession,
    *,
    execution_id: str,
    workflow_id: str,
    organization_id: UUID,
    error_type: str,
    error_message: str,
    workflow_version: int = 1,
) -> None:
    """Clôturer une exécution démarrée qui ne sera jamais publiée.

    `emit_workflow_started` commite l'événement de démarrage : lorsque
    l'admission échoue après ce point (par exemple parce que la réservation
    d'idempotence a été perdue), le rollback de la route ne peut plus l'annuler.
    Rapporter l'exécution comme `STARTED` alors qu'aucun message ne la fera
    progresser induirait les clients en erreur ; on écrit donc un événement
    d'échec terminal.

    L'échec de cette écriture est journalisé et ne masque pas l'erreur
    d'admission d'origine.
    """
    try:
        event_store_service = EventStoreService(db_session)
        await event_store_service.emit_workflow_failed(
            workflow_id=workflow_id,
            execution_id=execution_id,
            organization_id=organization_id,
            error_type=error_type,
            error_message=error_message,
            failed_step="schedule",
            workflow_version=workflow_version,
        )
    except Exception:
        logger.exception(
            "Execution %s: unable to record the abandoned execution event",
            execution_id,
        )


def scheduling_log_context(error: PublishFailedError) -> dict[str, Any]:
    """Contexte de log sans contenu de document ni charge utile publiée."""
    return {"stream": error.stream_name, "reason": error.reason}


async def compensate_s3_upload(
    s3_key: str | None,
    execution_id: str,
) -> None:
    """Supprime immédiatement un fichier S3 téléversé en cas d'échec de programmation.

    Ne lève jamais d'exception pour ne pas masquer l'erreur métier d'origine.
    Ne logue jamais la clé S3 brute pour respecter la règle de sécurité absolue.
    """
    if not s3_key:
        return
    try:
        deleted = await asyncio.shield(s3_service.delete_file(s3_key))
        if deleted:
            logger.info(
                "Compensated orphaned S3 upload for execution %s",
                execution_id,
            )
        else:
            logger.warning(
                "Failed to compensate S3 upload for execution %s (delete_file returned False)",
                execution_id,
            )
    except Exception as e:
        logger.warning(
            "Failed to compensate S3 upload for execution %s (%s)",
            execution_id,
            type(e).__name__,
        )


@dataclass
class S3CompensationGuard:
    """Tracks whether an uploaded S3 object may be safely deleted on abort.

    Armed immediately after S3 upload so pre-publish failures or cancellations
    still clean up orphaned objects. Disarmed before entering Redis publication
    so an `asyncio.CancelledError` or unresolved publication outcome never
    deletes a document whose message may already be queued in Redis.
    """

    execution_id: str
    s3_key: str | None = None
    should_compensate: bool = False

    def arm(self, s3_key: str) -> None:
        self.s3_key = s3_key
        self.should_compensate = True

    def disarm(self) -> None:
        self.should_compensate = False

    async def compensate_if_needed(self) -> None:
        if self.should_compensate and self.s3_key is not None:
            await compensate_s3_upload(self.s3_key, self.execution_id)


async def schedule_workflow_execution(
    db_session: AsyncSession,
    *,
    redis_producer: Any,
    execution_id: str,
    workflow_id: str,
    organization_id: UUID,
    workflow_version: int = 1,
    before_publish: Callable[[Any, Any], Awaitable[str]] | None = None,
    compensation_guard: S3CompensationGuard | None = None,
) -> None:
    """Publish a workflow execution message and reconcile failure states.

    Re-arms `compensation_guard` only when non-publication is confirmed
    (`ReservationLostError`, `IdempotencyReservationLostException`,
    `PublishFailedError`, or an unexpected non-cancellation exception). When
    interrupted by `asyncio.CancelledError` or `PublicationOutcomeUnknownError`,
    the guard remains disarmed so an already-queued execution never loses its
    S3 input file.
    """
    execution_message = WorkflowExecutionMessage(workflow_execution_id=execution_id)
    if compensation_guard is not None:
        compensation_guard.disarm()

    try:
        if before_publish is not None:
            await before_publish(redis_producer, execution_message)
        else:
            await redis_producer.publish_message(execution_message)
    except ReservationLostError, IdempotencyReservationLostException:
        if compensation_guard is not None:
            compensation_guard.should_compensate = True
        await fail_abandoned_execution(
            db_session,
            execution_id=execution_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            error_type=EXECUTION_RESERVATION_LOST_ERROR,
            error_message=(
                "The idempotency reservation was lost before this "
                "execution was scheduled; nothing is pending. Retry the "
                "request."
            ),
            workflow_version=workflow_version,
        )
        raise IdempotencyReservationLostException(execution_id=execution_id)
    except PublicationOutcomeUnknownError as unknown_error:
        # Only reached when the reply was lost AND the message is confirmed on
        # the stream (or unresolved): the reservation reconciles the outcome
        # before returning. Recording a failure here would invite a retry that
        # publishes the same work twice.
        logger.warning(
            "Execution %s: publication reply lost (%s) but the message "
            "is queued; accepted",
            execution_id,
            unknown_error.reason,
        )
    except PublishFailedError as publish_error:
        if compensation_guard is not None:
            compensation_guard.should_compensate = True
        logger.error(
            "Workflow execution %s: scheduling failed",
            execution_id,
            extra=scheduling_log_context(publish_error),
        )
        await fail_unscheduled_execution(
            db_session,
            execution_id=execution_id,
            workflow_id=workflow_id,
            organization_id=organization_id,
            error=publish_error,
            workflow_version=workflow_version,
        )
        raise scheduling_unavailable(execution_id) from publish_error
    except asyncio.CancelledError:
        logger.warning(
            "Execution %s: scheduling cancelled during publication; "
            "preserving S3 object because queue state is unconfirmed",
            execution_id,
        )
        raise
    except Exception:
        if compensation_guard is not None:
            compensation_guard.should_compensate = True
        raise
