"""Audio capture: recording_start's audio argument and the silent contract.

A take stays silent unless one of ``audio=auto|monitor|mic`` is requested.
``monitor`` records the host's default output sink (its monitor source) — the
desktop's own sound. ``mic`` records the host's default input. Resolution
happens at job construction; the recorder argv only carries the concrete
PulseAudio source, so the silent argv is untouched when no audio is wanted.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, encoding, media, outputs, recording, specs

from support import single_output

PACTL_INFO = (
    "Server Name: PulseAudio (on PipeWire 1.4.6)\n"
    "Default Sink: alsa_output.pci-0000_00_1f.3.analog-stereo\n"
    "Default Source: alsa_input.usb-PreSonus_Studio_24c_SC1E21090873-00.analog-stereo\n"
)

real_run = core.run_command

MONITOR_NAME = "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"


def pactl_result(stdout: str) -> core.CommandResult:
    return core.CommandResult(stdout=stdout.encode("utf-8"), stderr=b"", returncode=0)


def run_command_or_pactl(args, **kwargs):
    """Test router: `pactl` goes to the audio fixture, anything else to `real_run`.

    `outputs.get_outputs` also goes through `core.run_command` (swaymsg /
    hyprctl), so audio-argument tests can patch only the `pactl` probes.
    """
    if args and args[0] == "pactl":
        return pactl_result(PACTL_INFO)
    return real_run(args, **kwargs)


def routable_pactl(stdout: str):
    """A router like :func:`run_command_or_pactl`, but `pactl` answers `stdout`."""

    def router(args, **kwargs):
        if args and args[0] == "pactl":
            return pactl_result(stdout)
        if args and args[:2] == ["ffmpeg", "-hide_banner"]:
            return core.CommandResult(
                stdout=(
                    " V....D libsvtav1            SVT-AV1 encoder (codec av1)\n"
                    " V....D libx264             libx264 H.264\n"
                ).encode("utf-8"),
                stderr=b"",
                returncode=0,
            )
        return real_run(args, **kwargs)

    return router


def new_job(fmt: str = "webm", **overrides) -> recording.RecordingJob:
    """Build a take job with outputs faked and `pactl` answering from a fixture.

    `pactl` is only ever consulted through patched `core.run_command`, so the
    suite cannot depend on the host it runs on having a default sink. The gif
    deadline (15 s cap) is pre-allowed so format tests fail on audio, not on
    the deadline.
    """
    arguments: dict = {"max_duration_seconds": 15 if fmt == "gif" else 30}
    arguments["format"] = fmt
    arguments.update(overrides)
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
            with patch.object(outputs, "get_outputs", return_value=single_output()):
                with patch.object(core, "run_command", side_effect=run_command_or_pactl):
                    job = recording.new_recording_job(arguments)
    os.close(job.log_fd)
    return job


class AudioContractTests(unittest.TestCase):
    def test_schema_audio_enum_and_default(self) -> None:
        spec = next(s for s in specs.tool_specs() if s["name"] == "recording_start")
        audio = spec["inputSchema"]["properties"]["audio"]
        self.assertEqual(audio["enum"], ["auto", "monitor", "mic", "off", None])
        self.assertEqual(audio["default"], "off")

    def test_off_is_explicit_but_matches_silence(self) -> None:
        self.assertEqual(recording.parse_recording_audio("off"), "off")
        self.assertFalse(recording.wants_audio("off"))

    def test_none_defaults_to_off(self) -> None:
        self.assertEqual(recording.parse_recording_audio(None), "off")

    def test_mic_is_not_the_default_mode(self) -> None:
        """The default artifact is silent; asking for a track is explicit."""
        self.assertNotEqual(recording.RECORDING_AUDIO_DEFAULT_MODE, "mic")

    def test_unknown_audio_is_rejected(self) -> None:
        for value in (
            "system",
            "desktop",
            "default",
            "monitor,mic",
            7,
            True,
            ["monitor"],
        ):
            with self.assertRaises(core.ToolError, msg=f"{value!r}"):
                recording.parse_recording_audio(value)


class AudioResolutionTests(unittest.TestCase):
    def test_monitor_resolves_the_default_sink_monitor(self) -> None:
        job = new_job(fmt="webm", audio="monitor")
        self.assertEqual(job.audio, "monitor")
        self.assertEqual(job.audio_source, MONITOR_NAME)

    def test_auto_resolves_like_monitor(self) -> None:
        job = new_job(fmt="webm", audio="auto")
        self.assertEqual(job.audio_source, MONITOR_NAME)

    def test_mic_leaves_the_default_source_unresolved(self) -> None:
        """`mic` means PulseAudio's default input; no concrete name is pinned."""
        job = new_job(fmt="webm", audio="mic")
        self.assertIsNone(job.audio_source)

    def test_mic_never_consults_pactl(self) -> None:
        with patch.object(
            core, "run_command", side_effect=AssertionError("pactl called")
        ):
            with self.assertRaises(AssertionError):
                recording.new_recording_job(
                    {"format": "webm", "max_duration_seconds": 30, "audio": "mic"}
                )

    def test_resolution_failure_is_a_clear_error(self) -> None:
        for stdout in ("no sink in here", "Default Sink:  ", ""):
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
                    with patch.object(
                        outputs, "get_outputs", return_value=single_output()
                    ):
                        with patch.object(
                            core, "run_command", side_effect=routable_pactl(stdout)
                        ):
                            with self.assertRaises(core.ToolError, msg=stdout):
                                recording.new_recording_job(
                                    {
                                        "format": "webm",
                                        "max_duration_seconds": 30,
                                        "audio": "monitor",
                                    }
                                )

    def test_gif_refuses_audio_at_job_construction(self) -> None:
        with self.assertRaises(core.ToolError) as ctx:
            new_job(fmt="gif", audio="monitor")
        self.assertIn("gif cannot carry audio", str(ctx.exception))

    def test_off_ignores_the_audio_subsystem(self) -> None:
        """Silent takes never consult the audio subsystem at all."""
        captured = []
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
                with patch.object(outputs, "get_outputs", return_value=single_output()):
                    with patch.object(
                        core,
                        "run_command",
                        side_effect=lambda argv, **kw: captured.append(argv[0])
                        or pactl_result(""),
                    ):
                        job = recording.new_recording_job(
                            {"format": "webm", "max_duration_seconds": 30}
                        )
        self.assertEqual([name for name in captured if name == "pactl"], [])
        self.assertIsNone(job.audio_source)


