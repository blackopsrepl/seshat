"""Scheduling and every audio argv narration builds.

Split out of ``narration`` because that module owns the contract (parsing,
anchors, the one perform step) and this one owns the math and the ffmpeg argv:
how spoken segments are placed in time, compressed, mixed with the captured
bed, ducked and normalized.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import tts


@dataclass
class ScheduledSegment:
    index: int
    anchor_ms: float
    start_ms: float
    clip_ms: float
    duration_ms: float
    lead_silence_ms: float
    shift_ms: float
    tempo: float
    compressed: bool
    word_count: int


@dataclass
class NarrationSchedule:
    segments: list[ScheduledSegment]
    total_ms: float


CAPTION_MAX_CHARS = 84
CAPTION_MAX_LINES = 2


def atempo_filters(ratio: float) -> list[str]:
    filters: list[str] = []
    remaining = ratio
    guard = 0
    while remaining > tts.NARRATION_TEMPO_MAX and guard < tts.NARRATION_TEMPO_MAX_FILTERS:
        filters.append(f"atempo={tts.NARRATION_TEMPO_MAX:g}")
        remaining /= tts.NARRATION_TEMPO_MAX
        guard += 1
    while remaining < tts.NARRATION_TEMPO_MIN and guard < tts.NARRATION_TEMPO_MAX_FILTERS:
        filters.append(f"atempo={tts.NARRATION_TEMPO_MIN:g}")
        remaining /= tts.NARRATION_TEMPO_MIN
        guard += 1
    filters.append(f"atempo={remaining:.6f}")
    return filters


def schedule_narration(
    anchored: list[tuple[Any, float]],
    clips: dict[int, tts.TtsClip],
    capture_ms: float,
    request: Any,
) -> NarrationSchedule:
    """Place every spoken segment between its anchor and the next one.

    ``fit="natural"`` leaves a segment that outgrows its gap to push the next
    anchor later (reported as ``shift_ms``); ``fit="compress"`` time-compresses
    the clip to fit the gap. The recording's length governs the total.
    """
    scheduled: list[ScheduledSegment] = []
    previous_end = 0.0
    for position, (segment, anchor_ms) in enumerate(anchored):
        clip = clips[segment.index]
        lead_silence_ms = min(clip.lead_silence_ms, tts.NARRATION_MAX_LEAD_SILENCE_MS)
        clip_ms = max(clip.duration_ms - lead_silence_ms, 0.0)
        start_ms = max(anchor_ms, previous_end + tts.NARRATION_MIN_GAP_MS)
        tempo = 1.0
        compressed = False
        if request.fit == "compress":
            if position + 1 < len(anchored):
                limit_ms = anchored[position + 1][1] - tts.NARRATION_MIN_GAP_MS
            else:
                limit_ms = capture_ms
            available = limit_ms - start_ms
            if available > 0 and clip_ms > available:
                tempo = clip_ms / available
                clip_ms = clip_ms / tempo
                compressed = True
        scheduled.append(
            ScheduledSegment(
                index=segment.index,
                anchor_ms=anchor_ms,
                start_ms=start_ms,
                clip_ms=clip_ms,
                duration_ms=clip.duration_ms,
                lead_silence_ms=lead_silence_ms,
                shift_ms=start_ms - anchor_ms,
                tempo=tempo,
                compressed=compressed,
                word_count=len(clip.words),
            )
        )
        previous_end = start_ms + clip_ms
    total_ms = max(capture_ms, previous_end + request.tail_ms)
    return NarrationSchedule(segments=scheduled, total_ms=total_ms)


def segment_texts(anchored: list[tuple[Any, float]]) -> dict[int, str]:
    return {segment.index: segment.text for segment, _ in anchored}


def overruns(
    schedule: NarrationSchedule, anchored: list[tuple[Any, float]], capture_ms: float
) -> list[dict[str, float]]:
    """Segments whose speech outlasts the playable master — plan-mode facts."""
    return [
        {"index": item.index, "start_ms": item.start_ms, "end_ms": item.start_ms + item.clip_ms}
        for item in schedule.segments
        if item.start_ms + item.clip_ms > capture_ms
    ]


def schedule_meta(
    schedule: NarrationSchedule,
    anchored: list[tuple[Any, float]],
    texts: dict[int, str],
) -> dict[str, Any]:
    """The durable record of one schedule: exactly where every line lands."""
    return {
        "segment_count": len(schedule.segments),
        "total_duration_ms": round(schedule.total_ms, 3),
        "segments": [
            {
                "index": item.index,
                "anchor_ms": round(item.anchor_ms, 3),
                "start_ms": round(item.start_ms, 3),
                "duration_ms": round(item.duration_ms, 3),
                "text": texts.get(item.index, ""),
                "lead_silence_ms": round(item.lead_silence_ms, 3),
                "shift_ms": round(item.shift_ms, 3),
                "tempo": round(item.tempo, 6),
                "compressed": item.compressed,
                "word_count": item.word_count,
            }
            for item in schedule.segments
        ],
    }


def phrase_caption_entries(
    text: str,
    start_ms: float,
    clip_ms: float,
    lead_silence_ms: float,
    tempo: float,
    cues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Chunk one narrated segment into caption phrases timed to what is heard.

    A phrase that matches an engine cue exactly — edge-tts ships sentence cues
    with its media, and a short segment's sentence is one phrase — takes the
    cue's own window, lead-trimmed and tempo-mapped. Every other phrase shares
    the spoken span proportionally by characters: approximate by construction,
    and only ever a caption placement, never an anchor.
    """
    phrases = split_phrases(text)
    if not phrases:
        return []
    total_chars = sum(len(phrase) for phrase in phrases)
    cue_windows: list[tuple[str, float, float]] = []
    for cue in cues or []:
        cue_text = " ".join(str(cue.get("text", "")).split())
        if not cue_text:
            continue
        try:
            cue_start = max(float(cue["start_ms"]) - lead_silence_ms, 0.0)
            cue_end = max(float(cue["end_ms"]) - lead_silence_ms, 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        cue_windows.append((cue_text, cue_start, cue_end))
    captions: list[dict[str, Any]] = []
    consumed = 0
    for phrase in phrases:
        exact = next((window for window in cue_windows if window[0] == " ".join(phrase.split())), None)
        char_start = consumed * clip_ms / max(total_chars, 1)
        consumed += len(phrase)
        char_end = consumed * clip_ms / max(total_chars, 1)
        if exact is not None:
            begin, end = exact[1] / max(tempo, 1e-6), exact[2] / max(tempo, 1e-6)
        else:
            begin, end = char_start / max(tempo, 1e-6), char_end / max(tempo, 1e-6)
        begin = max(min(begin, clip_ms), 0.0)
        end = max(min(max(end, begin + 1.0), clip_ms), 0.0)
        captions.append(
            {"start_ms": round(start_ms + begin, 3), "end_ms": round(start_ms + end, 3), "text": phrase}
        )
    return captions


def split_phrases(text: str) -> list[str]:
    """Sentence-shaped phrases a caption can carry: at most two lines of ~84."""
    limit = CAPTION_MAX_CHARS * CAPTION_MAX_LINES
    phrases: list[str] = []
    for sentence in re.split(r"(?<=[.!?;:]) +", text.strip()):
        words = sentence.split()
        if not words:
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) > limit and current:
                phrases.append(current)
                current = word
            else:
                current = candidate
        if current:
            phrases.append(current)
    return phrases


