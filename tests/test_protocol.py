from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from seshat import server


class ProtocolTests(unittest.TestCase):
    def test_initialize_response_names_server(self) -> None:
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )

        self.assertIsNotNone(response)
        result = response["result"]
        self.assertEqual(result["serverInfo"]["name"], "seshat")
        self.assertEqual(result["capabilities"], {"tools": {}})

    def test_initialize_carries_operating_instructions(self) -> None:
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {}}
        )

        instructions = response["result"]["instructions"]
        self.assertIsInstance(instructions, str)
        self.assertIn("never drives the desktop", instructions)
        self.assertIn("never invent", instructions)
        self.assertIn("timeline streams", instructions)
        self.assertIn("recording_status", instructions)

    def test_tools_list_exposes_expected_tools(self) -> None:
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )

        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "seshat_info",
                "recording_start",
                "recording_status",
                "recording_stop",
                "recording_timeline",
                "recording_voiceover",
                "recording_scenes",
            },
        )

    def test_recording_tools_are_silent_and_closed(self) -> None:
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
        )

        specs = {tool["name"]: tool for tool in response["result"]["tools"]}
        for name, spec in specs.items():
            self.assertIs(spec["inputSchema"].get("additionalProperties"), False, name)
        # Silent stays the default contract: no live take gains audio implicitly.
        # The opt-in audio surface only exists where the user asked for it.
        for name in ("recording_status", "recording_stop"):
            self.assertNotIn("audio", json.dumps(specs[name]), name)

        start_schema = specs["recording_start"]["inputSchema"]
        self.assertEqual(start_schema["properties"]["format"]["enum"], ["mp4", "webm", "gif"])
        self.assertEqual(start_schema["properties"]["format"]["default"], "mp4")
        audio_schema = start_schema["properties"]["audio"]
        self.assertEqual(audio_schema["enum"], ["auto", "monitor", "mic", "off", None])
        self.assertEqual(audio_schema["default"], "off")
        region = start_schema["properties"]["region"]
        self.assertEqual(region["required"], ["x", "y", "width", "height"])
        self.assertEqual(start_schema["properties"]["timeline_sources"]["type"], ["array", "null"])

    def test_unknown_tool_returns_tool_error_result(self) -> None:
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "missing", "arguments": {}},
            }
        )

        result = response["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertIn("unknown tool", result["content"][0]["text"])

    def test_ping_answers_with_an_empty_result(self) -> None:
        response = server.handle_message({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(response, {"jsonrpc": "2.0", "id": 9, "result": {}})

    def test_non_object_bodies_are_refused_and_answered(self) -> None:
        """A batch array or bare value cannot be routed, so it is an invalid request."""
        for body in ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], "hello", 7, [], None):
            response = server.handle_message(body)
            self.assertEqual(response["error"]["code"], -32600, response)
            self.assertIsNone(response["id"])

    def test_non_object_arguments_answer_as_invalid_params(self) -> None:
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "recording_status", "arguments": [1, 2]},
            }
        )
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("arguments must be an object", response["error"]["message"])

    def test_null_arguments_default_to_an_empty_object(self) -> None:
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "recording_status", "arguments": None},
            }
        )
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(json.loads(response["result"]["content"][0]["text"]), {"phase": "idle"})

    def test_the_message_loop_outlives_an_undroutable_or_raising_message(self) -> None:
        bad = '[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]'
        good = '{"jsonrpc": "2.0", "id": 2, "method": "ping"}'
        raising = '{"jsonrpc": "2.0", "id": 3, "method": "tools/list"}'
        another_good = '{"jsonrpc": "2.0", "id": 4, "method": "ping"}'
        out = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO("\n".join([bad, good, raising, another_good]))):
            with patch.object(sys, "stdout", out):
                with patch.object(
                    server, "tool_specs", side_effect=RuntimeError("boom")
                ):
                    server.run_mcp_server()
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0]["error"]["code"], -32600)
        self.assertEqual(lines[1], {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertEqual(lines[2]["error"]["code"], -32603)
        self.assertEqual(lines[3], {"jsonrpc": "2.0", "id": 4, "result": {}})

    def test_notification_returns_no_response(self) -> None:
        response = server.handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )

        self.assertIsNone(response)

    def test_command_argv_uses_environment_override(self) -> None:
        with patch.dict(os.environ, {server.COMMAND_ENV: "/tmp/cus --flag"}, clear=False):
            self.assertEqual(server.command_argv(), ["/tmp/cus", "--flag"])

    def test_command_argv_prefers_invoked_console_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "seshat"
            script.write_text("#!/bin/sh\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(server.COMMAND_ENV, None)
                with patch.object(server.sys, "argv", [str(script)]):
                    self.assertEqual(server.command_argv(), [str(script)])


if __name__ == "__main__":
    unittest.main()
