import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Iterable
from types import FrameType

from document_ia_infra.redis.consumer import (
    ThreadConsumer,
    build_instance_consumer_name,
)
from document_ia_infra.redis.model.workflow_execution_message import (
    WorkflowExecutionMessage,
)
from document_ia_infra.redis.redis_manager import RedisManager
from document_ia_infra.redis.redis_settings import redis_settings
from document_ia_worker.config.logging import setup_logging
from document_ia_worker.core.temp_file_sweeper import TempFileSweeper
from document_ia_worker.core.two_ddoc_crl_refresher import TwoDDocCrlRefresher
from document_ia_worker.workflow.message_handler import process_message
from document_ia_worker.workflow.terminal_failure import (
    notify_watchdog_terminal_failure,
)

setup_logging()
logger = logging.getLogger(__name__)

# Configure CPU thread limits for C libraries to avoid RAM bloating in Scalingo containers
try:
    import torch

    torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "1")))  # pyright: ignore[reportPrivateImportUsage]
    if hasattr(torch, "set_num_interop_threads"):
        torch.set_num_interop_threads(1)  # pyright: ignore[reportPrivateImportUsage]
except Exception:
    pass

try:
    import cv2

    cv2.setNumThreads(int(os.environ.get("OPENCV_FOR_THREADS_NUM", "1")))
except Exception:
    pass


CONSUMER_SHUTDOWN_TIMEOUT_S: float = redis_settings.WORKER_SHUTDOWN_TIMEOUT_S
CANCELLATION_CLEANUP_TIMEOUT_S = 3


def _force_exit() -> None:
    logger.critical("Forcing worker exit; unfinished messages remain pending")
    os._exit(1)


# Instance globale pour permettre l'arrêt gracieux lors d'un signal système
_consumer: ThreadConsumer[WorkflowExecutionMessage] | None = None


def _prewarm_models_and_trust_store() -> None:
    logger.info(
        "[boot] Verifying and pre-warming AI/OCR models and 2D-Doc PKI trust store (Tesseract, YOLO-World, CLIP, QRDet, 2D-Doc TSL)..."
    )
    try:
        from document_ia_worker.core.model_artifacts import (
            resolve_and_verify_tesseract_lang_data,
        )

        tess_path = resolve_and_verify_tesseract_lang_data("fra")
        logger.info("[boot]   ✅ Tesseract French model verified at %s", tess_path)
    except Exception as e:
        logger.critical("[boot]   ❌ Could not verify Tesseract French model: %s", e)
        raise

    try:
        from document_ia_worker.core.model_artifacts import (
            resolve_and_verify_clip_path,
        )
        from document_ia_worker.core.preprocessing.yoloworld_crop import (
            ensure_yoloworld_classes,
            get_yoloworld_model,
        )

        clip_path = resolve_and_verify_clip_path()
        logger.info("[boot]   ✅ CLIP ViT-B/32 weights verified at %s", clip_path)
        yolo_model = get_yoloworld_model()
        if hasattr(yolo_model, "set_classes"):
            ensure_yoloworld_classes(yolo_model, "book")
        logger.info(
            "[boot]   ✅ YOLO-World & CLIP models verified and pre-loaded into RAM"
        )
    except Exception as e:
        logger.critical("[boot]   ❌ Could not pre-load YOLO-World/CLIP model: %s", e)
        raise

    try:
        from document_ia_worker.workflow.step.extract_barcode_data.extract_barcode_2ddoc_data import (
            _get_qrdet_detector,  # pyright: ignore[reportPrivateUsage]
        )

        _get_qrdet_detector()
        logger.info("[boot]   ✅ QRDet model verified and pre-loaded into RAM")
    except Exception as e:
        logger.critical("[boot]   ❌ Could not pre-load QRDet model: %s", e)
        raise

    try:
        from fr_2ddoc_parser.crypto.key_resolver import get_default_key_resolver
        from fr_2ddoc_parser.model.models import TrustListFreshness

        resolver = get_default_key_resolver()
        tsl_freshness = resolver.get_tsl_freshness()
        expired_crl_cas = resolver.get_expired_crl_cas()
        logger.info(
            "[boot]   ✅ 2D-Doc PKI trust store verified and pre-loaded into RAM "
            "(tsl_sequence_number=%s, tsl_next_update=%s, tsl_freshness=%s)",
            resolver.tsl_sequence_number,
            resolver.tsl_next_update,
            tsl_freshness.value,
        )
        if tsl_freshness != TrustListFreshness.FRESH:
            logger.warning(
                "[boot]   ⚠️ 2D-Doc PKI trust list is not fresh "
                "(tsl_sequence_number=%s, tsl_next_update=%s, tsl_freshness=%s)",
                resolver.tsl_sequence_number,
                resolver.tsl_next_update,
                tsl_freshness.value,
            )
        if expired_crl_cas:
            logger.warning(
                "[boot]   ⚠️ Bundled 2D-Doc CRLs are expired at pre-warm for CAs %s "
                "(will be refreshed in memory at startup if CRL refresh is enabled)",
                expired_crl_cas,
            )
    except Exception as e:
        logger.critical("[boot]   ❌ Could not pre-load 2D-Doc PKI trust store: %s", e)
        raise


