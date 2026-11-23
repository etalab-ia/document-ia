"""Unit tests for TwoDDocCrlRefresher and 2D-Doc CRL refresh settings (100% offline)."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from fr_2ddoc_parser.crypto.key_resolver import CrlRefreshResult
from pydantic import ValidationError

from document_ia_worker.core import two_ddoc_crl_refresher as refresher_module
from document_ia_worker.core.barcode_settings import BarcodeSettings
from document_ia_worker.core.two_ddoc_crl_refresher import TwoDDocCrlRefresher


def test_barcode_settings_crl_refresh_defaults_and_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TWO_D_DOC_CRL_REFRESH_ENABLED", raising=False)
    monkeypatch.delenv("TWO_D_DOC_CRL_REFRESH_INTERVAL_HOURS", raising=False)
    monkeypatch.delenv("TWO_D_DOC_CRL_REFRESH_TIMEOUT_SECONDS", raising=False)

    settings = BarcodeSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.TWO_D_DOC_CRL_REFRESH_ENABLED is True
    assert settings.TWO_D_DOC_CRL_REFRESH_INTERVAL_HOURS == 3.0
    assert settings.TWO_D_DOC_CRL_REFRESH_TIMEOUT_SECONDS == 10

    with pytest.raises(ValidationError):
        BarcodeSettings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            TWO_D_DOC_CRL_REFRESH_INTERVAL_HOURS=0.0,
        )

    with pytest.raises(ValidationError):
        BarcodeSettings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            TWO_D_DOC_CRL_REFRESH_INTERVAL_HOURS=-1.5,
        )

    with pytest.raises(ValidationError):
        BarcodeSettings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            TWO_D_DOC_CRL_REFRESH_TIMEOUT_SECONDS=0,
        )

    with pytest.raises(ValidationError):
        BarcodeSettings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            TWO_D_DOC_CRL_REFRESH_TIMEOUT_SECONDS=-5,
        )


def test_refresher_interval_seconds_and_constructor_validation() -> None:
    refresher = TwoDDocCrlRefresher(enabled=True, interval_hours=2.5, timeout_seconds=8)
    assert refresher.interval_seconds == pytest.approx(9000.0)

    with pytest.raises(ValueError, match="interval_hours"):
        TwoDDocCrlRefresher(interval_hours=0.0)

    with pytest.raises(ValueError, match="timeout_seconds"):
        TwoDDocCrlRefresher(timeout_seconds=0)


def test_refresh_once_calls_refresh_default_crls_with_only_expired_false_and_logs_info(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = CrlRefreshResult(
        refreshed_cas=("FR02", "FR03"),
        skipped_fresh_cas=(),
        failed_cas={},
        expired_cas=(),
    )
    mock_refresh = MagicMock(return_value=expected)
    monkeypatch.setattr(refresher_module, "refresh_default_crls", mock_refresh)

    refresher = TwoDDocCrlRefresher(
        enabled=True,
        interval_hours=3.0,
        timeout_seconds=12,
    )

    with caplog.at_level(logging.INFO, logger=refresher_module.__name__):
        result = refresher.refresh_once()

    assert result == expected
    mock_refresh.assert_called_once_with(timeout=12, only_expired=False)
    assert "refreshed_cas=('FR02', 'FR03')" in caplog.text
    assert "skipped_fresh_cas=()" in caplog.text
    assert "failed_cas={}" in caplog.text
    assert "expired_cas=()" in caplog.text
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.parametrize(
    "refresh_result",
    [
        CrlRefreshResult(
            refreshed_cas=("FR02",),
            skipped_fresh_cas=(),
            failed_cas={"FR01": "fetch_failed"},
            expired_cas=(),
        ),
        CrlRefreshResult(
            refreshed_cas=("FR02",),
            skipped_fresh_cas=(),
            failed_cas={},
            expired_cas=("FR01",),
        ),
        CrlRefreshResult(
            refreshed_cas=(),
            skipped_fresh_cas=(),
            failed_cas={"FR01": "fetch_failed"},
            expired_cas=("FR01", "FR06"),
        ),
    ],
)
def test_refresh_once_warns_when_failed_or_expired_cas_present(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    refresh_result: CrlRefreshResult,
) -> None:
    monkeypatch.setattr(
        refresher_module,
        "refresh_default_crls",
        MagicMock(return_value=refresh_result),
    )

    refresher = TwoDDocCrlRefresher(enabled=True, interval_hours=3.0, timeout_seconds=5)
    with caplog.at_level(logging.WARNING, logger=refresher_module.__name__):
        result = refresher.refresh_once()

    assert result == refresh_result
    warning_messages = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert len(warning_messages) == 1
    assert "failed or expired CRLs" in warning_messages[0]


def test_refresh_once_disabled_skips_network_call(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_refresh = MagicMock(
        side_effect=AssertionError("refresh_default_crls must not be called")
    )
    monkeypatch.setattr(refresher_module, "refresh_default_crls", mock_refresh)

    refresher = TwoDDocCrlRefresher(enabled=False)
    with caplog.at_level(logging.INFO, logger=refresher_module.__name__):
        result = refresher.refresh_once()

    assert result is None
    mock_refresh.assert_not_called()
    assert "disabled" in caplog.text


def test_refresh_once_catches_unexpected_exception_without_leaking_error_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_detail = "http://user:super-secret-token@internal.example/path/pii-123"
    monkeypatch.setattr(
        refresher_module,
        "refresh_default_crls",
        MagicMock(side_effect=RuntimeError(secret_detail)),
    )

    refresher = TwoDDocCrlRefresher(enabled=True)
    with caplog.at_level(logging.WARNING, logger=refresher_module.__name__):
        result = refresher.refresh_once()

    assert result is None
    assert "RuntimeError" in caplog.text
    assert secret_detail not in caplog.text
    assert "super-secret-token" not in caplog.text


@pytest.mark.asyncio
async def test_startup_refresh_runs_asynchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = CrlRefreshResult(refreshed_cas=("FR02",))
    mock_refresh = MagicMock(return_value=expected)
    monkeypatch.setattr(refresher_module, "refresh_default_crls", mock_refresh)

    refresher = TwoDDocCrlRefresher(enabled=True, timeout_seconds=7)
    result = await refresher.startup_refresh()

    assert result == expected
    mock_refresh.assert_called_once_with(timeout=7, only_expired=False)


@pytest.mark.asyncio
async def test_run_periodic_refresh_executes_multiple_cycles_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0
    two_cycles_reached = asyncio.Event()
    loop = asyncio.get_running_loop()

    def fake_refresh(*, timeout: int, only_expired: bool) -> CrlRefreshResult:
        nonlocal call_count
        assert timeout == 6
        assert only_expired is False
        call_count += 1
        if call_count >= 2:
            loop.call_soon_threadsafe(two_cycles_reached.set)
        return CrlRefreshResult(refreshed_cas=("FR02",))

    monkeypatch.setattr(refresher_module, "refresh_default_crls", fake_refresh)

    stop_event = asyncio.Event()
    # 0.015s interval expressed in hours
    refresher = TwoDDocCrlRefresher(
        enabled=True,
        interval_hours=0.015 / 3600.0,
        timeout_seconds=6,
    )

    task = asyncio.create_task(refresher.run_periodic_refresh(stop_event))
    await asyncio.wait_for(two_cycles_reached.wait(), timeout=2.0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert call_count >= 2


@pytest.mark.asyncio
async def test_run_periodic_refresh_returns_immediately_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_refresh = MagicMock(
        side_effect=AssertionError("refresh_default_crls must not be called")
    )
    monkeypatch.setattr(refresher_module, "refresh_default_crls", mock_refresh)

    stop_event = asyncio.Event()
    refresher = TwoDDocCrlRefresher(enabled=False, interval_hours=1.0)
    await asyncio.wait_for(refresher.run_periodic_refresh(stop_event), timeout=1.0)
    mock_refresh.assert_not_called()
