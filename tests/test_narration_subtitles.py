from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, encoding, media, narration, outputs, recording, server, subtitles, tts

from support import completed_job, event


class _FakeEngine:
    name = "edge"
    default_voice = None
    network = False

    def synthesize(self, text: str, voice: str | None, out_path: Path, character: tts.TtsCharacter | None = None) -> tts.TtsClip:
        out_path.write_bytes(b"audio")
        return tts.TtsClip(out_path, 1000.0, 100.0, [{"text": "x", "start_ms": 0, "end_ms": 1}])


class TimestampTests(unittest.TestCase):
    def test_formats_ass_timestamps(self) -> None:
        self.assertEqual(subtitles.format_timestamp(0), "0:00:00.00")
        self.assertEqual(subtitles.format_timestamp(1500), "0:00:01.50")
        self.assertEqual(subtitles.format_timestamp(3_661_230), "1:01:01.23")

    def test_rounds_carries_into_seconds(self) -> None:
        self.assertEqual(subtitles.format_timestamp(999), "0:00:01.00")


class EscapeTests(unittest.TestCase):
    def test_escapes_override_syntax_and_newlines(self) -> None:
        self.assertEqual(
            subtitles.escape_text("a{b}\\c\nd"),
            "a\\{b\\}\\\\c\\Nd",
        )


class AssDocumentTests(unittest.TestCase):
    def test_builds_styled_dialogue(self) -> None:
        entries = [
            {"start_ms": 1000.0, "end_ms": 3500.0, "text": "First line"},
            {"start_ms": 3500.0, "end_ms": 6000.0, "text": "Second line"},
        ]
        document = subtitles.build_ass_document(entries, width=1920, height=1080)
        self.assertIn("PlayResX: 1920", document)
        self.assertIn("PlayResY: 1080", document)
        self.assertIn("Style: Caption,DejaVu Sans", document)
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:03.50,Caption,,0,0,0,,First line", document)
        self.assertIn("Dialogue: 0,0:00:03.50,0:00:06.00,Caption,,0,0,0,,Second line", document)

    def test_write_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = subtitles.write_subtitle_file(Path(tmp), "doc")
            self.assertEqual(path.name, subtitles.SUBTITLE_FILE_NAME)
            self.assertEqual(path.read_text(encoding="utf-8"), "doc")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), recording.RECORDING_FILE_MODE)