async def run_consumer(stop_event: asyncio.Event | None = None) -> None:
    global _consumer
    logger.info("--- Starting document-ia-worker ---")
    _prewarm_models_and_trust_store()

    crl_refresher = TwoDDocCrlRefresher()
    await crl_refresher.startup_refresh()
    crl_stop_event = stop_event if stop_event is not None else asyncio.Event()
    crl_refresh_task: asyncio.Task[None] | None = None

    logger.info("Register redis stream consumer for workflow execution")

    # This runtime owns the main-loop Redis pool; the consumer borrows it.
    redis_manager = RedisManager()
    _consumer = ThreadConsumer[WorkflowExecutionMessage](
        consumer_name=build_instance_consumer_name("document-ia-worker"),
        consumer_group=redis_settings.EVENT_CONSUMER_GROUP,
        stream_name=redis_settings.EVENT_STREAM_NAME,
        batch_size=10,
        block_time=10000,
        message_class=WorkflowExecutionMessage,
        process_message_callable=process_message,
        max_retry_number=redis_settings.WORKFLOW_MAX_RETRY_NUMBER,
        reclaim_idle_ms=redis_settings.EVENT_STREAM_RECLAIM_IDLE_MS,
        reclaim_poll_interval_s=5.0,
        drain_timeout_s=redis_settings.WORKER_DRAIN_TIMEOUT_S,
        current_redis_manager=redis_manager,
        max_processing_time_s=120.0,
        step_progress_timeout_s=90.0,
        use_execution_lease=True,
        requeue_on_shutdown=True,
        on_terminal_failure=notify_watchdog_terminal_failure,
        retry_backoff_delays_s=redis_settings.WORKFLOW_RETRY_BACKOFF_DELAYS_S,
        delayed_retry_poll_interval_s=redis_settings.RETRY_DELAYED_POLL_INTERVAL_S,
    )
    if stop_event is not None and stop_event.is_set():
        _consumer.stop_flag.set()

    try:
        crl_refresh_task = asyncio.create_task(
            crl_refresher.run_periodic_refresh(crl_stop_event)
        )
        await _consumer.start_consumer()
    finally:
        if stop_event is None:
            crl_stop_event.set()
        if crl_refresh_task is not None and not crl_refresh_task.done():
            crl_refresh_task.cancel()
        if crl_refresh_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await crl_refresh_task
        try:
            await asyncio.wait_for(redis_manager.close(), timeout=5)
        except Exception as exc:
            logger.warning("Failed to close worker Redis resources: %s", exc)


def _register_signal_handlers(
    stop_event: asyncio.Event,
    sigs: Iterable[int] = (signal.SIGINT, signal.SIGTERM),
) -> list[int]:
    loop = asyncio.get_running_loop()
    registered_loop_sigs: list[int] = []

    def _fallback_handler(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for s in sigs:
        try:
            try:
                loop.add_signal_handler(s, stop_event.set)
                registered_loop_sigs.append(s)
            except NotImplementedError:
                signal.signal(s, _fallback_handler)
        except ValueError, RuntimeError:
            logger.warning(
                "Impossible d'enregistrer le handler pour le signal %s (environnement non supporté)",
                s,
            )
    return registered_loop_sigs


async def main() -> None:
    global _consumer
    stop_event = asyncio.Event()
    registered_sigs = _register_signal_handlers(stop_event)

    # Startup cleanup and periodic background sweeper for temporary files
    sweeper_redis_manager = RedisManager()
    sweeper_task: asyncio.Task[None] | None = None
    consumer_task: asyncio.Task[None] | None = None
    stop_wait: asyncio.Task[bool] | None = None

    try:
        sweeper = TempFileSweeper(redis_manager=sweeper_redis_manager)
        await sweeper.startup_cleanup()
        sweeper_task = asyncio.create_task(sweeper.run_periodic_sweep(stop_event))

        consumer_task = asyncio.create_task(run_consumer(stop_event))
        stop_wait = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            {consumer_task, stop_wait}, return_when=asyncio.FIRST_COMPLETED
        )

        if consumer_task in done:
            stop_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_wait
            try:
                await consumer_task
            except Exception:
                logger.error("❌ Consumer crashed — exiting with error")
                if _consumer is not None and _consumer.has_running_handlers:
                    _force_exit()
                raise
            if stop_event.is_set() and _consumer is not None:
                if _consumer.has_running_handlers:
                    _force_exit()
            return

        # Un signal d'arrêt (SIGINT / SIGTERM) a été reçu : arrêt gracieux
        logger.info("Shutdown requested; stopping consumer")
        if _consumer is not None:
            _consumer.stop_flag.set()
        done, _ = await asyncio.wait(
            {consumer_task}, timeout=CONSUMER_SHUTDOWN_TIMEOUT_S
        )
        if consumer_task in done:
            await consumer_task
            if _consumer is not None and _consumer.has_running_handlers:
                _force_exit()
            return

        logger.warning("Consumer shutdown timed out; cancelling remaining tasks")
        consumer_task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.shield(consumer_task), timeout=CANCELLATION_CLEANUP_TIMEOUT_S
            )
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            logger.warning(
                "Consumer cancellation timed out; unfinished messages remain pending"
            )
        _force_exit()
    finally:
        loop = asyncio.get_running_loop()
        for s in registered_sigs or ():
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(s)
        if stop_wait is not None and not stop_wait.done():
            stop_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_wait
        if sweeper_task is not None and not sweeper_task.done():
            sweeper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper_task
        try:
            await asyncio.wait_for(sweeper_redis_manager.close(), timeout=2.0)
        except Exception as exc:
            logger.warning("Failed to close sweeper Redis resources: %s", exc)
        _consumer = None


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
