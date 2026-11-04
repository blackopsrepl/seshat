"""Real-ffmpeg media tier: the pipelines argv tests cannot prove.

The other tiers assert which arguments seshat builds; these run them and assert
on the produced media: stream counts, durations, loudness and caption presence.
Every input is synthesized with ffmpeg lavfi sources, so no screen, no
microphone and no TTS binary is involved. The module skips itself when
ffmpeg/ffprobe are absent, and runs in `make check` like the rest of the suite.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import (  # noqa: E402
    encoding,
    media,
    narration,
    outputs,
    recording,
    server,
    tts,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def output_rect() -> list[dict]:
    return [
        {"name": "DP-1", "active": True, "rect": {"x": 0, "y": 0, "width": 640, "height": 480}}
    ]


class SineEngine(tts.TtsEngine):
    """Deterministic stand-in TTS: a sine whose pitch and length follow the text."""

    name = "sine"

    def __init__(self, length_override_ms: float | None = None) -> None:
        self.length_override_ms = length_override_ms

    def synthesize(self, text: str, voice: str | None, out_path: Path, character: tts.TtsCharacter | None = None) -> tts.TtsClip:
        length_ms = self.length_override_ms or 500.0 + 100.0 * len(text)
        hz = 300 + (sum(text.encode()) % 40) * 10
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                f"sine=frequency={hz}:sample_rate=48000:duration={length_ms / 1000.0:.3f}",
                "-ac", "2", str(out_path),
            ],
            check=True,
            capture_output=True,
        )
        return tts.TtsClip(out_path, length_ms, 0.0, [])


def stream_types(path: Path) -> list[str]:
    probe = media.probe_media(path)
    return [str(s["codec_type"]) for s in probe.get("streams") or []]


def audio_volumes(path: Path, from_s: float, span: float) -> tuple[float, float]:
    """(mean, max) volume in dB of the audio between from_s and from_s + span."""
    report = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner",
            "-ss", f"{from_s:.3f}", "-t", f"{span:.3f}", "-i", str(path),
            "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True, text=True, check=False,
    ).stderr
    mean = maximum = None
    for line in report.splitlines():
        if "mean_volume" in line:
            mean = float(line.split("mean_volume:")[1].replace("dB", "").strip())
        if "max_volume" in line:
            maximum = float(line.split("max_volume:")[1].replace("dB", "").strip())
    return mean or -99.0, maximum or -99.0


def bottom_strip_mean(path: Path, from_s: float) -> float:
    """Mean luma of one frame's bottom quarter, as raw gray bytes."""
    probe = media.probe_media(path)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    height = int(video["height"]) or 1
    strip = max(height // 4, 1)
    raw = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", f"{from_s:.3f}", "-i", str(path),
            "-frames:v", "1",
            "-vf", f"crop=trunc(iw/2)*2:{strip}:0:ih-{strip}",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        check=True, capture_output=True,
    ).stdout
    if not raw:
        return 0.0
    return sum(raw) / len(raw)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not available")
class MediaPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)

    def new_job(self, fmt: str = "mp4", audio: str = "off") -> recording.RecordingJob:
        # The tier is about the media pipelines, not about pactl: the mode's
        # concrete source is pinned, standing in for `pactl info`'s answer.
        source = "test.monitor" if audio in ("monitor", "auto") else None
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.work)}):
            with patch.object(outputs, "get_outputs", return_value=output_rect()):
                with patch.object(
                    recording, "resolve_default_monitor_source", return_value=source
                ):
                    job = server.new_recording_job(
                        {"format": fmt, "max_duration_seconds": 30, "audio": audio}
                    )
        return job

    def synth_intermediate(
        self, job: recording.RecordingJob, *, seconds: float = 2.0, tone_hz: int | None = None
    ) -> None:
        """A libx264rgb mkv with the shape wf-recorder leaves behind."""
        argv = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={seconds:.3f}",
            "-f", "lavfi", "-i", f"sine=frequency={tone_hz or 1000}:sample_rate=48000:duration={seconds:.3f}",
            "-c:v", "libx264rgb", "-preset", "ultrafast", "-crf", "0",
            "-pix_fmt", "bgr24",
            "-c:a", "pcm_s16le",
            str(job.intermediate),
        ]
        if tone_hz is None:
            index = argv.index("-c:a")
            argv[index:index + 2] = []
        subprocess.run(argv, check=True, capture_output=True)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not available")
