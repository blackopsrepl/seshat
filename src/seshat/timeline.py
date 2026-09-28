from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from . import recording


TIMELINE_SUFFIX = ".timeline.json"


def recording_relative_ms(epoch_monotonic: float, at_monotonic: float) -> float:
    """Milliseconds from the recording epoch to ``at_monotonic``, never negative."""
    return round(max(at_monotonic - epoch_monotonic, 0.0) * 1000.0, 3)


def ordered_events(job: recording.RecordingJob) -> list[dict[str, Any]]:
    """The take's ingested events in host-monotonic order.

    Event ids are positions in this order, so they are only final once the take
    has stopped and no further event can fall inside its window.
    """
    return sorted(job.events, key=lambda event: event["at_monotonic"])


def event_times_ms(job: recording.RecordingJob) -> dict[int, float]:
    """Narration anchor lookup: event id -> recording-relative milliseconds."""
    return {
        index: recording_relative_ms(job.started_monotonic, event["at_monotonic"])
        for index, event in enumerate(ordered_events(job), 1)
    }


def media_extent(job: recording.RecordingJob) -> tuple[int, float | None]:
    """How far the ingested timeline reaches, and how much of it has no picture.

    Returns the number of events past the playable video extent and the latest
    event time in milliseconds. Before finalization the extent is not known yet,
    so nothing is counted as past it. The comparison is strict: this is a report,
    not the gate that refuses an anchor (that one allows a small slack).
    """
    times = list(event_times_ms(job).values())
    if not times:
        return 0, None
    latest = max(times)
    if job.media_duration_ms is None:
        return 0, latest
    return sum(1 for t_ms in times if t_ms > job.media_duration_ms), latest


def timeline_document(job: recording.RecordingJob) -> dict[str, Any]:
    end = job.ended_monotonic if job.ended_monotonic is not None else time.monotonic()
    events_beyond_media, latest_event_ms = media_extent(job)
    return {
        "id": job.id,
        "format": job.fmt,
        "output": job.output,
        "region": job.region,
        "started_utc": job.started_utc,
        "capture_elapsed_seconds": round(max(end - job.started_monotonic, 0.0), 3),
        "media_duration_seconds": (
            round(job.media_duration_ms / 1000.0, 3)
            if job.media_duration_ms is not None
            else None
        ),
        "event_count": len(job.events),
        "latest_event_ms": latest_event_ms,
        "events_beyond_media": events_beyond_media,
        "sources": [dict(report) for report in job.stream_report],
        "events": [
            {
                "id": index,
                "t_ms": recording_relative_ms(job.started_monotonic, event["at_monotonic"]),
                "tool": event["tool"],
                "ok": bool(event["ok"]),
                "payload": dict(event["payload"]),
                "source": event["source"],
            }
            for index, event in enumerate(ordered_events(job), 1)
        ],
    }


def write_timeline_sidecar(job: recording.RecordingJob) -> Path | None:
    from . import recording as _recording

    if job.timeline_path is None:
        return None
    data = json.dumps(timeline_document(job), indent=2, sort_keys=True).encode("utf-8")
    fd = os.open(
        job.timeline_path,
        os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
        _recording.RECORDING_FILE_MODE,
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return job.timeline_path
