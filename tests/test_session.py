from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, session


def bind_socket(path: Path) -> None:
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path))


class SessionTestCase(unittest.TestCase):
    """Each test runs against an empty session environment."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runtime = tmp.name
        env = patch.dict(
            os.environ,
            {"XDG_RUNTIME_DIR": self.runtime, "PATH": os.environ.get("PATH", "")},
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)

    def instance_dir(self, name: str) -> Path:
        path = Path(self.runtime) / "hypr" / name
        path.mkdir(parents=True)
        return path


class DetectCompositorTests(SessionTestCase):
    def test_without_evidence_the_error_names_both_compositors_and_the_override(self) -> None:
        with self.assertRaises(core.ToolError) as ctx:
            session.detect_compositor()
        message = str(ctx.exception)
        self.assertIn("Sway", message)
        self.assertIn("Hyprland", message)
        self.assertIn(session.COMPOSITOR_ENV, message)

    def test_the_override_selects_a_compositor_without_evidence(self) -> None:
        with patch.dict(os.environ, {session.COMPOSITOR_ENV: "hyprland"}):
            self.assertEqual(session.detect_compositor(), "hyprland")
        with patch.dict(os.environ, {session.COMPOSITOR_ENV: "Sway"}):
            self.assertEqual(session.detect_compositor(), "sway")

    def test_the_override_rejects_unknown_names(self) -> None:
        with patch.dict(os.environ, {session.COMPOSITOR_ENV: "gnome"}):
            with self.assertRaises(core.ToolError) as ctx:
                session.detect_compositor()
            self.assertIn("must be one of: sway, hyprland", str(ctx.exception))

    def test_a_sway_socket_is_evidence(self) -> None:
        with patch.dict(os.environ, {"SWAYSOCK": "/run/user/1000/sway-ipc.sock"}):
            self.assertEqual(session.detect_compositor(), "sway")

    def test_a_sway_socket_is_recovered_from_the_runtime_dir(self) -> None:
        bind_socket(Path(self.runtime) / f"sway-ipc.{os.getuid()}.99.sock")
        self.assertEqual(session.detect_compositor(), "sway")

    def test_the_newest_sway_socket_wins(self) -> None:
        old = Path(self.runtime) / f"sway-ipc.{os.getuid()}.1.sock"
        new = Path(self.runtime) / f"sway-ipc.{os.getuid()}.2.sock"
        bind_socket(old)
        bind_socket(new)
        os.utime(old, (1, 1))
        session.ensure_session_environment()
        self.assertEqual(os.environ["SWAYSOCK"], str(new))

    def test_a_hyprland_instance_signature_is_evidence(self) -> None:
        with patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "sig"}):
            self.assertEqual(session.detect_compositor(), "hyprland")

    def test_a_hyprland_instance_dir_is_recovered(self) -> None:
        self.instance_dir("sig1")
        self.assertEqual(session.detect_compositor(), "hyprland")
        session.ensure_session_environment()
        self.assertEqual(os.environ["HYPRLAND_INSTANCE_SIGNATURE"], "sig1")

    def test_the_newest_hyprland_instance_wins(self) -> None:
        old = self.instance_dir("old")
        new = self.instance_dir("new")
        os.utime(old, (1, 1))
        session.ensure_session_environment()
        self.assertEqual(os.environ["HYPRLAND_INSTANCE_SIGNATURE"], new.name)

    def test_both_compositors_let_the_desktop_hint_decide(self) -> None:
        with patch.dict(os.environ, {"SWAYSOCK": "s", "HYPRLAND_INSTANCE_SIGNATURE": "h"}):
            with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "Hyprland"}):
                self.assertEqual(session.detect_compositor(), "hyprland")
            with patch.dict(os.environ, {"XDG_SESSION_DESKTOP": "Hyprland"}):
                self.assertEqual(session.detect_compositor(), "hyprland")
            with patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "sway"}):
                self.assertEqual(session.detect_compositor(), "sway")

    def test_both_compositors_without_a_hint_keep_sway(self) -> None:
        with patch.dict(os.environ, {"SWAYSOCK": "s", "HYPRLAND_INSTANCE_SIGNATURE": "h"}):
            self.assertEqual(session.detect_compositor(), "sway")


class RequireSessionTests(SessionTestCase):
    def setUp(self) -> None:
        super().setUp()
        binaries = patch.object(core, "require_binaries", lambda names: None)
        binaries.start()
        self.addCleanup(binaries.stop)
        probe = patch.object(core, "read_json_command", lambda args, **kwargs: [])
        probe.start()
        self.addCleanup(probe.stop)

    def test_a_sway_session_is_verified_and_named(self) -> None:
        with patch.dict(os.environ, {"SWAYSOCK": "s", "WAYLAND_DISPLAY": "wayland-1"}):
            self.assertEqual(session.require_session(), "sway")

    def test_a_hyprland_session_is_verified_and_named(self) -> None:
        with patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "h", "WAYLAND_DISPLAY": "wayland-1"}):
            self.assertEqual(session.require_session(), "hyprland")

    def test_a_missing_wayland_display_is_reported_against_the_label(self) -> None:
        with patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "h"}):
            with self.assertRaises(core.ToolError) as ctx:
                session.require_session()
        message = str(ctx.exception)
        self.assertIn("Hyprland", message)
        self.assertIn("WAYLAND_DISPLAY", message)


class SessionEnvironmentTests(SessionTestCase):
    def test_reports_the_raw_session_variables(self) -> None:
        with patch.dict(os.environ, {"HYPRLAND_INSTANCE_SIGNATURE": "h"}):
            report = session.session_environment()
        self.assertIsNone(report["SWAYSOCK"])
        self.assertEqual(report["HYPRLAND_INSTANCE_SIGNATURE"], "h")
        self.assertEqual(report["XDG_RUNTIME_DIR"], self.runtime)


if __name__ == "__main__":
    unittest.main()
