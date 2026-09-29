from __future__ import annotations

import os
import signal
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import lifecycle, recording, server

from support import *


class RecordingLifecycleTests(RecordingManagerCase):
    """The take's clocks and how its recorder is stopped.

    These assertions are the regression net for the deadline defects: what time is
    counted as capture, when the capture window closes, and which recorder exits
    are allowed to become a published artifact.
    """

    def test_capture_clock_starts_when_recorder_launches(self) -> None:
        """The deadline clock is the recorder's, not the argument parser's.

        Encoder discovery and process launch happen before capture exists; counting
        them as capture time is what made a 210s deadline report 225s. The fake
        clock advances inside the Popen stub, so a clock taken during allocation can
        be told apart from one taken after the recorder is running.
        """
        now = [100.0]

        def fake_monotonic() -> float:
            value = now[0]
            now[0] += 1.0
            return value

        process = FakeProcess()

        def factory(argv, **kwargs):
            now[0] += 5.0
            process.argv = argv
            process.popen_kwargs = kwargs
            return process

        with patch.object(server.time, "monotonic", side_effect=fake_monotonic):
            with patch.object(server.subprocess, "Popen", side_effect=factory):
                summary = self.manager.start({"format": "mp4", "max_duration_seconds": 30})
        self.assertGreater(self.manager._job.started_monotonic, 100.0)
        self.assertEqual(self.manager._job.started_monotonic, 105.0)
        self.assertEqual(summary["phase"], "recording")
        self.manager._job.phase = "failed"

    def test_completed_result_separates_capture_time_from_playable_media(self) -> None:
        """Shutdown wait time is not playable capture and must not be reported as it."""
        process = self.start_ok()
        job = self.manager._job
        job.intermediate.write_bytes(b"capture-bytes")
        job.started_monotonic = time.monotonic() - 225.213
        summary = {
            "codec": "h264",
            "container": "mp4",
            "mime_type": "video/mp4",
            "width": 1920,
            "height": 1080,
            "media_duration_seconds": 204.233,
            "frame_rate": 30.0,
        }
        with exit_process_on_signal(process):
            with patch.object(recording, "finalize_recording", return_value=summary):
                self.manager.stop()
                final = self.wait_terminal()
        self.assertAlmostEqual(final["capture_elapsed_seconds"], 225.213, delta=0.05)
        self.assertEqual(final["media_duration_seconds"], 204.233)
        self.assertEqual(final["events_beyond_media"], 0)
        self.assertIsNone(final["latest_event_ms"])

    def test_stop_is_nonblocking_and_freezes_capture_at_request(self) -> None:
        process = self.start_ok()
        job = self.manager._job
        job.intermediate.write_bytes(b"capture-bytes")
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def terminate(_process):
            entered.set()
            # Long enough that a capture clock taken after the exit, rather than at
            # the stop request, would include it.
            exited = time.monotonic() + 3.0
            release.wait(timeout=5)
            process.returncode = 0
            return lifecycle.TerminationOutcome(
                stage="sigint",
                returncode=0,
                process_exited_monotonic=exited,
            )

        with patch.object(lifecycle, "terminate_process_group", side_effect=terminate):
            with patch.object(recording, "finalize_recording", return_value={"codec": "av1"}):
                summary = self.manager.stop()
                self.assertEqual(summary["phase"], "stopping")
                self.assertTrue(entered.wait(timeout=1))
                during = self.manager.status()
                self.assertEqual(during["phase"], "stopping")
                capture_elapsed = during["capture_elapsed_seconds"]
                release.set()
                final = self.wait_terminal()

        self.assertEqual(final["phase"], "completed")
        self.assertEqual(final["capture_elapsed_seconds"], capture_elapsed)
        self.assertAlmostEqual(final["shutdown_latency_seconds"], 3.0, delta=0.1)
        self.assertEqual(final["termination_stage"], "sigint")
        self.assertEqual(final["recorder_returncode"], 0)

    def test_sigkill_fails_before_finalization(self) -> None:
        process = self.start_ok()
        job = self.manager._job
        job.intermediate.write_bytes(b"capture-bytes")
        outcome = lifecycle.TerminationOutcome(
            stage="sigkill",
            returncode=-signal.SIGKILL,
            process_exited_monotonic=time.monotonic(),
        )
        with patch.object(lifecycle, "terminate_process_group", return_value=outcome):
            with patch.object(recording, "finalize_recording") as finalize:
                self.manager.stop()
                final = self.wait_terminal()
        self.assertEqual(final["phase"], "failed")
        self.assertIn("SIGKILL", final["detail"])
        self.assertEqual(final["termination_stage"], "sigkill")
        self.assertEqual(final["recorder_returncode"], -signal.SIGKILL)
        self.assertTrue(job.intermediate.exists())
        finalize.assert_not_called()

    def test_stop_escalation_to_sigkill_fails(self) -> None:
        process = self.start_ok()
        job = self.manager._job
        job.intermediate.write_bytes(b"capture-bytes")
        sent: list[int] = []

        def record(_pid: int, sig: int) -> None:
            sent.append(sig)
            if sig == signal.SIGKILL:
                process.returncode = -signal.SIGKILL

        terminate = lifecycle.terminate_process_group
        timeouts = ((signal.SIGINT, 0.0), (signal.SIGTERM, 0.0), (signal.SIGKILL, 0.0))
        with patch.object(
            lifecycle,
            "terminate_process_group",
            side_effect=lambda proc: terminate(proc, timeouts=timeouts),
        ):
            with patch.object(lifecycle.os, "killpg", side_effect=record):
                self.manager.stop()
                final = self.wait_terminal()
        self.assertEqual(final["phase"], "failed")
        self.assertEqual(final["termination_stage"], "sigkill")
        self.assertEqual(sent, [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])

    def test_failed_finalization_reports_the_retained_candidate(self) -> None:
        """A corrupt candidate is forensics: its path must be in the failure."""
        process = self.start_ok()
        job = self.manager._job
        job.intermediate.write_bytes(b"capture-bytes")
        candidate = job.artifact.with_name(job.artifact.name + ".part")
        candidate.write_bytes(b"corrupt-tail")

        def finalize(_job):
            raise server.ToolError("Invalid data found when processing input")

        with exit_process_on_signal(process):
            with patch.object(recording, "finalize_recording", side_effect=finalize):
                self.manager.stop()
                summary = self.wait_terminal()
        self.assertEqual(summary["phase"], "failed")
        self.assertIn("Invalid data found", summary["detail"])
        self.assertEqual(summary["candidate_path"], str(candidate))
        self.assertTrue(candidate.exists())
        self.assertTrue(job.intermediate.exists())

    def test_unexpected_shutdown_error_fails_instead_of_sticking(self) -> None:
        """A worker that dies silently would leave the take in `stopping` forever."""
        process = self.start_ok()
        job = self.manager._job
        job.intermediate.write_bytes(b"capture-bytes")
        with patch.object(lifecycle, "terminate_process_group", side_effect=OSError("boom")):
            self.manager.stop()
            summary = self.wait_terminal()
        self.assertEqual(summary["phase"], "failed")
        self.assertIn("recorder shutdown", summary["detail"])


if __name__ == "__main__":
    unittest.main()
