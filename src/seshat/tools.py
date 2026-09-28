"""MCP tool wrappers for the take lifecycle.

Each wrapper is a thin adapter over :mod:`seshat.manager`; the manager owns all
state, so these functions stay free of lifecycle logic.
"""

from __future__ import annotations

import shutil
from typing import Any

from . import core, manager, outputs, recording, streams, tts
from .version import SERVER_NAME, SERVER_VERSION


def tool_seshat_info(_: dict[str, Any]) -> list[dict[str, str]]:
    """Report what this host can record and narrate with, and where it writes.

    Deliberately forgiving: this is the tool an operator reaches for when a take
    misbehaves, so an unusable session is reported as a field instead of failing
    the whole call.
    """
    outputs_report: list[dict[str, Any]] | None = None
    outputs_error: str | None = None
    try:
        outputs_report = [
            {
                "name": output.get("name"),
                "rect": output.get("rect"),
                "scale": output.get("scale"),
                "current_mode": output.get("current_mode"),
            }
            for output in outputs.get_outputs()
        ]
    except core.ToolError as exc:
        outputs_error = str(exc)
    info = {
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "take": manager.RECORDINGS.status(),
        "runtime": {
            "streams": str(streams.stream_directory()),
            "recordings": str(streams.runtime_root() / recording.RECORDING_DIRECTORY_NAME),
        },
        "streams": [str(path) for path in streams.discover_streams()],
        "outputs": outputs_report,
        "outputs_error": outputs_error,
        "tts": {
            "engine_preference": list(tts.NARRATION_ENGINE_PREFERENCE),
            "engines": list(tts.NARRATION_ENGINES),
        },
        "binaries": {
            name: shutil.which(name)
            for name in (
                "swaymsg",
                "wf-recorder",
                "ffmpeg",
                "ffprobe",
                tts.NARRATION_EDGE_COMMAND,
                tts.NARRATION_PIPER_COMMAND,
                "tesseract",
            )
        },
    }
    return core.json_text(info)


def tool_recording_start(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.start(arguments))


def tool_recording_status(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.status(arguments))


def tool_recording_stop(_: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.stop())


def tool_recording_timeline(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.timeline(arguments))


def tool_recording_voiceover(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.voiceover(arguments))


def tool_recording_scenes(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.scenes(arguments))


TOOLS = {
    "seshat_info": tool_seshat_info,
    "recording_start": tool_recording_start,
    "recording_status": tool_recording_status,
    "recording_stop": tool_recording_stop,
    "recording_timeline": tool_recording_timeline,
    "recording_voiceover": tool_recording_voiceover,
    "recording_scenes": tool_recording_scenes,
}
