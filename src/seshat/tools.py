"""MCP tool wrappers for the take lifecycle.

Each wrapper is a thin adapter over :mod:`seshat.manager`; the manager owns all
state, so these functions stay free of lifecycle logic.
"""

from __future__ import annotations

import shutil
from typing import Any

from . import core, manager, outputs, recording, session, streams, store, tts
from .version import SERVER_NAME, SERVER_VERSION


def tool_seshat_info(arguments: dict[str, Any]) -> list[dict[str, str]]:
    """Report what this host can record and narrate with, and where it writes.

    Deliberately forgiving: this is the tool an operator reaches for when a take
    misbehaves, so an unusable session is reported as a field instead of failing
    the whole call.
    """
    core.reject_unknown(arguments, (), "seshat_info")
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
    session_report: dict[str, Any] = {
        "compositor": None,
        "distribution": session.distribution_id(),
    }
    try:
        session_report["compositor"] = session.detect_compositor()
    except core.ToolError:
        pass
    info = {
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "take": manager.RECORDINGS.status(),
        "runtime": {
            "streams": str(streams.stream_directory()),
            "recordings": str(streams.runtime_root() / recording.RECORDING_DIRECTORY_NAME),
            "store": str(store.state_takes_root()),
        },
        "streams": [str(path) for path in streams.discover_streams()],
        "session": session_report,
        "outputs": outputs_report,
        "outputs_error": outputs_error,
        "audio": recording.audio_defaults_report(),
        "tts": {
            "engine_preference": list(tts.NARRATION_ENGINE_PREFERENCE),
            "engines": list(tts.NARRATION_ENGINES),
        },
        "binaries": {
            name: shutil.which(name)
            for name in (
                "swaymsg",
                "hyprctl",
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


def tool_recording_stop(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.stop(arguments))


def tool_recording_timeline(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.timeline(arguments))


def tool_recording_voiceover(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.voiceover(arguments))


def tool_recording_scenes(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.scenes(arguments))


def tool_recording_list(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.list_takes(arguments))


def tool_recording_discard(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.discard(arguments))


def tool_recording_export(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.export(arguments))


def tool_recording_render(arguments: dict[str, Any]) -> list[dict[str, str]]:
    return core.json_text(manager.RECORDINGS.render(arguments))


TOOLS = {
    "seshat_info": tool_seshat_info,
    "recording_start": tool_recording_start,
    "recording_status": tool_recording_status,
    "recording_stop": tool_recording_stop,
    "recording_timeline": tool_recording_timeline,
    "recording_voiceover": tool_recording_voiceover,
    "recording_scenes": tool_recording_scenes,
    "recording_list": tool_recording_list,
    "recording_discard": tool_recording_discard,
    "recording_export": tool_recording_export,
    "recording_render": tool_recording_render,
}
