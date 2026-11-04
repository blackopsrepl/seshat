from __future__ import annotations

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
    render,
    render_ffmpeg,
    reporting,
    scenes,
    store,
    streams,
    takes,
    timeline,
    tts,
    workers,
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
        for document in store.scan_store():
            try:
                job = takes.job_from_manifest(document)
            except (core.ToolError, KeyError, ValueError, TypeError) as exc:
                core.eprint(f"could not rehydrate a stored take: {exc}")
                continue
            self._jobs[job.id] = job

    def _select(self, requested: Any) -> recording.RecordingJob | None:
        """Resolve an explicit recording id, defaulting to the latest take.

        Caller must hold the lock. Without an explicit id the last take wins:
        the one this process recorded if it has one, else the newest rehydrated
        take from the store, so a fresh session can still narrate the take it
        just found. An unknown id is always a clear error so a multi-take
        workflow cannot silently narrate the wrong take.
        """
        if requested is None:
            if self._job is not None:
                return self._job
            jobs = sorted(self._jobs.values(), key=lambda job: (job.started_utc, job.id))
            return jobs[-1] if jobs else None
        if not isinstance(requested, str) or not requested:
            raise core.ToolError("id must be a non-empty recording id")
        job = self._jobs.get(requested)
        if job is None:
            raise core.ToolError(f"unknown recording id: {requested}")
        return job

    # -- lifecycle -------------------------------------------------------

    def start(self, arguments: dict[str, Any]) -> dict[str, Any]:
        core.reject_unknown(arguments, recording.RECORDING_ARGUMENTS, "recording_start")
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
                detail = workers.log_tail(new_job)
                self._cleanup_failed_start(new_job)
                raise core.ToolError(
                    f"wf-recorder exited during startup (code {new_job.process.returncode})"
                    + (f": {detail}" if detail else "")
                    + _audio_start_hint(new_job)
                )
            self._jobs[new_job.id] = new_job
            self._job = new_job
            watchdog = threading.Thread(target=workers.watchdog, args=(self, new_job), daemon=True)
            new_job.thread = watchdog
            watchdog.start()
            return self._summary(new_job)

    def status(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        core.reject_unknown(arguments, ("id", "wait_seconds"), "recording_status")
        wait_seconds = 0.0
        if arguments.get("wait_seconds") is not None:
            wait_seconds = core.strict_number(
                arguments["wait_seconds"], "wait_seconds", minimum=0.0, maximum=60.0
            )
        deadline = time.monotonic() + wait_seconds
        while True:
            with self._lock:
                job = self._select(arguments.get("id"))
                if job is None:
                    return {"phase": "idle"}
                summary = self._refresh(job)
            if summary["phase"] in {"completed", "failed"} or wait_seconds <= 0.0:
                return summary
            if time.monotonic() >= deadline:
                return summary
            time.sleep(0.25)

    def _refresh(self, job: recording.RecordingJob) -> dict[str, Any]:
        """Advance a recording-phase take whose recorder has exited, then summarize.

        Caller must hold the lock. The watchdog also calls this path, so a
        recorder that dies between polls is discovered within half a second
        instead of only when someone looks.
        """
        if job.phase == "recording" and workers.reap_exited_recorder(self, job):
            pass
        return self._summary(job)

    def stop(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        core.reject_unknown(arguments or {}, (), "recording_stop")
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
        core.reject_unknown(arguments or {}, ("id",), "recording_timeline")
        with self._lock:
            job = self._select((arguments or {}).get("id"))
            if job is None:
                return {"phase": "idle", "event_count": 0, "events": [], "sources": []}
            if job.rehydrated:
                document = timeline.timeline_document(job)
                document["phase"] = job.phase
                if job.timeline_path is not None and job.timeline_path.exists():
                    document["timeline_path"] = str(job.timeline_path)
                return document
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
        request: narration.NarrationRequest | None = None
        plan_worker: threading.Thread | None = None
        job: recording.RecordingJob | None = None
        with self._lock:
            job = self._select(arguments.get("id"))
            if job is None:
                raise core.ToolError("no recording to narrate; record something first")
            if job.fmt == "gif":
                raise core.ToolError(
                    "GIF cannot carry an audio track; record format=\"mp4\" to add narration"
                )
            if job.phase != "completed":
                raise core.ToolError(
                    f"recording must be completed before narration (phase: {job.phase})"
                )
            narration_arguments = {key: value for key, value in arguments.items() if key != "id"}
            if not job.rehydrated:
                self.ingest_events(job)
            else:
                # A stored take's sidecar is already its timeline; re-ingesting
                # would read whatever streams this new process can see.
                pass
            request = narration.parse_narration_arguments(narration_arguments, job)
            engine = tts.resolve_tts_engine(request.engine)
            job.thread = None
            if request.dry_run:
                # Plan mode answers synchronously with the schedule it computed.
                # Synthesis can take seconds — the worker takes this same lock at
                # its end, so it must be joined with the lock released.
                job.plan = None
                job.plan_error = None
                plan_worker = threading.Thread(
                    target=workers.plan,
                    args=(self, job, request, engine, narration_arguments),
                    daemon=True,
                )
                job.thread = plan_worker
                plan_worker.start()
        # Past the lock: the plan worker takes the same lock at its end, so it
        # must be joined here, not while this call still holds the lock.
        if plan_worker is None:
            # The narrating phase is set here, under the lock, exactly once.
            with self._lock:
                if self._jobs.get(job.id) is not job:
                    raise core.ToolError(f"take {job.id} was discarded before narration")
                engine = tts.resolve_tts_engine(request.engine)
                job.phase = "narrating"
                job.detail = None
                job.thread = threading.Thread(
                    target=workers.narrate, args=(self, job, request, engine, narration_arguments), daemon=True
                )
                job.thread.start()
                return self._summary(job)
        plan_worker.join(
            timeout=tts.NARRATION_BUILD_TIMEOUT_SECONDS + tts.NARRATION_SYNTH_TIMEOUT_SECONDS
        )
        with self._lock:
            if self._jobs.get(job.id) is not job:
                raise core.ToolError(f"take {job.id} was discarded while planning")
            plan, plan_error = job.plan, job.plan_error
            job.plan = None
            job.plan_error = None
        if plan is None:
            raise core.ToolError(
                str(plan_error) or "the narration plan did not finish in time"
            )
        return plan

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
        ocr = arguments.get("ocr", False)
        if not isinstance(ocr, bool):
            raise core.ToolError("ocr must be a boolean")
        core.reject_unknown(
            arguments, ("id", "threshold", "max_scenes", "ocr"), "recording_scenes"
        )
        with self._lock:
            job = self._select(arguments.get("id"))
            if job is None or job.phase != "completed":
                raise core.ToolError("no completed recording to analyze")
            artifact = recording.published_artifact(job)
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

    def list_takes(self, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        core.reject_unknown(arguments or {}, (), "recording_list")
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda job: (job.started_utc, job.id),
            )
            entries = [takes.list_entry(job) for job in jobs]
        return {
            "store": str(store.state_takes_root()),
            "count": len(entries),
            "takes": entries,
        }

    def discard(self, arguments: dict[str, Any]) -> dict[str, Any]:
        core.reject_unknown(arguments, ("id",), "recording_discard")
        with self._lock:
            job = self._select(arguments.get("id"))
            if job is None:
                raise core.ToolError("no recording to discard; nothing has been recorded")
            if job.phase in {"recording", "stopping", "processing", "narrating"}:
                raise core.ToolError(
                    f"cannot discard an active take (phase: {job.phase}); stop it first"
                )
            removed = store.take_directory(job.id) if job.state_dir is not None else None
            self._jobs.pop(job.id, None)
            if self._job is job:
                self._job = None
        if removed is not None:
            store.delete_take(job.id)
        return {
            "id": job.id,
            "discarded": True,
            "state_dir": str(job.state_dir) if job.state_dir is not None else None,
        }

    def export(self, arguments: dict[str, Any]) -> dict[str, Any]:
        core.reject_unknown(arguments, ("id", "destination"), "recording_export")
        destination = arguments.get("destination")
        if not isinstance(destination, str) or not destination.strip():
            raise core.ToolError("destination must be a non-empty path")
        with self._lock:
            job = self._select(arguments.get("id"))
            if job is None:
                raise core.ToolError("no recording to export")
            if job.phase != "completed":
                raise core.ToolError(
                    f"export needs a completed take (phase: {job.phase})"
                )
            files = store.export_take(job, destination)
            return {
                "id": job.id,
                "destination": destination,
                "count": len(files),
                "files": files,
            }

    # -- internals -------------------------------------------------------

    def _cleanup_failed_start(self, job: recording.RecordingJob) -> None:
        workers.close_log_fd(job)
        for path in (job.intermediate, job.artifact, job.log_path):
            path.unlink(missing_ok=True)

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
                workers.close_log_fd(job)
                workers.fail(job, f"recorder shutdown failed: {exc}")
            return
        except Exception as exc:
            # A worker that dies silently would leave the take in `stopping` forever.
            core.eprint(f"unexpected recorder shutdown error for {job.id}: {exc}")
            with self._lock:
                workers.close_log_fd(job)
                workers.fail(job, "unexpected recorder shutdown failure; see server log")
            return

        with self._lock:
            job.process_exited_monotonic = outcome.process_exited_monotonic
            job.termination_stage = outcome.stage
            job.recorder_returncode = outcome.returncode
            workers.close_log_fd(job)
            if outcome.stage == "sigkill":
                workers.fail(job, "wf-recorder required SIGKILL; capture may be truncated")
                return
            if outcome.returncode != 0:
                tail = workers.log_tail(job)
                workers.fail(
                    job,
                    f"wf-recorder exited during shutdown (code {outcome.returncode})"
                    + (f": {tail}" if tail else ""),
                )
                return
            if workers.intermediate_size(job) == 0:
                workers.fail(job, "capture produced no data")
                return
            job.phase = "processing"
            worker = threading.Thread(target=workers.finalize, args=(self, job), daemon=True)
            job.thread = worker
            worker.start()

    def _summary(self, job: recording.RecordingJob) -> dict[str, Any]:
        return reporting.phase_summary(job, intermediate_bytes=workers.intermediate_size(job))


    def render(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Timeline-driven post-production; dispatches to the render module."""
        return render.perform(self, dict(arguments or {}))


RECORDINGS = RecordingManager()