class CaptureArgvTests(unittest.TestCase):
    def test_silent_capture_argv_has_no_audio_flag(self) -> None:
        job = new_job(
            fmt="webm",
            region={"x": 10, "y": 20, "width": 800, "height": 600},
        )
        argv = encoding.recording_capture_argv(job)
        self.assertEqual(argv[0], "wf-recorder")
        self.assertNotIn("-a", argv)
        self.assertFalse(any("audio" in str(part).lower() for part in argv))

    def test_audio_monitor_extends_the_silent_argv(self) -> None:
        """Audio capture extends the silent argv; the video side is untouched."""
        def strip_paths(argv: list) -> list[str]:
            # the intermediate path is private per-job and is the only variable token
            return [str(part) for part in argv if "/seshat/recordings/" not in str(part)]

        silent = strip_paths(encoding.recording_capture_argv(new_job(fmt="webm")))
        argv = strip_paths(
            encoding.recording_capture_argv(new_job(fmt="webm", audio="monitor"))
        )
        self.assertEqual(silent + ["-a", MONITOR_NAME], argv)

    def test_audio_mic_appends_bare_a(self) -> None:
        """`mic` captures the host's PulseAudio default source (a microphone)."""
        argv = encoding.recording_capture_argv(new_job(fmt="webm", audio="mic"))
        self.assertEqual(argv[-1], "-a")

    def test_audio_argv_is_appended_not_interleaved(self) -> None:
        job = new_job(
            fmt="webm",
            region={"x": 0, "y": 0, "width": 1280, "height": 720},
            audio="monitor",
        )
        argv = encoding.recording_capture_argv(job)
        self.assertIn("-g", argv)
        self.assertEqual(argv[-2:], ["-a", MONITOR_NAME])


