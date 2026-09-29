from __future__ import annotations

import os
import secrets
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import core, encoding, media, outputs, streams, timeline


RECORDING_FORMATS = ("mp4", "webm", "gif")
RECORDING_MIME_TYPES = {"mp4": "video/mp4", "webm": "video/webm", "gif": "image/gif"}
RECORDING_MAX_DURATION_SECONDS = {"mp4": 300.0, "webm": 300.0, "gif": 15.0}
RECORDING_DEFAULT_DURATION_SECONDS = {"mp4": 60.0, "webm": 60.0, "gif": 15.0}
RECORDING_MAX_INTERMEDIATE_BYTES = 1024**3
RECORDING_DIRECTORY_NAME = "recordings"
RECORDING_DIR_MODE = 0o700
RECORDING_FILE_MODE = 0o600
RECORDING_STARTUP_PROBE_SECONDS = 1.0


@dataclass
class RecordingJob:
    id: str
    fmt: str
    output: str
    region: dict[str, int] | None
    width: int
    height: int
    max_duration: float
    started_monotonic: float
    started_utc: str
    directory: Path
    intermediate: Path
    artifact: Path
    log_path: Path
    log_fd: int
    encoder: str = "libsvtav1"
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    phase: str = "recording"
    detail: str | None = None
    auto_stopped: bool = False
    stop_requested_monotonic: float | None = None
    process_exited_monotonic: float | None = None
    termination_stage: str | None = None
    recorder_returncode: int | None = None
    ended_monotonic: float | None = None
    result: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    stream_sources: list[str] | None = None
    stream_report: list[dict[str, Any]] = field(default_factory=list)
    media_duration_ms: float | None = None
    timeline_path: Path | None = None
    narration: dict[str, Any] | None = None


def parse_recording_format(value: Any) -> str:
    fmt = "mp4" if value is None else value
    if not isinstance(fmt, str) or fmt not in RECORDING_FORMATS:
        raise core.ToolError("format must be one of: " + ", ".join(RECORDING_FORMATS))
    return fmt


def parse_max_duration(value: Any, fmt: str) -> float:
    if value is None:
        return RECORDING_DEFAULT_DURATION_SECONDS[fmt]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise core.ToolError("max_duration_seconds must be a number")
    duration = float(value)
    if duration <= 0:
        raise core.ToolError("max_duration_seconds must be positive")
    if duration > RECORDING_MAX_DURATION_SECONDS[fmt]:
        raise core.ToolError(
            f"max_duration_seconds must not exceed {RECORDING_MAX_DURATION_SECONDS[fmt]:g} for {fmt}"
        )
    return duration


def select_recording_output(requested: Any) -> dict[str, Any]:
    active = outputs.get_outputs()
    names = [str(output.get("name")) for output in active]
    if requested is not None:
        if not isinstance(requested, str) or requested not in names:
            raise core.ToolError(
                f"unknown output {requested!r}; active outputs: {', '.join(names) or 'none'}"
            )
        return next(output for output in active if str(output.get("name")) == requested)
    if len(active) == 1:
        return active[0]
    raise core.ToolError(
        "multiple active outputs; provide output as one of: " + ", ".join(names)
    )


def validate_contained_region(region: Any, output_rect: dict[str, Any]) -> dict[str, int] | None:
    if region is None:
        return None
    if not isinstance(region, dict):
        raise core.ToolError("region must be an object with integer x, y, width, and height")
    x = core.strict_int(region.get("x"), "region x")
    y = core.strict_int(region.get("y"), "region y")
    width = core.strict_int(region.get("width"), "region width")
    height = core.strict_int(region.get("height"), "region height")
    if width <= 0 or height <= 0:
        raise core.ToolError("region width and height must be positive")
    try:
        rect_x = int(output_rect["x"])
        rect_y = int(output_rect["y"])
        rect_width = int(output_rect["width"])
        rect_height = int(output_rect["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise core.ToolError("selected output has invalid geometry") from exc
    if (
        x < rect_x
        or y < rect_y
        or x + width > rect_x + rect_width
        or y + height > rect_y + rect_height
    ):
        raise core.ToolError(
            f"region must be fully contained in output rect {rect_x},{rect_y} {rect_width}x{rect_height}"
        )
    return {"x": x, "y": y, "width": width, "height": height}


def recording_directory() -> Path:
    directory = streams.runtime_root() / RECORDING_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True, mode=RECORDING_DIR_MODE)
    os.chmod(directory, RECORDING_DIR_MODE)
    return directory


def create_private_file(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, RECORDING_FILE_MODE)
    os.close(fd)


def allocate_recording_paths(fmt: str) -> tuple[str, Path, Path, Path, int]:
    directory = recording_directory()
    for _ in range(5):
        recording_id = secrets.token_hex(8)
        intermediate = directory / f"{recording_id}.mkv"
        artifact = directory / f"{recording_id}.{fmt}"
        log_path = directory / f"{recording_id}.log"
        try:
            log_fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, RECORDING_FILE_MODE)
            try:
                create_private_file(intermediate)
                create_private_file(artifact)
            except FileExistsError:
                os.close(log_fd)
                log_path.unlink(missing_ok=True)
                raise
            return recording_id, intermediate, artifact, log_path, log_fd
        except FileExistsError:
            continue
    raise core.ToolError("could not allocate unique recording paths")


