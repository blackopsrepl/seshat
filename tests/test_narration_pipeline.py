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

from seshat import core, encoding, media, narration, outputs, recording, server, streams, subtitles, tts

from support import *
class NarrationArgvTests(unittest.TestCase):
    def build(self):
        anchored = [
            (server.NarrationSegment(1, 1, None, None, 1000.0, "a"), 1000.0),
            (server.NarrationSegment(2, 2, None, None, 2000.0, "b"), 6000.0),
        ]
        clips = {1: clip("/tmp/1.mp3", 1200.0, lead_ms=100.0), 2: clip("/tmp/2.mp3", 800.0, words=3)}
        request = server.NarrationRequest(
            segments=[item[0] for item in anchored],
            engine="edge",
            voice=None,
            offset_ms=0.0,
            fit="compress",
            tail_ms=300.0,
        )
        schedule = server.schedule_narration(anchored, clips, 10000.0, request)
        return anchored, clips, schedule

    def test_track_argv_builds_mix_and_delays(self) -> None:
        anchored, clips, schedule = self.build()
        argv = server.narration_track_argv(
            anchored, clips, schedule, server.Path("/tmp/narration.wav")
        )
        joined = " ".join(argv)
        self.assertEqual(argv[0], "ffmpeg")
        self.assertIn("/tmp/1.mp3", argv)
        self.assertIn("/tmp/2.mp3", argv)
        self.assertIn("anullsrc=channel_layout=stereo", joined)
        self.assertIn("adelay=1000|1000", joined)
        self.assertIn("amix=inputs=3:normalize=0", joined)
        self.assertIn("aformat=channel_layouts=stereo", joined)
        self.assertIn("pcm_s16le", argv)
        self.assertEqual(argv[-1], "/tmp/narration.wav")

    def test_track_argv_uses_atempo_when_compressed(self) -> None:
        anchored = [
            (server.NarrationSegment(1, 1, None, None, 1000.0, "a"), 1000.0),
            (server.NarrationSegment(2, 2, None, None, 2000.0, "b"), 1700.0),
        ]
        clips = {1: clip("/tmp/1.mp3", 3000.0), 2: clip("/tmp/2.mp3", 200.0)}
        request = server.NarrationRequest(
            segments=[item[0] for item in anchored],
            engine="edge",
            voice=None,
            offset_ms=0.0,
            fit="compress",
            tail_ms=300.0,
        )
        schedule = server.schedule_narration(anchored, clips, 10000.0, request)
        self.assertTrue(schedule.segments[0].compressed)
        argv = server.narration_track_argv(
            anchored, clips, schedule, server.Path("/tmp/narration.wav")
        )
        self.assertIn("atempo", " ".join(argv))

    def test_mux_argv_copies_video_and_adds_opus(self) -> None:
        from types import SimpleNamespace

        job = SimpleNamespace(artifact=server.Path("/tmp/a.webm"), fmt="webm", encoder="libsvtav1")
        argv = encoding.narration_mux_argv(
            job, server.Path("/tmp/n.wav"), server.Path("/tmp/out.webm")
        )
        self.assertEqual(argv[:4], ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel"])
        self.assertIn("0:v:0", argv)
        self.assertIn("1:a:0", argv)
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertEqual(argv[argv.index("-c:a") + 1], "libopus")
        self.assertIn("-map_metadata", argv)
        self.assertEqual(argv[-2], "webm")
        self.assertEqual(argv[-1], "/tmp/out.webm")

    def test_mux_argv_mp4_uses_h264_and_aac(self) -> None:
        from types import SimpleNamespace

        job = SimpleNamespace(artifact=server.Path("/tmp/a.mp4"), fmt="mp4", encoder="libx264")
        argv = encoding.narration_mux_argv(
            job, server.Path("/tmp/n.wav"), server.Path("/tmp/out.mp4")
        )
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertEqual(argv[argv.index("-c:a") + 1], "aac")
        self.assertIn("+faststart", argv)
        self.assertEqual(argv[-1], "/tmp/out.mp4")

    def test_track_argv_with_bed_ducks_it_under_the_voice(self) -> None:
        anchored, clips, schedule = self.build()
        argv = server.narration_track_argv(
            anchored, clips, schedule, server.Path("/tmp/narration.wav"), bed_path=server.Path("/tmp/master.webm")
        )
        joined = " ".join(argv)
        self.assertIn("/tmp/master.webm", argv)
        self.assertIn("asplit", joined)
        self.assertIn("sidechaincompress=threshold=0.03", joined)
        self.assertIn("[vmix][ducked][pad]amix=inputs=3", joined)
        self.assertIn("[bedsrc]", joined)
        self.assertNotIn("[bed][", joined)

    def test_track_argv_without_bed_stays_voice_only(self) -> None:
        anchored, clips, schedule = self.build()
        argv = server.narration_track_argv(
            anchored, clips, schedule, server.Path("/tmp/narration.wav")
        )
        joined = " ".join(argv)
        self.assertNotIn("sidechaincompress", joined)
        self.assertIn("[voice][pad]amix=inputs=2", joined)

    def test_mux_argv_extends_the_picture_when_speech_overruns(self) -> None:
        from types import SimpleNamespace

        job = SimpleNamespace(artifact=server.Path("/tmp/a.webm"), fmt="webm", encoder="libsvtav1")
        argv = encoding.narration_mux_argv(
            job, server.Path("/tmp/n.wav"), server.Path("/tmp/out.webm"), extend_seconds=1.5
        )
        joined = " ".join(argv)
        self.assertIn("tpad=stop_mode=clone:stop_duration=1.500", joined)
        self.assertEqual(argv[argv.index("-c:v") + 1], "libsvtav1")
        self.assertIn("0:v:0", argv)
        self.assertIn("1:a:0", argv)
        self.assertNotIn("copy", argv)

    def test_mux_argv_pads_before_the_caption_burn(self) -> None:
        from types import SimpleNamespace

        job = SimpleNamespace(artifact=server.Path("/tmp/a.webm"), fmt="webm", encoder="libsvtav1")
        graph = subtitles.video_filter(
            server.Path("/tmp/x.ass"), "tpad=stop_mode=clone:stop_duration=0.250,"
        )
        argv = encoding.narration_mux_argv(
            job, server.Path("/tmp/n.wav"), server.Path("/tmp/out.webm"), graph
        )
        joined = " ".join(argv)
        self.assertEqual(argv[argv.index("-filter_complex") + 1], graph)
        self.assertIn("tpad=stop_mode=clone:stop_duration=0.250,subtitles=filename=", joined)
        self.assertEqual(argv[argv.index("-c:v") + 1], "libsvtav1")

    def test_video_filter_puts_the_pad_ahead_of_the_burn(self) -> None:
        self.assertEqual(
            subtitles.video_filter(server.Path("/tmp/x.ass")),
            "[0:v]subtitles=filename='/tmp/x.ass'[v]",
        )
        self.assertEqual(
            subtitles.video_filter(server.Path("/tmp/x.ass"), "tpad=stop_mode=clone,"),
            "[0:v]tpad=stop_mode=clone,subtitles=filename='/tmp/x.ass'[v]",
        )

    def test_perform_narration_reports_the_extension(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        job = completed_job(tmp.name, capture_seconds=1.0, events=[event(500.0)])
        request = narration.parse_narration_arguments(
            {"segments": [{"anchor": {"event_id": 1}, "text": "hello"}]},
            job,
            1000.0,
        )
        captured: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured.append(argv)
            if argv[-1].endswith(".part"):
                server.Path(argv[-1]).write_bytes(b"out")
            return core.CommandResult(stdout=b"", stderr=b"", returncode=0)

        class LongEngine:
            name = "edge"

            def synthesize(self, text, voice, out_path, character=None):
                out_path.write_bytes(b"audio")
                return server.TtsClip(out_path, 3000.0, 0.0, [])

        with patch.object(core, "run_command", side_effect=fake_run):
            with patch.object(recording, "validate_recording_artifact", return_value={}):
                metadata = narration.perform_narration(job, request, LongEngine())
        mux = captured[-1]
        # Clip starts at the 500 ms anchor and runs 3 s; +300 ms tail → 3800 ms.
        self.assertEqual(metadata["extended_ms"], 2800.0)
        self.assertIn("tpad=stop_mode=clone", " ".join(mux))


if __name__ == "__main__":
    unittest.main()
