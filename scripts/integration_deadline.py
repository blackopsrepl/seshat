#!/usr/bin/env python3
"""Check the auto-stop deadline path against a real recorder.

The unit suite mocks the recorder process and ffprobe, so it cannot see what a
deadline-stopped capture actually leaves behind. This script records the live
screen for a short deadline, publishes a late event, and then compares the three
numbers that a narrated take depends on: how long capture ran, how much video is
playable, and how far the ingested timeline reaches.

It records whatever is on screen, so it is never part of `make check`:

    SESHAT_INTEGRATION=1 make integration
    SESHAT_INTEGRATION=1 SESHAT_INTEGRATION_SECONDS=20 make integration
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seshat import manager, streams  # noqa: E402


def publish_late_event(recordings, take_id: str, duration: float) -> None:
    """Publish an event close to the deadline, in the take's own time base.

    Sleeping for the deadline from outside would miss it: take-relative time
    starts before recording_start returns (the encoder probe and the startup
    check happen first). elapsed_seconds is the take's own clock, so the event
    lands inside the capture window rather than after it.
    """
    target = max(duration - 1.0, 0.0)
    while True:
        elapsed = float(recordings.status({"id": take_id}).get("elapsed_seconds") or 0.0)
        if elapsed >= target:
            break
        time.sleep(min(0.25, max(target - elapsed, 0.05)))
    streams.append_event("integration-deadline", "click", payload={"late": True})


def report(document: dict, elapsed: float, media: float) -> None:
    print(
        f"      capture_elapsed={elapsed:g}s playable_media={media:g}s "
        f"events={document['event_count']} latest_event_ms={document['latest_event_ms']} "
        f"events_beyond_media={document['events_beyond_media']}"
    )


def main() -> int:
    if os.environ.get("SESHAT_INTEGRATION") != "1":
        print("refusing to run: this records the live screen. Set SESHAT_INTEGRATION=1.")
        return 2
    duration = float(os.environ.get("SESHAT_INTEGRATION_SECONDS", "6"))
    recordings = manager.RECORDINGS
    take = recordings.start({"format": "mp4", "max_duration_seconds": duration})
    take_id = take["id"]
    print(f"recording {take_id} on output {take['output']} for {duration:g}s ...")

    publish_late_event(recordings, take_id, duration)
    summary = recordings.status({"id": take_id})
    while summary["phase"] not in {"completed", "failed"}:
        time.sleep(0.5)
        summary = recordings.status({"id": take_id})

    failures: list[str] = []

    def check(condition: bool, ok: str, bad: str) -> None:
        print(("PASS  " if condition else "FAIL  ") + (ok if condition else bad))
        if not condition:
            failures.append(bad)

    if summary["phase"] != "completed":
        print(json.dumps(summary, indent=2, sort_keys=True))
        check(False, "", f"the take ended as {summary['phase']}: {summary.get('detail')}")
        return 1

    artifact = Path(summary["path"])
    document = recordings.timeline({"id": take_id})
    elapsed = float(summary["capture_elapsed_seconds"])
    media = float(summary.get("media_duration_seconds") or 0.0)

    check(
        artifact.is_file() and artifact.stat().st_size > 0,
        f"artifact written to {artifact}",
        "no artifact was written",
    )
    check(
        summary.get("auto_stopped") is True,
        "the take was stopped by its own deadline",
        "the deadline did not stop the take",
    )
    check(
        not summary["audio_included"],
        "the silent artifact carries no audio",
        "the silent artifact carries audio",
    )
    check(media > 0.0, f"the published video is {media:g}s long", "the video has no duration")
    check(
        elapsed + 0.001 >= media,
        f"capture elapsed ({elapsed:g}s) covers the media ({media:g}s)",
        f"the media ({media:g}s) outlasts capture ({elapsed:g}s)",
    )
    report(document, elapsed, media)
    if document["events_beyond_media"]:
        print(
            "WARN  the timeline reaches past the playable picture, so those event ids "
            "cannot be used as narration anchors (they are refused, not silently placed)"
        )
    print(json.dumps({"take": take_id, "artifact": str(artifact), "summary": summary,
                      "timeline": {k: v for k, v in document.items() if k != "events"}},
                     indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