class ArtifactValidationTests(unittest.TestCase):
    """The silent contract is now conditional; the narrated shape is unchanged."""

    def webm_probe(self, audio: str | None = None, count: int = 1) -> dict:
        streams = [
            {
                "codec_type": "video",
                "codec_name": "av1",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "30/1",
            }
        ]
        if audio:
            for _ in range(count):
                streams.append({"codec_type": "audio", "codec_name": audio})
        return {
            "format": {"format_name": "matroska,webm", "duration": "12.5"},
            "streams": streams,
        }

    def test_silent_take_with_audio_streams_is_rejected(self) -> None:
        job = new_job(fmt="webm")
        with patch.object(media, "probe_media", return_value=self.webm_probe("opus")):
            with self.assertRaises(core.ToolError) as ctx:
                recording.validate_recording_artifact(job)
        self.assertIn("must not contain audio", str(ctx.exception))

    def test_audio_take_accepts_exactly_one_sound_stream(self) -> None:
        job = new_job(fmt="webm", audio="monitor")
        with patch.object(media, "probe_media", return_value=self.webm_probe("opus")):
            summary = recording.validate_recording_artifact(job)
        self.assertEqual(summary["codec"], "av1")

    def test_audio_take_still_rejects_wrong_audio_codec(self) -> None:
        job = new_job(fmt="webm", audio="monitor")
        with patch.object(media, "probe_media", return_value=self.webm_probe("vorbis")):
            with self.assertRaises(core.ToolError):
                recording.validate_recording_artifact(job)

    def test_gif_with_audio_is_rejected(self) -> None:
        job = new_job(fmt="gif")
        probe = {
            "format": {"format_name": "gif", "duration": "4.2"},
            "streams": [
                {"codec_type": "video", "codec_name": "gif", "width": 960, "height": 540},
                {"codec_type": "audio", "codec_name": "opus"},
            ],
        }
        with patch.object(media, "probe_media", return_value=probe):
            with self.assertRaises(core.ToolError):
                recording.validate_recording_artifact(job)


if __name__ == "__main__":
    unittest.main()

class FinalizeAudioArgvTests(unittest.TestCase):
    """A take that captured audio re-encodes that track during finalization."""

    def test_mp4_audio_capture_maps_and_encodes_one_sound_stream(self) -> None:
        job = new_job(fmt="mp4", audio="monitor")
        argv = encoding.recording_mp4_argv(job)
        self.assertNotIn("-an", argv)
        self.assertEqual(argv.count("-map"), 2)
        self.assertIn("-map", argv)
        self.assertIn("0:v:0", argv)
        self.assertIn("0:a:0", argv)
        self.assertEqual(argv[argv.index("-c:a") + 1], "aac")
        self.assertIn("-b:a", argv)
        self.assertIn("128k", argv)

    def test_webm_audio_capture_maps_and_encodes_one_sound_stream(self) -> None:
        job = new_job(fmt="webm", audio="monitor")
        argv = encoding.recording_webm_argv(job)
        self.assertNotIn("-an", argv)
        self.assertIn("0:v:0", argv)
        self.assertIn("0:a:0", argv)
        self.assertEqual(argv[argv.index("-c:a") + 1], "libopus")
        self.assertIn("96k", argv)

    def test_silent_finalize_keeps_an_and_no_maps(self) -> None:
        job = new_job(fmt="mp4")
        argv = encoding.recording_mp4_argv(job)
        self.assertIn("-an", argv)
        self.assertNotIn("0:a:0", argv)

    def test_gif_finalize_stays_silent(self) -> None:
        job = new_job(fmt="gif")
        argv = encoding.recording_gif_argv(job, capture_width=1600)
        self.assertIn("-an", argv)


class StartFailureHintTests(unittest.TestCase):
    """A recorder killed at launch with audio asked names the audio cause."""

    def _start_expecting_failure(self, arguments):
        from support import FakeProcess

        dead = FakeProcess(returncode=1, already_exited=True)
        log = "ffmpeg: no pulse audio device"
        from seshat import manager as manager_module

        try:
            manager = manager_module.RecordingManager()
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}):
                    with patch.object(outputs, "get_outputs", return_value=single_output()):
                        with patch.object(
                            core, "run_command", side_effect=routable_pactl(PACTL_INFO)
                        ):
                            with patch.object(
                                recording.subprocess,
                                "Popen",
                                return_value=dead,
                            ):
                                manager.start(arguments)
        except recording.core.ToolError as exc:
            return str(exc)
        return None

    def test_launch_failure_with_audio_mentions_the_audio_mode(self) -> None:
        message = self._start_expecting_failure(
            {"format": "mp4", "max_duration_seconds": 30, "audio": "monitor"}
        )
        self.assertIsNotNone(message)
        self.assertIn('audio="monitor"', message)

    def test_launch_failure_without_audio_stays_plain(self) -> None:
        message = self._start_expecting_failure({"format": "mp4", "max_duration_seconds": 30})
        self.assertIsNotNone(message)
        self.assertNotIn("audio", message)
