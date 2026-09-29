from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, outputs, session, server

from support import publish, single_output


class SeshatInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def info(self) -> dict:
        return json.loads(server.TOOLS["seshat_info"]({})[0]["text"])

    def test_reports_identity_runtime_and_binaries(self) -> None:
        with patch.object(outputs, "get_outputs", return_value=single_output()):
            info = self.info()
        self.assertEqual(info["server"]["name"], "seshat")
        self.assertEqual(info["outputs"][0]["name"], "DP-1")
        self.assertIsNone(info["outputs_error"])
        self.assertEqual(info["take"], {"phase": "idle"})
        self.assertEqual(info["runtime"]["streams"], os.path.join(self.tmp.name, "seshat", "streams"))
        self.assertTrue(info["runtime"]["recordings"].endswith("recordings"))
        self.assertIn("wf-recorder", info["binaries"])
        self.assertIn("ffprobe", info["binaries"])
        self.assertEqual(info["tts"]["engine_preference"], ["edge", "piper"])

    def test_lists_the_streams_that_would_be_ingested(self) -> None:
        path = publish(self.tmp.name, "computer-use-sway", [1000.0])
        with patch.object(outputs, "get_outputs", return_value=single_output()):
            info = self.info()
        self.assertEqual(info["streams"], [str(path)])

    def test_reports_an_unusable_session_instead_of_failing(self) -> None:
        with patch.object(outputs, "get_outputs", side_effect=core.ToolError("no sway session")):
            info = self.info()
        self.assertIsNone(info["outputs"])
        self.assertEqual(info["outputs_error"], "no sway session")

    def test_reports_the_detected_compositor_and_distribution(self) -> None:
        with patch.object(outputs, "get_outputs", return_value=single_output()):
            with patch.object(session, "detect_compositor", return_value="hyprland"):
                with patch.object(session, "distribution_id", return_value="omarchy"):
                    info = self.info()
        self.assertEqual(info["session"], {"compositor": "hyprland", "distribution": "omarchy"})
        self.assertIn("hyprctl", info["binaries"])

    def test_an_undetected_compositor_is_reported_as_none(self) -> None:
        with patch.object(outputs, "get_outputs", side_effect=core.ToolError("no session")):
            with patch.object(
                session, "detect_compositor", side_effect=core.ToolError("no Sway or Hyprland session detected")
            ):
                info = self.info()
        self.assertIsNone(info["session"]["compositor"])
        self.assertEqual(info["outputs_error"], "no session")


class ToolRegistryTests(unittest.TestCase):
    def test_registry_matches_the_specs(self) -> None:
        self.assertEqual({spec["name"] for spec in server.tool_specs()}, set(server.TOOLS))

    def test_every_schema_is_closed(self) -> None:
        for spec in server.tool_specs():
            self.assertIs(spec["inputSchema"].get("additionalProperties"), False, spec["name"])
            self.assertIn("description", spec, spec["name"])


if __name__ == "__main__":
    unittest.main()
