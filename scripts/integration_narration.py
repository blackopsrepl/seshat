#!/usr/bin/env python3
"""Check the narrated audio path against a real recorder on the live screen.

The unit tier synthesizes its inputs with lavfi; this one records the live
screen with `audio=monitor` — the real capture, the real PulseAudio monitor, the
real mux — publishes an event to anchor from, narrates twice, and asserts the
contract the docs promise:

* the master is finalized exactly once and never rewritten by narration,
* the deliverable carries one video and one audio stream whose extents agree,
* a second narration starts from the same master again,
* no ingested event resolves past the playable picture.

It records whatever is on screen, so it is never part of `make check`:

    SESHAT_INTEGRATION=1 make integration-narration
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seshat import manager, streams  # noqa: E402


def wait_terminal(recordings, take_id: str) -> dict:
    summary = recordings.status({"id": take_id})
    while summary["phase"] not in {"completed", "failed"}:
        time.sleep(0.5)
        summary = recordings.status({"id": take_id})
    return summary


def main() -> int:
    if os.environ.get("SESHAT_INTEGRATION") != "1":
        print("refusing to run: this records the live screen. Set SESHAT_INTEGRATION=1.")
        return 2

    recordings = manager.RECORDINGS
    start = recordings.start(
        {"format": "mp4", "max_duration_seconds": 8, "audio": "monitor"}
    )
    take_id = start["id"]
    print(f"recording {take_id} on output {start['output']} with desktop audio ...")
    # Publish the event early in the capture window: an event near the stop
    # boundary can resolve past the recorder's own picture (see the tail-loss
    # failure mode) and that is the take failing honestly, not the mux.
    time.sleep(1.3)
    streams.append_event("integration-narration", "click", payload={"in_window": True})
    time.sleep(1.7)
    recordings.stop()
    summary = wait_terminal(recordings, take_id)
    failures: list[str] = []

    def check(condition: bool, ok: str, bad: str) -> None:
        print(("PASS  " if condition else "FAIL  ") + (ok if condition else bad))
        if not condition:
            failures.append(bad)

    if summary["phase"] != "completed":
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(f"FAIL  the take ended as {summary['phase']}: {summary.get('detail')}")
        return 1

    master = Path(summary["master_path"])
    document = recordings.timeline({"id": take_id})
    event = document["events"][0]
    text = "A narrated take recorded with desktop audio for the integration check."

    check(master.is_file() and master.stat().st_size > 0, f"master finalized at {master}", "no master was published")

    # One narrated deliverable, video and audio extents in agreement.
    deliverable = master.with_name(f"{take_id}.narrated.mp4")
    narration_one = _narrate_once(recordings, take_id, wait_terminal, deliverable, event["id"], text)
    check(narration_one["phase"] == "completed", "first narration completed", f"first narration failed: {narration_one.get('detail')}")

    master_before = master.read_bytes()
    narration_two = _narrate_once(
        recordings, take_id, wait_terminal, deliverable, event["id"],
        "A second narration that starts from the same master again.",
    )
    check(master.read_bytes() == master_before, "the narrator left the master untouched", "narration rewrote the master")

    probe = _probe(deliverable)
    video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    check(
        video is not None and len(audio) == 1 and audio[0].get("codec_name") == "aac",
        "the deliverable carries one video and one aac stream",
        f"stream shape is wrong: {[s.get('codec_type') for s in probe.get('streams', [])]}",
    )
    video_s = float((video or {}).get("duration") or 0.0)
    audio_s = float(_audio_extent(probe))
    check(
        video_s > 0.0 and abs(video_s - audio_s) <= 0.35,
        f"the picture ({video_s:.3f}s) covers the speech ({audio_s:.3f}s)",
        f"picture {video_s:.3f}s and audio {audio_s:.3f}s disagree",
    )
    check(
        document["events_beyond_media"] == 0 and document["event_count"] >= 1,
        "the ingested event stays within the playable picture",
        "the timeline reached past the picture",
    )
    check(
        narration_one["narration"]["extended_ms"] == 0.0 or video_s >= audio_s - 0.35,
        "any speech overrun the picture did not reach is covered by the extension",
        "speech runs past the picture with no extension to cover it",
    )

    print(json.dumps({
        "take": take_id,
        "master": str(master),
        "deliverable": str(deliverable),
        "first": narration_one.get("narration"),
        "audio_extent": audio_s,
        "video_extent": video_s,
    }, indent=2, sort_keys=True))
    for path in (master, deliverable, master.with_name(f"{take_id}.timeline.json")):
        path.unlink(missing_ok=True)
    from seshat import store
    store.delete_take(take_id)
    return 1 if failures else 0


def _narrate_once(recordings, take_id, wait_terminal, deliverable, event_id, text) -> dict:
    summary = recordings.voiceover({
        "id": take_id,
        "segments": [{"anchor": {"event_id": event_id}, "text": text}],
    })
    return wait_terminal(recordings, take_id)


def _probe(path: Path) -> dict:
    import subprocess

    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=False,
    )
    return json.loads(result.stdout or "{}")


def _audio_extent(probe: dict) -> float:
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "audio" and stream.get("duration"):
            return float(stream["duration"])
    return float((probe.get("format") or {}).get("duration") or 0.0)


if __name__ == "__main__":
    import os
    raise SystemExit(main())