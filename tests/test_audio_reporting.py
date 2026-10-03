"""How a take that captured audio is reported.

``audio_included`` is a fact about the artifact, surfaced as soon as the take
asks for a track (phase ``recording``), not only after finalization.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, outputs, recording, reporting

from support import RecordingManagerCase, FakeProcess, exit_process_on_signal

from test_audio_capture import PACTL_INFO, routable_pactl


class ReportingAudioTests(unittest.TestCase):
    def make_job(self, audio: str) -> recording.RecordingJob:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
                with patch.object(outputs, "get_outputs", return_value=[{
                    "name": "DP-1",
                    "active": True,
                    "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                }]):
                    with patch.object(
                        core, "run_command", side_effect=routable_pactl(PACTL_INFO)
                    ):
                        job = recording.new_recording_job(
                            {"format": "webm", "max_duration_seconds": 30, "audio": audio}
                        )
        os.close(job.log_fd)
        return job

    def test_recording_phase_reports_audio_included_for_monitor(self) -> None:
        job = self.make_job("monitor")
        summary = reporting.phase_summary(job, intermediate_bytes=10)
        self.assertTrue(summary["audio_included"])

    def test_recording_phase_reports_silent_for_off(self) -> None:
        job = self.make_job("off")
        summary = reporting.phase_summary(job, intermediate_bytes=10)
        self.assertFalse(summary["audio_included"])


class ManagerFinalizeAudioTests(RecordingManagerCase):
    def finalize_ok(self) -> None:
        """Drive a start→stop→completed cycle with a stubbed finalization."""
        process = self.start_ok({"audio": "monitor", "max_duration_seconds": 30})
        job = self.manager._job
        job.intermediate.write_bytes(b"capture-bytes")
        with exit_process_on_signal(process):
            with patch.object(
                recording,
                "finalize_recording",
                side_effect=lambda j: {"codec": "av1", "media_duration_seconds": 2.0},
            ):
                self.manager.stop()
                final = self.wait_terminal()
        self.assertEqual(final["phase"], "completed")
        return final

    def test_completed_audio_take_reports_audio_included(self) -> None:
        final = self.finalize_ok()
        self.assertTrue(final["audio_included"])

    def test_completed_silent_take_stays_false(self) -> None:
        process = self.start_ok({"max_duration_seconds": 30})
        job = self.manager._job
        job.intermediate.write_bytes(b"capture-bytes")
        with exit_process_on_signal(process):
            with patch.object(
                recording,
                "finalize_recording",
                side_effect=lambda j: {"codec": "av1", "media_duration_seconds": 2.0},
            ):
                self.manager.stop()
                final = self.wait_terminal()
        self.assertFalse(final["audio_included"])


if __name__ == "__main__":
    unittest.main()