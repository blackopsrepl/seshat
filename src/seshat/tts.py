from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import core, media


NARRATION_ENGINES = ("auto", "edge", "piper")
# engine="auto" is offline-only: the same rule as audio capture. Nothing leaves
# the host unless the caller explicitly asks for it, and edge-tts sends the
# narration prose to the network.
NARRATION_ENGINE_PREFERENCE = ("piper",)
NARRATION_EDGE_PREFERENCE_LABEL = "edge"
NARRATION_FITS = ("natural", "compress")
NARRATION_EDGE_COMMAND = "edge-tts"
NARRATION_EDGE_DEFAULT_VOICE = "en-US-EmmaMultilingualNeural"
NARRATION_PIPER_COMMAND = "piper"
NARRATION_PIPER_MODEL_ENV = "SESHAT_PIPER_MODEL"
NARRATION_MIN_GAP_MS = 60.0
NARRATION_DEFAULT_TAIL_MS = 300.0
NARRATION_MAX_TAIL_MS = 2000.0
NARRATION_MAX_OFFSET_MS = 5000.0
NARRATION_MAX_SEGMENT_CHARS = 2000
NARRATION_MAX_LEAD_SILENCE_MS = 1500.0
NARRATION_SAMPLE_RATE = 48000
NARRATION_AUDIO_BITRATE = "96k"
NARRATION_TEMPO_MIN = 0.5
NARRATION_TEMPO_MAX = 2.0
NARRATION_TEMPO_MAX_FILTERS = 24
NARRATION_SYNTH_TIMEOUT_SECONDS = 180.0
NARRATION_BUILD_TIMEOUT_SECONDS = 300.0
NARRATION_ANCHOR_SLACK_MS = 250.0
# Ducking: the captured desktop track is compressed while the voice speaks.
NARRATION_DUCK_THRESHOLD = 0.03
NARRATION_DUCK_RATIO = 4.0
NARRATION_DUCK_ATTACK_MS = 20
NARRATION_DUCK_RELEASE_MS = 250


@dataclass
class TtsClip:
    path: Path
    duration_ms: float
    lead_silence_ms: float
    words: list[dict[str, Any]]


def parse_vtt_word_boundaries(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})[^\n]*\n(.*?)(?:\n\s*\n|\Z)",
        re.DOTALL,
    )
    words: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        hours, minutes, seconds, millis, end_h, end_m, end_s, end_ms, label = match.groups()
        label = " ".join(label.split())
        if not label:
            continue
        start_total = ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)
        end_total = ((int(end_h) * 60 + int(end_m)) * 60 + int(end_s)) * 1000 + int(end_ms)
        words.append({"text": label, "start_ms": start_total, "end_ms": end_total})
    return words