def narration_track_argv(
    anchored: list[tuple[Any, float]],
    clips: dict[int, tts.TtsClip],
    schedule: NarrationSchedule,
    out_path: Path,
    bed_path: Path | None = None,
) -> list[str]:
    """Build the narration track: every clip on a silent carrier, and — when the
    take captured sound — the captured desktop track mixed underneath, ducked
    while the voice speaks, and the whole mix normalized to one loudness.

    The bed enters this one audio pipeline instead of the later mux so the mix
    is a single amix decision and the deliverable never loses it: the mux maps
    only this track's audio, which already carries both voices.
    """
    argv = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for segment, _ in anchored:
        argv.extend(["-i", str(clips[segment.index].path)])
    bed_index = len(anchored)
    if bed_path is not None:
        argv.extend(["-i", str(bed_path)])
    total_seconds = schedule.total_ms / 1000.0
    parts = [
        "anullsrc=channel_layout=stereo:sample_rate="
        f"{tts.NARRATION_SAMPLE_RATE}:duration={total_seconds:.3f}[silence]",
        "anullsrc=channel_layout=stereo:sample_rate="
        f"{tts.NARRATION_SAMPLE_RATE}:duration={total_seconds:.3f}[pad]",
    ]
    labels = ["[silence]"]
    for position, item in enumerate(schedule.segments):
        filters: list[str] = []
        if item.lead_silence_ms > 0:
            filters.append(f"atrim=start={item.lead_silence_ms / 1000.0:.6f}")
        filters.append("asetpts=PTS-STARTPTS")
        if abs(item.tempo - 1.0) > 1e-9:
            filters.extend(atempo_filters(item.tempo))
        filters.append(f"aresample={tts.NARRATION_SAMPLE_RATE}")
        filters.append("aformat=channel_layouts=stereo")
        delay = int(round(item.start_ms))
        filters.append(f"adelay={delay}|{delay}")
        label = f"[n{position}]"
        parts.append(f"[{position}:a]" + ",".join(filters) + label)
        labels.append(label)
    parts.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0[voice]"
    )
    if bed_path is None:
        # The pads are what hold the track to the schedule's full length.
        parts.append("[voice][pad]amix=inputs=2:normalize=0:dropout_transition=0[aout]")
    else:
        parts.append(
            f"[{bed_index}:a]aresample={tts.NARRATION_SAMPLE_RATE}"
            ",aformat=channel_layouts=stereo[bedsrc]"
        )
        parts.append("[voice]asplit=2[vduck][vmix]")
        parts.append(
            f"[bedsrc][vduck]sidechaincompress=threshold={tts.NARRATION_DUCK_THRESHOLD:g}"
            f":ratio={tts.NARRATION_DUCK_RATIO:g}"
            f":attack={tts.NARRATION_DUCK_ATTACK_MS:g}"
            f":release={tts.NARRATION_DUCK_RELEASE_MS:g}[ducked]"
        )
        parts.append("[vmix][ducked][pad]amix=inputs=3:normalize=0:dropout_transition=0[aout]")
    # Everything a caller hears — voice and bed — leaves at one loudness.
    parts.append(
        f"[aout]loudnorm=I={tts.NARRATION_LOUDNESS_TARGET:g}:TP={tts.NARRATION_LOUDNESS_TP:g}"
        f":LRA={tts.NARRATION_LOUDNESS_LRA:g}[loud]"
    )
    argv.extend(
        [
            "-filter_complex",
            ";".join(parts),
            "-map",
            "[loud]",
            "-t",
            f"{total_seconds:.3f}",
            "-ac",
            "2",
            "-ar",
            str(tts.NARRATION_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(out_path),
        ]
    )
    return argv