"""How a take is reported: one document per phase, built from the job alone.

Split out of ``manager`` because it answers a different question. The manager
owns when a take is captured and stopped; this module owns what an operator can
see of it. The manager hands over the job and the one fact only it knows — the
intermediate's current size — and gets back the tool's answer.

Two rules live here:

- A take is reported through separate lifecycle facts, never one collapsed
  duration: the capture window, how long the recorder took to exit, and the
  playable picture. Conflating them is what made a 210-second deadline look like
  a 225-second recording.
- A failure names every path that survived it, including a corrupt candidate,
  because an artifact nobody can find is an artifact nobody can diagnose.
"""

from __future__ import annotations

import time
from typing import Any

from . import lifecycle, recording


def phase_summary(job: recording.RecordingJob, *, intermediate_bytes: int) -> dict[str, Any]:
    """The tool's answer for ``job``, for whatever phase it is in."""
    base = {
        "id": job.id,
        "format": job.fmt,
        "output": job.output,
        "region": job.region,
        "audio_included": bool(job.result and job.result.get("audio_included")),
        "cursor_included": True,
        "termination_stage": job.termination_stage,
        "recorder_returncode": job.recorder_returncode,
        "shutdown_latency_seconds": lifecycle.shutdown_latency_seconds(
            job.stop_requested_monotonic, job.process_exited_monotonic
        ),
    }
    if job.phase == "recording":
        return {
            **base,
            "phase": "recording",
            "width": job.width,
            "height": job.height,
            "elapsed_seconds": round(_now() - job.started_monotonic, 3),
            "max_duration_seconds": job.max_duration,
            "bytes": intermediate_bytes,
        }
    if job.phase == "stopping":
        return {**base, "phase": "stopping", "capture_elapsed_seconds": _capture_elapsed(job)}
    if job.phase == "processing":
        return {
            **base,
            "phase": "processing",
            "capture_elapsed_seconds": _capture_elapsed(job),
            "note": "finalizing artifact; poll recording_status until completed or failed",
        }
    if job.phase == "narrating":
        return {
            **base,
            "phase": "narrating",
            "capture_elapsed_seconds": _capture_elapsed(job),
            "note": "synthesizing and muxing narration; poll recording_status",
        }
    if job.phase == "completed":
        return dict(job.result or {"id": job.id, "phase": "completed"})
    candidate = recording.candidate_artifact_path(job)
    return {
        **base,
        "phase": "failed",
        "detail": job.detail or "recording failed",
        "intermediate_path": str(job.intermediate) if job.intermediate.exists() else None,
        "candidate_path": str(candidate) if candidate.exists() else None,
        "log_path": str(job.log_path) if job.log_path.exists() else None,
    }


def _now() -> float:
    return time.monotonic()


def _capture_elapsed(job: recording.RecordingJob) -> float:
    end = job.ended_monotonic if job.ended_monotonic is not None else _now()
    return round(max(end - job.started_monotonic, 0.0), 3)
