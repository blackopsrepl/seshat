"""ffmpeg argv for `recording_render`, built from a computed `render.Edit`.

One render is one main pass (trim → idle re-time → zoom/markers → profile)
into a validated `.part` candidate, plus the cheap derived outputs the plan
asks for (poster PNG, teaser GIF, chapters). Trim happens inside the filter
graph so boundaries are frame-accurate on every master; stitch is the one
container-level exception — whole masters at `-c copy`, refused on shape
mismatch instead of half a catalog re-encoded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import encoding, media
from .core import ToolError
from .render import Edit, chapter_list, poster_t_ms, teaser_span_ms


RENDER_GIF_FPS = 12
RENDER_GIF_WIDTH = 480
RENDER_TEASER_SECONDS_DEFAULT = 6.0


def _input_section(job: Any) -> list[str]:
    return ["-i", str(job.artifact)]


def main_pass_argv(
    job: Any,
    edit: Edit,
    out_path: Path,
    width: int,
    height: int,
    has_audio: bool,
    chapters_path: Path | None,
    fmt: str,
) -> list[str]:
    """One ffmpeg pass over the master, from the pure plan."""
    argv = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *_input_section(job),
    ]
    if chapters_path is not None:
        argv += ["-i", str(chapters_path)]

    trim_part = (
        f"trim=start={edit.trim_start_ms / 1000.0:.6f}:end={edit.trim_end_ms / 1000.0:.6f},"
        "setpts=PTS-STARTPTS"
    )
    filters: list[str] = []
    video_label = "0:v"
    audio_label = "0:a"

    if any(segment.speed != 1.0 for segment in edit.segments):
        # Idle speed-up: each segment re-times its own slice, then back together.
        # A single merged sped segment still needs this: the merge is about the
        # boundaries, not about whether the map re-times anything.
        labels_v: list[str] = []
        labels_a: list[str] = []
        for index, segment in enumerate(edit.segments):
            labels_v.append(f"v{index}")
            filters.append(
                f"[0:v]trim=start={segment.start_ms / 1000.0:.6f}:end={segment.end_ms / 1000.0:.6f},"
                f"setpts=(PTS-STARTPTS)*{1.0 / segment.speed:.6f}[v{index}]"
            )
            if has_audio:
                labels_a.append(f"a{index}")
                filters.append(
                    f"[0:a]atrim=start={segment.start_ms / 1000.0:.6f}:end={segment.end_ms / 1000.0:.6f},"
                    f"asetpts=(PTS-STARTPTS)*{1.0 / segment.speed:.6f}[a{index}]"
                )
        filters.append(
            "".join(f"[{l}]" for l in labels_v)
            + f"concat=n={len(labels_v)}:v=1:a=0[vbase]"
        )
        video_label = "vbase"
        if has_audio:
            filters.append(
                "".join(f"[{l}]" for l in labels_a)
                + f"concat=n={len(labels_a)}:v=0:a=1[abase]"
            )
            audio_label = "abase"
    elif edit.trim_start_ms > 0.0 or edit.trim_end_ms > 0.0:
        filters.append(f"[0:v]{trim_part}[vbase]")
        video_label = "vbase"
        if has_audio:
            filters.append(
                f"[0:a]atrim=start={edit.trim_start_ms / 1000.0:.6f}:end={edit.trim_end_ms / 1000.0:.6f},"
                "asetpts=PTS-STARTPTS[abase]"
            )
            audio_label = "abase"

    zoom_filters = _zoom_overlay_filters(edit, width, height, video_label)
    filters.extend(zoom_filters)
    if edit.zoom_windows:
        video_label = f"bz{len(edit.zoom_windows) - 1}"
    marker_chain = _marker_filters(edit)
    if marker_chain:
        filters.append(f"[{video_label}]" + ",".join(marker_chain) + "[bv]")
        video_label = "bv"
    shape = _profile_filter(edit, width, height)
    if shape:
        filters.append(f"[{video_label}]{shape}[vo]")
        video_label = "vo"

    if filters:
        argv += ["-filter_complex", ";".join(filters), "-map", f"[{video_label}]"]
    else:
        # Nothing touched the picture at all: the profile is native with no
        # trim, no idle, no zoom and no markers; the master's video maps as-is.
        argv += ["-map", "0:v:0"]
    argv += encoding.video_encode_args(fmt, getattr(job, "encoder", "libsvtav1"))
    if has_audio:
        if audio_label == "0:a" and not filters:
            argv += ["-map", "0:a:0", "-c:a", "copy"]
        else:
            argv += ["-map", f"[{audio_label}]"]
            argv += encoding.audio_encode_args(fmt)
    if chapters_path is not None:
        argv += ["-map_chapters", "1"]
    argv += ["-f", fmt, *encoding.container_mux_flags(fmt), str(out_path)]
    return argv


def _zoom_overlay_filters(edit: Edit, iw: int, ih: int, base: str) -> list[str]:
    """One overlay branch per window; windows are hard cuts that hold, then release."""
    filters: list[str] = []
    label = base
    for index, window in enumerate(edit.zoom_windows):
        scale = window.scale
        crop_w = max(int(round(iw / scale)) & ~1, 2)
        crop_h = max(int(round(ih / scale)) & ~1, 2)
        x = max(0.0, min(window.x - crop_w / 2.0, iw - crop_w))
        y = max(0.0, min(window.y - crop_h / 2.0, ih - crop_h))
        start = max(window.t_ms - 50.0, 0.0) / 1000.0
        end = (window.t_ms + window.hold_ms + 50.0) / 1000.0
        zoomed = f"z{index}"
        merged = f"bz{index}"
        filters.append(
            f"[{label}]crop=w={crop_w}:h={crop_h}:x={int(round(x))}:y={int(round(y))},"
            f"scale=w={iw}:h={ih}[{zoomed}]"
        )
        filters.append(
            f"[{label}][{zoomed}]overlay=enable='between(t,{start:.3f},{end:.3f})'[{merged}]"
        )
        label = merged
    return filters


def _marker_filters(edit: Edit) -> list[str]:
    """A two-stage pulse per click: a wider box, then a tighter one."""
    filters: list[str] = []
    size = 24.0
    for marker in edit.markers:
        t_start = max(marker["t_ms"] - 50.0, 0.0) / 1000.0
        t_mid = (marker["t_ms"] + 250.0) / 1000.0
        t_end = (marker["t_ms"] + 500.0) / 1000.0
        x, y = marker["x"], marker["y"]
        filters.append(
            f"drawbox=x={int(round(x - size))}:y={int(round(y - size))}:"
            f"w={int(round(size * 2))}:h={int(round(size * 2))}:"
            f"color=red@0.4:t=4:enable='between(t,{t_start:.3f},{t_mid:.3f})'"
        )
        filters.append(
            f"drawbox=x={int(round(x - size / 2))}:y={int(round(y - size / 2))}:"
            f"w={int(round(size))}:h={int(round(size))}:"
            f"color=red@0.7:t=3:enable='between(t,{t_mid:.3f},{t_end:.3f})'"
        )
    return filters


def _profile_filter(edit: Edit, iw: int, ih: int) -> str:
    """The output shape; vertical crops to 9:16 around the median pointer x."""
    if edit.profile == "native":
        return ""
    if edit.profile in ("1080p", "720p"):
        target = 1080 if edit.profile == "1080p" else 720
        if ih <= target:
            return ""
        return f"scale=w=-2:h={target}"
    crop_w = int(ih * 9.0 / 16.0) & ~3
    xs = sorted(window.x for window in edit.zoom_windows) or sorted(
        marker["x"] for marker in edit.markers
    )
    mid = xs[len(xs) // 2] if xs else iw / 2.0
    x = max(0.0, min(mid - crop_w / 2.0, iw - crop_w))
    return f"crop=w={crop_w}:h={ih}:x={int(round(x))}:y=0"


def poster_argv(candidate: Path, t_ms: float, out_path: Path) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{t_ms / 1000.0:.3f}", "-i", str(candidate),
        "-frames:v", "1", str(out_path),
    ]


def teaser_argv(candidate: Path, start_ms: float, end_ms: float, out_path: Path) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start_ms / 1000.0:.3f}", "-t", f"{(end_ms - start_ms) / 1000.0:.3f}",
        "-i", str(candidate),
        "-vf", f"fps={RENDER_GIF_FPS},scale={RENDER_GIF_WIDTH}:-1:flags=lanczos",
        "-loop", "0", str(out_path),
    ]


def chapters_metadata_text(edit: Edit, media_ms: float) -> str:
    """The ;FFMETADATA1 body for the chaptered render's second input."""
    entries = chapter_list(edit)
    if not entries:
        raise ToolError(
            "chapters asked, but no ingested event carries a payload chapter label"
        )
    lines = [";FFMETADATA1"]
    for index, (start_ms, title) in enumerate(entries):
        end_ms = entries[index + 1][0] if index + 1 < len(entries) else media_ms
        lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={int(round(max(start_ms, 0.0)))}",
            f"END={int(round(max(end_ms, start_ms + 500.0)))}",
            f"title={title}",
        ]
    return "\n".join(lines) + "\n"


def stitch_list_text(order: list[Path]) -> str:
    return "\n".join(f"file '{path.resolve()}'" for path in order) + "\n"


def stitch_argv(
    list_path: Path, out_path: Path, has_audio: bool, fmt: str,
) -> list[str]:
    """Container-level concat of whole masters, in the given order."""
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c:v", "copy",
        "-c:a", "copy" if has_audio else "-an",
        "-f", fmt,
        str(out_path),
    ]


def probes_match_shape(probes: list[dict[str, Any]]) -> dict[str, Any]:
    """Stitched masters must share pixel shape and stream count to concat."""
    shapes: set[tuple[Any, ...]] = set()
    for probe in probes:
        streams = probe.get("streams") or []
        video = [stream for stream in streams if stream.get("codec_type") == "video"]
        if not video:
            raise ToolError("a stitched master has no video stream to concat")
        shapes.add(
            (
                int(video[0]["width"]),
                int(video[0]["height"]),
                tuple(str(stream.get("codec_type")) for stream in streams),
                str(video[0].get("codec_name")),
            )
        )
    if len(shapes) != 1:
        raise ToolError(
            "stitch refuses: the masters' shape or codec differs; "
            "re-render each take through the same profile first"
        )
    return probes[0]