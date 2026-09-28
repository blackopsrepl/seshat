from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import outputs, recording, server, streams, tts


EPOCH_MONOTONIC = 100.0


def single_output(name: str = "DP-1", width: int = 1920, height: int = 1080) -> list[dict]:
    return [
        {
            "name": name,
            "active": True,
            "rect": {"x": 0, "y": 0, "width": width, "height": height},
        }
    ]


def event(t_ms: float, tool: str = "click", source: str = "seshat") -> dict:
    """One ingested event ``t_ms`` into a take that started at ``EPOCH_MONOTONIC``."""
    return {
        "at_monotonic": EPOCH_MONOTONIC + t_ms / 1000.0,
        "tool": tool,
        "ok": True,
        "payload": {},
        "source": source,
    }


def stream_path(runtime_dir: str, source: str) -> Path:
    return Path(runtime_dir) / "seshat" / streams.STREAM_DIRECTORY_NAME / f"{source}.jsonl"


def publish(runtime_dir: str, source: str, times_ms: list[float], tool: str = "click") -> Path:
    """Publish a stream exactly the way an emitting tool server would."""
    path = stream_path(runtime_dir, source)
    for t_ms in times_ms:
        streams.append_event(
            source,
            tool,
            at_monotonic=EPOCH_MONOTONIC + t_ms / 1000.0,
            path=path,
        )
    return path


def completed_job(
    tmpdir: str,
    fmt: str = "webm",
    capture_seconds: float = 10.0,
    events: list[dict] | None = None,
) -> server.RecordingJob:
    duration = 15 if fmt == "gif" else 30
    with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
        with patch.object(outputs, "get_outputs", return_value=single_output()):
            job = server.new_recording_job(
                {"format": fmt, "max_duration_seconds": duration}
            )
    os.close(job.log_fd)
    job.started_monotonic = EPOCH_MONOTONIC
    job.ended_monotonic = EPOCH_MONOTONIC + capture_seconds
    job.events = list(events or [])
    job.artifact.write_bytes(b"artifact")
    job.phase = "completed"
    return job


def clip(index_path: str, duration_ms: float, lead_ms: float = 0.0, words: int = 0) -> server.TtsClip:
    return server.TtsClip(
        path=server.Path(index_path),
        duration_ms=duration_ms,
        lead_silence_ms=lead_ms,
        words=[{"text": "w", "start_ms": 0, "end_ms": 1}] * words,
    )


class FakeProcess:
    def __init__(self, returncode: int = 0, already_exited: bool = False) -> None:
        self.pid = 424242
        self._final_returncode = returncode
        self.returncode = returncode if already_exited else None
        self.popen_kwargs: dict = {}
        self.argv: list[str] = []
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def receive_signal(self, sig: int) -> None:
        self.signals.append(sig)
        self.returncode = 0


def exit_process_on_signal(process: FakeProcess):
    def fake_killpg(pid: int, sig: int) -> None:
        process.receive_signal(sig)

    return patch.object(server.os, "killpg", side_effect=fake_killpg)