class NarratedDeliverableTests(unittest.TestCase):
    """Narration publishes a derived file and never touches the master."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = completed_job(self.tmp.name, events=[event(500.0)])

    def run_narration(self, arguments: dict) -> dict:
        request = narration.parse_narration_arguments(
            arguments, self.job, server.recording_capture_ms(self.job)
        )
        with patch.object(core, "run_command", side_effect=self._fake_run):
            with patch.object(recording, "validate_recording_artifact", return_value={}):
                return narration.perform_narration(self.job, request, _FakeEngine())

    @staticmethod
    def _fake_run(argv, **kwargs):
        if argv[-1].endswith(".part"):
            Path(argv[-1]).write_bytes(b"muxed")
        return core.CommandResult(stdout=b"", stderr=b"", returncode=0)

    def test_publishes_to_the_narrated_path_and_keeps_the_master(self) -> None:
        master_bytes = self.job.artifact.read_bytes()
        self.run_narration(
            {"segments": [{"anchor": {"event_id": 1}, "text": "hello"}]}
        )
        self.assertEqual(self.job.narrated_path.name, f"{self.job.id}.narrated.{self.job.fmt}")
        self.assertEqual(self.job.narrated_path.read_bytes(), b"muxed")
        self.assertEqual(self.job.artifact.read_bytes(), master_bytes)
        self.assertEqual(recording.published_artifact(self.job), self.job.narrated_path)

    def test_narration_probes_the_master_not_the_deliverable(self) -> None:
        with patch.object(
            media, "probe_duration_ms", return_value=5000.0
        ) as probe:
            narration.parse_narration_arguments(
                {"segments": [{"anchor": {"event_id": 1}, "text": "hello"}]}, self.job
            )
        self.assertEqual(probe.call_args[0][0], self.job.artifact)
        self.assertEqual(
            recording.published_artifact(self.job), self.job.artifact
        )


class MuxArgvTests(unittest.TestCase):
    FILTER = "[0:v]subtitles=filename='x'[v]"

    def test_webm_burn_reencodes_with_subtitles(self) -> None:
        from types import SimpleNamespace

        job = SimpleNamespace(artifact=Path("/tmp/a.webm"), fmt="webm", encoder="libsvtav1")
        argv = encoding.narration_mux_argv(
            job, Path("/tmp/n.wav"), Path("/tmp/out.webm"), self.FILTER
        )
        joined = " ".join(argv)
        self.assertIn("subtitles=filename=", joined)
        self.assertEqual(argv[argv.index("-c:v") + 1], "libsvtav1")
        self.assertEqual(argv[argv.index("-c:a") + 1], "libopus")
        self.assertEqual(argv[argv.index("-f") + 1], "webm")
        self.assertEqual(argv[-1], "/tmp/out.webm")

    def test_mp4_burn_uses_h264_and_aac(self) -> None:
        from types import SimpleNamespace

        job = SimpleNamespace(artifact=Path("/tmp/a.mp4"), fmt="mp4", encoder="libx264")
        argv = encoding.narration_mux_argv(
            job, Path("/tmp/n.wav"), Path("/tmp/out.mp4"), self.FILTER
        )
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertEqual(argv[argv.index("-c:a") + 1], "aac")
        self.assertIn("+faststart", argv)
        self.assertEqual(argv[-1], "/tmp/out.mp4")

    def test_copy_argv_stays_a_stream_copy(self) -> None:
        from types import SimpleNamespace

        job = SimpleNamespace(artifact=Path("/tmp/a.webm"), fmt="webm", encoder="libsvtav1")
        argv = encoding.narration_mux_argv(job, Path("/tmp/n.wav"), Path("/tmp/out.webm"))
        self.assertEqual(argv[argv.index("-c:v") + 1], "copy")
        self.assertNotIn("subtitles=filename=", " ".join(argv))


class PerformNarrationSubtitlesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = completed_job(self.tmp.name, events=[event(500.0)])

    def run_narration(self, arguments: dict) -> tuple[dict, list[list[str]]]:
        request = narration.parse_narration_arguments(
            arguments, self.job, server.recording_capture_ms(self.job)
        )
        captured: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured.append(argv)
            if argv[-1].endswith(".part"):
                Path(argv[-1]).write_bytes(b"muxed")
            return core.CommandResult(stdout=b"", stderr=b"", returncode=0)

        with patch.object(core, "run_command", side_effect=fake_run):
            with patch.object(recording, "validate_recording_artifact", return_value={}):
                metadata = narration.perform_narration(self.job, request, _FakeEngine())
        return metadata, captured

    def test_default_burns_subtitles(self) -> None:
        metadata, captured = self.run_narration(
            {"segments": [{"anchor": {"event_id": 1}, "text": "hello"}]}
        )
        self.assertTrue(metadata["subtitles"])
        self.assertTrue(any("subtitles=filename=" in " ".join(argv) for argv in captured))

    def test_disabled_copies_video(self) -> None:
        metadata, captured = self.run_narration(
            {"segments": [{"anchor": {"event_id": 1}, "text": "hello"}], "subtitles": False}
        )
        self.assertFalse(metadata["subtitles"])
        mux = captured[-1]
        self.assertEqual(mux[mux.index("-c:v") + 1], "copy")



class PhraseCaptionTests(unittest.TestCase):
    """Captions are phrases timed to the engine's cues, never solid blocks."""

    def test_cue_matched_phrases_take_the_cue_window(self) -> None:
        cues = [
            {"text": "First sentence is seen.", "start_ms": 200.0, "end_ms": 1800.0},
            {"text": "Second sentence is heard.", "start_ms": 1800.0, "end_ms": 3000.0},
        ]
        text = "First sentence is seen. Second sentence is heard."
        entries = server.scheduling.phrase_caption_entries(
            text, 1000.0, 2800.0, 100.0, 1.0, cues
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["text"], "First sentence is seen.")
        # The cue window maps through the lead-silence trim (200-100) onto the
        # segment's start at 1000 ms.
        self.assertEqual(entries[0]["start_ms"], 1100.0)
        self.assertEqual(entries[0]["end_ms"], 2700.0)
        self.assertEqual(entries[1]["text"], "Second sentence is heard.")

    def test_long_text_splits_and_shares_time_proportionally(self) -> None:
        long_text = " ".join(["word"] * 40)
        entries = server.scheduling.phrase_caption_entries(
            long_text, 0.0, 2000.0, 0.0, 1.0, []
        )
        self.assertGreater(len(entries), 1)
        self.assertEqual(entries[0]["start_ms"], 0.0)
        self.assertGreater(entries[-1]["end_ms"], entries[0]["end_ms"])
        self.assertTrue(all("word" in e["text"] for e in entries))

    def test_split_phrases_respects_a_broad_limit(self) -> None:
        phrases = server.scheduling.split_phrases("A short one. And another sentence here.")
        self.assertEqual(phrases, ["A short one.", "And another sentence here."])

    def test_tempo_scales_cue_timing(self) -> None:
        cues = [{"text": "Sentence.", "start_ms": 1000.0, "end_ms": 2000.0}]
        entries = server.scheduling.phrase_caption_entries(
            "Sentence.", 0.0, 1000.0, 0.0, 2.0, cues
        )
        self.assertEqual(entries[0]["start_ms"], 500.0)

    def test_top_alignment_and_sidecars(self) -> None:
        captions = [
            {"start_ms": 1000.0, "end_ms": 3000.0, "text": "Bottom one", "index": 1},
            {"start_ms": 3000.0, "end_ms": 4000.0, "text": "Top one", "index": 2, "align": "top"},
        ]
        document = subtitles.build_ass_document(captions, 1920, 1080)
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:03.00,Caption,,0,0,0,,Bottom one", document)
        self.assertIn("Dialogue: 0,0:00:03.00,0:00:04.00,Caption,,0,0,0,,{\\an8}Top one", document)
        srt = subtitles.build_srt_document(captions)
        self.assertIn("1\n00:00:01,000 --> 00:00:03,000\nBottom one", srt)
        vtt = subtitles.build_vtt_document(captions)
        self.assertIn("00:00:03.000 --> 00:00:04.000\nline:0\nTop one", vtt)

    def test_soft_sidecars_land_beside_the_deliverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            deliverable = Path(tmp) / "abc123.narrated.mp4"
            deliverable.write_bytes(b"")
            from types import SimpleNamespace as _NS
            job = _NS(id="abc123", fmt="mp4")
            written = subtitles.soft_caption_paths(
                job, deliverable,
                [{"start_ms": 0, "end_ms": 900.0, "text": "Hi", "index": 1}],
            )
            names = {w["format"]: Path(w["path"]).name for w in written}
            self.assertEqual(names, {"srt": "abc123.narrated.srt", "vtt": "abc123.narrated.vtt"})
            self.assertIn("00:00:00,000 --> 00:00:00,900", Path(written[0]["path"]).read_text())


if __name__ == "__main__":
    unittest.main()
