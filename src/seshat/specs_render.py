"""The `recording_render` tool's JSON schema, split for the file-length gate."""

from __future__ import annotations

from typing import Any

from . import recording


def render_spec() -> dict[str, Any]:
    return {
        "name": "recording_render",
            "description": (
                "Post-produce a completed take from its own timeline: trim to the "
                "events plus pad, speed up idle gaps, cut to zoom windows over the "
                "coordinates the events carry, pulse a marker box, rescale to a "
                "profile (1080p/720p/vertical), and derive a poster PNG, a teaser "
                "GIF or chapters from labeled events. The finalized master is never "
                "modified; the render is a new variant in the take's store "
                "directory. Answers synchronously with every event's remapped "
                "t_ms, so narration can follow with at_ms anchors. stitch takes "
                "whole finalized masters into one container instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "Take id; defaults to the latest take.",
                    },
                    "trim": {
                        "type": "object",
                        "properties": {
                            "pad_ms": {"type": "number", "minimum": 0, "maximum": 2000}
                        },
                        "additionalProperties": False,
                        "description": "Keep first-event to last-event, plus pad.",
                    },
                    "idle": {
                        "type": "object",
                        "properties": {
                            "min_gap_ms": {"type": "number", "minimum": 200, "maximum": 30000},
                            "speed": {"type": "number", "minimum": 1.05, "maximum": 16},
                        },
                        "additionalProperties": False,
                        "description": "Speed up every event-free gap this long.",
                    },
                    "zoom": {
                        "type": "object",
                        "properties": {
                            "scale": {"type": "number", "minimum": 1.1, "maximum": 4},
                            "hold_ms": {"type": "number", "minimum": 0, "maximum": 10000},
                        },
                        "additionalProperties": False,
                        "description": "Hold a hard-cut zoom centered on each coordinated event.",
                    },
                    "markers": {
                        "type": "boolean",
                        "description": "Pulse a marker box at each coordinated event.",
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["native", "1080p", "720p", "vertical"],
                        "description": "Output shape; vertical crops to 9:16 around the pointer.",
                    },
                    "poster": {"type": "boolean", "description": "Also write <render>.poster.png."},
                    "teaser": {"type": "boolean", "description": "Also write <render>.teaser.gif."},
                    "chapters": {
                        "type": "boolean",
                        "description": (
                            "Build chapters from events whose payload carries a chapter "
                            "label; muxed into the render and written as a YouTube list."
                        ),
                    },
                    "stitch": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                        "description": (
                            "Concat these finalized take ids (in this session's store) "
                            "before the rendered take, at the container level."
                        ),
                    },
                    "narrate": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "anchor": {
                                    "type": "object",
                                    "properties": {
                                        "at_ms": {"type": "number", "minimum": 0},
                                        "event_id": {
                                            "type": "integer", "minimum": 1,
                                            "description": "Position in the render's event map.",
                                        },
                                        "match": {
                                            "type": "object",
                                            "properties": {
                                                "tool": {"type": "string"},
                                                "payload": {"type": "object"},
                                                "nth": {"type": "integer", "minimum": 1},
                                            },
                                            "additionalProperties": False,
                                        },
                                    },
                                    "additionalProperties": False,
                                    "description": (
                                        "Resolved on the render's own clock: at_ms is "
                                        "render-relative; matching rows resolve to their "
                                        "remapped t_ms."
                                    ),
                                },
                                "text": {"type": "string"},
                            },
                            "required": ["anchor", "text"],
                            "additionalProperties": False,
                        },
                        "description": (
                            "Narrate the rendered cut: the track is scheduled against "
                            "the render's extent and muxed onto it as "
                            "<id>.render.narrated.<fmt>; the cut is a failure-proof step."
                        ),
                    },
                    "engine": {"type": "string", "enum": ["auto", "edge", "piper"]},
                    "voice": {"type": "string"},
                    "fit": {"type": "string", "enum": ["natural", "compress"]},
                    "offset_ms": {"type": "number"},
                    "tail_ms": {"type": "number", "minimum": 0, "maximum": 2000},
                    "subtitles": {"type": "boolean"},
                    "position": {"type": "string", "enum": ["bottom", "top", "auto"]},
                    "rate": {"type": "string"},
                    "volume": {"type": "string"},
                    "pitch": {"type": "string"},
                    "length_scale": {"type": "number", "minimum": 0.1, "maximum": 3},
                },
                "additionalProperties": False,
            },
        },
