from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import core


def probe_media(path: Path) -> dict[str, Any]:
    result = core.run_command(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=30.0,
    )
    try:
        return json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise core.ToolError(f"ffprobe returned invalid JSON for {path.name}") from exc


def probe_video_stream(path: Path) -> dict[str, Any]:
    probe = probe_media(path)
    video = [stream for stream in probe.get("streams") or [] if stream.get("codec_type") == "video"]
    if not video:
        raise core.ToolError(f"{path.name} contains no video stream")
    return video[0]


def probe_duration_ms(path: Path) -> float:
    """Playable extent of the video stream, falling back to the container.

    Stream duration is the extent of the picture; container duration can be
    longer when audio or metadata run past the last frame. Anchoring narration
    to the container is how speech ends up talking over a frame that does not
    exist, so the video stream wins whenever ffprobe reports one.
    """
    seconds = duration_seconds_from_probe(probe_media(path))
    if seconds <= 0.0:
        raise core.ToolError(f"artifact has no readable duration: {path.name}")
    return round(seconds * 1000.0, 3)


def duration_seconds_from_probe(probe: dict[str, Any]) -> float:
    """Playable video extent from an ffprobe document, 0.0 when there is none."""
    video = [stream for stream in probe.get("streams") or [] if stream.get("codec_type") == "video"]
    candidates: list[Any] = []
    if video:
        candidates.append(video[0].get("duration"))
    candidates.append((probe.get("format") or {}).get("duration"))
    for candidate in candidates:
        try:
            seconds = float(candidate)
        except (TypeError, ValueError):
            continue
        if seconds > 0.0:
            return seconds
    return 0.0


def parse_frame_rate(rate: Any) -> float:
    try:
        numerator, denominator = str(rate).split("/", 1)
        denominator_value = float(denominator)
        if denominator_value == 0:
            return 0.0
        return float(numerator) / denominator_value
    except (TypeError, ValueError):
        return 0.0
