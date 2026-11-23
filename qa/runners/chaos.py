"""Pluggable chaos providers (none, manual, docker, scalingo) without shell=True."""

from __future__ import annotations

import asyncio
import re
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

import httpx

from qa.client import DocumentIAClient
from qa.fixtures import ResolvedFixture
from qa.models import ChaosEvent, ChaosMode, utc_now_iso

_SAFE_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_PROD_APP_NAME_RE = re.compile(r"(^|[-_.])prod(uction)?($|[-_.])", re.IGNORECASE)

DEFAULT_CHAOS_CMD_TIMEOUT_S = 60.0

CmdRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class ChaosConfigError(ValueError):
    """Raised when chaos parameters are invalid or unsafe."""


def validate_safe_identifier(value: str, field_name: str) -> str:
    """Validate container/app identifier to prevent argument injection."""
    if not value or not _SAFE_IDENTIFIER_RE.fullmatch(value):
        raise ChaosConfigError(
            f"Invalid {field_name} {value!r}: must match \\A[A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}\\Z."
        )
    return value


def _default_cmd_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        shell=False,
        check=True,
        capture_output=True,
        text=True,
        timeout=DEFAULT_CHAOS_CMD_TIMEOUT_S,
    )


class ChaosProvider(ABC):
    """Abstract fault-injection provider invoked during a load run."""

    mode: ChaosMode

    @abstractmethod
    async def trigger(
        self,
        *,
        client: DocumentIAClient | None = None,
        probe_execution_id: str | None = None,
        probe_workflow_id: str | None = None,
        probe_fixture: ResolvedFixture | None = None,
    ) -> ChaosEvent:
        """Inject fault, wait for recovery, and return an audit ChaosEvent."""


class NoopChaosProvider(ChaosProvider):
    """No-op provider when chaos mode is disabled."""

    mode: ChaosMode = "none"

    async def trigger(
        self,
        *,
        client: DocumentIAClient | None = None,
        probe_execution_id: str | None = None,
        probe_workflow_id: str | None = None,
        probe_fixture: ResolvedFixture | None = None,
    ) -> ChaosEvent:
        now = utc_now_iso()
        return ChaosEvent(
            mode="none",
            triggered_at=now,
            recovered_at=now,
            downtime_s=0.0,
            details="No chaos injected.",
        )


class ManualPauseChaosProvider(ChaosProvider):
    """Pause and prompt operator to restart/stop target manually."""

    mode: ChaosMode = "manual"

    def __init__(
        self,
        *,
        target: str | None = None,
        prompt_fn: Callable[[str], str] = input,
    ) -> None:
        self.target = target or "manual-target"
        self._prompt_fn = prompt_fn

    async def trigger(
        self,
        *,
        client: DocumentIAClient | None = None,
        probe_execution_id: str | None = None,
        probe_workflow_id: str | None = None,
        probe_fixture: ResolvedFixture | None = None,
    ) -> ChaosEvent:
        triggered_at = utc_now_iso()
        t0 = time.perf_counter()
        prompt = (
            f"\n[CHAOS MANUAL] Inject fault on '{self.target}' now "
            "(e.g. restart worker/API/Redis), then press Enter to continue: "
        )
        await asyncio.to_thread(self._prompt_fn, prompt)
        downtime = time.perf_counter() - t0
        return ChaosEvent(
            mode="manual",
            target=self.target,
            triggered_at=triggered_at,
            recovered_at=utc_now_iso(),
            downtime_s=round(downtime, 3),
            details="Operator confirmed manual chaos injection and recovery.",
        )


