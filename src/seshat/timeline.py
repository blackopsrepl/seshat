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


def timeline_document(job: recording.RecordingJob) -> dict[str, Any]:
    end = job.ended_monotonic if job.ended_monotonic is not None else time.monotonic()
    return {
        "id": job.id,
        "format": job.fmt,
        "output": job.output,
        "region": job.region,
        "started_utc": job.started_utc,
        "capture_seconds": round(max(end - job.started_monotonic, 0.0), 3),
        "event_count": len(job.events),
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
