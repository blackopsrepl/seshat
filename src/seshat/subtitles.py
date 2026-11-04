"""Slick, burned-in captions for narrated recordings (ASS rendered by libass)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import recording

SUBTITLE_FONT = "DejaVu Sans"
SUBTITLE_FILE_NAME = "captions.ass"

_ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "PlayResX: {width}\n"
    "PlayResY: {height}\n"
    "WrapStyle: 0\n"
    "ScaledBorderAndShadow: yes\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Caption,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,"
    "-1,0,0,0,100,100,0,0,1,3,2,2,80,80,{margin},1\n"
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)


def format_timestamp(milliseconds: float) -> str:
    """Format milliseconds as an ASS ``h:mm:ss.cc`` timestamp."""
    total = max(milliseconds, 0.0)
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    centis = int(round(millis / 10.0))
    if centis >= 100:
        centis -= 100
        seconds += 1
    return f"{int(hours)}:{int(minutes):02d}:{int(seconds):02d}.{centis:02d}"


def escape_text(text: str) -> str:
    """Escape ASS override syntax so caption prose renders literally."""
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r", "")
        .replace("\n", "\\N")
    )


def build_ass_document(entries: list[dict[str, Any]], width: int, height: int) -> str:
    """Build a styled ASS document with one ``Dialogue`` line per caption entry.

    An entry may carry ``align: "top"`` (auto placement lifted it away from the
    action at the bottom of the picture); the override tag wins over the style.
    """
    safe_height = max(int(height), 1)
    font_size = max(24, round(safe_height * 0.045))
    margin = max(36, round(safe_height * 0.07))
    lines = [
        _ASS_HEADER.format(
            width=max(int(width), 1),
            height=safe_height,
            font=SUBTITLE_FONT,
            size=font_size,
            margin=margin,
        )
    ]
    for entry in entries:
        text = escape_text(str(entry["text"]))
        if entry.get("align") == "top":
            text = "{\\an8}" + text
        lines.append(
            "Dialogue: 0,{start},{end},Caption,,0,0,0,,{text}".format(
                start=format_timestamp(entry["start_ms"]),
                end=format_timestamp(entry["end_ms"]),
                text=text,
            )
        )
    return "\n".join(lines) + "\n"


def soft_caption_paths(
    job: recording.RecordingJob, deliverable: Path, captions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Write SRT and WebVTT sidecars beside a narrated deliverable.

    Burn-in styling and portability are different facts: players, YouTube and
    translators want the same timings as files, and an ffmpeg without libass
    can still ship those. The naming follows the deliverable, so variants
    carry their own sidecars.
    """
    stem = deliverable.with_suffix("")
    written = []
    for suffix, builder in ((".srt", build_srt_document), (".vtt", build_vtt_document)):
        path = deliverable.with_name(stem.name + suffix)
        _write_private(path, builder(captions))
        written.append({"format": suffix.lstrip("."), "path": str(path)})
    return written


def _write_private(path: Path, document: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, recording.RECORDING_FILE_MODE)
    try:
        os.write(fd, document.encode("utf-8"))
    finally:
        os.close(fd)


def _srt_timestamp(milliseconds: float) -> str:
    total = max(milliseconds, 0.0)
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d},{int(millis):03d}"


def _vtt_timestamp(milliseconds: float) -> str:
    total = max(milliseconds, 0.0)
    hours, remainder = divmod(total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{int(millis):03d}"


def _cue_position(entry: dict[str, Any]) -> str:
    # WebVTT line:0 anchors the cue at the top of the frame, matching burn-in.
    return "line:0" if entry.get("align") == "top" else ""


def build_srt_document(captions: list[dict[str, Any]]) -> str:
    blocks = []
    for index, entry in enumerate(captions, 1):
        blocks.append(
            f"{index}\n{_srt_timestamp(entry['start_ms'])} --> "
            f"{_srt_timestamp(entry['end_ms'])}\n{str(entry['text']).strip()}\n"
        )
    return "\n".join(blocks)


def build_vtt_document(captions: list[dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for entry in captions:
        position = _cue_position(entry)
        timing = f"{_vtt_timestamp(entry['start_ms'])} --> {_vtt_timestamp(entry['end_ms'])}"
        if position:
            timing += f"\n{position}"
        lines.append(timing)
        lines.append(str(entry["text"]).strip())
        lines.append("")
    return "\n".join(lines)


def write_subtitle_file(workdir: Path, document: str) -> Path:
    path = workdir / SUBTITLE_FILE_NAME
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, recording.RECORDING_FILE_MODE)
    try:
        os.write(fd, document.encode("utf-8"))
    finally:
        os.close(fd)
    return path


def escape_filter_path(path: Path) -> str:
    return (
        str(path)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def video_filter(subtitle_path: Path, prefix: str = "") -> str:
    """Build the ``subtitles`` filtergraph that burns the captions into ``[v]``.

    ``prefix`` puts video filters ahead of the burn-in, e.g. a ``tpad`` that
    extends the picture to the narration's length.
    """
    return f"[0:v]{prefix}subtitles=filename='{escape_filter_path(subtitle_path)}'[v]"
