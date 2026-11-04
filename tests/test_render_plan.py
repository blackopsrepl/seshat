"""Unit tests for the render plan, its argv, and the render worker's contract."""

import os
import time
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, manager as manager_module, media, narration, recording, render, render_ffmpeg, workers  # noqa: E402
from support import completed_job, event  # noqa: E402


class EditPlanTests(unittest.TestCase):
    def build_edit(self, tmp: str, events, plan, duration_ms=20_000.0):
        job = completed_job(tmp, events=events)
        with patch.object(narration, "recording_duration_ms", return_value=duration_ms):
            return render.compute_edit(job, plan)

    def test_trim_uses_the_first_and_last_event_plus_pad(self) -> None:
        with TemporaryDirectory() as tmp:
            edit = self.build_edit(
                tmp,
                [event(3_000 + i * 1_000) for i in range(4)],
                {"trim": {"pad_ms": 500.0}},
            )
            self.assertEqual(edit.trim_start_ms, 2_500.0)
            self.assertEqual(edit.trim_end_ms, 6_500.0)
            self.assertEqual(len(edit.segments), 1)
            self.assertEqual(edit.segments[0].speed, 1.0)
            self.assertEqual(len(edit.events), 4)
            first, last = edit.events[0], edit.events[-1]
            self.assertEqual(first.t_ms, 3_000.0)
            self.assertEqual(first.out_ms, 500.0)
            self.assertEqual(last.out_ms, 3_500.0)

    def test_idle_speeds_only_the_long_gaps(self) -> None:
        with TemporaryDirectory() as tmp:
            edit = self.build_edit(
                tmp,
                [event(1_000.0), event(2_000.0), event(16_000.0)],
                {"idle": {"min_gap_ms": 5_000.0, "speed": 4.0}},
            )
            speeds = [s.speed for s in edit.segments]
            self.assertEqual(speeds, [1.0, 4.0, 1.0])
            self.assertEqual(len(edit.segments), 3)
            first = edit.events[0]
            middle = edit.events[1]
            last = edit.events[2]
            self.assertEqual(first.out_ms, 1_000.0)
            self.assertEqual(middle.out_ms, 2_000.0)
            self.assertEqual(last.out_ms, 2_000.0 + 14_000.0 / 4.0)

    def test_zoom_windows_and_markers_carry_the_payload_points(self) -> None:
        with TemporaryDirectory() as tmp:
            edit = self.build_edit(
                tmp,
                [
                    event(1_000.0, payload={"x": 100, "y": 200}),
                    event(2_000.0, payload={"x": 1920 - 8, "y": 1080 - 8}),
                ],
                {
                    "zoom": {"scale": 3.0, "hold_ms": 600.0},
                    "markers": True,
                },
            )
            self.assertEqual(len(edit.zoom_windows), 2)
            window = edit.zoom_windows[1]
            self.assertEqual(window.scale, 3.0)
            self.assertEqual(window.x, 1_912.0)
            self.assertEqual(window.y, 1_072.0)
            self.assertEqual(len(edit.markers), 2)
            self.assertEqual(edit.markers[0]["t_ms"], edit.events[0].out_ms)

    def test_zoom_without_coordinates_is_a_clear_error(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(core.ToolError) as ctx:
                self.build_edit(
                    tmp,
                    [event(1_000.0, payload={"text": "no point here"})],
                    {"zoom": {"scale": 2.0}},
                )
            self.assertIn("coordinates (payload x/y)", str(ctx.exception))

    def test_timeline_operations_without_events_are_refused(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(core.ToolError) as ctx:
                self.build_edit(tmp, [], {"trim": {"pad_ms": 100.0}})
            self.assertIn("no ingested events", str(ctx.exception))
            profile_only = self.build_edit(tmp, [], {"profile": "1080p"})
            self.assertEqual(profile_only.profile, "1080p")
            self.assertEqual(profile_only.events, [])

    def test_chapter_list_and_derived_frames(self) -> None:
        with TemporaryDirectory() as tmp:
            edit = self.build_edit(
                tmp,
                [
                    event(1_000.0, payload={"chapter": "Install"}),
                    event(5_000.0, payload={"chapter": "Demo"}),
                    event(7_000.0),
                ],
                {"chapters": True, "markers": True},
            )
            # Untrimmed, one-second-in: the render's chapter marks match the
            # source event times.
            self.assertEqual(
                render.chapter_list(edit), [(1_000.0, "Install"), (5_000.0, "Demo")]
            )
            self.assertEqual(render.poster_t_ms(edit), 1_150.0)
        with TemporaryDirectory() as tmp:
            edit = self.build_edit(
                tmp,
                [
                    event(1_000.0, payload={"x": 480, "y": 540}),
                    event(2_000.0, payload={"x": 960, "y": 540}),
                ],
                {"zoom": {"scale": 2.0, "hold_ms": 800.0}},
            )
            # Poster parks inside the first zoom hold: 1.0s + 0.8s/2.
            self.assertEqual(render.poster_t_ms(edit), 1_400.0)
            start, end = render.teaser_span_ms(edit, 8_000.0)
            self.assertEqual((start, end), (0.0, 6_000.0))


class ParseRenderArgumentsTests(unittest.TestCase):
    def test_unknown_operation_is_rejected(self) -> None:
        with self.assertRaises(core.ToolError):
            render.parse_render_arguments({"speedup": {"speed": 2.0}})

    def test_idle_speed_at_or_below_one_is_rejected(self) -> None:
        with self.assertRaises(core.ToolError):
            render.parse_render_arguments({"idle": {"speed": 1.0}})

    def test_profile_names_are_the_supported_set(self) -> None:
        with self.assertRaises(core.ToolError):
            render.parse_render_arguments({"profile": "square"})

    def test_stitch_must_be_a_list_of_ids(self) -> None:
        with self.assertRaises(core.ToolError):
            render.parse_render_arguments({"stitch": "first"})
        self.assertEqual(
            render.parse_render_arguments({"stitch": ["first"]}), {"stitch": ["first"]}
        )

    def test_nothing_to_render_is_refused(self) -> None:
        with self.assertRaises(core.ToolError) as ctx:
            render.parse_render_arguments({})
        self.assertIn("nothing to render", str(ctx.exception))


class ArgvShapeTests(unittest.TestCase):
    def test_idle_render_concatenates_and_reamtimes_video_and_audio(self) -> None:
        edit = render.Edit(trim_start_ms=500.0, trim_end_ms=20_000.0)
        edit.segments = [
            render.Segment(500.0, 4_000.0, 1.0),
            render.Segment(4_000.0, 18_000.0, 4.0),
            render.Segment(18_000.0, 20_000.0, 1.0),
        ]
        from seshat import render as render_module

        with patch.object(media, "probe_media", return_value={
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080}]
        }):
            argv = render_ffmpeg.main_pass_argv(
                type("J", (), {"artifact": Path("/x/a.mp4"), "fmt": "mp4"})(),
                edit, Path("/x/out.mp4"), 1920, 1080, True, None, "mp4",
            )
        self.assertIn("concat=n=3:v=1:a=0[vbase]", argv[argv.index("-filter_complex") + 1])
        self.assertIn("asetpts", argv[argv.index("-filter_complex") + 1])
        self.assertIn("libx264", " ".join(argv))
        self.assertIn("aac", " ".join(argv))

    def test_zoom_and_markers_compose_into_the_graph(self) -> None:
        edit = render.Edit(trim_start_ms=0.0, trim_end_ms=10_000.0)
        edit.segments = [render.Segment(0.0, 10_000.0, 1.0)]
        edit.zoom_windows = [render.ZoomWindow(t_ms=2_000.0, hold_ms=800.0, scale=2.0, x=960.0, y=540.0)]
        with patch.object(media, "probe_media", return_value={
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080}]
        }):
            argv = render_ffmpeg.main_pass_argv(
                type("J", (), {"artifact": Path("/x/a.mp4"), "fmt": "mp4"})(),
                edit, Path("/x/out.mp4"), 1920, 1080, False, None, "mp4",
            )
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertIn("crop=w=960:h=540:x=480:y=270", graph)
        self.assertIn("overlay=enable='between(t,1.950,2.850)'", graph)
        self.assertNotIn("0:a", graph)

    def test_trim_only_render_is_still_frame_accurate_and_reencoded(self) -> None:
        edit = render.Edit(trim_start_ms=1_500.0, trim_end_ms=9_500.0)
        edit.segments = [render.Segment(1_500.0, 9_500.0, 1.0)]
        with patch.object(media, "probe_media", return_value={
            "streams": [{"codec_type": "video", "width": 1920, "height": 1080},
                        {"codec_type": "audio"}]
        }):
            argv = render_ffmpeg.main_pass_argv(
                type("J", (), {"artifact": Path("/x/a.mp4"), "fmt": "mp4"})(),
                edit, Path("/x/out.mp4"), 1920, 1080, True, None, "mp4",
            )
        graph = argv[argv.index("-filter_complex") + 1]
        self.assertIn("trim=start=1.500000:end=9.500000", graph)
        self.assertIn("-c:a", argv)
        self.assertIn("aac", argv)

    def test_chapters_metadata_carries_millisecond_starts(self) -> None:
        edit = render.Edit(trim_start_ms=0.0, trim_end_ms=10_000.0)
        edit.events = [
            render.RenderEvent("s", "click", True, {"chapter": "Setup"}, 100.0, 100.0),
            render.RenderEvent("s", "click", True, {"chapter": "Demo"}, 5_000.0, 5_000.0),
        ]
        with TemporaryDirectory() as tmp:
            text = render_ffmpeg.chapters_metadata_text(edit, 10_000.0)
            self.assertIn(";FFMETADATA1", text)
            self.assertIn("START=100", text)
            self.assertIn("START=5000", text)
            self.assertIn("timebase", text.lower() or ";FFMETADATA1" in text)

    def test_stitch_refuses_mismatched_shapes(self) -> None:
        with self.assertRaises(core.ToolError) as ctx:
            render_ffmpeg.probes_match_shape([
                {"streams": [{"codec_type": "video", "codec_name": "h264",
                              "width": 1920, "height": 1080}]},
                {"streams": [{"codec_type": "video", "codec_name": "h264",
                              "width": 1280, "height": 720}]},
            ])
        self.assertIn("shape or codec differs", str(ctx.exception))


