#!/usr/bin/env python3
"""Check the deadline path against a real recorder.

The unit suite mocks the recorder process and ffprobe, so it cannot see what a
deadline-stopped capture actually leaves behind. This script records the live
screen for a short deadline, publishes an event inside the take's own window, and
then asserts the properties a narrated take depends on:

* capture stops on its own deadline and the recorder exits cleanly,
* the playable picture reaches the stop boundary (continuous capture),
* the ingested timeline does not reach past the picture.

It records whatever is on screen, so it is never part of `make check`:

    SESHAT_INTEGRATION=1 make integration
    SESHAT_INTEGRATION=1 SESHAT_INTEGRATION_SECONDS=20 make integration

The picture cannot reach the stop request exactly: the recorder needs a moment
to attach to the compositor after launch, and that attach happens inside the
capture window. `SESHAT_INTEGRATION_TOLERANCE_SECONDS` (default 2.5) is the gap
treated as normal; a larger one means the recorder stopped producing frames
before it was asked to.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seshat import manager, streams  # noqa: E402

DEFAULT_TOLERANCE_SECONDS = 2.5


def publish_in_window_event(recordings, take_id: str, duration: float) -> float:
    """Publish an event inside the take's capture window, in the take's own time base.

    Sleeping for the deadline from outside would miss it: the take-relative clock
    starts when the recorder is launched, before recording_start returns. The
    status clock is the take's own, so the event lands inside the window rather
    than after it. The margin keeps the event clear of the stop boundary, where
    the last recorder frames and the event cannot be ordered reliably.
    """
    target = max(duration - 2.5, duration / 2.0)
    while True:
        elapsed = float(recordings.status({"id": take_id}).get("elapsed_seconds") or 0.0)
        if elapsed >= target:
            break
        time.sleep(min(0.25, max(target - elapsed, 0.05)))
    streams.append_event("integration-deadline", "click", payload={"in_window": True})
    return float(recordings.status({"id": take_id}).get("elapsed_seconds") or 0.0)


def main() -> int:
    if os.environ.get("SESHAT_INTEGRATION") != "1":
        print("refusing to run: this records the live screen. Set SESHAT_INTEGRATION=1.")
        return 2
    duration = float(os.environ.get("SESHAT_INTEGRATION_SECONDS", "6"))
    tolerance = float(
        os.environ.get("SESHAT_INTEGRATION_TOLERANCE_SECONDS", str(DEFAULT_TOLERANCE_SECONDS))
    )

    recordings = manager.RECORDINGS
    take = recordings.start({"format": "mp4", "max_duration_seconds": duration})
    take_id = take["id"]
    print(f"recording {take_id} on output {take['output']} for {duration:g}s ...")

    event_at = publish_in_window_event(recordings, take_id, duration)
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
        print(f"FAIL  the take ended as {summary['phase']}: {summary.get('detail')}")
        return 1

    artifact = Path(summary["path"])
    document = recordings.timeline({"id": take_id})
    capture = float(summary["capture_elapsed_seconds"])
    media = float(summary.get("media_duration_seconds") or 0.0)
    shutdown = summary.get("shutdown_latency_seconds")
    stage = summary.get("termination_stage")
    returncode = summary.get("recorder_returncode")

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
    check(
        stage != "sigkill" and returncode == 0,
        f"the recorder exited cleanly (stage={stage}, code={returncode})",
        f"the recorder did not exit cleanly (stage={stage}, code={returncode})",
    )
    check(
        isinstance(shutdown, (int, float)) and float(shutdown) >= 0.0,
        f"shutdown latency measured ({shutdown}s after the stop request)",
        "shutdown latency was not measured",
    )
    check(media > 0.0, f"the published video is {media:g}s long", "the video has no duration")
    check(
        capture <= duration + tolerance,
        f"capture window ({capture:g}s) matches the deadline ({duration:g}s)",
        f"the capture window ({capture:g}s) outran the deadline ({duration:g}s) by more than "
        f"{tolerance:g}s: non-capture time is inside it",
    )
    check(
        media >= capture - tolerance,
        f"the picture ({media:g}s) reaches the stop boundary ({capture:g}s within {tolerance:g}s)",
        f"the picture ({media:g}s) falls {capture - media:g}s short of the stop boundary "
        f"({capture:g}s): capture stopped before the recorder was asked to",
    )
    check(
        document["events_beyond_media"] == 0,
        f"every ingested event has a picture ({document['event_count']} event(s))",
        f"{document['events_beyond_media']} event(s) resolve past the playable picture, so they "
        "cannot be narration anchors",
    )
    check(
        document["event_count"] >= 1,
        "the published event was ingested",
        "the published event did not reach the timeline",
    )

    print(
        f"      capture_window={capture:g}s shutdown_latency={shutdown}s picture={media:g}s "
        f"recorder_attach_gap={capture - media:g}s event_at={event_at:g}s "
        f"latest_event_ms={document['latest_event_ms']} "
        f"events_beyond_media={document['events_beyond_media']}"
    )
    print(json.dumps({"take": take_id, "artifact": str(artifact), "summary": summary,
                      "timeline": {k: v for k, v in document.items() if k != "events"}},
                     indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
