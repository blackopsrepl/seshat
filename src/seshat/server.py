#!/usr/bin/env python3
"""MCP server that records a screen and narrates the result.

seshat owns capture, the take timeline, speech synthesis and muxing. It does not
own the desktop: the actions a take documents are performed by whatever tool
server the harness called, and reach this timeline through published event
streams (see :mod:`seshat.streams`).
"""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .core import *
from .encoding import *
from .manager import *
from .media import *
from .narration import *
from .outputs import *
from .recording import *
from .scenes import *
from .session import *
from .specs import *
from .streams import *
from .timeline import *
from .tools import *
from .tts import *
from .version import *


MCP_PROTOCOL_VERSION = "2024-11-05"


OPERATING_INSTRUCTIONS = (
    "seshat records a screen and narrates the result; it never drives the desktop. "
    "Recording is a lifecycle: recording_start, let the demonstration run, recording_stop, "
    "then poll recording_status until the phase is completed or failed. Illustration "
    "content is never invented: narration anchors are real event ids from the take's "
    "timeline, which is assembled from the timeline streams ingested for that take. Call "
    "recording_timeline once the take is completed to read those events, author the "
    "narration prose yourself, pass it to recording_voiceover, then poll recording_status. "
    "If recording_timeline reports no events, the tool server that drove the "
    "demonstration did not publish a stream: anchor segments with at_ms instead, or use "
    "recording_scenes for approximate cuts. GIF cannot carry audio. Only one take may be "
    "active at a time; the pointer cursor is always captured."
)


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": OPERATING_INSTRUCTIONS,
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": tool_specs()},
        }

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                },
            }
        try:
            content = TOOLS[name](arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": content, "isError": False},
            }
        except ToolError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            }
        except Exception as exc:
            eprint(f"unexpected tool error in {name}: {exc}")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "internal tool error"}],
                    "isError": True,
                },
            }

    if request_id is None:
        return None

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def run_mcp_server() -> int:
    def terminate(_signum: int, _frame: Any) -> None:
        raise SystemExit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, terminate)
        except (OSError, ValueError):
            pass
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
            else:
                response = handle_message(message)
            if response is not None:
                print(json.dumps(response, separators=(",", ":")), flush=True)
    finally:
        RECORDINGS.shutdown()
    return 0


def install_codex_mcp() -> int:
    require_binaries(["codex"])
    expected_command = command_argv()
    expected_args = expected_command[1:]
    existing = subprocess.run(
        ["codex", "mcp", "get", SERVER_NAME, "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if existing.returncode == 0:
        try:
            data = json.loads(existing.stdout)
        except json.JSONDecodeError:
            data = {}
        transport = data.get("transport") or {}
        command = data.get("command") or transport.get("command")
        args = data.get("args")
        if args is None:
            args = transport.get("args") or []
        if command == expected_command[0] and args == expected_args:
            print(f"{SERVER_NAME} MCP entry already installed")
            return 0
        subprocess.run(["codex", "mcp", "remove", SERVER_NAME], check=False)

    add = subprocess.run(
        ["codex", "mcp", "add", SERVER_NAME, "--", *expected_command],
        check=False,
    )
    if add.returncode != 0:
        return add.returncode
    print(f"installed {SERVER_NAME} MCP entry")
    return 0


def uninstall_codex_mcp() -> int:
    require_binaries(["codex"])
    existing = subprocess.run(
        ["codex", "mcp", "get", SERVER_NAME, "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if existing.returncode != 0:
        print(f"{SERVER_NAME} MCP entry is not installed")
        return 0
    removed = subprocess.run(["codex", "mcp", "remove", SERVER_NAME], check=False)
    if removed.returncode == 0:
        print(f"removed {SERVER_NAME} MCP entry")
    return removed.returncode


def self_test() -> int:
    checks: list[tuple[str, str]] = []
    for name in (
        "python3",
        "swaymsg",
        "hyprctl",
        "wf-recorder",
        "ffmpeg",
        "ffprobe",
        "codex",
        NARRATION_EDGE_COMMAND,
        NARRATION_PIPER_COMMAND,
        "tesseract",
    ):
        checks.append((name, shutil.which(name) or "missing"))
    require_binaries(["wf-recorder", "ffmpeg", "ffprobe"])
    compositor = require_session()
    outputs = get_outputs()
    summary = {
        "ok": True,
        "server": SERVER_NAME,
        "version": SERVER_VERSION,
        "environment": session_environment(),
        "session": {"compositor": compositor, "distribution": distribution_id()},
        "binaries": dict(checks),
        "outputs": [
            {
                "name": output.get("name"),
                "rect": output.get("rect"),
                "scale": output.get("scale"),
                "current_mode": output.get("current_mode"),
            }
            for output in outputs
        ],
        "streams": [str(path) for path in discover_streams()],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def doctor() -> int:
    report: dict[str, Any] = {
        "server": {
            "name": SERVER_NAME,
            "version": SERVER_VERSION,
            "command": command_argv(),
            "module_path": __file__,
        },
        "environment": session_environment(),
        "binaries": {
            name: shutil.which(name)
            for name in (
                "python3",
                "swaymsg",
                "hyprctl",
                "wf-recorder",
                "ffmpeg",
                "ffprobe",
                "codex",
                NARRATION_EDGE_COMMAND,
                NARRATION_PIPER_COMMAND,
                "tesseract",
            )
        },
        "runtime": {
            "streams": str(stream_directory()),
            "recordings": str(runtime_root() / RECORDING_DIRECTORY_NAME),
        },
        "streams": [str(path) for path in discover_streams()],
        "take": RECORDINGS.status(),
        "session": None,
        "codex_mcp": None,
    }
    try:
        compositor = require_session()
        report["session"] = {
            "ok": True,
            "compositor": compositor,
            "distribution": distribution_id(),
            "outputs": [
                {
                    "name": output.get("name"),
                    "active": output.get("active"),
                    "rect": output.get("rect"),
                    "scale": output.get("scale"),
                    "transform": output.get("transform"),
                    "current_mode": output.get("current_mode"),
                }
                for output in get_outputs()
            ],
        }
    except ToolError as exc:
        report["session"] = {"ok": False, "error": str(exc)}

    if command_available("codex"):
        proc = subprocess.run(
            ["codex", "mcp", "get", SERVER_NAME, "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            try:
                report["codex_mcp"] = json.loads(proc.stdout)
            except json.JSONDecodeError:
                report["codex_mcp"] = {"raw": proc.stdout}
        else:
            report["codex_mcp"] = {"installed": False, "stderr": proc.stderr.strip()}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seshat recording and narration MCP server")
    parser.add_argument("--self-test", action="store_true", help="run non-mutating checks")
    parser.add_argument("--doctor", action="store_true", help="print detailed diagnostics")
    parser.add_argument("--install-codex-mcp", action="store_true", help="register with Codex")
    parser.add_argument("--uninstall-codex-mcp", action="store_true", help="remove Codex registration")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.self_test:
            return self_test()
        if args.doctor:
            return doctor()
        if args.install_codex_mcp:
            return install_codex_mcp()
        if args.uninstall_codex_mcp:
            return uninstall_codex_mcp()
        return run_mcp_server()
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cli() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(cli())
