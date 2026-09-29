from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, outputs


def hyprland_monitors() -> list[dict]:
    """The shape ``hyprctl -j monitors`` reports (sampled from a live session)."""
    return [
        {
            "id": 0,
            "name": "eDP-1",
            "description": "BOE 0x06B3",
            "make": "BOE",
            "model": "0x06B3",
            "serial": "",
            "width": 1366,
            "height": 768,
            "refreshRate": 60.05900,
            "x": 0,
            "y": 0,
            "scale": 1,
            "transform": 0,
            "focused": True,
            "dpmsStatus": True,
            "disabled": False,
            "mirrorOf": "none",
            "availableModes": ["1366x768@60.06Hz", "1366x768@47.99Hz"],
        },
        {
            "id": 1,
            "name": "DP-2",
            "width": 1920,
            "height": 1080,
            "refreshRate": 144.0,
            "x": 1366,
            "y": 0,
            "scale": 1.25,
            "transform": 3,
            "focused": False,
            "dpmsStatus": False,
            "disabled": True,
        },
    ]


def sway_outputs() -> list[dict]:
    return [
        {"name": "DP-1", "active": True, "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}},
        {"name": "HDMI-A-1", "active": False, "rect": {"x": 0, "y": 0, "width": 0, "height": 0}},
    ]


class HyprlandOutputTests(unittest.TestCase):
    def get(self, monitors: list[dict]) -> list[dict]:
        with patch.object(outputs.session, "require_session", return_value="hyprland"):
            with patch.object(core, "read_json_command", return_value=monitors):
                return outputs.get_outputs()

    def test_monitors_project_onto_the_recorder_output_shape(self) -> None:
        result = self.get(hyprland_monitors())
        self.assertEqual([output["name"] for output in result], ["eDP-1"])
        output = result[0]
        self.assertTrue(output["active"])
        self.assertTrue(output["focused"])
        self.assertEqual(output["rect"], {"x": 0, "y": 0, "width": 1366, "height": 768})
        self.assertEqual(output["scale"], 1)
        self.assertEqual(output["transform"], 0)
        self.assertEqual(output["mirror_of"], "none")
        self.assertEqual(output["available_modes"], ["1366x768@60.06Hz", "1366x768@47.99Hz"])

    def test_disabled_monitors_are_not_capture_targets(self) -> None:
        monitors = hyprland_monitors()
        monitors[1]["disabled"] = False
        result = self.get(monitors)
        self.assertEqual(
            [output["rect"] for output in result],
            [
                {"x": 0, "y": 0, "width": 1366, "height": 768},
                {"x": 1366, "y": 0, "width": 1920, "height": 1080},
            ],
        )

    def test_current_mode_is_synththesized_in_millihertz_like_sway(self) -> None:
        output = self.get(hyprland_monitors())[0]
        self.assertEqual(output["current_mode"], {"width": 1366, "height": 768, "refresh": 60059})

    def test_a_monitor_without_a_refresh_rate_still_resolves(self) -> None:
        monitors = hyprland_monitors()
        del monitors[0]["refreshRate"]
        output = self.get(monitors)[0]
        self.assertIsNone(output["current_mode"])

    def test_invalid_geometry_is_a_tool_error(self) -> None:
        monitors = hyprland_monitors()
        del monitors[0]["height"]
        with self.assertRaises(core.ToolError) as ctx:
            self.get(monitors)
        self.assertIn("invalid geometry", str(ctx.exception))

    def test_a_monitor_without_a_name_is_a_tool_error(self) -> None:
        monitors = hyprland_monitors()
        del monitors[0]["name"]
        with self.assertRaises(core.ToolError) as ctx:
            self.get(monitors)
        self.assertIn("without a name", str(ctx.exception))


class SwayOutputTests(unittest.TestCase):
    def test_sway_outputs_pass_through_active_only(self) -> None:
        with patch.object(outputs.session, "require_session", return_value="sway"):
            with patch.object(core, "read_json_command", return_value=sway_outputs()):
                result = outputs.get_outputs()
        self.assertEqual([output["name"] for output in result], ["DP-1"])
        self.assertEqual(result[0]["rect"], {"x": 0, "y": 0, "width": 1920, "height": 1080})


if __name__ == "__main__":
    unittest.main()
