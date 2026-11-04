"""JSON schemas for the seshat MCP tool surface."""

from __future__ import annotations

from typing import Any

from . import core, recording, scenes, streams, tts


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "seshat_info",
            "description": (
                "Inspect the recorder: active take, stream and recording directories, "
                "the durable take store, the streams that will be ingested, the "
                "capture outputs, and the binaries narration depends on."
            ),
            "annotations": {"readOnlyHint": True},
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "recording_start",
            "description": (
                "Start recording an active output (or a region within it): H.264 MP4 "
                "by default, or AV1 WebM, or a constrained GIF fallback. Returns "
                "immediately; call recording_stop to finish, then poll "
                "recording_status until the phase is completed or failed. The pointer "
                "cursor is always included. Narration anchors come from the timeline "
                "streams ingested for this take, so the tool server that drives the "
                "demonstration has to publish one."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "output": {
                        "type": "string",
                        "description": (
                            "Exact output name (Sway or Hyprland); inferred when "
                            "exactly one output is active."
                        ),
                    },
                    "region": {
                        "type": ["object", "null"],
                        "properties": {
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "width": {"type": "integer"},
                            "height": {"type": "integer"},
                        },
                        "required": ["x", "y", "width", "height"],
                        "additionalProperties": False,
                        "description": "Region fully contained in the selected output.",
                    },
                    "format": {
                        "type": "string",
                        "enum": list(recording.RECORDING_FORMATS),
                        "default": "mp4",
                        "description": (
                            "mp4: H.264 MP4 at 30 fps (default, widest compatibility); "
                            "webm: AV1 WebM at 30 fps; gif: constrained fallback at "
                            "12 fps, 960 px maximum width."
                        ),
                    },
                    "audio": {
                        "type": ["string", "null"],
                        "enum": ["auto", "monitor", "mic", "off", None],
                        "default": "off",
                        "description": (
                            "Desktop audio to capture alongside the picture. Defaults to "
                            "off: the artifact stays silent unless you ask for a track. "
                            "\"monitor\" records the desktop's default output (application "
                            "and system sound); \"mic\" records the host's default "
                            "microphone input; \"auto\" is monitor when a default sink is "
                            "resolvable and mic otherwise. Refused for gif (GIF cannot "
                            "carry audio; the tool errors rather than ignoring it). "
                            "Capturing with a narration added later puts "
                            "speech on top of this track (ducked, mixed into one stream)."
                        ),
                    },
                    "max_duration_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 300,
                        "description": (
                            "Automatic stop deadline. Defaults to 60 (mp4/webm) or 15 (gif); "
                            "GIF recordings are capped at 15 seconds."
                        ),
                    },
                    "timeline_sources": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": (
                            "Stream files to ingest for this take. Defaults to every "
                            f"*{streams.STREAM_SUFFIX} file in "
                            f"{streams.STREAM_DIRECTORY_NAME}/ under the seshat runtime "
                            "directory. Paths are validated when the take starts."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "recording_status",
            "description": (
                "Report the take lifecycle phase (idle, recording, stopping, processing, "
                "narrating, completed, failed) plus live progress or final artifact "
                "metadata. Set wait_seconds to have the call block until a terminal "
                "phase or the cap, instead of polling."
            ),
            "annotations": {"readOnlyHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Take id from recording_start; defaults to the latest take.",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 60,
                        "default": 0,
                        "description": (
                            "Block up to this long for the phase to reach completed/failed "
                            "before returning the current phase."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "recording_stop",
            "description": (
                "Gracefully stop the active recording and begin finalizing the requested "
                "artifact. Returns the stopping/processing state; poll recording_status "
                "for the final artifact path and metadata."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"destructiveHint": True},
        },
        {
            "name": "recording_list",
            "description": (
                "List every take this process knows: in-memory ones and those rehydrated "
                "from the durable store, with their deliverable and master paths."
            ),
            "annotations": {"readOnlyHint": True},
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "recording_discard",
            "description": (
                "Delete one inactive take's store directory and drop it from memory. "
                "Irreversible: export first if the files matter."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Take id from recording_list; defaults to the latest take.",
                    },
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "recording_export",
            "description": (
                "Copy one completed take's published files (master, deliverable, "
                "timeline, manifest, narration journal) into a destination directory. "
                "The only way take files leave the store."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Take id from recording_list; defaults to the latest take.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Directory the files are copied into; created when missing.",
                    },
                },
                "required": ["destination"],
                "additionalProperties": False,
            },
        },
        {
            "name": "recording_timeline",
            "description": (
                "Return the take's monotonic event timeline: every event ingested from the "
                "take's timeline streams, filtered to the capture window, with a "
                "recording-relative t_ms, the emitting source, and a compact payload. Use "
                "event ids as narration anchors. A sidecar JSON copy is written next to "
                "the artifact on completion."
            ),
            "annotations": {"readOnlyHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Take id from recording_start; defaults to the latest take.",
                    }
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "recording_voiceover",
            "description": (
                "Attach a scripted narration track to a completed take: the caller "
                "supplies the prose, the server synthesizes speech, aligns segments to "
                "timeline anchors (or to an explicit at_ms), and muxes the audio over the "
                "existing video stream, optionally burning styled captions. Starts an "
                "async 'narrating' phase; poll recording_status. GIF cannot carry audio."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Take id from recording_start; defaults to the latest take.",
                    },
                    "segments": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "anchor": {
                                    "type": "object",
                                    "properties": {
                                        "event_id": {"type": "integer", "minimum": 1},
                                        "at_ms": {"type": "number", "minimum": 0},
                                        "match": {
                                            "type": "object",
                                            "properties": {
                                                "tool": {"type": "string"},
                                                "payload": {"type": "object"},
                                                "nth": {"type": "integer", "minimum": 1},
                                            },
                                            "additionalProperties": False,
                                            "description": (
                                                "Match one real event by tool and/or a payload "
                                                "subset, so a script survives a re-record."
                                            ),
                                        },
                                    },
                                    "additionalProperties": False,
                                },
                                "text": {
                                    "type": "string",
                                    "maxLength": tts.NARRATION_MAX_SEGMENT_CHARS,
                                },
                            },
                            "required": ["anchor", "text"],
                            "additionalProperties": False,
                        },
                    },
                    "engine": {
                        "type": "string",
                        "enum": list(tts.NARRATION_ENGINES),
                        "default": "auto",
                        "description": (
                            "auto resolves to the offline piper engine only; edge-tts "
                            "runs only when asked for explicitly, because it sends the "
                            "narration prose to Microsoft over the network."
                        ),
                    },
                    "voice": {"type": ["string", "null"]},
                    "offset_ms": {
                        "type": "number",
                        "minimum": -tts.NARRATION_MAX_OFFSET_MS,
                        "maximum": tts.NARRATION_MAX_OFFSET_MS,
                        "default": 0,
                    },
                    "fit": {
                        "type": "string",
                        "enum": list(tts.NARRATION_FITS),
                        "default": "natural",
                    },
                    "tail_ms": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": tts.NARRATION_MAX_TAIL_MS,
                        "default": tts.NARRATION_DEFAULT_TAIL_MS,
                    },
                    "subtitles": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "Burn styled captions from the narration into the video "
                            "(re-encodes the video); SRT/VTT sidecars are written "
                            "either way. Set false to copy the video without captions."
                        ),
                    },
                    "rate": {
                        "type": "string",
                        "default": "+0%",
                        "description": "Voice rate as edge-tts accepts it, e.g. \"+10%\" or \"-5%\".",
                    },
                    "volume": {
                        "type": "string",
                        "default": "+0%",
                        "description": "Voice volume as edge-tts accepts it.",
                    },
                    "pitch": {
                        "type": "string",
                        "default": "+0Hz",
                        "description": "Voice pitch as edge-tts accepts it, e.g. \"+2Hz\".",
                    },
                    "length_scale": {
                        "type": "number",
                        "minimum": 0.1,
                        "maximum": 3.0,
                        "default": 1.0,
                        "description": "Piper voice pace (<1 faster, >1 slower).",
                    },
                    "position": {
                        "type": "string",
                        "enum": ["bottom", "top", "auto"],
                        "default": "bottom",
                        "description": (
                            "Caption placement. auto lifts a caption to the top when the "
                            "events in its window acted in the bottom third of the picture."
                        ),
                    },
                    "variant": {
                        "type": ["string", "null"],
                        "description": (
                            "Render a named variant (e.g. \"it\" for Italian narration) to "
                            "<id>.<variant>.<fmt>; every render is rebuilt from the master."
                        ),
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Synthesize and schedule without muxing or publishing: the "
                            "answer carries every segment's duration and overruns, so a "
                            "demonstration can be paced to the script before it is recorded."
                        ),
                    },
                },
                "required": ["segments"],
                "additionalProperties": False,
            },
        },
        {
            "name": "recording_scenes",
            "description": (
                "Optional, approximate fallback anchors for a completed take: ffmpeg "
                "scene-cut timestamps, with optional keyframe OCR via tesseract. Use when "
                "no timeline stream was published for the take; scene cuts are secondary "
                "evidence, never the sync source."
            ),
            "annotations": {"readOnlyHint": True},
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Take id from recording_start; defaults to the latest take.",
                    },
                    "threshold": {
                        "type": "number",
                        "minimum": scenes.SCENE_MIN_THRESHOLD,
                        "maximum": scenes.SCENE_MAX_THRESHOLD,
                        "default": scenes.SCENE_DEFAULT_THRESHOLD,
                    },
                    "max_scenes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": scenes.SCENE_MAX_CUTS,
                        "default": scenes.SCENE_DEFAULT_MAX,
                    },
                    "ocr": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        },
    ]
