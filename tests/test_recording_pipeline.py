from __future__ import annotations

import io
import os
import signal
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, media, narration, outputs, recording, server, tts

from support import *
class ProbeAndValidateTests(unittest.TestCase):
    def webm_probe(self, codec: str = "av1", audio: bool = False, duration: str = "12.5") -> dict:
        streams = [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "30/1",
            }
        ]
        if audio:
            streams.append({"codec_type": "audio", "codec_name": "opus"})
        return {"format": {"format_name": "matroska,webm", "duration": duration}, "streams": streams}

    def mp4_probe(
        self, codec: str = "h264", audio: str | None = None, duration: str = "12.5"
    ) -> dict:
        streams = [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "30/1",
            }
        ]
        if audio:
            streams.append({"codec_type": "audio", "codec_name": audio})
        return {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": duration},
            "streams": streams,
        }

    def gif_probe(self, audio: bool = False) -> dict:
        streams = [
            {
                "codec_type": "video",
                "codec_name": "gif",
                "width": 960,
                "height": 540,
                "avg_frame_rate": "12/1",
            }
        ]
        if audio:
            streams.append({"codec_type": "audio", "codec_name": "opus"})
        return {"format": {"format_name": "gif", "duration": "4.2"}, "streams": streams}

    def make_job(self, fmt: str = "webm") -> recording.RecordingJob:
        duration = 10 if fmt == "gif" else 30
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
                with patch.object(outputs, "get_outputs", return_value=single_output()):
                    job = recording.new_recording_job(
                        {"format": fmt, "max_duration_seconds": duration}
                    )
        os.close(job.log_fd)
        return job

    def test_probe_media_parses_json(self) -> None:
        with patch.object(core, "run_command") as run:
            run.return_value.text = '{"format": {}, "streams": []}'
            probe = server.probe_media(server.Path("/tmp/x.webm"))
        self.assertEqual(probe, {"format": {}, "streams": []})

    def test_probe_media_rejects_invalid_json(self) -> None:
        with patch.object(core, "run_command") as run:
            run.return_value.text = "not json"
            with self.assertRaises(server.ToolError):
                server.probe_media(server.Path("/tmp/x.webm"))

    def test_full_decode_checks_every_video_frame(self) -> None:
        with patch.object(core, "run_command") as run:
            media.validate_full_video_decode(server.Path("/tmp/take.mp4"))
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "ffmpeg")
        self.assertIn("-xerror", argv)
        self.assertEqual(argv[-3:], ["-f", "null", "-"])

    def test_valid_webm_artifact_passes(self) -> None:
        job = self.make_job("webm")
        with patch.object(media, "probe_media", return_value=self.webm_probe()):
            summary = server.validate_recording_artifact(job)
        self.assertEqual(summary["codec"], "av1")
        self.assertEqual(summary["container"], "webm")
        self.assertEqual(summary["mime_type"], "video/webm")
        self.assertEqual(summary["width"], 1280)
        self.assertEqual(summary["media_duration_seconds"], 12.5)
        self.assertEqual(summary["frame_rate"], 30.0)

    def test_webm_artifact_rejections(self) -> None:
        job = self.make_job("webm")
        cases = [
            self.webm_probe(codec="h264"),
            self.webm_probe(audio=True),
            {"format": {"format_name": "matroska", "duration": "1"}, "streams": []},
            self.gif_probe(),
        ]
        for probe in cases:
            with patch.object(media, "probe_media", return_value=probe):
                with self.assertRaises(server.ToolError, msg=str(probe)[:60]):
                    server.validate_recording_artifact(job)

    def test_zero_duration_is_rejected(self) -> None:
        job = self.make_job("webm")
        probe = self.webm_probe(duration="0")
        with patch.object(media, "probe_media", return_value=probe):
            with self.assertRaises(server.ToolError):
                server.validate_recording_artifact(job)

    def test_valid_gif_artifact_passes(self) -> None:
        job = self.make_job("gif")
        with patch.object(media, "probe_media", return_value=self.gif_probe()):
            summary = server.validate_recording_artifact(job)
        self.assertEqual(summary["codec"], "gif")
        self.assertEqual(summary["container"], "gif")
        self.assertEqual(summary["mime_type"], "image/gif")

    def test_gif_with_audio_is_rejected(self) -> None:
        job = self.make_job("gif")
        with patch.object(media, "probe_media", return_value=self.gif_probe(audio=True)):
            with self.assertRaises(server.ToolError):
                server.validate_recording_artifact(job)

    def test_valid_mp4_artifact_passes(self) -> None:
        job = self.make_job("mp4")
        with patch.object(media, "probe_media", return_value=self.mp4_probe()):
            summary = server.validate_recording_artifact(job)
        self.assertEqual(summary["codec"], "h264")
        self.assertEqual(summary["container"], "mp4")
        self.assertEqual(summary["mime_type"], "video/mp4")

    def test_mp4_artifact_rejections(self) -> None:
        job = self.make_job("mp4")
        cases = [
            self.mp4_probe(codec="av1"),
            self.mp4_probe(audio="aac"),
            self.webm_probe(),
        ]
        for probe in cases:
            with patch.object(media, "probe_media", return_value=probe):
                with self.assertRaises(server.ToolError, msg=str(probe)[:60]):
                    server.validate_recording_artifact(job)

    def test_mp4_expect_audio_accepts_aac(self) -> None:
        job = self.make_job("mp4")
        with patch.object(media, "probe_media", return_value=self.mp4_probe(audio="aac")):
            summary = server.validate_recording_artifact(job, expect_audio=True)
        self.assertEqual(summary["codec"], "h264")
        with patch.object(media, "probe_media", return_value=self.mp4_probe(audio="opus")):
            with self.assertRaises(server.ToolError):
                server.validate_recording_artifact(job, expect_audio=True)


