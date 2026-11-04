"""The narration contract: parsing, anchor resolution and the perform step.

Narration is strictly opt-in and never invented: anchors come from the take's
ingested timeline (an event id, a content match, or an explicit ``at_ms``), the
prose comes from the calling agent, and seshat owns timestamping, speech
synthesis, scheduling, captions and muxing — the math lives in ``scheduling``,
the engines in ``tts``, the captions in ``subtitles``.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import core, encoding, media, recording, scheduling, subtitles, timeline, tts
from .scheduling import (
    NarrationSchedule,
    narration_track_argv,
    schedule_meta,
    schedule_narration,
    segment_texts,
)


def recording_capture_ms(job: recording.RecordingJob) -> float:
    end = job.ended_monotonic if job.ended_monotonic is not None else time.monotonic()
    return round(max(end - job.started_monotonic, 0.0) * 1000.0, 3)


def recording_duration_ms(job: recording.RecordingJob) -> float:
    """Playable extent of the take's master artifact, in milliseconds.

    The master is what narration builds from and what anchors map to; the
    narrated deliverable is rebuilt from scratch on every run, so re-narration
    never compounds captions or encodes. The video stream's own extent is
    preferred over the container's, because an anchor placed in the gap between
    the last frame and the end of the container has no picture to point at.
    """
    return media.probe_duration_ms(job.artifact)


@dataclass
class NarrationSegment:
    index: int
    anchor_event_id: int | None
    anchor_at_ms: float | None
    anchor_match: dict[str, Any] | None
    resolved_ms: float
    text: str


@dataclass
class NarrationRequest:
    segments: list[NarrationSegment]
    engine: str
    voice: str | None
    offset_ms: float
    fit: str
    tail_ms: float
    subtitles: bool = True
    duration_ms: float | None = None
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"
    length_scale: float = 1.0
    position: str = "bottom"
    variant: str | None = None
    dry_run: bool = False

    def character(self) -> tts.TtsCharacter:
        return tts.TtsCharacter(
            rate=self.rate, volume=self.volume, pitch=self.pitch, length_scale=self.length_scale
        )


NARRATION_ARGUMENT_KEYS = (
    "segments", "engine", "voice", "offset_ms", "fit", "tail_ms", "subtitles",
    "rate", "volume", "pitch", "length_scale", "position", "variant", "dry_run",
)
NARRATION_POSITIONS = ("bottom", "top", "auto")
ANCHOR_KEYS = ("event_id", "at_ms", "match")
MATCH_KEYS = ("tool", "payload", "nth")


def _match_anchor_ms(raw: dict[str, Any], job: recording.RecordingJob) -> float:
    """Resolve a content anchor to one real event, or refuse.

    The match is a tool name and/or a payload subset; ``nth`` picks among the
    candidates, defaulting to the first. Refusals name what was ingested so the
    caller can fix the script instead of guessing.
    """
    tool = raw.get("tool")
    payload = raw.get("payload")
    nth = raw.get("nth", 1)
    if not isinstance(raw, dict):
        raise core.ToolError("anchor.match must be an object")
    if tool is not None and (not isinstance(tool, str) or not tool):
        raise core.ToolError("anchor.match.tool must be a non-empty string")
    if payload is not None and not isinstance(payload, dict):
        raise core.ToolError("anchor.match.payload must be an object")
    nth = core.strict_int(nth, "anchor.match.nth")
    if nth < 1:
        raise core.ToolError("anchor.match.nth must be at least 1")
    ordered = timeline.ordered_events(job)
    candidates = []
    for event in ordered:
        if tool is not None and str(event.get("tool")) != tool:
            continue
        if payload:
            event_payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if any(event_payload.get(key) != value for key, value in payload.items()):
                continue
        candidates.append(event)
    if not candidates:
        raise core.ToolError(
            "the take timeline has no event matching this anchor "
            f"({len(ordered)} event(s) ingested; the match named tool={tool!r}, "
            f"payload keys={sorted(payload) if payload else []})"
        )
    if nth > len(candidates):
        raise core.ToolError(
            f"anchor.match.nth {nth} exceeds the {len(candidates)} matching event(s)"
        )
    return timeline.recording_relative_ms(job.started_monotonic, candidates[nth - 1]["at_monotonic"])


def parse_narration_segment(
    raw: Any, index: int, job: recording.RecordingJob, media_ms: float
) -> NarrationSegment:
    if not isinstance(raw, dict):
        raise core.ToolError(f"segments[{index}] must be an object")
    unknown = set(raw) - {"anchor", "text"}
    if unknown:
        raise core.ToolError(f"segments[{index}] has unknown key(s): {', '.join(sorted(unknown))}")
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        raise core.ToolError(f"segments[{index}].text must be a non-empty string")
    if "\x00" in text:
        raise core.ToolError(f"segments[{index}].text must not contain NUL bytes")
    if len(text) > tts.NARRATION_MAX_SEGMENT_CHARS:
        raise core.ToolError(
            f"segments[{index}].text exceeds {tts.NARRATION_MAX_SEGMENT_CHARS} characters"
        )
    anchor = raw.get("anchor")
    if not isinstance(anchor, dict):
        raise core.ToolError(f"segments[{index}].anchor must be an object")
    unknown = set(anchor) - set(ANCHOR_KEYS)
    if unknown:
        raise core.ToolError(
            f"segments[{index}].anchor has unknown key(s): {', '.join(sorted(unknown))}"
        )
    kinds = [key for key in ANCHOR_KEYS if key in anchor]
    if len(kinds) != 1:
        raise core.ToolError(
            f"segments[{index}].anchor must contain exactly one of " + ", ".join(ANCHOR_KEYS)
        )
    kind = kinds[0]
    if kind == "match":
        match = anchor["match"]
        if not isinstance(match, dict) or set(match) - set(MATCH_KEYS) or not match:
            raise core.ToolError(
                f"segments[{index}].anchor.match must be a non-empty object with at most "
                + ", ".join(MATCH_KEYS)
            )
        anchor_ms = _match_anchor_ms(match, job)
        if anchor_ms > media_ms + tts.NARRATION_ANCHOR_SLACK_MS:
            raise core.ToolError(
                f"segments[{index}].anchor.match resolves to {anchor_ms:g} ms, past "
                f"the end of the playable video ({media_ms:g} ms)"
            )
        return NarrationSegment(index, None, None, dict(match), anchor_ms, text)
    if kind == "event_id":
        event_id = core.strict_int(anchor["event_id"], f"segments[{index}].anchor.event_id")
        event_times = timeline.event_times_ms(job)
        if event_id not in event_times:
            raise core.ToolError(
                f"segments[{index}].anchor.event_id {event_id} is not in the take timeline "
                f"({len(event_times)} event(s) ingested; an event id only exists when the tool "
                "server that performed the action publishes a timeline stream)"
            )
        event_ms = event_times[event_id]
        if event_ms > media_ms + tts.NARRATION_ANCHOR_SLACK_MS:
            raise core.ToolError(
                f"segments[{index}].anchor.event_id {event_id} resolves to {event_ms:g} ms, past "
                f"the end of the playable video ({media_ms:g} ms): the picture it anchors to is "
                "not in the artifact (a take stopped by its own deadline can lose its tail)"
            )
        return NarrationSegment(index, event_id, None, None, event_ms, text)
    at_ms = core.strict_number(anchor["at_ms"], f"segments[{index}].anchor.at_ms", minimum=0.0)
    if at_ms > media_ms + tts.NARRATION_ANCHOR_SLACK_MS:
        raise core.ToolError(
            f"segments[{index}].anchor.at_ms {at_ms:g} is beyond the recording duration"
        )
    return NarrationSegment(index, None, at_ms, None, at_ms, text)


def _character_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise core.ToolError(f"{name} must be a string")
    return value.strip()


def _optional_variant(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise core.ToolError("variant must be a non-empty string")
    variant = value.strip()
    if not variant.isalnum() or len(variant) > 16:
        raise core.ToolError("variant must be alphanumeric and at most 16 characters")
    return variant


def parse_narration_arguments(
    arguments: dict[str, Any],
    job: recording.RecordingJob,
    duration_ms: float | None = None,
) -> NarrationRequest:
    """Parse and validate the voiceover argument set against the take."""
    if not isinstance(arguments, dict):
        raise core.ToolError("narration arguments must be an object")
    unknown = set(arguments) - set(NARRATION_ARGUMENT_KEYS) - {"id"}
    if unknown:
        raise core.ToolError(f"unknown narration key(s): {', '.join(sorted(unknown))}")
    raw_segments = arguments.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise core.ToolError("segments must be a non-empty list")
    engine = arguments.get("engine", "auto")
    if engine not in tts.NARRATION_ENGINES:
        raise core.ToolError("engine must be one of: " + ", ".join(tts.NARRATION_ENGINES))
    fit = arguments.get("fit", "natural")
    if fit not in tts.NARRATION_FITS:
        raise core.ToolError("fit must be one of: " + ", ".join(tts.NARRATION_FITS))
    voice = arguments.get("voice")
    if voice is not None and (not isinstance(voice, str) or not voice.strip()):
        raise core.ToolError("voice must be a non-empty string")
    offset_ms = core.strict_number(
        arguments.get("offset_ms", 0.0),
        "offset_ms",
        minimum=-tts.NARRATION_MAX_OFFSET_MS,
        maximum=tts.NARRATION_MAX_OFFSET_MS,
    )
    tail_ms = core.strict_number(
        arguments.get("tail_ms", tts.NARRATION_DEFAULT_TAIL_MS),
        "tail_ms",
        minimum=0.0,
        maximum=tts.NARRATION_MAX_TAIL_MS,
    )
    subtitles_enabled = arguments.get("subtitles", True)
    if not isinstance(subtitles_enabled, bool):
        raise core.ToolError("subtitles must be a boolean")
    rate = _character_text(arguments.get("rate", tts.NARRATION_EDGE_RATE_DEFAULT), "rate")
    volume = _character_text(arguments.get("volume", "+0%"), "volume")
    pitch = _character_text(arguments.get("pitch", "+0Hz"), "pitch")
    length_scale = core.strict_number(
        arguments.get("length_scale", 1.0), "length_scale", minimum=0.1, maximum=3.0
    )
    position = arguments.get("position", "bottom")
    if position not in NARRATION_POSITIONS:
        raise core.ToolError("position must be one of: " + ", ".join(NARRATION_POSITIONS))
    variant = _optional_variant(arguments.get("variant"))
    dry_run = arguments.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise core.ToolError("dry_run must be a boolean")
    if duration_ms is None:
        duration_ms = recording_duration_ms(job)
    segments = [
        parse_narration_segment(raw, index + 1, job, duration_ms)
        for index, raw in enumerate(raw_segments)
    ]
    if sum(len(segment.text) for segment in segments) > core.TEXT_LIMIT:
        raise core.ToolError(f"narration text exceeds the total limit of {core.TEXT_LIMIT} characters")
    return NarrationRequest(
        segments=segments,
        engine=engine,
        voice=voice,
        offset_ms=offset_ms,
        fit=fit,
        tail_ms=tail_ms,
        subtitles=subtitles_enabled,
        duration_ms=duration_ms,
        rate=rate,
        volume=volume,
        pitch=pitch,
        length_scale=length_scale,
        position=position,
        variant=variant,
        dry_run=dry_run,
    )


def resolve_segment_anchors(request: NarrationRequest) -> list[tuple[NarrationSegment, float]]:
    anchored: list[tuple[NarrationSegment, float]] = []
    for segment in request.segments:
        anchored.append((segment, segment.resolved_ms + request.offset_ms))
    anchored.sort(key=lambda item: (item[1], item[0].index))
    return anchored


def deliverable_path(job: recording.RecordingJob, variant: str | None = None) -> Path:
    """Where a narrated render lands: `<id>.narrated.<fmt>`, or `<id>.<variant>.<fmt>`.

    Every render is rebuilt from the master, so variants overwrite only their
    own name and the master stays untouched.
    """
    if variant:
        return job.artifact.with_name(f"{job.id}.{variant}.{job.fmt}")
    return job.narrated_path


def perform_narration(
    job: recording.RecordingJob, request: NarrationRequest, engine: tts.TtsEngine
) -> dict[str, Any]:
    duration_ms = request.duration_ms
    if duration_ms is None:
        duration_ms = recording_duration_ms(job)
    anchored = resolve_segment_anchors(request)
    workdir = job.directory / f"{job.id}.narration"
    workdir.mkdir(mode=recording.RECORDING_DIR_MODE, exist_ok=True)
    os.chmod(workdir, recording.RECORDING_DIR_MODE)
    character = request.character()
    try:
        clips: dict[int, tts.TtsClip] = {}
        for segment, _ in anchored:
            suffix = ".mp3" if engine.name == "edge" else ".wav"
            out_path = workdir / f"segment-{segment.index:03d}{suffix}"
            clips[segment.index] = engine.synthesize(
                segment.text, request.voice, out_path, character
            )
        schedule = schedule_narration(anchored, clips, duration_ms, request)
        captions = caption_plan(job, schedule, request, clips)
        texts = segment_texts([(segment, segment.resolved_ms) for segment in request.segments])
        schedule_payload: dict[str, Any] = schedule_meta(schedule, anchored, texts)
        schedule_payload.update(
            {
                "engine": engine.name,
                "voice": request.voice or getattr(engine, "default_voice", None),
                "network": bool(getattr(engine, "network", False)),
                "character": {
                    "rate": request.rate,
                    "volume": request.volume,
                    "pitch": request.pitch,
                    "length_scale": request.length_scale,
                },
                "duration_ms": duration_ms,
                "captions": captions,
            }
        )
        if request.dry_run:
            # Plan mode: real synthesis and scheduling, no mux, no publish. The
            # caller uses the durations to pace a demonstration before recording.
            schedule_payload["dry_run"] = True
            schedule_payload["overruns"] = scheduling.overruns(schedule, anchored, duration_ms)
            return schedule_payload
        extend_seconds = max(0.0, (schedule.total_ms - duration_ms) / 1000.0)
        # A take that captured sound mixes its own track under the voice, ducked
        # while the voice speaks; a silent master keeps the track voice-only.
        bed_path = job.artifact if recording.wants_audio(job.audio) else None
        track_path = workdir / "narration.wav"
        core.run_command(
            narration_track_argv(anchored, clips, schedule, track_path, bed_path),
            timeout=tts.NARRATION_BUILD_TIMEOUT_SECONDS,
        )
        deliverable = deliverable_path(job, request.variant)
        temp_path = deliverable.with_name(deliverable.name + ".part")
        subtitle_filter = None
        if request.subtitles and captions:
            document = subtitles.build_ass_document(captions, job.width, job.height)
            subtitle_path = subtitles.write_subtitle_file(workdir, document)
            pad_prefix = (
                f"tpad=stop_mode=clone:stop_duration={extend_seconds:.3f},"
                if extend_seconds > 0
                else ""
            )
            subtitle_filter = subtitles.video_filter(subtitle_path, pad_prefix)
        if captions:
            schedule_payload["soft_captions"] = subtitles.soft_caption_paths(
                job, deliverable, captions
            )
        core.run_command(
            encoding.narration_mux_argv(
                job, track_path, temp_path, subtitle_filter, extend_seconds
            ),
            timeout=tts.NARRATION_BUILD_TIMEOUT_SECONDS,
        )
        try:
            recording.validate_recording_artifact(job, expect_audio=True, path=temp_path)
            os.chmod(temp_path, recording.RECORDING_FILE_MODE)
            os.replace(temp_path, deliverable)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        schedule_payload.update(
            {
                "offset_ms": request.offset_ms,
                "fit": request.fit,
                "subtitles": request.subtitles,
                "position": request.position,
                "extended_ms": round(extend_seconds * 1000.0, 3),
                "path": str(deliverable),
                "variant": request.variant,
            }
        )
        return schedule_payload
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def caption_plan(
    job: recording.RecordingJob,
    schedule: NarrationSchedule,
    request: NarrationRequest,
    clips: dict[int, tts.TtsClip],
) -> list[dict[str, Any]]:
    """Caption phrases for the whole narration, timed and placed.

    Cue-timed where the engine provides cues, character-proportional where it
    does not; `position="auto"` lifts a caption to the top when the events in
    its window acted in the bottom third of the picture — the agent's events,
    read only for placement, never for prose.
    """
    texts = segment_texts([(segment, segment.resolved_ms) for segment in request.segments])
    entries: list[dict[str, Any]] = []
    for item in schedule.segments:
        cues = clips[item.index].words
        phrases = scheduling.phrase_caption_entries(
            texts[item.index], item.start_ms, item.clip_ms, item.lead_silence_ms, item.tempo, cues
        )
        entries.extend([dict(phrase, index=item.index) for phrase in phrases])
    if request.position == "auto":
        height = max(job.height, 1)
        for entry in entries:
            if window_acted_low(job, entry["start_ms"], entry["end_ms"], height):
                entry["align"] = "top"
    return entries


def window_acted_low(job: recording.RecordingJob, start_ms: float, end_ms: float, height: int) -> bool:
    """Whether an event inside the caption's window acted in the bottom third."""
    for event in timeline.ordered_events(job):
        t_ms = timeline.recording_relative_ms(job.started_monotonic, event["at_monotonic"])
        if not (start_ms <= t_ms <= end_ms):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        y = payload.get("y")
        if y is None:
            continue
        try:
            if float(y) >= height * 2 / 3:
                return True
        except (TypeError, ValueError):
            continue
    return False