def parse_lead_silence_ms(stderr_text: str) -> float:
    starts = [float(value) for value in re.findall(r"silence_start:\s*(-?[\d.]+)", stderr_text)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*(-?[\d.]+)", stderr_text)]
    if not starts or not ends or starts[0] > 0.05:
        return 0.0
    return round(min(max(ends[0], 0.0) * 1000.0, NARRATION_MAX_LEAD_SILENCE_MS), 3)


def probe_audio_duration_ms(path: Path) -> float:
    probe = media.probe_media(path)
    try:
        duration = float((probe.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0.0:
        for stream in probe.get("streams") or []:
            if stream.get("codec_type") == "audio":
                try:
                    duration = float(stream.get("duration") or 0.0)
                except (TypeError, ValueError):
                    duration = 0.0
                if duration > 0.0:
                    break
    if duration <= 0.0:
        raise core.ToolError(f"could not read audio duration for {path.name}")
    return round(duration * 1000.0, 3)


def detect_lead_silence_ms(path: Path) -> float:
    result = core.run_command(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "silencedetect=noise=-40dB:d=0.02",
            "-f",
            "null",
            "-",
        ],
        timeout=30.0,
    )
    return parse_lead_silence_ms(result.stderr.decode("utf-8", errors="replace"))


class TtsEngine:
    name = "base"
    default_voice: str | None = None
    network = False

    def synthesize(self, text: str, voice: str | None, out_path: Path) -> TtsClip:
        raise NotImplementedError


class PiperTtsEngine(TtsEngine):
    name = "piper"
    default_voice = None
    network = False

    def synthesize(self, text: str, voice: str | None, out_path: Path) -> TtsClip:
        core.require_binaries([NARRATION_PIPER_COMMAND, "ffmpeg", "ffprobe"])
        model = voice or os.environ.get(NARRATION_PIPER_MODEL_ENV)
        if not model:
            raise core.ToolError(
                f"piper needs a voice model: pass voice or set {NARRATION_PIPER_MODEL_ENV}"
            )
        if not Path(model).is_file():
            raise core.ToolError(f"piper model not found: {model}")
        core.run_command(
            [NARRATION_PIPER_COMMAND, "--model", model, "--output_file", str(out_path)],
            input_text=text + "\n",
            timeout=NARRATION_SYNTH_TIMEOUT_SECONDS,
        )
        duration_ms = probe_audio_duration_ms(out_path)
        lead_silence_ms = min(detect_lead_silence_ms(out_path), NARRATION_MAX_LEAD_SILENCE_MS)
        return TtsClip(out_path, duration_ms, lead_silence_ms, [])


class EdgeTtsEngine(TtsEngine):
    name = "edge"
    # Pinned rather than whatever the binary's current default is, so narration
    # does not drift when edge-tts changes its default; the resolved voice is
    # always in the result.
    default_voice = NARRATION_EDGE_DEFAULT_VOICE
    network = True

    def synthesize(self, text: str, voice: str | None, out_path: Path) -> TtsClip:
        core.require_binaries([NARRATION_EDGE_COMMAND, "ffmpeg", "ffprobe"])
        vtt_path = out_path.with_suffix(".vtt")
        argv = [
            NARRATION_EDGE_COMMAND,
            "--text",
            text,
            "--write-media",
            str(out_path),
            "--write-subtitles",
            str(vtt_path),
        ]
        argv.extend(["--voice", voice or NARRATION_EDGE_DEFAULT_VOICE])
        core.run_command(argv, timeout=NARRATION_SYNTH_TIMEOUT_SECONDS)
        try:
            vtt_text = vtt_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            vtt_text = ""
        words = parse_vtt_word_boundaries(vtt_text)
        duration_ms = probe_audio_duration_ms(out_path)
        if words:
            lead_silence_ms = float(words[0]["start_ms"])
        else:
            lead_silence_ms = detect_lead_silence_ms(out_path)
        return TtsClip(
            out_path, duration_ms, min(lead_silence_ms, NARRATION_MAX_LEAD_SILENCE_MS), words
        )


def resolve_tts_engine(requested: str) -> TtsEngine:
    """Resolve the engine argument.

    ``auto`` is offline-only: piper with a model, or the take is refused with
    the opt-in named. ``edge`` is an explicit choice to send the narration
    prose to Microsoft over the network; ``piper`` stays on the host.
    """
    if requested == "edge":
        if not core.command_available(NARRATION_EDGE_COMMAND):
            raise core.ToolError(f"edge-tts is not installed (need {NARRATION_EDGE_COMMAND})")
        return EdgeTtsEngine()
    if requested == "piper":
        if not core.command_available(NARRATION_PIPER_COMMAND):
            raise core.ToolError(f"piper is not installed (need {NARRATION_PIPER_COMMAND})")
        return PiperTtsEngine()
    if requested == "auto":
        if not core.command_available(NARRATION_PIPER_COMMAND):
            raise core.ToolError(
                'no offline TTS engine for engine="auto"; install piper and a voice '
                'model to keep narration on the host, or pass engine="edge" to send '
                "the narration prose to Microsoft over the network"
            )
        return PiperTtsEngine()
    raise core.ToolError("engine must be one of: " + ", ".join(NARRATION_ENGINES))
