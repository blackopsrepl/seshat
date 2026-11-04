"""`recording_render`: the pure edit plan over a take's ingested events.

This module decides; `render_ffmpeg` builds the argv from what it decides.
Nothing here looks at the picture: every cut, speed, zoom window and marker is
derived from the take's ingested events or from explicit arguments. The plan
carries every event's remapped `t_ms` so a script re-narrates a render with
explicit `at_ms` anchors against the render's own timeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import core, narration, recording, timeline


RENDER_TRIM_PAD_MS_DEFAULT = 300.0
RENDER_TRIM_PAD_MS_MAX = 2000.0
RENDER_IDLE_MIN_GAP_DEFAULT = 1500.0
RENDER_IDLE_MIN_GAP_MIN = 200.0
RENDER_IDLE_MIN_GAP_MAX = 30_000.0
RENDER_IDLE_SPEED_DEFAULT = 4.0
RENDER_IDLE_SPEED_MAX = 16.0
RENDER_ZOOM_SCALE_DEFAULT = 2.0
RENDER_ZOOM_SCALE_MAX = 4.0
RENDER_ZOOM_HOLD_DEFAULT = 800.0
RENDER_ZOOM_HOLD_MAX = 10_000.0
RENDER_PROFILES = ("native", "1080p", "720p", "vertical")
RENDER_OPERATION_SCHEMA = (
    "trim",
    "idle",
    "zoom",
    "markers",
    "profile",
    "poster",
    "teaser",
    "chapters",
    "stitch",
)


@dataclass
class RenderEvent:
    """An ingested event, on the take's timeline and on the render's."""

    source: str
    tool: str
    ok: bool
    payload: dict[str, Any]
    t_ms: float
    out_ms: float


@dataclass
class Segment:
    """One piece of the trimmed window, played back at one speed."""

    start_ms: float
    end_ms: float
    speed: float = 1.0


@dataclass
class ZoomWindow:
    """A hard-cut zoom that holds the pointer's neighborhood for hold_ms."""

    t_ms: float
    hold_ms: float
    scale: float
    x: float
    y: float


@dataclass
class Edit:
    """The computed plan for one render: one video mapping, remapped events."""

    trim_start_ms: float
    trim_end_ms: float
    segments: list[Segment] = field(default_factory=lambda: [Segment(0.0, 0.0, 1.0)])
    events: list[RenderEvent] = field(default_factory=list)
    zoom_windows: list[ZoomWindow] = field(default_factory=list)
    markers: list[dict[str, float]] = field(default_factory=list)
    profile: str = "native"

    def out_ms(self, src_ms: float) -> float:
        """Remap a take-relative source time through the piecewise map."""
        out = 0.0
        for segment in self.segments:
            if src_ms <= segment.start_ms:
                break
            span = min(src_ms, segment.end_ms) - segment.start_ms
            if span > 0:
                out += span / segment.speed
            if src_ms <= segment.end_ms:
                break
        return round(out, 3)

    def scale(self) -> float:
        """Output seconds per input whole-map second."""
        total_input = sum(s.end_ms - s.start_ms for s in self.segments)
        if total_input <= 0:
            return 1.0
        return (
            sum((s.end_ms - s.start_ms) / s.speed for s in self.segments)
            / total_input
        )


