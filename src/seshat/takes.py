"""Rehydrated takes and the list/discard/export surface.

A completed take outlives this process through its store manifest (`store.py`);
this module owns the two directions of that handoff: rebuilding a `RecordingJob`
from a manifest at startup, and building what `recording_list` shows an operator.
"""

from __future__ import annotations

from typing import Any

from . import core, recording, store, timeline


def job_from_manifest(document: dict[str, Any]) -> recording.RecordingJob:
    """Rebuild a completed take from its store manifest and sidecar.

    The manifest carries the take fact and the completion result; the timeline
    sidecar carries the ingested events, whose recording-relative offsets are
    what anchoring needs (event ids are final on a completed take). The pseudo
    events put `at_monotonic` at `t_ms / 1000` against a zero epoch, which makes
    `recording_relative_ms` a no-op and every derived t_ms exact.
    """
    take_id = document.get("id")
    if not isinstance(take_id, str) or not take_id:
        raise core.ToolError("store manifest without a take id")
    result = document.get("result") if isinstance(document.get("result"), dict) else None
    if result is None:
        raise core.ToolError(f"take {take_id} has no completion result in its manifest")
    fmt = result.get("format") or document.get("format")
    if fmt not in (recording.RECORDING_FORMATS):
        raise core.ToolError(f"take {take_id} has an unknown format in its manifest: {fmt!r}")
    directory = store.take_directory(take_id)
    job = recording.RecordingJob(
        id=take_id,
        fmt=fmt,
        output=str(document.get("output") or result.get("output") or ""),
        region=document.get("region"),
        width=int(document.get("width") or 0),
        height=int(document.get("height") or 0),
        max_duration=0.0,
        started_monotonic=0.0,
        started_utc=str(document.get("started_utc") or ""),
        directory=directory,
        intermediate=directory / f"{take_id}.missing",
        artifact=directory / f"{take_id}.{fmt}",
        log_path=directory / "log.unused",
        log_fd=-1,
        narrated_path=directory / f"{take_id}.narrated.{fmt}",
        audio=str(document.get("audio") or "off"),
        audio_source=None,
        stream_sources=None,
        timeline_path=store.timeline_path(take_id),
    )
    job.phase = "completed"
    job.rehydrated = True
    job.state_dir = directory
    job.result = result
    job.narration = document.get("narration")
    job.media_duration_ms = _number_or_none(result.get("media_duration_seconds"), 1000.0)
    return _attach_sidecar_timeline(job)


def _number_or_none(value: Any, scale: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number * scale, 3)


def _attach_sidecar_timeline(job: recording.RecordingJob) -> recording.RecordingJob:
    document: dict[str, Any] | None = None
    if job.timeline_path is not None and job.timeline_path.exists():
        try:
            document = timeline.timeline_sidecar_document(job.timeline_path)
        except (OSError, ValueError):
            document = None
    if document is None:
        job.events = []
        job.stream_report = []
        return job
    sources = document.get("sources")
    job.stream_report = (
        [report for report in sources if isinstance(report, dict)] if isinstance(sources, list) else []
    )
    sidecar_events = document.get("events")
    if isinstance(sidecar_events, list):
        job.events = [
            {
                "at_monotonic": float(event["t_ms"]) / 1000.0,
                "tool": str(event.get("tool") or ""),
                "ok": bool(event.get("ok", True)),
                "payload": dict(event.get("payload") or {}),
                "source": str(event.get("source") or "unknown"),
            }
            for event in sidecar_events
            if isinstance(event, dict) and "t_ms" in event
        ]
    return job


def list_entry(job: recording.RecordingJob) -> dict[str, Any]:
    """What `recording_list` reports about one take."""
    result = job.result if isinstance(job.result, dict) else {}
    entry: dict[str, Any] = {
        "id": job.id,
        "phase": job.phase,
        "format": job.fmt,
        "output": job.output,
        "started_utc": job.started_utc,
        "audio": job.audio,
        "audio_included": bool(result.get("audio_included")) if job.phase == "completed" else recording.wants_audio(job.audio),
        "narrated": job.narrated_path.exists() if job.narrated_path is not None else False,
        "in_memory": not job.rehydrated,
        "media_duration_seconds": result.get("media_duration_seconds"),
    }
    if job.phase == "completed":
        entry["path"] = result.get("path") or str(recording.published_artifact(job))
        entry["master_path"] = result.get("master_path") or str(job.artifact)
    if job.detail is not None:
        entry["detail"] = job.detail
    return entry