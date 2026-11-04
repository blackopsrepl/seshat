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
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import (
    core,
    lifecycle,
    media,
    narration,
    recording,
    render,
    render_ffmpeg,
    store,
    timeline,
    tts,
)

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


def reap_exited_recorder(manager: "RecordingManager", job: recording.RecordingJob) -> None:
    """Advance a recording-phase take whose recorder process has already exited.

    Caller must hold the manager lock. Both the status poll and the watchdog
    come through here, so a recorder that died on its own is reaped once, from
    either path, and the take's facts record the exact way it ended.
    """
    process = job.process
    if process is None or process.poll() is None:
        return
    job.stop_requested_monotonic = time.monotonic()
    job.ended_monotonic = job.stop_requested_monotonic
    job.process_exited_monotonic = job.stop_requested_monotonic
    job.termination_stage = "natural"
    job.recorder_returncode = process.returncode
    close_log_fd(job)
    if process.returncode != 0:
        tail = log_tail(job)
        fail(
            job,
            f"wf-recorder exited unexpectedly (code {process.returncode})"
            + (f": {tail}" if tail else ""),
        )
        return
    if intermediate_size(job) == 0:
        fail(job, "capture produced no data")
        return
    job.phase = "processing"
    job.thread = threading.Thread(target=finalize, args=(manager, job), daemon=True)
    job.thread.start()


def watchdog(manager: "RecordingManager", job: recording.RecordingJob) -> None:
    deadline = job.started_monotonic + job.max_duration
    while True:
        with manager._lock:
            if manager._job is not job or job.phase != "recording":
                return
            reap_exited_recorder(manager, job)
            if job.phase != "recording":
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
        "master_path": str(job.artifact),
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
        # The finalization owns adoption: the store keeps the take when this
        # process ends; a failed store write keeps the take in the runtime dir
        # whole and only costs the next session its memory.
        adopt_into_store(manager, job, timeline_sidecar=timeline_path)


def adopt_into_store(
    manager: "RecordingManager",
    job: recording.RecordingJob,
    *,
    timeline_sidecar: Any,
) -> None:
    """Move a completed take's published files into the durable store.

    Caller must hold the manager lock.
    """
    try:
        directory = store.adopt(job, timeline_sidecar)
    except (OSError, core.ToolError) as exc:
        core.eprint(f"could not move take {job.id} into the store: {exc}")
        if job.result is not None:
            job.result["store_error"] = str(exc)
        return
    job.state_dir = directory
    # Derived work (narration's .part and its workdir) belongs beside the take:
    # job.directory follows, so the final deliverable never replaces across the
    # tmpfs/store device boundary.
    job.directory = directory
    job.artifact = directory / job.artifact.name
    if job.timeline_path is not None:
        job.timeline_path = directory / job.timeline_path.name
    job.narrated_path = directory / job.narrated_path.name
    if job.result is not None:
        job.result["master_path"] = str(job.artifact)
        job.result["path"] = str(job.artifact)
        if job.timeline_path is not None:
            job.result["timeline_path"] = str(job.timeline_path)
        job.result["state_dir"] = str(directory)
    try:
        store.write_manifest(job)
    except OSError as exc:
        core.eprint(f"could not write the manifest for {job.id}: {exc}")


def plan(
    manager: "RecordingManager",
    job: recording.RecordingJob,
    request: narration.NarrationRequest,
    engine: tts.TtsEngine,
    narration_arguments: dict[str, Any] | None = None,
) -> None:
    """The dry-run worker: real synthesis and schedule, nothing published.

    The joining voiceover call turns ``job.plan`` into its answer, so the tool
    returns the plan synchronously without a publish or a narrating phase.
    """
    with manager._lock:
        job.plan = None
        job.plan_error = None
    try:
        meta = narration.perform_narration(job, request, engine)
    except core.ToolError as exc:
        with manager._lock:
            job.plan_error = str(exc)
            job.detail = f"narration plan failed: {exc}"
        return
    except Exception as exc:
        core.eprint(f"unexpected narration plan error for {job.id}: {exc}")
        with manager._lock:
            job.plan_error = "unexpected narration plan failure; see server log"
            job.detail = "unexpected narration plan failure; see server log"
        return
    with manager._lock:
        job.plan = meta
        job.detail = None
        if job.state_dir is not None:
            try:
                store.append_narration_record(job.id, narration_arguments or {},
                                              {"plan": True, "segments": meta.get("segments", [])})
            except OSError as exc:
                core.eprint(f"could not journal the plan for {job.id}: {exc}")