class DockerChaosProvider(ChaosProvider):
    """Stop/start or restart a local Docker container using safe subprocess args."""

    mode: ChaosMode = "docker"

    def __init__(
        self,
        *,
        target: str,
        downtime_s: float = 5.0,
        cmd_runner: CmdRunner = _default_cmd_runner,
    ) -> None:
        self.target = validate_safe_identifier(target, "chaos_target")
        self.downtime_s = max(0.0, downtime_s)
        self._cmd_runner = cmd_runner

    async def trigger(
        self,
        *,
        client: DocumentIAClient | None = None,
        probe_execution_id: str | None = None,
        probe_workflow_id: str | None = None,
        probe_fixture: ResolvedFixture | None = None,
    ) -> ChaosEvent:
        triggered_at = utc_now_iso()
        t0 = time.perf_counter()
        redis_read_ok: bool | None = None
        redis_write_503: bool | None = None

        if self.downtime_s <= 0:
            await asyncio.to_thread(
                self._cmd_runner, ["docker", "restart", self.target]
            )
            details = f"Executed docker restart {self.target}"
        else:
            await asyncio.to_thread(self._cmd_runner, ["docker", "stop", self.target])
            try:
                # If targeting Redis, optionally verify #149 fail-closed write + readable GET
                if "redis" in self.target.lower() and client is not None:
                    await asyncio.sleep(min(1.0, self.downtime_s / 2.0))
                    if probe_execution_id:
                        try:
                            get_resp = await client.get_execution(probe_execution_id)
                            redis_read_ok = get_resp.status_code == 200
                        except httpx.HTTPError:
                            redis_read_ok = False
                    if probe_workflow_id and probe_fixture is not None:
                        try:
                            post_resp = await client.execute_workflow_v2(
                                probe_workflow_id,
                                fixture=probe_fixture,
                            )
                            redis_write_503 = post_resp.status_code == 503
                        except httpx.HTTPError:
                            redis_write_503 = False

                remaining = self.downtime_s - (time.perf_counter() - t0)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            finally:
                await asyncio.to_thread(
                    self._cmd_runner, ["docker", "start", self.target]
                )
            details = (
                f"Executed docker stop/start on {self.target} "
                f"(downtime={self.downtime_s:.1f}s)"
            )

        downtime = time.perf_counter() - t0
        return ChaosEvent(
            mode="docker",
            target=self.target,
            triggered_at=triggered_at,
            recovered_at=utc_now_iso(),
            downtime_s=round(downtime, 3),
            details=details,
            redis_read_during_outage_ok=redis_read_ok,
            redis_write_fail_closed_503=redis_write_503,
        )


class ScalingoChaosProvider(ChaosProvider):
    """Restart a Scalingo container type via `scalingo --app <app> restart <target>`."""

    mode: ChaosMode = "scalingo"

    def __init__(
        self,
        *,
        scalingo_app: str,
        target: str = "worker",
        cmd_runner: CmdRunner = _default_cmd_runner,
    ) -> None:
        self.scalingo_app = validate_safe_identifier(scalingo_app, "scalingo_app")
        if _PROD_APP_NAME_RE.search(self.scalingo_app):
            raise ChaosConfigError(
                f"Refusing to run Scalingo chaos against production-named app '{self.scalingo_app}'."
            )
        self.target = validate_safe_identifier(target, "chaos_target")
        self._cmd_runner = cmd_runner

    async def trigger(
        self,
        *,
        client: DocumentIAClient | None = None,
        probe_execution_id: str | None = None,
        probe_workflow_id: str | None = None,
        probe_fixture: ResolvedFixture | None = None,
    ) -> ChaosEvent:
        triggered_at = utc_now_iso()
        t0 = time.perf_counter()
        cmd = [
            "scalingo",
            "--app",
            self.scalingo_app,
            "restart",
            self.target,
        ]
        await asyncio.to_thread(self._cmd_runner, cmd)
        downtime = time.perf_counter() - t0
        return ChaosEvent(
            mode="scalingo",
            target=f"{self.scalingo_app}:{self.target}",
            triggered_at=triggered_at,
            recovered_at=utc_now_iso(),
            downtime_s=round(downtime, 3),
            details=f"Executed {' '.join(cmd)}",
        )


def build_chaos_provider(
    *,
    mode: ChaosMode,
    target: str | None = None,
    scalingo_app: str | None = None,
    downtime_s: float = 5.0,
) -> ChaosProvider:
    """Factory creating the appropriate ChaosProvider from CLI options."""
    if mode == "none":
        return NoopChaosProvider()
    if mode == "manual":
        return ManualPauseChaosProvider(target=target)
    if mode == "docker":
        if not target:
            raise ChaosConfigError(
                "--chaos-target (Docker container name) is required when --chaos-mode docker."
            )
        return DockerChaosProvider(target=target, downtime_s=downtime_s)
    if mode == "scalingo":
        if not scalingo_app:
            raise ChaosConfigError(
                "--scalingo-app is required when --chaos-mode scalingo."
            )
        return ScalingoChaosProvider(
            scalingo_app=scalingo_app,
            target=target or "worker",
        )
    raise ChaosConfigError(f"Unsupported chaos mode: {mode}")
