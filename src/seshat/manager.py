from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any

from . import (
    core,
    encoding,
    lifecycle,
    narration,
    recording,
    reporting,
    scenes,
    streams,
    timeline,
    tts,
)

from .recording import wants_audio


def _audio_start_hint(job: recording.RecordingJob) -> str:
    """Name the audio mode when a take that asked for audio died at launch.

    A recorder without audio support (or without a reachable PulseAudio/PipeWire
    session) exits with code 1 exactly when ``-a`` was passed; the log tail is
    the concrete evidence, this line turns it into an actionable cause.
    """
    if not wants_audio(job.audio):
        return ""
    return (
        f' (the take asked for audio="{job.audio}": a wf-recorder without audio support '
        "or without a reachable audio session exits just like this; retry without the "
        "audio argument to confirm, and read the log for the audio backend error)"
    )


class RecordingManager:
    """Owns the single capture/finalization lifecycle for this server process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: recording.RecordingJob | None = None
        self._jobs: dict[str, recording.RecordingJob] = {}

    @staticmethod
    def _intermediate_size(job: recording.RecordingJob) -> int:
        try:
            return job.intermediate.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _close_log_fd(job: recording.RecordingJob) -> None:
        if job.log_fd >= 0:
            try:
                os.close(job.log_fd)
            except OSError:
                pass
            job.log_fd = -1

    @staticmethod
    def _log_tail(job: recording.RecordingJob) -> str:
        try:
            data = job.log_path.read_bytes()
        except OSError:
            return ""
        return data[-recording.RECORDING_LOG_TAIL_BYTES:].decode("utf-8", errors="replace").strip()

    @staticmethod
    def _discard_intermediate(job: recording.RecordingJob) -> None:
        job.intermediate.unlink(missing_ok=True)

    def _fail(self, job: recording.RecordingJob, message: str) -> None:
        job.phase = "failed"
        job.detail = f"{job.detail}; {message}" if job.detail else message
        job.artifact.unlink(missing_ok=True)

    def _cleanup_failed_start(self, job: recording.RecordingJob) -> None:
        self._close_log_fd(job)
        for path in (job.intermediate, job.artifact, job.log_path):
            path.unlink(missing_ok=True)

    def _select(self, requested: Any) -> recording.RecordingJob | None:
        """Resolve an explicit recording id, defaulting to the latest job.

        Caller must hold the lock. An unknown id is always a clear error so a
        multi-take workflow cannot silently narrate the wrong take.
        """
        if requested is None:
            return self._job
        if not isinstance(requested, str) or not requested:
            raise core.ToolError("id must be a non-empty recording id")
        job = self._jobs.get(requested)
        if job is None:
            raise core.ToolError(f"unknown recording id: {requested}")
        return job

    # -- lifecycle -------------------------------------------------------

    def start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        core.require_binaries(["wf-recorder", "ffmpeg", "ffprobe"])
        with self._lock:
            job = self._job
            if job is not None and job.phase in {"recording", "stopping", "processing", "narrating"}:
                raise core.ToolError(
                    f"a recording is already active (phase: {job.phase}); call recording_status"
                )
            new_job = recording.new_recording_job(arguments)
            if new_job.fmt == "webm":
                new_job.encoder = encoding.choose_video_encoder()
            elif new_job.fmt == "mp4":
                new_job.encoder = encoding.choose_h264_encoder()
            try:
                new_job.process = subprocess.Popen(
                    encoding.recording_capture_argv(new_job),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=new_job.log_fd,
                    start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                self._cleanup_failed_start(new_job)
                raise core.ToolError(f"failed to launch wf-recorder: {exc}") from exc
            new_job.started_monotonic = time.monotonic()
            new_job.started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            time.sleep(recording.RECORDING_STARTUP_PROBE_SECONDS)
            if new_job.process.poll() is not None:
                detail = self._log_tail(new_job)
                self._cleanup_failed_start(new_job)
                raise core.ToolError(
                    f"wf-recorder exited during startup (code {new_job.process.returncode})"
                    + (f": {detail}" if detail else "")
                    + _audio_start_hint(new_job)
                )
            self._jobs[new_job.id] = new_job
            self._job = new_job
            watchdog = threading.Thread(target=self._watchdog, args=(new_job,), daemon=True)
            new_job.thread = watchdog
            watchdog.start()
            return self._summary(new_job)

    def status(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            job = self._select((arguments or {}).get("id"))
            if job is None:
                return {"phase": "idle"}
            if job.phase == "recording":
                process = job.process
                if process is not None and process.poll() is not None:
                    exited = time.monotonic()
                    job.stop_requested_monotonic = exited
                    job.ended_monotonic = exited
                    job.process_exited_monotonic = exited
                    job.termination_stage = "natural"
                    job.recorder_returncode = process.returncode
                    self._close_log_fd(job)
                    if process.returncode != 0:
                        tail = self._log_tail(job)
                        self._fail(
                            job,
                            f"wf-recorder exited unexpectedly (code {process.returncode})"
                            + (f": {tail}" if tail else ""),
                        )
                    elif self._intermediate_size(job) == 0:
                        self._fail(job, "capture produced no data")
                    else:
                        job.phase = "processing"
                        job.thread = threading.Thread(
                            target=self._finalize, args=(job,), daemon=True
                        )
                        job.thread.start()
            return self._summary(job)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            job = self._job
            if job is None or job.phase != "recording":
                phase = job.phase if job is not None else "idle"
                raise core.ToolError(f"no active recording to stop (phase: {phase})")
            self._request_stop(job)
            return self._summary(job)

    def ingest_events(self, job: recording.RecordingJob) -> None:
        """Rebuild the take's event list from every ingested stream.

        Caller must hold the lock. Rebuilding from scratch rather than appending
        makes this idempotent, so it can run on every timeline read, at
        finalization, and again before narration anchors are resolved.

        The window is closed at the take's own end: an event another server
        publishes after capture stopped describes something the video does not
        contain, so it is not an anchor.
        """
        until = job.ended_monotonic if job.ended_monotonic is not None else time.monotonic()
        try:
            paths = streams.resolve_sources(job.stream_sources)
            reads = streams.read_sources(paths, job.started_monotonic, until)
        except core.ToolError as exc:
            core.eprint(f"timeline ingestion failed for {job.id}: {exc}")
            job.events = []
            job.stream_report = [{"error": str(exc)}]
            return
        job.events = streams.merge_events(reads)
        job.stream_report = [read.report() for read in reads]

    def timeline(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            job = self._select((arguments or {}).get("id"))
            if job is None:
                return {"phase": "idle", "event_count": 0, "events": [], "sources": []}
            self.ingest_events(job)
            document = timeline.timeline_document(job)
            document["phase"] = job.phase
            if job.phase == "recording":
                document["note"] = (
                    "event ids are provisional until the take stops; further events "
                    "can still land ahead of these"
                )
            if job.timeline_path is not None and job.timeline_path.exists():
                document["timeline_path"] = str(job.timeline_path)
            return document

    def voiceover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job = self._select(arguments.get("id"))
            if job is None:
                raise core.ToolError("no recording to narrate; record something first")
            if job.fmt == "gif":
                raise core.ToolError(
                    "GIF cannot carry an audio track; record format=webm to add narration"
                )
            if job.phase != "completed":
                raise core.ToolError(
                    f"recording must be completed before narration (phase: {job.phase})"
                )
            narration_arguments = {key: value for key, value in arguments.items() if key != "id"}
            self.ingest_events(job)
            request = narration.parse_narration_arguments(narration_arguments, job)
            engine = tts.resolve_tts_engine(request.engine)
            job.phase = "narrating"
            job.detail = None
            job.thread = threading.Thread(
                target=self._narrate, args=(job, request, engine), daemon=True
            )
            job.thread.start()
            return self._summary(job)

    def scenes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        threshold = core.strict_number(
            arguments.get("threshold", scenes.SCENE_DEFAULT_THRESHOLD),
            "threshold",
            minimum=scenes.SCENE_MIN_THRESHOLD,
            maximum=scenes.SCENE_MAX_THRESHOLD,
        )
        max_scenes = core.strict_int(arguments.get("max_scenes", scenes.SCENE_DEFAULT_MAX), "max_scenes")
        if max_scenes < 1 or max_scenes > scenes.SCENE_MAX_CUTS:
            raise core.ToolError(f"max_scenes must be between 1 and {scenes.SCENE_MAX_CUTS}")
        ocr = bool(arguments.get("ocr", False))
        with self._lock:
            job = self._select(arguments.get("id"))
            if job is None or job.phase != "completed":
                raise core.ToolError("no completed recording to analyze")
            artifact = job.artifact
            workdir = job.directory
            recording_id = job.id
        core.require_binaries(["ffmpeg", "ffprobe"])
        cuts = scenes.detect_scene_cuts(artifact, threshold, max_scenes)
        if ocr:
            workdir.mkdir(mode=recording.RECORDING_DIR_MODE, exist_ok=True)
            for cut in cuts:
                cut["text"] = scenes.ocr_frame(artifact, cut["t_ms"], workdir)
        return {
            "id": recording_id,
            "path": str(artifact),
            "threshold": threshold,
            "approximate": True,
            "note": "scene cuts are approximate fallback anchors, not the sync source",
            "scene_count": len(cuts),
            "scenes": cuts,
        }

    def shutdown(self) -> None:
        worker: threading.Thread | None = None
        with self._lock:
            job = self._job
            if job is None or job.phase != "recording":
                return
            if job.detail is None:
                job.detail = "capture ended at server shutdown"
            self._request_stop(job)
            worker = job.thread
        if worker is not None:
            worker.join(timeout=sum((
                lifecycle.RECORDING_STOP_TIMEOUT_SECONDS,
                lifecycle.RECORDING_TERM_TIMEOUT_SECONDS,
                lifecycle.RECORDING_KILL_TIMEOUT_SECONDS,
                lifecycle.RECORDING_FINAL_WAIT_SECONDS,
            )) + 1.0)

    # -- internals -------------------------------------------------------

    def _request_stop(self, job: recording.RecordingJob) -> None:
        """Close the capture window and stop the recorder without holding the manager lock."""
        stopped = time.monotonic()
        job.stop_requested_monotonic = stopped
        job.ended_monotonic = stopped
        job.phase = "stopping"
        worker = threading.Thread(target=self._stop_recorder, args=(job,), daemon=True)
        job.thread = worker
        worker.start()

    def _stop_recorder(self, job: recording.RecordingJob) -> None:
        process = job.process
        try:
            if process is None:
                raise core.ToolError("wf-recorder process is unavailable")
            outcome = lifecycle.terminate_process_group(process)
        except core.ToolError as exc:
            with self._lock:
                self._close_log_fd(job)
                self._fail(job, f"recorder shutdown failed: {exc}")
            return
        except Exception as exc:
            # A worker that dies silently would leave the take in `stopping` forever.
            core.eprint(f"unexpected recorder shutdown error for {job.id}: {exc}")
            with self._lock:
                self._close_log_fd(job)
                self._fail(job, "unexpected recorder shutdown failure; see server log")
            return

        with self._lock:
            job.process_exited_monotonic = outcome.process_exited_monotonic
            job.termination_stage = outcome.stage
            job.recorder_returncode = outcome.returncode
            self._close_log_fd(job)
            if outcome.stage == "sigkill":
                self._fail(job, "wf-recorder required SIGKILL; capture may be truncated")
                return
            if outcome.returncode != 0:
                tail = self._log_tail(job)
                self._fail(
                    job,
                    f"wf-recorder exited during shutdown (code {outcome.returncode})"
                    + (f": {tail}" if tail else ""),
                )
                return
            if self._intermediate_size(job) == 0:
                self._fail(job, "capture produced no data")
                return
            job.phase = "processing"
            worker = threading.Thread(target=self._finalize, args=(job,), daemon=True)
            job.thread = worker
            worker.start()

    def _watchdog(self, job: recording.RecordingJob) -> None:
        deadline = job.started_monotonic + job.max_duration
        while True:
            with self._lock:
                if self._job is not job or job.phase != "recording":
                    return
                exceeded_time = time.monotonic() >= deadline
                exceeded_size = self._intermediate_size(job) > recording.RECORDING_MAX_INTERMEDIATE_BYTES
                if exceeded_time or exceeded_size:
                    job.auto_stopped = True
                    if exceeded_size and not exceeded_time:
                        job.detail = "recording stopped: intermediate size limit reached"
                    self._request_stop(job)
                    return
            time.sleep(0.5)

    def _finalize(self, job: recording.RecordingJob) -> None:
        try:
            summary = recording.finalize_recording(job)
            job.artifact.chmod(recording.RECORDING_FILE_MODE)
            with self._lock:
                self.ingest_events(job)
                timeline_path = timeline.write_timeline_sidecar(job)
        except core.ToolError as exc:
            with self._lock:
                self._fail(job, f"finalization failed: {exc}")
            return
        except Exception as exc:
            core.eprint(f"unexpected finalization error for {job.id}: {exc}")
            with self._lock:
                self._fail(job, "unexpected finalization failure; see server log")
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
            "audio_included": wants_audio(job.audio),
            "cursor_included": True,
            "auto_stopped": job.auto_stopped,
            "timeline_path": str(timeline_path) if timeline_path is not None else None,
            "timeline_event_count": len(job.events),
            **summary,
        }
        with self._lock:
            job.result = result
            job.phase = "completed"
            self._discard_intermediate(job)
            job.log_path.unlink(missing_ok=True)

    def _narrate(
        self, job: recording.RecordingJob, request: narration.NarrationRequest, engine: tts.TtsEngine
    ) -> None:
        try:
            narration_meta = narration.perform_narration(job, request, engine)
            summary = recording.validate_recording_artifact(job, expect_audio=True)
        except core.ToolError as exc:
            with self._lock:
                job.phase = "completed"
                job.detail = f"narration failed: {exc}"
                if job.result is not None:
                    job.result["narration"] = {"error": str(exc)}
            return
        except Exception as exc:
            core.eprint(f"unexpected narration error for {job.id}: {exc}")
            with self._lock:
                job.phase = "completed"
                job.detail = "unexpected narration failure; see server log"
                if job.result is not None:
                    job.result["narration"] = {"error": "unexpected narration failure"}
            return
        with self._lock:
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

    def _summary(self, job: recording.RecordingJob) -> dict[str, Any]:
        return reporting.phase_summary(job, intermediate_bytes=self._intermediate_size(job))


RECORDINGS = RecordingManager()