def parse_render_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise core.ToolError("render arguments must be an object")
    core.reject_unknown(arguments, RENDER_OPERATION_SCHEMA, "recording_render")
    plan: dict[str, Any] = {}
    trim = arguments.get("trim")
    if trim is not None:
        if not isinstance(trim, dict):
            raise core.ToolError("trim must be an object")
        core.reject_unknown(trim, ("pad_ms",), "recording_render.trim")
        pad = core.strict_number(
            trim.get("pad_ms", RENDER_TRIM_PAD_MS_DEFAULT), "trim.pad_ms",
            minimum=0.0, maximum=RENDER_TRIM_PAD_MS_MAX,
        )
        plan["trim"] = {"pad_ms": pad}
    idle = arguments.get("idle")
    if idle is not None:
        if not isinstance(idle, dict):
            raise core.ToolError("idle must be an object")
        core.reject_unknown(idle, ("min_gap_ms", "speed"), "recording_render.idle")
        plan["idle"] = {
            "min_gap_ms": core.strict_number(
                idle.get("min_gap_ms", RENDER_IDLE_MIN_GAP_DEFAULT), "idle.min_gap_ms",
                minimum=RENDER_IDLE_MIN_GAP_MIN, maximum=RENDER_IDLE_MIN_GAP_MAX,
            ),
            "speed": core.strict_number(
                idle.get("speed", RENDER_IDLE_SPEED_DEFAULT), "idle.speed",
                minimum=1.05, maximum=RENDER_IDLE_SPEED_MAX,
            ),
        }
    zoom = arguments.get("zoom")
    if zoom is not None:
        if not isinstance(zoom, dict):
            raise core.ToolError("zoom must be an object")
        core.reject_unknown(zoom, ("scale", "hold_ms"), "recording_render.zoom")
        plan["zoom"] = {
            "scale": core.strict_number(
                zoom.get("scale", RENDER_ZOOM_SCALE_DEFAULT), "zoom.scale",
                minimum=1.1, maximum=RENDER_ZOOM_SCALE_MAX,
            ),
            "hold_ms": core.strict_number(
                zoom.get("hold_ms", RENDER_ZOOM_HOLD_DEFAULT), "zoom.hold_ms",
                minimum=0.0, maximum=RENDER_ZOOM_HOLD_MAX,
            ),
        }
    markers = arguments.get("markers")
    if markers is not None:
        if not isinstance(markers, bool):
            raise core.ToolError("markers must be a boolean")
        plan["markers"] = markers
    profile = arguments.get("profile")
    if profile is not None:
        if not isinstance(profile, str) or profile not in RENDER_PROFILES:
            raise core.ToolError("profile must be one of: " + ", ".join(RENDER_PROFILES))
        plan["profile"] = profile
    stitch = arguments.get("stitch")
    if stitch is not None:
        if not isinstance(stitch, list) or not stitch or not all(
            isinstance(value, str) for value in stitch
        ):
            raise core.ToolError(
                'stitch must be a non-empty list of take ids, e.g. ["<first-id>"]'
            )
        plan["stitch"] = stitch
    for key in ("poster", "teaser", "chapters"):
        value = arguments.get(key)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise core.ToolError(f"{key} must be a boolean")
        plan[key] = value
    if not plan:
        raise core.ToolError(
            "nothing to render: pass at least one of " + ", ".join(RENDER_OPERATION_SCHEMA)
        )
    return plan


def event_t_ms(job: recording.RecordingJob, event: dict[str, Any]) -> float:
    return timeline.recording_relative_ms(
        job.started_monotonic, float(event.get("at_monotonic") or 0.0)
    )