def new_recording_job(arguments: dict[str, Any]) -> RecordingJob:
    fmt = parse_recording_format(arguments.get("format"))
    output = select_recording_output(arguments.get("output"))
    region = validate_contained_region(arguments.get("region"), output.get("rect") or {})
    max_duration = parse_max_duration(arguments.get("max_duration_seconds"), fmt)
    stream_sources = streams.parse_timeline_sources(arguments.get("timeline_sources"))
    output_rect = output.get("rect") or {}
    if region is not None:
        width = region["width"]
        height = region["height"]
    else:
        try:
            width = int(output_rect["width"])
            height = int(output_rect["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise core.ToolError(f"output {output.get('name')} has invalid geometry") from exc
    recording_id, intermediate, artifact, log_path, log_fd = allocate_recording_paths(fmt)
    return RecordingJob(
        id=recording_id,
        fmt=fmt,
        output=str(output.get("name")),
        region=region,
        width=width,
        height=height,
        max_duration=max_duration,
        started_monotonic=0.0,
        started_utc="",
        directory=intermediate.parent,
        intermediate=intermediate,
        artifact=artifact,
        log_path=log_path,
        log_fd=log_fd,
        stream_sources=stream_sources,
        timeline_path=intermediate.with_suffix(timeline.TIMELINE_SUFFIX),
    )


RECORDING_LOG_TAIL_BYTES = 500


def validate_recording_artifact(
    job: RecordingJob, expect_audio: bool = False, path: Path | None = None
) -> dict[str, Any]:
    target = path or job.artifact
    probe = media.probe_media(target)
    format_name = str((probe.get("format") or {}).get("format_name") or "")
    streams = probe.get("streams") or []
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if job.fmt == "gif":
        if audio:
            raise core.ToolError("artifact must not contain audio streams")
        if "gif" not in format_name:
            raise core.ToolError(f"artifact is not a GIF (format: {format_name or 'unknown'})")
        if len(video) != 1:
            raise core.ToolError("artifact must contain exactly one image stream")
    else:
        label = "MP4" if job.fmt == "mp4" else "WebM"
        if job.fmt not in format_name:
            raise core.ToolError(f"artifact is not {label} (format: {format_name or 'unknown'})")
        expected_video = "h264" if job.fmt == "mp4" else "av1"
        if len(video) != 1 or video[0].get("codec_name") != expected_video:
            raise core.ToolError(
                f"artifact must contain exactly one {expected_video.upper()} video stream"
            )
        expected_audio = encoding.RECORDING_AUDIO_CODEC_NAMES[job.fmt]
        if expect_audio:
            if len(audio) != 1 or audio[0].get("codec_name") != expected_audio:
                raise core.ToolError(
                    f"narrated artifact must contain exactly one {expected_audio} audio stream"
                )
        elif audio:
            raise core.ToolError("artifact must not contain audio streams")
    try:
        duration = media.duration_seconds_from_probe(probe)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0.0:
        raise core.ToolError("artifact has no readable duration")
    return {
        "codec": str(video[0].get("codec_name")),
        "container": job.fmt,
        "mime_type": RECORDING_MIME_TYPES[job.fmt],
        "width": int(video[0].get("width") or 0),
        "height": int(video[0].get("height") or 0),
        "media_duration_seconds": round(duration, 3),
        "frame_rate": round(media.parse_frame_rate(video[0].get("avg_frame_rate")), 3),
    }


def candidate_artifact_path(job: RecordingJob) -> Path:
    """Private path a finalized artifact is built at before it is published."""
    return job.artifact.with_name(job.artifact.name + ".part")


def finalize_recording(job: RecordingJob) -> dict[str, Any]:
    try:
        capture = media.probe_video_stream(job.intermediate)
    except core.ToolError as exc:
        hint = (
            " (this take was stopped by its own deadline, which can leave the recorder's "
            "tail unwritten)"
            if job.auto_stopped
            else ""
        )
        raise core.ToolError(f"capture produced an unreadable intermediate{hint}: {exc}") from exc
    try:
        capture_width = int(capture.get("width") or 0)
    except (TypeError, ValueError):
        capture_width = 0
    candidate = candidate_artifact_path(job)
    candidate.unlink(missing_ok=True)
    if job.fmt == "webm":
        argv = encoding.recording_webm_argv(job, candidate)
    elif job.fmt == "mp4":
        argv = encoding.recording_mp4_argv(job, candidate)
    else:
        argv = encoding.recording_gif_argv(job, capture_width, candidate)
    core.run_command(argv, timeout=600.0)
    summary = validate_recording_artifact(job, path=candidate)
    media.validate_full_video_decode(candidate)
    os.replace(candidate, job.artifact)
    return summary

