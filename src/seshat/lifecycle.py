from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Literal, Sequence

from . import core


RECORDING_STOP_TIMEOUT_SECONDS = 10.0
RECORDING_TERM_TIMEOUT_SECONDS = 5.0
RECORDING_KILL_TIMEOUT_SECONDS = 5.0
RECORDING_FINAL_WAIT_SECONDS = 10.0

TerminationStage = Literal["natural", "sigint", "sigterm", "sigkill"]
SignalTimeouts = Sequence[tuple[int, float]]


@dataclass(frozen=True)
class TerminationOutcome:
    stage: TerminationStage
    returncode: int
    process_exited_monotonic: float

    @property
    def graceful(self) -> bool:
        return self.stage != "sigkill" and self.returncode == 0


def shutdown_latency_seconds(
    stop_requested_monotonic: float | None, process_exited_monotonic: float | None
) -> float | None:
    if stop_requested_monotonic is None or process_exited_monotonic is None:
        return None
    return round(max(process_exited_monotonic - stop_requested_monotonic, 0.0), 3)


def _stage_for_signal(sig: int) -> TerminationStage:
    if sig == signal.SIGINT:
        return "sigint"
    if sig == signal.SIGTERM:
        return "sigterm"
    if sig == signal.SIGKILL:
        return "sigkill"
    raise ValueError(f"unsupported recorder termination signal: {sig}")


def _outcome(
    process: subprocess.Popen,
    stage: TerminationStage,
    monotonic: Callable[[], float],
) -> TerminationOutcome:
    returncode = process.poll()
    if returncode is None:
        raise core.ToolError("wf-recorder did not report an exit code after termination")
    return TerminationOutcome(stage, int(returncode), monotonic())


def _wait_exited(
    process: subprocess.Popen,
    timeout: float,
    *,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if process.poll() is not None:
            return True
        sleep(min(0.05, max(deadline - monotonic(), 0.0)))
    return process.poll() is not None


def terminate_process_group(
    process: subprocess.Popen,
    *,
    timeouts: SignalTimeouts | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TerminationOutcome:
    """Stop one recorder process group and report how it actually exited."""
    if process.poll() is not None:
        return _outcome(process, "natural", monotonic)

    escalations = timeouts or (
        (signal.SIGINT, RECORDING_STOP_TIMEOUT_SECONDS),
        (signal.SIGTERM, RECORDING_TERM_TIMEOUT_SECONDS),
        (signal.SIGKILL, RECORDING_KILL_TIMEOUT_SECONDS),
    )
    for sig, timeout in escalations:
        if process.poll() is not None:
            return _outcome(process, "natural", monotonic)
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            if process.poll() is not None:
                return _outcome(process, "natural", monotonic)
        stage = _stage_for_signal(sig)
        if _wait_exited(process, timeout, monotonic=monotonic, sleep=sleep):
            return _outcome(process, stage, monotonic)

    try:
        process.wait(timeout=RECORDING_FINAL_WAIT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise core.ToolError("wf-recorder did not exit after SIGKILL") from exc
    return _outcome(process, "sigkill", monotonic)