class FinalizePublishesMasterTests(MediaPipelineTests):
    def test_silent_master_never_carries_the_intermediates_audio(self) -> None:
        job = self.new_job()
        self.synth_intermediate(job, tone_hz=440)
        summary = recording.finalize_recording(job)
        job.artifact.chmod(recording.RECORDING_FILE_MODE)
        self.assertEqual(stream_types(job.artifact), ["video"])
        self.assertEqual(summary["codec"], "h264")

    def test_audio_capture_master_carries_one_aac_track(self) -> None:
        job = self.new_job(audio="monitor")
        self.synth_intermediate(job, tone_hz=1000)
        recording.finalize_recording(job)
        job.artifact.chmod(recording.RECORDING_FILE_MODE)
        self.assertEqual(stream_types(job.artifact), ["video", "audio"])
        mean, maximum = audio_volumes(job.artifact, 0.0, 1.0)
        self.assertGreater(maximum, -40.0)

    def test_recording_argv_is_the_shape_the_test_intermediate_mimics(self) -> None:
        job = self.new_job()
        argv = encoding.recording_capture_argv(job)
        self.assertIn("libx264rgb", argv)
        self.assertNotIn("-a", argv)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe not available")
class NarrationMediaTests(MediaPipelineTests):
    def narrate(self, job, request) -> dict:
        return narration.perform_narration(job, request, SineEngine())

    def make_request(self, job, segments) -> narration.NarrationRequest:
        return narration.parse_narration_arguments(
            {"segments": segments, "subtitles": False}, job
        )

    def test_narration_mixes_and_ducks_the_captured_bed(self) -> None:
        job = self.new_job(audio="monitor")
        self.synth_intermediate(job, tone_hz=1000)
        recording.finalize_recording(job)
        request = self.make_request(
            job, [{"anchor": {"at_ms": 800}, "text": "A voiced sentence."}]
        )
        meta = self.narrate(job, request)
        deliverable = job.narrated_path
        self.assertEqual(stream_types(deliverable), ["video", "audio"])
        # Before the anchor the bed plays alone; during speech the voice is on
        # top and the bed is ducked; before the anchor the bed is not ducked.
        bed_only, _ = audio_volumes(deliverable, 0.1, 0.5)
        speech, _ = audio_volumes(deliverable, 0.9, 0.9)
        self.assertGreater(bed_only, -45.0)
        self.assertGreater(speech, bed_only)
        # 2.3 s of speech over a 2 s master outruns it; the picture follows.
        self.assertGreaterEqual(meta["extended_ms"], 1000.0)
        video = next(s for s in media.probe_media(deliverable)["streams"] if s["codec_type"] == "video")
        self.assertGreater(float(video["duration"]), 3.3)

    def test_narration_republishes_from_an_unchanged_master(self) -> None:
        job = self.new_job()
        self.synth_intermediate(job, tone_hz=None)
        recording.finalize_recording(job)
        master = hashlib.sha256(job.artifact.read_bytes()).hexdigest()
        self.narrate(job, self.make_request(job, [
            {"anchor": {"at_ms": 200}, "text": "one voiced sentence here"}
        ]))
        first_duration = float(media.probe_media(job.narrated_path)["format"]["duration"])
        self.narrate(job, self.make_request(job, [
            {"anchor": {"at_ms": 200}, "text": "one voiced sentence here"}
        ]))
        again_duration = float(media.probe_media(job.narrated_path)["format"]["duration"])
        self.assertAlmostEqual(first_duration, again_duration, delta=0.2)
        self.assertEqual(hashlib.sha256(job.artifact.read_bytes()).hexdigest(), master)

    def test_picture_is_extended_when_the_voice_outlasts_it(self) -> None:
        job = self.new_job()
        self.synth_intermediate(job, seconds=1.0, tone_hz=None)
        recording.finalize_recording(job)
        request = self.make_request(job, [{"anchor": {"at_ms": 0}, "text": "speak"}])
        meta = narration.perform_narration(job, request, SineEngine(length_override_ms=4000.0))
        deliverable = job.narrated_path
        self.assertGreater(meta["extended_ms"], 3000.0)
        probe = media.probe_media(deliverable)
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        video_s = float(video["duration"])
        container_s = float(probe["format"]["duration"])
        self.assertGreater(video_s, 3.5)
        self.assertLess(abs(video_s - container_s), 0.5)

    def test_burned_captions_change_the_bottom_strip(self) -> None:
        job = self.new_job()
        self.synth_intermediate(job, tone_hz=None)
        recording.finalize_recording(job)
        master_mean = bottom_strip_mean(job.artifact, 0.1)
        request = narration.parse_narration_arguments(
            {"segments": [{"anchor": {"at_ms": 100}, "text": "A caption to burn."}]},
            job,
        )
        self.narrate(job, request)
        narrated_mean = bottom_strip_mean(job.narrated_path, 0.1)
        self.assertGreater(abs(narrated_mean - master_mean), 1.0)


if __name__ == "__main__":
    unittest.main()