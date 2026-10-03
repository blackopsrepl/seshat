"""seshat_info's audio surface: what a take's audio=... would resolve to."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, outputs, server

from support import single_output
from test_audio_capture import PACTL_INFO, routable_pactl


class SeshatInfoAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def info(self) -> dict:
        with patch.object(outputs, "get_outputs", return_value=single_output()):
            info = json.loads(server.TOOLS["seshat_info"]({})[0]["text"])
        return info

    def test_info_reports_the_default_sink_and_source(self) -> None:
        with patch.object(core, "run_command", side_effect=routable_pactl(PACTL_INFO)):
            info = self.info()
        self.assertEqual(info["audio"]["default_sink"], "alsa_output.pci-0000_00_1f.3.analog-stereo")
        self.assertEqual(info["audio"]["default_source"], "alsa_input.usb-PreSonus_Studio_24c_SC1E21090873-00.analog-stereo")
        # the resolved monitor source is what audio="monitor" would capture
        self.assertEqual(
            info["audio"].get("monitor_source"),
            "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
        )

    def test_info_stays_forgiving_without_pactl(self) -> None:
        with patch.object(
            core,
            "run_command",
            side_effect=lambda args, **kw: (_ for _ in ()).throw(
                core.ToolError("required command not found: pactl")
            ),
        ):
            info = self.info()
        self.assertIsNone(info["audio"]["default_sink"])
        self.assertEqual(info["audio"]["error"], "required command not found: pactl")

    def test_info_without_audio_reports_unresolved(self) -> None:
        info = self.info()
        self.assertIsNone(info["audio"]["default_sink"])


if __name__ == "__main__":
    unittest.main()
