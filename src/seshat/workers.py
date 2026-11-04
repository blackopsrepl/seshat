"""Worker steps the manager runs on background threads.

Split out of ``manager`` because the file-length ceiling demanded it and these
two transitions are the long ones: finalization (re-encode, validate, publish,
ingest, sidecar) and narration (synthesize, schedule, build, mux, publish).
Both take the owning manager — for its state lock and event ingestion — and the
job they act on, and are started by the manager, never run standalone.

The job-file helpers here are plain functions on a job's own paths; callers
hold the manager lock where a phase transition is involved.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from . import core, lifecycle, narration, recording, timeline, tts

if TYPE_CHECKING:
    from .manager import RecordingManager


def intermediate_size(job: recording.RecordingJob) -> int:
    try:
        return job.intermediate.stat().st_size
    except OSError:
        return 0


def close_log_fd(job: recording.RecordingJob) -> None:
    if job.log_fd >= 0:
        try:
            os.close(job.log_fd)
        except OSError:
            pass
        job.log_fd = -1


def log_tail(job: recording.RecordingJob) -> str:
    try:
        data = job.log_path.read_bytes()
    except OSError:
        return ""
    return data[-recording.RECORDING_LOG_TAIL_BYTES:].decode("utf-8", errors="replace").strip()


def discard_intermediate(job: recording.RecordingJob) -> None:
    job.intermediate.unlink(missing_ok=True)


def fail(job: recording.RecordingJob, message: str) -> None:
    """Mark the take failed and drop its published artifact.

    Caller must hold the manager lock.
    """
    job.phase = "failed"
    job.detail = f"{job.detail}; {message}" if job.detail else message
    job.artifact.unlink(missing_ok=True)


def watchdog(manager: "RecordingManager", job: recording.RecordingJob) -> None:
    deadline = job.started_monotonic + job.max_duration
    while True:
        with manager._lock:
            if manager._job is not job or job.phase != "recording":
                return
            exceeded_time = time.monotonic() >= deadline
            exceeded_size = intermediate_size(job) > recording.RECORDING_MAX_INTERMEDIATE_BYTES
            if exceeded_time or exceeded_size:
                job.auto_stopped = True
                if exceeded_size and not exceeded_time:
                    job.detail = "recording stopped: intermediate size limit reached"
                manager._request_stop(job)
                return
        time.sleep(0.5)


def finalize(manager: "RecordingManager", job: recording.RecordingJob) -> None:
    try:
        summary = recording.finalize_recording(job)
        job.artifact.chmod(recording.RECORDING_FILE_MODE)
        with manager._lock:
            manager.ingest_events(job)
            timeline_path = timeline.write_timeline_sidecar(job)
    except core.ToolError as exc:
        with manager._lock:
            fail(job, f"finalization failed: {exc}")
        return
    except Exception as exc:
        core.eprint(f"unexpected finalization error for {job.id}: {exc}")
        with manager._lock:
            fail(job, "unexpected finalization failure; see server log")
        return
    capture_elapsed = max(
        (job.ended_monotonic or time.monotonic()) - job.started_monotonic, 0.0
    )
    job.media_duration_ms = round(
        float(summary.get("media_duration_seconds") or 0.0) * 1000.0, 3
    )
    events_beyond_media, latest_event_ms = timeline.media_extent(job)
    try:
        artifact_bytes = job.artifact.stat().st_size
    except OSError:
        artifact_bytes = 0
    shutdown_latency = lifecycle.shutdown_latency_seconds(
        job.stop_requested_monotonic, job.process_exited_monotonic
    )
    result = {
        "id": job.id,
        "phase": "completed",
        "format": job.fmt,
        "output": job.output,
        "region": job.region,
        "path": str(job.artifact),
        "bytes": artifact_bytes,
        "capture_elapsed_seconds": round(capture_elapsed, 3),
        "latest_event_ms": latest_event_ms,
        "events_beyond_media": events_beyond_media,
        "graceful": job.termination_stage != "sigkill" and job.recorder_returncode == 0,
        "termination_stage": job.termination_stage,
        "recorder_returncode": job.recorder_returncode,
        "shutdown_latency_seconds": shutdown_latency,
        "audio_included": recording.wants_audio(job.audio),
        "cursor_included": True,
        "auto_stopped": job.auto_stopped,
        "timeline_path": str(timeline_path) if timeline_path is not None else None,
        "timeline_event_count": len(job.events),
        **summary,
    }
    with manager._lock:
        job.result = result
        job.phase = "completed"
        discard_intermediate(job)
        job.log_path.unlink(missing_ok=True)


def narrate(
    manager: "RecordingManager",
    job: recording.RecordingJob,
    request: narration.NarrationRequest,
    engine: tts.TtsEngine,
) -> None:
    try:
        narration_meta = narration.perform_narration(job, request, engine)
        summary = recording.validate_recording_artifact(job, expect_audio=True)
    except core.ToolError as exc:
        with manager._lock:
            job.phase = "completed"
            job.detail = f"narration failed: {exc}"
            if job.result is not None:
                job.result["narration"] = {"error": str(exc)}
        return
    except Exception as exc:
        core.eprint(f"unexpected narration error for {job.id}: {exc}")
        with manager._lock:
            job.phase = "completed"
            job.detail = "unexpected narration failure; see server log"
            if job.result is not None:
                job.result["narration"] = {"error": "unexpected narration failure"}
        return
    with manager._lock:
        job.media_duration_ms = round(
            float(summary.get("media_duration_seconds") or 0.0) * 1000.0, 3
        )
        events_beyond_media, latest_event_ms = timeline.media_extent(job)
        job.narration = narration_meta
        job.phase = "completed"
        job.detail = None
        if job.result is not None:
            job.result.update(summary)
            job.result["events_beyond_media"] = events_beyond_media
            job.result["latest_event_ms"] = latest_event_ms
            try:
                job.result["bytes"] = job.artifact.stat().st_size
            except OSError:
                pass
            job.result["audio_included"] = True
            job.result["narration"] = narration_meta