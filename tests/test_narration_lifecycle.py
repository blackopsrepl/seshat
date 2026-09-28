from __future__ import annotations

import io
import os
import signal
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, media, narration, outputs, recording, server, streams, tts

from support import *

class VoiceoverLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.manager = server.RecordingManager()
        # Every take in this suite ran while a driver published one click, which
        # is what makes event ids available as narration anchors.
        publish(self.tmp.name, "driver", [500.0])

    def attach(self, job: recording.RecordingJob) -> None:
        job.result = {
            "id": job.id,
            "phase": "completed",
            "path": str(job.artifact),
            "audio_included": False,
        }
        self.manager._job = job

    def test_gif_narration_is_refused(self) -> None:
        job = completed_job(self.tmp.name, fmt="gif")
        self.attach(job)
        with self.assertRaises(server.ToolError) as ctx:
            self.manager.voiceover(
                {"segments": [{"anchor": {"at_ms": 0}, "text": "hi"}]}
            )
        self.assertIn("GIF cannot carry", str(ctx.exception))

    def test_narration_requires_completed_recording(self) -> None:
        job = completed_job(self.tmp.name)
        self.attach(job)
        job.phase = "recording"
        with self.assertRaises(server.ToolError):
            self.manager.voiceover({"segments": [{"anchor": {"at_ms": 0}, "text": "hi"}]})

    def test_voiceover_runs_async_and_updates_result(self) -> None:
        job = completed_job(self.tmp.name, events=[event(500.0)])
        self.attach(job)

        def fake_perform(j, request, engine):
            j.artifact.write_bytes(b"narrated")
            return {"engine": engine.name, "segment_count": len(request.segments)}

        fresh = {
            "codec": "av1",
            "container": "webm",
            "mime_type": "video/webm",
            "width": 640,
            "height": 360,
            "duration_seconds": 9.75,
            "frame_rate": 30.0,
        }
        with patch.object(tts, "resolve_tts_engine", return_value=server.EdgeTtsEngine()):
            with patch.object(narration, "perform_narration", side_effect=fake_perform):
                with patch.object(recording, "validate_recording_artifact", return_value=fresh):
                    with patch.object(narration, "recording_duration_ms", return_value=9000.0):
                        summary = self.manager.voiceover(
                            {"segments": [{"anchor": {"event_id": 1}, "text": "hi"}]}
                        )
                        self.assertEqual(summary["phase"], "narrating")
                        job.thread.join(timeout=5)
        final = self.manager.status()
        self.assertEqual(final["phase"], "completed")
        self.assertTrue(final["audio_included"])
        self.assertEqual(final["narration"]["engine"], "edge")
        self.assertEqual(final["bytes"], len(b"narrated"))
        self.assertEqual(final["duration_seconds"], 9.75)

    def test_narration_failure_reverts_to_completed_with_error(self) -> None:
        job = completed_job(self.tmp.name, events=[event(500.0)])
        self.attach(job)
        with patch.object(tts, "resolve_tts_engine", return_value=server.EdgeTtsEngine()):
            with patch.object(narration, "perform_narration", side_effect=server.ToolError("tts exploded")
            ):
                with patch.object(narration, "recording_duration_ms", return_value=9000.0):
                    self.manager.voiceover(
                        {"segments": [{"anchor": {"event_id": 1}, "text": "hi"}]}
                    )
                    job.thread.join(timeout=5)
        final = self.manager.status()
        self.assertEqual(final["phase"], "completed")
        self.assertIn("tts exploded", final["narration"]["error"])


class TimelineIngestionTests(unittest.TestCase):
    """Anchors come from published streams, never from seshat's own dispatch."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.manager = server.RecordingManager()

    def take(self, **arguments) -> server.RecordingJob:
        with patch.object(outputs, "get_outputs", return_value=single_output()):
            job = recording.new_recording_job({"max_duration_seconds": 30, **arguments})
        os.close(job.log_fd)
        job.started_monotonic = EPOCH_MONOTONIC
        job.ended_monotonic = EPOCH_MONOTONIC + 10.0
        job.phase = "completed"
        self.manager._job = job
        self.manager._jobs[job.id] = job
        return job

    def test_published_events_become_timeline_anchors(self) -> None:
        publish(self.tmp.name, "computer-use-sway", [1500.0, 4000.0])
        self.take()
        document = self.manager.timeline()
        self.assertEqual(document["event_count"], 2)
        self.assertEqual([event["t_ms"] for event in document["events"]], [1500.0, 4000.0])
        self.assertEqual([event["id"] for event in document["events"]], [1, 2])
        self.assertEqual(document["events"][0]["source"], "computer-use-sway")
        self.assertEqual(document["sources"][0]["source"], "computer-use-sway")
        self.assertEqual(document["sources"][0]["events"], 2)

    def test_events_outside_the_capture_window_are_not_anchors(self) -> None:
        publish(self.tmp.name, "computer-use-sway", [-500.0, 2000.0, 12000.0])
        self.take()
        document = self.manager.timeline()
        self.assertEqual([event["t_ms"] for event in document["events"]], [2000.0])

    def test_reading_the_timeline_twice_does_not_duplicate_events(self) -> None:
        publish(self.tmp.name, "computer-use-sway", [1000.0])
        self.take()
        self.manager.timeline()
        self.assertEqual(self.manager.timeline()["event_count"], 1)

    def test_malformed_lines_are_reported_not_fatal(self) -> None:
        path = publish(self.tmp.name, "computer-use-sway", [1000.0])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        self.take()
        document = self.manager.timeline()
        self.assertEqual(document["event_count"], 1)
        self.assertEqual(document["sources"][0]["malformed"], 1)

    def test_a_take_without_streams_reports_no_anchors(self) -> None:
        self.take()
        document = self.manager.timeline()
        self.assertEqual(document["event_count"], 0)
        self.assertEqual(document["sources"], [])

    def test_explicit_sources_replace_discovery(self) -> None:
        publish(self.tmp.name, "ignored", [1000.0])
        outside = Path(self.tmp.name) / "custom.jsonl"
        streams.append_event(
            "custom", "key", at_monotonic=EPOCH_MONOTONIC + 2.0, path=outside
        )
        self.take(timeline_sources=[str(outside)])
        document = self.manager.timeline()
        self.assertEqual([event["source"] for event in document["events"]], ["custom"])

    def test_explicit_sources_are_validated_when_the_take_starts(self) -> None:
        with self.assertRaises(server.ToolError):
            self.take(timeline_sources=["/nonexistent/stream.jsonl"])

if __name__ == "__main__":
    unittest.main()