def narrate(
    manager: "RecordingManager",
    job: recording.RecordingJob,
    request: narration.NarrationRequest,
    engine: tts.TtsEngine,
    narration_arguments: dict[str, Any] | None = None,
) -> None:
    """The narration worker: synthesize, schedule, mux, validate, publish."""
    try:
        narration_meta = narration.perform_narration(job, request, engine)
        deliverable = Path(narration_meta["path"])
        summary = recording.validate_recording_artifact(
            job, expect_audio=True, path=deliverable
        )
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
                job.result["bytes"] = deliverable.stat().st_size
            except OSError:
                pass
            job.result["path"] = narration_meta["path"]
            job.result["master_path"] = str(job.artifact)
            job.result["audio_included"] = True
            job.result["narration"] = narration_meta
            if job.state_dir is not None:
                try:
                    store.append_narration_record(job.id, narration_arguments or {}, narration_meta)
                    store.write_manifest(job)
                except OSError as exc:
                    core.eprint(f"could not journal the narration for {job.id}: {exc}")

def build_render(
    manager: "RecordingManager",
    job: recording.RecordingJob,
    plan: dict[str, Any],
    edit: "render.Edit",
) -> None:
    """The render worker: build, validate, publish, then derive the extras."""
    profile = edit.profile
    base = f"{job.id}.render" + ("" if profile == "native" else f".{profile}")
    out_path = job.directory / f"{base}.{job.fmt}"
    candidate = out_path.with_name(out_path.name + ".part")
    poster_path = job.directory / f"{base}.poster.png"
    teaser_path = job.directory / f"{base}.teaser.gif"
    chapters_path = job.directory / f"{base}.chapters.meta"
    try:
        probe = media.probe_media(job.artifact)
        video = next(
            stream for stream in probe.get("streams") or []
            if stream.get("codec_type") == "video"
        )
        width, height = int(video["width"]), int(video["height"])
        has_audio = any(
            stream.get("codec_type") == "audio" for stream in probe.get("streams") or []
        )
        chapters_input: Path | None = None
        if plan.get("chapters"):
            text = render_ffmpeg.chapters_metadata_text(edit, edit.trim_end_ms - edit.trim_start_ms)
            chapters_input = chapters_path
            chapters_input.write_text(text, encoding="utf-8")
            os.chmod(chapters_input, recording.RECORDING_FILE_MODE)
        argv = render_ffmpeg.main_pass_argv(
            job, edit, candidate, width, height, has_audio, chapters_input, job.fmt
        )
        core.run_command(argv, timeout=600.0)
        summary = recording.validate_recording_artifact(
            job, expect_audio=has_audio, path=candidate
        )
        os.replace(candidate, out_path)
        os.chmod(out_path, recording.RECORDING_FILE_MODE)
        media_ms = round(float(summary.get("media_duration_seconds") or 0.0) * 1000.0, 3)
        meta = {
            "path": str(out_path),
            "bytes": out_path.stat().st_size,
            "format": job.fmt,
            "profile": profile,
            "media_duration_ms": media_ms,
            "trim_ms": [round(edit.trim_start_ms, 3), round(edit.trim_end_ms, 3)],
            "scale": round(edit.scale(), 4),
            "zoom_windows": len(edit.zoom_windows),
            "markers": len(edit.markers),
            "event_map": [
                {"tool": event.tool, "source": event.source, "t_ms": event.t_ms, "out_ms": event.out_ms}
                for event in edit.events
            ],
        }
        if chapters_input is not None:
            # The YouTube-listable form of the same chapters, next to the render.
            lines = []
            for start_ms, title in render.chapter_list(edit):
                minutes, seconds = divmod(int(round(start_ms / 1000.0)), 60)
                lines.append(f"{minutes}:{seconds:02d} {title}")
            out_path.with_suffix(".chapters.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.chmod(out_path.with_suffix(".chapters.txt"), recording.RECORDING_FILE_MODE)
            meta["chapters"] = str(out_path.with_suffix(".chapters.txt"))
        if plan.get("poster"):
            poster_argv = render_ffmpeg.poster_argv(
                out_path, render.poster_t_ms(edit), poster_path
            )
            core.run_command(poster_argv, timeout=120.0)
            os.chmod(poster_path, recording.RECORDING_FILE_MODE)
            meta["poster"] = str(poster_path)
        if plan.get("teaser"):
            start_ms, end_ms = render.teaser_span_ms(edit, media_ms, render_ffmpeg.RENDER_TEASER_SECONDS_DEFAULT)
            core.run_command(
                render_ffmpeg.teaser_argv(out_path, start_ms, end_ms, teaser_path), timeout=300.0
            )
            meta["teaser"] = str(teaser_path)
    except core.ToolError as exc:
        candidate.unlink(missing_ok=True)
        with manager._lock:
            job.render_error = str(exc)
            job.detail = f"render failed: {exc}"
        return
    except Exception as exc:
        core.eprint(f"unexpected render error for {job.id}: {exc!r}")
        candidate.unlink(missing_ok=True)
        detail = f"unexpected render failure: {exc!r}"
        with manager._lock:
            job.render_error = detail
            job.detail = detail
        return
    with manager._lock:
        job.render_result = meta
        job.detail = None
        if job.state_dir is not None:
            try:
                store.append_render_record(job.id, dict(plan), meta)
            except OSError as exc:
                core.eprint(f"could not journal the render for {job.id}: {exc}")


def render_stitch(
    manager: "RecordingManager",
    job: recording.RecordingJob,
    order: list[tuple[str, recording.RecordingJob, bool]],
) -> dict[str, Any]:
    """Concatenates whole finalized masters in order; container-level, no cuts."""
    out_path = job.directory / f"{job.id}.stitch.{job.fmt}"
    candidate = out_path.with_name(out_path.name + ".part")
    list_path = out_path.with_suffix(".concat.txt")
    try:
        masters = [entry.artifact for _, entry, _ in order]
        render_ffmpeg.probes_match_shape([media.probe_media(master) for master in masters])
        list_path.write_text(render_ffmpeg.stitch_list_text(masters), encoding="utf-8")
        os.chmod(list_path, recording.RECORDING_FILE_MODE)
        has_audio = any(wants for _, _, wants in order)
        core.run_command(
            render_ffmpeg.stitch_argv(list_path, candidate, has_audio, job.fmt), timeout=600.0
        )
        summary = recording.validate_recording_artifact(
            job, expect_audio=has_audio, path=candidate
        )
        os.replace(candidate, out_path)
        os.chmod(out_path, recording.RECORDING_FILE_MODE)
        media_ms = round(float(summary.get("media_duration_seconds") or 0.0) * 1000.0, 3)
        meta = {
            "path": str(out_path),
            "bytes": out_path.stat().st_size,
            "format": job.fmt,
            "profile": "native",
            "media_duration_ms": media_ms,
            "stitched": [entry_id for entry_id, _, _ in order],
        }
    except core.ToolError as exc:
        candidate.unlink(missing_ok=True)
        job.stitch_error = str(exc)
        job.detail = f"stitch failed: {exc}"
        raise
    except Exception as exc:
        core.eprint(f"unexpected stitch error for {job.id}: {exc}")
        candidate.unlink(missing_ok=True)
        job.stitch_error = "unexpected stitch failure; see server log"
        job.detail = job.stitch_error
        raise core.ToolError(job.stitch_error)
    finally:
        list_path.unlink(missing_ok=True)
    job.detail = None
    if job.state_dir is not None:
        try:
            store.append_render_record(job.id, {"stitched": meta["stitched"]}, meta)
        except OSError as exc:
            core.eprint(f"could not journal the stitch for {job.id}: {exc}")
    job.stitch_result = meta
    return meta