class FinalizeRecordingTests(unittest.TestCase):
    def unreadable_intermediate_job(self, tmpdir: str, auto_stopped: bool):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
            with patch.object(outputs, "get_outputs", return_value=single_output()):
                job = recording.new_recording_job({"max_duration_seconds": 10})
        os.close(job.log_fd)
        job.auto_stopped = auto_stopped
        return job

    def test_unreadable_intermediate_names_the_deadline(self) -> None:
        """A deadline-stopped recorder can leave a tail nobody wrote; say so."""
        with tempfile.TemporaryDirectory() as tmpdir:
            job = self.unreadable_intermediate_job(tmpdir, auto_stopped=True)
            with patch.object(
                media, "probe_video_stream", side_effect=server.ToolError("End of file")
            ):
                with self.assertRaises(server.ToolError) as ctx:
                    server.finalize_recording(job)
        message = str(ctx.exception)
        self.assertIn("unreadable intermediate", message)
        self.assertIn("deadline", message)
        self.assertIn("End of file", message)

    def test_unreadable_intermediate_without_a_deadline_stays_plain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            job = self.unreadable_intermediate_job(tmpdir, auto_stopped=False)
            with patch.object(
                media, "probe_video_stream", side_effect=server.ToolError("End of file")
            ):
                with self.assertRaises(server.ToolError) as ctx:
                    server.finalize_recording(job)
        message = str(ctx.exception)
        self.assertIn("unreadable intermediate", message)
        self.assertNotIn("deadline", message)

    def test_finalize_publishes_only_after_candidate_decodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            job = self.unreadable_intermediate_job(tmpdir, auto_stopped=False)
            job.intermediate.write_bytes(b"capture")
            capture = {"codec_type": "video", "width": 1280, "height": 720}
            candidate = job.artifact.with_name(job.artifact.name + ".part")

            def run(argv, timeout):
                if argv[0] == "ffmpeg" and argv[-1] == str(candidate):
                    candidate.write_bytes(b"validated-candidate")
                return None

            summary = {"codec": "h264", "media_duration_seconds": 1.0}
            with patch.object(media, "probe_video_stream", return_value=capture):
                with patch.object(core, "run_command", side_effect=run) as command:
                    with patch.object(
                        recording, "validate_recording_artifact", return_value=summary
                    ) as validate:
                        result = recording.finalize_recording(job)

            self.assertEqual(result, summary)
            self.assertEqual(job.artifact.read_bytes(), b"validated-candidate")
            self.assertFalse(candidate.exists())
            validate.assert_called_once_with(job, path=candidate)
            self.assertEqual(command.call_count, 2)

    def test_decode_failure_retains_candidate_without_publishing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            job = self.unreadable_intermediate_job(tmpdir, auto_stopped=False)
            job.intermediate.write_bytes(b"capture")
            capture = {"codec_type": "video", "width": 1280, "height": 720}
            candidate = job.artifact.with_name(job.artifact.name + ".part")

            def run(argv, timeout):
                if argv[-1] == str(candidate):
                    candidate.write_bytes(b"corrupt-tail")
                    return None
                raise server.ToolError("Invalid data found when processing input")

            with patch.object(media, "probe_video_stream", return_value=capture):
                with patch.object(core, "run_command", side_effect=run):
                    with patch.object(
                        recording,
                        "validate_recording_artifact",
                        return_value={"codec": "h264"},
                    ):
                        with self.assertRaises(server.ToolError):
                            recording.finalize_recording(job)

            self.assertEqual(job.artifact.read_bytes(), b"")
            self.assertEqual(candidate.read_bytes(), b"corrupt-tail")

    def test_finalize_converts_then_validates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
                with patch.object(outputs, "get_outputs", return_value=single_output()):
                    job = recording.new_recording_job(
                        {"format": "webm", "max_duration_seconds": 30}
                    )
            os.close(job.log_fd)
            job.encoder = "libsvtav1"
            capture_probe = {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1280,
                        "height": 720,
                    }
                ]
            }
            artifact_probe = {
                "format": {"format_name": "matroska,webm", "duration": "9.9"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "av1",
                        "width": 1280,
                        "height": 720,
                        "avg_frame_rate": "30/1",
                    }
                ],
            }
            candidate = job.artifact.with_name(job.artifact.name + ".part")

            def run_command(argv, timeout):
                if argv[-1] == str(candidate):
                    candidate.write_bytes(b"artifact")
                return None

            with patch.object(media, "probe_media", side_effect=[capture_probe, artifact_probe]
            ) as probe:
                with patch.object(core, "run_command", side_effect=run_command) as run:
                    summary = server.finalize_recording(job)

            self.assertEqual(probe.call_count, 2)
            self.assertEqual(run.call_count, 2)
            ffmpeg_argv = run.call_args_list[0].args[0]
            self.assertEqual(ffmpeg_argv[0], "ffmpeg")
            self.assertEqual(ffmpeg_argv[-1], str(candidate))
            self.assertIn("libsvtav1", ffmpeg_argv)
            self.assertEqual(summary["media_duration_seconds"], 9.9)

    def test_finalize_gif_uses_capture_width(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
                with patch.object(outputs, "get_outputs", return_value=single_output()):
                    job = recording.new_recording_job(
                        {"format": "gif", "max_duration_seconds": 10}
                    )
            os.close(job.log_fd)
            capture_probe = {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                    }
                ]
            }
            artifact_probe = {
                "format": {"format_name": "gif", "duration": "5"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "gif",
                        "width": 960,
                        "height": 540,
                        "avg_frame_rate": "12/1",
                    }
                ],
            }
            candidate = job.artifact.with_name(job.artifact.name + ".part")

            def run_command(argv, timeout):
                if argv[-1] == str(candidate):
                    candidate.write_bytes(b"artifact")
                return None

            with patch.object(media, "probe_media", side_effect=[capture_probe, artifact_probe]
            ):
                with patch.object(core, "run_command", side_effect=run_command) as run:
                    server.finalize_recording(job)
            ffmpeg_argv = run.call_args_list[0].args[0]
            self.assertIn("scale=960:-1:flags=lanczos", " ".join(ffmpeg_argv))


if __name__ == "__main__":
    unittest.main()
