"""Narration composed onto a rendered cut.

A render is its own picture with its own clock: the render's event map holds
every event's remapped `t_ms`, so the script's anchors resolve against the
render, the track is scheduled against the render's extent, and the mux lands
on `<id>.render.narrated.<fmt>`. The master's narration contract is untouched:
`recording_voiceover` still muxes the master; this is the render worker's own
follow-through when the render plan carried a `narrate` list.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from . import core, media, narration, recording
from .render import NARRATE_ARGUMENT_KEYS, Edit, RenderEvent


RENDER_NARRATED_SUFFIX = "narrated"


def resolve_render_anchor(
    raw: dict[str, Any], rows: list[RenderEvent], render_ms: float, index: int
) -> dict[str, Any]:
    """One narrate row to an `at_ms` anchor on the render's own clock."""
    if not isinstance(raw, dict):
        raise core.ToolError(f"narrate segment {index} must be an object")
    core.reject_unknown(raw, ("anchor", "text"), "recording_render.narrate")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise core.ToolError(f"narrate segment {index} needs prose in text")
    anchor = raw.get("anchor")
    if not isinstance(anchor, dict):
        raise core.ToolError(f"narrate segment {index} needs an anchor object")
    core.reject_unknown(anchor, ("at_ms", "event_id", "match"), "recording_render.narrate.anchor")
    keys = {key for key in anchor if anchor[key] is not None}
    if "at_ms" in keys:
        at_ms = core.strict_number(anchor["at_ms"], "narrate.anchor.at_ms", minimum=0.0)
        if at_ms > render_ms:
            raise core.ToolError(
                f"narrate anchor at_ms {at_ms} runs past the render's extent "
                f"({round(render_ms)} ms)"
            )
        return {"anchor": {"at_ms": at_ms}, "text": text}
    if "event_id" in keys:
        position = core.strict_int(anchor["event_id"], "narrate.anchor.event_id")
        if position < 1 or position > len(rows):
            raise core.ToolError(
                f"narrate anchor event_id {position} is not a row of the render's "
                f"event map ({len(rows)} event(s))"
            )
        return {"anchor": {"at_ms": rows[position - 1].out_ms}, "text": text}
    # A content match, resolved against the render's own map: the same
    # exactly-one rule the take's narration applies, on the remapped rows.
    match = anchor.get("match") or {}
    if not isinstance(match, dict):
        raise core.ToolError("narrate anchor match must be an object")
    core.reject_unknown(
        match, ("tool", "payload", "nth"), "recording_render.narrate.anchor.match"
    )
    tool = match.get("tool")
    payload_match = match.get("payload")
    nth = match.get("nth")
    candidates = rows
    if tool is not None:
        if not isinstance(tool, str):
            raise core.ToolError("narrate anchor match tool must be a string")
        candidates = [row for row in candidates if row.tool == tool]
    if isinstance(payload_match, dict) and payload_match:
        def matches(row: RenderEvent) -> bool:
            return all(row.payload.get(key) == value for key, value in payload_match.items())

        candidates = [row for row in candidates if matches(row)]
    if nth is not None:
        if not isinstance(nth, int) or nth < 1:
            raise core.ToolError("narrate anchor match nth must be a 1-based integer")
        candidates = candidates[nth - 1 : nth]
    if len(candidates) != 1:
        raise core.ToolError(
            f"narrate anchor match resolves to {len(candidates)} event(s) on the "
            "render's timeline; it must resolve to exactly one"
        )
    return {"anchor": {"at_ms": candidates[0].out_ms}, "text": text}


def perform_render_narration(
    job: recording.RecordingJob,
    edit: Edit,
    render_path: "Any",
    narrate_rows: list[dict[str, Any]],
    character: dict[str, Any],
) -> dict[str, Any]:
    """Synthesize and mux the narration over the rendered cut.

    The base shifts to the render for every step: the extent, the bed, the
    caption canvas and the deliverable name all belong to the cut now.
    """
    from pathlib import Path

    from . import tts as tts_module

    engine = tts_module.resolve_tts_engine(character.get("engine", "auto"))
    render_path = Path(render_path)
    probe = media.probe_media(render_path)
    audio_stream = next(
        (stream for stream in probe.get("streams") or [] if stream.get("codec_type") == "audio"),
        None,
    )
    video_stream = next(
        stream for stream in probe.get("streams") or [] if stream.get("codec_type") == "video"
    )
    width = int(video_stream["width"])
    height = int(video_stream["height"])
    render_ms = media.probe_duration_ms(render_path)
    resolved = [
        resolve_render_anchor(raw, edit.events, render_ms, index + 1)
        for index, raw in enumerate(narrate_rows)
    ]
    shim = dataclasses.replace(
        job,
        artifact=render_path,
        narrated_path=render_path.with_name(
            f"{job.id}.render.{RENDER_NARRATED_SUFFIX}.{job.fmt}"
        ),
        width=width,
        height=height,
        audio="monitor" if audio_stream is not None else "off",
        audio_source=None,
    )
    request = narration.parse_narration_arguments(
        {
            "segments": resolved,
            **{key: value for key, value in character.items() if key != "engine"},
        },
        shim,
        duration_ms=render_ms,
    )
    return narration.perform_narration(shim, request, engine)


def narrate_render(
    manager: Any,
    job: recording.RecordingJob,
    edit: Edit,
    plan: dict[str, Any],
    render_path: Any,
    render_meta: dict[str, Any],
) -> dict[str, Any]:
    """The render worker's narrate step, isolated from the cut's success.

    A published cut stands even when its narration fails: the failure becomes
    the narration section's error, the cut keeps its validated picture, and
    the take stays completed — the same contract the master's narration has.
    """
    from . import tts

    try:
        character = {
            **{key: plan[key] for key in NARRATE_ARGUMENT_KEYS if key in plan}
        }
        meta = perform_render_narration(
            job, edit, render_path, plan["narrate"], character
        )
    except core.ToolError as exc:
        detail = {"error": str(exc)}
        core.eprint(f"render narration failed for {job.id}: {exc}")
        return detail
    except Exception as exc:  # the cut is published; never fail the worker here
        core.eprint(f"unexpected render narration error for {job.id}: {exc!r}")
        return {"error": "unexpected render narration failure; see server log"}
    if manager is not None and job.state_dir is not None:
        try:
            from . import store as store_module

            record = dict(render_meta)
            record["narration"] = meta
            args = {
                key: value
                for key, value in plan.items()
                if key in RENDER_NARRATE_JOURNAL_KEYS
            }
            store_module.append_render_record(job.id, args, record)
        except OSError as exc:
            core.eprint(f"could not journal the render's narration for {job.id}: {exc}")
    return meta


RENDER_NARRATE_JOURNAL_KEYS = frozenset(NARRATE_ARGUMENT_KEYS) | {"narrate"}
