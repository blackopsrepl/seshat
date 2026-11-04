"""Window capture targets: the compositor tree resolves to a static rect."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, outputs, recording, session, windows  # noqa: E402
from support import single_output  # noqa: E402


SWAY_TREE = {
    "name": "root",
    "nodes": [
        {
            "name": "DP-1",
            "nodes": [
                {
                    "app_id": "foot",
                    "name": "waybar",
                    "rect": {"x": 0, "y": 0, "width": 24, "height": 1080},
                },
                {
                    "app_id": "org.qutebrowser.qutebrowser",
                    "name": "SolverForge - qutebrowser",
                    "rect": {"x": 0, "y": 24, "width": 1920, "height": 1032},
                    "nodes": [
                        {
                            "app_id": "ghostty",
                            "name": "terminal",
                            "rect": {"x": 480, "y": 96, "width": 400, "height": 300},
                        },
                    ],
                },
            ],
            "floating_nodes": [
                {
                    "app_id": "io.github.mudlet.Mudlet",
                    "name": "client",
                    "rect": {"x": 100, "y": 100, "width": 640, "height": 480},
                },
            ],
        },
    ],
}

HYPR_CLIENTS = [
    {"class": "foot", "title": "waybar", "at": [0, 0], "size": [24, 1080]},
    {"class": "ghostty", "title": "dev", "at": [480, 96], "size": [400, 300]},
    {"class": "chromium", "title": "SolverForge", "at": [0, 24], "size": [1920, 1032]},
]


class WindowRectTests(unittest.TestCase):
    def test_sway_tree_walk_finds_by_class_and_title(self) -> None:
        with patch.dict(os.environ, {"SESHAT_COMPOSITOR": "sway"}):
            with patch.object(core, "read_json_command", return_value=SWAY_TREE):
                rect = windows.find_window_rect("qutebrowser")
        self.assertEqual(rect, {"x": 0, "y": 24, "width": 1920, "height": 1032})

    def test_sway_walk_matches_title_case_insensitively(self) -> None:
        with patch.dict(os.environ, {"SESHAT_COMPOSITOR": "sway"}):
            with patch.object(core, "read_json_command", return_value=SWAY_TREE):
                rect = windows.find_window_rect("SOLVERFORGE")
        self.assertEqual(rect["width"], 1920)

    def test_sway_floating_windows_are_reached(self) -> None:
        with patch.dict(os.environ, {"SESHAT_COMPOSITOR": "sway"}):
            with patch.object(core, "read_json_command", return_value=SWAY_TREE):
                rect = windows.find_window_rect("mudlet")
        self.assertEqual(rect, {"x": 100, "y": 100, "width": 640, "height": 480})

    def test_hyprland_clients_walk(self) -> None:
        with patch.dict(os.environ, {"SESHAT_COMPOSITOR": "hyprland"}):
            with patch.object(core, "read_json_command", return_value=HYPR_CLIENTS):
                rect = windows.find_window_rect("ghostty")
        self.assertEqual(rect, {"x": 480, "y": 96, "width": 400, "height": 300})

    def test_no_match_names_the_needle(self) -> None:
        with patch.dict(os.environ, {"SESHAT_COMPOSITOR": "sway"}):
            with patch.object(core, "read_json_command", return_value=SWAY_TREE):
                with self.assertRaises(core.ToolError) as ctx:
                    windows.find_window_rect("spotify")
        self.assertIn('"spotify"', str(ctx.exception))

    def test_degenerate_windows_are_skipped(self) -> None:
        tree = {"app_id": "x", "name": "needle", "rect": {"x": 0, "y": 0, "width": 1, "height": 1}}
        with patch.dict(os.environ, {"SESHAT_COMPOSITOR": "sway"}):
            with patch.object(core, "read_json_command", return_value=tree):
                with self.assertRaises(core.ToolError):
                    windows.find_window_rect("needle")

    def test_blank_needle_is_refused(self) -> None:
        with self.assertRaises(core.ToolError):
            windows.find_window_rect("  ")


class WindowStartTests(unittest.TestCase):
    def test_new_job_captures_the_window_rect_on_its_output(self) -> None:
        two_outputs = single_output() + [
            {"name": "DP-2", "active": True, "focused": False,
             "rect": {"x": 1920, "y": 0, "width": 1280, "height": 720}}
        ]
        window_rect = {"x": 2200, "y": 100, "width": 640, "height": 480}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                with patch.object(outputs, "get_outputs", return_value=two_outputs):
                    with patch.object(windows, "find_window_rect", return_value=window_rect):
                        job = recording.new_recording_job({"format": "mp4", "window": "client"})
        self.assertEqual(job.region, window_rect)
        self.assertEqual(job.output, "DP-2")
        self.assertEqual((job.width, job.height), (640, 480))

    def test_window_and_region_are_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmp}):
                with patch.object(outputs, "get_outputs", return_value=single_output()):
                    with self.assertRaises(core.ToolError) as ctx:
                        recording.new_recording_job({
                            "format": "mp4", "window": "ghostty",
                            "region": {"x": 0, "y": 0, "width": 100, "height": 100},
                        })
        self.assertIn("exclusive", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()