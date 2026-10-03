"""Encoder selection and ffmpeg argv construction.

Split out of ``recording`` because it answers a different question: not what a
take is, but how to ask the encoders for it. Every argument list the recorder
builds — capture, finalization, muxing and caption burn-in — is constructed here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from . import core
from . import recording

if TYPE_CHECKING:
    from .recording import RecordingJob


RECORDING_CAPTURE_CODEC = "libx264rgb"
RECORDING_CAPTURE_FPS = 30
RECORDING_VIDEO_FPS = 30
RECORDING_GIF_FPS = 12
RECORDING_GIF_MAX_WIDTH = 960
RECORDING_H264_ENCODERS = ("libx264",)
RECORDING_MP4_CRF = "23"
RECORDING_MP4_PRESET = "medium"
RECORDING_AUDIO_ENCODERS = {"mp4": "aac", "webm": "libopus"}
RECORDING_AUDIO_CODEC_NAMES = {"mp4": "aac", "webm": "opus"}
RECORDING_AUDIO_BITRATES = {"mp4": "128k", "webm": "96k"}
RECORDING_AUDIO_SAMPLE_RATE = 48000
RECORDING_ENCODER_PREFERENCE = ("libsvtav1", "libaom-av1")


def recording_capture_argv(job: RecordingJob) -> list[str]:
    argv = ["wf-recorder", "-D", "-o", job.output]
    if job.region is not None:
        argv.extend(
            [
                "-g",
                f"{job.region['x']},{job.region['y']} {job.region['width']}x{job.region['height']}",
            ]
        )
    argv.extend(
        [
            "-f",
            str(job.intermediate),
            "-c",
            RECORDING_CAPTURE_CODEC,
            "-r",
            str(RECORDING_CAPTURE_FPS),
            "-p",
            "preset=ultrafast",
            "-p",
            "crf=0",
            "-y",
        ]
    )
    argv.extend(capture_audio_argv(job))
    return argv


def capture_audio_argv(job: RecordingJob) -> list[str]:
    """The trailing ``-a [DEVICE]`` when a take asked for an audio track.

    ``-a`` with a device records the PulseAudio source named by ``job.source``,
    which the host resolved beforehand to the default sink's monitor (desktop
    sound) or to nothing, meaning the host's default source (a microphone).
    A `wf-recorder` built without audio support exits with code 1 on startup;
    that surfaces as the standard launch failure with the recorder's log tail.
    """
    if not recording.wants_audio(job.audio):
        return []
    argv = ["-a"]
    if job.audio_source is not None:
        argv.append(str(job.audio_source))
    return argv


def choose_video_encoder() -> str:
    result = core.run_command(["ffmpeg", "-hide_banner", "-encoders"], timeout=10.0)
    for name in RECORDING_ENCODER_PREFERENCE:
        if re.search(rf"^\s*V\S*\s+{re.escape(name)}\s", result.text, re.MULTILINE):
            return name
    raise core.ToolError(
        "no AV1 encoder available in ffmpeg (need one of: "
        + ", ".join(RECORDING_ENCODER_PREFERENCE)
        + ")"
    )


def choose_h264_encoder() -> str:
    result = core.run_command(["ffmpeg", "-hide_banner", "-encoders"], timeout=10.0)
    for name in RECORDING_H264_ENCODERS:
        if re.search(rf"^\s*V\S*\s+{re.escape(name)}\s", result.text, re.MULTILINE):
            return name
    raise core.ToolError(
        "no H.264 encoder available in ffmpeg (need one of: "
        + ", ".join(RECORDING_H264_ENCODERS)
        + ")"
    )


def av1_encoder_args(encoder: str) -> list[str]:
    """Encoder tuning shared by recording finalization and narration burn-in."""
    if encoder == "libsvtav1":
        return ["-crf", "28", "-preset", "8"]
    return ["-crf", "30", "-cpu-used", "6", "-row-mt", "1", "-tiles", "2x2"]


def sound_passthrough_argv(fmt: str) -> list[str]:
    """Map the capture's one audio stream through finalization, encoded for `fmt`.

    A capture that recorded sound (``audio=monitor``/``mic``) re-encodes that
    track beside the picture here; a silent capture keeps ``-an``. The output
    codec matches the one narration later re-adds, so both shapes satisfy the
    same artifact contract.
    """
    return ["-map", "0:v:0", "-map", "0:a:0", *audio_encode_args(fmt)]


def recording_webm_argv(
    job, output_path: Path | None = None
) -> list[str]:
    filters = [
        f"fps={RECORDING_VIDEO_FPS}",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "format=yuv420p",
    ]
    argv = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(job.intermediate),
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-vf",
        ",".join(filters),
        "-c:v",
        job.encoder,
    ]
    argv.extend(av1_encoder_args(job.encoder))
    if wants_recording_sound(job):
        argv.extend(sound_passthrough_argv("webm"))
    else:
        argv.append("-an")
    argv.extend(
        [
            "-force_key_frames",
            "expr:gte(t,n_forced*2)",
            "-f",
            "webm",
            str(output_path or job.artifact),
        ]
    )
    return argv


def recording_mp4_argv(
    job, output_path: Path | None = None
) -> list[str]:
    filters = [
        f"fps={RECORDING_VIDEO_FPS}",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "format=yuv420p",
    ]
    argv = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(job.intermediate),
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-vf",
        ",".join(filters),
        "-c:v",
        "libx264",
        "-crf",
        RECORDING_MP4_CRF,
        "-preset",
        RECORDING_MP4_PRESET,
    ]
    if wants_recording_sound(job):
        argv.extend(sound_passthrough_argv("mp4"))
    else:
        argv.append("-an")
    argv.extend(container_mux_flags("mp4"))
    argv.extend(["-f", "mp4", str(output_path or job.artifact)])
    return argv


def wants_recording_sound(job) -> bool:
    from . import recording as _r

    return _r.wants_audio(getattr(job, "audio", "off"))


def recording_gif_argv(
    job: RecordingJob, capture_width: int, output_path: Path | None = None
) -> list[str]:
    filters = [f"fps={RECORDING_GIF_FPS}"]
    if capture_width > RECORDING_GIF_MAX_WIDTH:
        filters.append(f"scale={RECORDING_GIF_MAX_WIDTH}:-1:flags=lanczos")
    filters.append(
        "split[a][b];[a]palettegen=stats_mode=diff[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    )
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(job.intermediate),
        "-vf",
        ",".join(filters),
        "-loop",
        "0",
        "-an",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-f",
        "gif",
        str(output_path or job.artifact),
    ]


def video_encode_args(fmt: str, encoder: str) -> list[str]:
    if fmt == "mp4":
        return ["-c:v", "libx264", "-crf", RECORDING_MP4_CRF, "-preset", RECORDING_MP4_PRESET]
    return ["-c:v", encoder] + av1_encoder_args(encoder)


def audio_encode_args(fmt: str) -> list[str]:
    return [
        "-c:a",
        RECORDING_AUDIO_ENCODERS[fmt],
        "-b:a",
        RECORDING_AUDIO_BITRATES[fmt],
        "-ac",
        "2",
        "-ar",
        str(RECORDING_AUDIO_SAMPLE_RATE),
    ]


def container_mux_flags(fmt: str) -> list[str]:
    return ["-movflags", "+faststart"] if fmt == "mp4" else []


def narration_mux_argv(
    job: RecordingJob, track: Path, out_path: Path, subtitle_filter: str | None = None
) -> list[str]:
    """Mux narration into the recording's container, optionally burning captions."""
    argv = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(job.artifact),
        "-i",
        str(track),
    ]
    if subtitle_filter:
        argv.extend(["-filter_complex", subtitle_filter, "-map", "[v]"])
    else:
        argv.extend(["-map", "0:v:0"])
    argv.extend(["-map", "1:a:0"])
    if subtitle_filter:
        argv.extend(video_encode_args(job.fmt, job.encoder))
    else:
        argv.extend(["-c:v", "copy"])
    argv.extend(audio_encode_args(job.fmt))
    argv.extend(container_mux_flags(job.fmt))
    argv.extend(["-map_metadata", "-1", "-f", job.fmt, str(out_path)])
    return argv