def has_point(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    try:
        x = float(payload["x"])
        y = float(payload["y"])
        return x == x and y == y
    except (KeyError, TypeError, ValueError):
        return False


def compute_edit(job: recording.RecordingJob, plan: dict[str, Any]) -> Edit:
    """The pure plan: every map and window is computed before any argv exists."""
    media_ms = narration.recording_duration_ms(job)
    ordered = sorted(
        timeline.ordered_events(job), key=lambda event: event_t_ms(job, event)
    )
    in_window = [event for event in ordered if event_t_ms(job, event) <= media_ms]
    times = [event_t_ms(job, event) for event in in_window]

    timeline_needs = bool(
        plan.get("trim") or plan.get("idle") or plan.get("zoom")
        or plan.get("markers") or plan.get("chapters")
    )
    if not times and timeline_needs:
        raise core.ToolError(
            "the take has no ingested events inside the playable picture; "
            "trim, idle, zoom, markers and chapters need the timeline"
        )

    trim_start_ms = 0.0
    trim_end_ms = media_ms
    if plan.get("trim"):
        pad = plan["trim"]["pad_ms"]
        trim_start_ms = max(times[0] - pad, 0.0)
        trim_end_ms = min(times[-1] + pad, media_ms)
        if trim_end_ms - trim_start_ms < 100.0:
            raise core.ToolError(
                "the trim collapses to an empty window: the take's last "
                "in-picture event is at "
                f"{round(times[-1])} ms and the picture runs {round(media_ms)} ms; "
                "pass a wider pad or record a longer demonstration"
            )

    idle = plan.get("idle") or {"min_gap_ms": RENDER_IDLE_MIN_GAP_MAX, "speed": 1.0}
    if idle["speed"] <= 1.0:
        segments = [Segment(trim_start_ms, trim_end_ms, 1.0)]
    else:
        segments = build_segments(trim_start_ms, trim_end_ms, times, idle)
        if not segments:
            segments = [Segment(trim_start_ms, trim_end_ms, 1.0)]

    edit = Edit(
        trim_start_ms=trim_start_ms,
        trim_end_ms=trim_end_ms,
        segments=segments,
        profile=plan.get("profile", "native"),
    )
    for event, t_ms in zip(in_window, times):
        out_ms = edit.out_ms(t_ms)
        payload = event.get("payload")
        edit.events.append(
            RenderEvent(
                source=str(event.get("source") or ""),
                tool=str(event.get("tool") or ""),
                ok=bool(event.get("ok")),
                payload=dict(payload or {}),
                t_ms=round(t_ms, 3),
                out_ms=out_ms,
            )
        )
        if not has_point(payload):
            continue
        assert isinstance(payload, dict)
        point = {"x": float(payload["x"]), "y": float(payload["y"])}
        if plan.get("zoom"):
            edit.zoom_windows.append(
                ZoomWindow(
                    t_ms=out_ms,
                    hold_ms=plan["zoom"]["hold_ms"],
                    scale=plan["zoom"]["scale"],
                    **point,
                )
            )
        if plan.get("markers"):
            edit.markers.append({"t_ms": out_ms, **point})
    if plan.get("zoom") and not edit.zoom_windows:
        raise core.ToolError(
            "zoom asked, but no ingested event carries coordinates (payload x/y); "
            "the driving tool server's events must publish where the pointer acted"
        )
    return edit


def build_segments(
    trim_start_ms: float, trim_end_ms: float, times: list[float],
    idle: dict[str, Any],
) -> list[Segment]:
    """Monotonic coverage of the trimmed window.

    Every event-free span is a candidate: between consecutive events, and the
    head and tail spans around them. Spans shorter than min_gap_ms stay at
    natural speed; longer ones are the idle this operation exists for.
    """
    min_gap_ms = idle["min_gap_ms"]
    speed = idle["speed"]
    if speed <= 1.0:
        return [Segment(trim_start_ms, trim_end_ms, 1.0)]
    edges = [trim_start_ms, *[t for t in times if trim_start_ms <= t <= trim_end_ms], trim_end_ms]
    pieces: list[Segment] = []
    for index in range(len(edges) - 1):
        start, end = edges[index], edges[index + 1]
        if end <= start:
            continue
        pieces.append(Segment(start, end, speed if end - start >= min_gap_ms else 1.0))
    if not pieces:
        return [Segment(trim_start_ms, trim_end_ms, 1.0)]
    merged: list[Segment] = []
    for piece in pieces:
        if merged and merged[-1].speed == piece.speed:
            merged[-1] = Segment(merged[-1].start_ms, piece.end_ms, piece.speed)
        else:
            merged.append(piece)
    return merged


def chapter_list(edit: Edit) -> list[tuple[float, str]]:
    """Chapter marks: events whose payload carries a `chapter` label, in order."""
    chapters: list[tuple[float, str]] = []
    for index, event in enumerate(edit.events):
        label = event.payload.get("chapter")
        if not isinstance(label, str) or not label.strip():
            continue
        chapters.append((event.out_ms, label.strip()))
    return chapters


def poster_t_ms(edit: Edit) -> float:
    """Where the poster frame lives: the first zoom hold, else the first mark."""
    if edit.zoom_windows:
        return round(edit.zoom_windows[0].t_ms + edit.zoom_windows[0].hold_ms / 2.0, 3)
    if edit.markers:
        return round(edit.markers[0]["t_ms"] + 150.0, 3)
    if edit.events:
        return round(max(edit.events[0].out_ms + 150.0, 0.0), 3)
    return 0.0


def teaser_span_ms(edit: Edit, media_ms: float, seconds: float = 6.0) -> tuple[float, float]:
    """The most eventful window for a teaser: the span holding the most events."""
    span_ms = min(seconds, (edit.trim_end_ms - edit.trim_start_ms) / 1000.0 * 1000.0) * 1000.0
    span_ms = min(span_ms, media_ms) or 6000.0
    if not edit.events or edit.events[-1].out_ms - edit.events[0].out_ms <= span_ms:
        return 0.0, min(span_ms, media_ms)
    best_start = 0.0
    best_count = -1
    for anchor in edit.events:
        start = max(anchor.out_ms - span_ms / 4.0, 0.0)
        end = start + span_ms
        count = sum(1 for event in edit.events if start <= event.out_ms <= end)
        if count > best_count:
            best_start, best_count = start, count
    return best_start, best_start + span_ms


def perform(manager: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """The render tool's body: validate, plan, build on a worker, join, answer.

    The manager owns the take; the sync-join pattern here mirrors the plan
    worker's: the worker takes the same lock at its end, so the join happens
    with the lock released.
    """
    import threading
    import time

    from . import workers

    with manager._lock:
        job = manager._select(arguments.get("id"))
        if job is None:
            raise core.ToolError("no recording to render; record something first")
        if job.phase != "completed":
            raise core.ToolError(
                f"recording must be completed before a render (phase: {job.phase})"
            )
        if not job.rehydrated:
            manager.ingest_events(job)
        plan = parse_render_arguments(
            {key: value for key, value in arguments.items() if key != "id"}
        )
        if plan.get("stitch") is not None:
            return stitch(manager, job, arguments)
        if job.fmt == "gif":
            raise core.ToolError(
                'GIF cannot be re-rendered this way; render mp4 takes with recording_render'
            )
        edit = compute_edit(job, plan)
        job.render_result = None
        job.render_error = None
        worker = threading.Thread(
            target=workers.build_render, args=(manager, job, plan, edit), daemon=True
        )
        worker.start()
    # The join runs with the lock released: the worker takes the same lock at
    # its end, so joining inside the block deadlocks both sides.
    deadline = time.monotonic() + 540.0
    while time.monotonic() < deadline:
        worker.join(timeout=5.0)
        with manager._lock:
            if manager._jobs.get(job.id) is not job:
                raise core.ToolError(f"take {job.id} was discarded while rendering")
            if job.render_result is not None or job.render_error is not None:
                result, detail = job.render_result, job.render_error
                job.render_result = None
                job.render_error = None
                break
    else:
        with manager._lock:
            job.render_result = None
        raise core.ToolError(
            "the render did not finish within 9 minutes; split the take to render it"
        )
    if detail is not None:
        raise core.ToolError(str(detail))
    return result


def stitch(manager: Any, selected: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """Concat whole finalized masters: the listed ids first, this take last."""
    from . import recording as recording_module
    from . import workers

    order_ids = list(arguments.get("stitch") or []) + [selected.id]
    order = []
    with manager._lock:
        for entry_id in order_ids:
            entry = manager._jobs.get(entry_id)
            if entry is None:
                raise core.ToolError(
                    f"take {entry_id} is not in this session; stitch within one session"
                )
            if entry.phase != "completed":
                raise core.ToolError(
                    f"take {entry_id} must be completed before it is stitched (phase: {entry.phase})"
                )
            order.append((entry.id, entry, recording_module.wants_audio(entry.audio)))
    event_offsets = []
    offset_ms = 0.0
    for entry_id, entry, _ in order:
        for event in timeline.ordered_events(entry):
            event_offsets.append({
                "source": str(event.get("source") or ""),
                "tool": str(event.get("tool") or ""),
                "t_ms": round(offset_ms + timeline.recording_relative_ms(
                    entry.started_monotonic, float(event.get("at_monotonic") or 0.0)
                ), 3),
            })
        offset_ms = round(offset_ms + (entry.media_duration_ms or 0.0), 3)
    result = workers.render_stitch(manager, selected, order)
    result["event_map"] = event_offsets
    return result