class StitchHoldsNoLockTests(unittest.TestCase):
    """stitch runs outside the manager lock: it re-locks to collect the order."""

    def test_the_stitch_worker_probes_a_free_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            env = patch.dict(
                os.environ,
                {"XDG_RUNTIME_DIR": tmp, "XDG_STATE_HOME": str(Path(tmp) / "state")},
            )
            env.start()
            self.addCleanup(env.stop)
            manager = manager_module.RecordingManager()
            first = completed_job(tmp, events=[event(500.0)])
            second = completed_job(tmp, events=[event(500.0)])
            for job in (first, second):
                manager._job = job
                manager._jobs[job.id] = job
            held = {}

            def probe(manager_, job_, order):
                held["free"] = manager_._lock.acquire(blocking=False)
                if held["free"]:
                    manager_._lock.release()
                return {"path": "", "media_duration_ms": 0.0, "stitched": []}

            with patch.object(workers, "render_stitch", side_effect=probe):
                render.perform(manager, {"stitch": [second.id]})
            self.assertTrue(held.get("free"))


class RenderJoinHoldsNoLockTests(unittest.TestCase):
    """The sync render must not join its worker while holding the manager lock."""

    def test_the_worker_runs_with_the_lock_free(self) -> None:
        with TemporaryDirectory() as tmp:
            env = patch.dict(
                os.environ,
                {"XDG_RUNTIME_DIR": tmp, "XDG_STATE_HOME": str(Path(tmp) / "state")},
            )
            env.start()
            self.addCleanup(env.stop)
            manager = manager_module.RecordingManager()
            job = completed_job(tmp, events=[event(500.0)])
            manager._job = job
            manager._jobs[job.id] = job
            held = {}

            def slow_worker(manager_, job_, plan_, edit_):
                # Give the spawning thread time to leave the with-block; a join
                # still holding the lock would block both sides meanwhile.
                time.sleep(0.4)
                held["free"] = manager_._lock.acquire(blocking=False)
                if held["free"]:
                    manager_._lock.release()
                job_.render_result = {"render": {"path": "", "media_duration_ms": 0.0}}

            with patch.object(render, "compute_edit", return_value=render.Edit(0.0, 1000.0)):
                with patch.object(workers, "build_render", side_effect=slow_worker):
                    try:
                        render.perform(manager, {"profile": "native", "poster": False})
                    except core.ToolError:
                        pass
            self.assertEqual(held.get("free"), True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()