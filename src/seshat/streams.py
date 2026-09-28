"""Cross-server timeline event streams.

A take is narratable only if seshat can see what the agent did, and seshat does
not own those actions: the agent's clicks and keystrokes are dispatched by
whatever tool server the harness called. So the actions are published instead of
captured. Any tool server that wants its actions to be narratable appends one
JSON object per line to a stream file, and seshat merges every stream that
overlaps the take window into the take timeline.

Contract — one JSON object per line, UTF-8, newline-terminated::

    {"at_monotonic": 12345.678, "tool": "click", "ok": true,
     "payload": {"x": 640, "y": 360}, "source": "computer-use-sway"}

- ``at_monotonic`` (required) is CLOCK_MONOTONIC seconds, the value of
  ``time.monotonic()`` in the emitting process. That clock is host-wide, which
  is exactly what makes an unrelated process's timestamps comparable with the
  recording epoch; no handshake or shared session id is needed.
- ``tool`` (required) is the emitting server's tool name.
- ``ok``, ``payload`` and ``source`` are optional. ``source`` defaults to the
  file stem and identifies the emitter in the merged timeline.
- Streams live in ``$XDG_RUNTIME_DIR/seshat/streams/<source>.jsonl`` (0700).
  Each emitter owns its own file and should truncate it when a new session
  starts so it cannot grow without bound.

Unusable lines are counted, never fatal: a malformed line must not be able to
destroy a take that is otherwise fine.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import core


STREAM_SUFFIX = ".jsonl"
STREAM_DIRECTORY_NAME = "streams"
STREAM_MAX_LINE_BYTES = 8_192
STREAM_MAX_EVENTS = 5_000
STREAM_DIR_MODE = 0o700
STREAM_FILE_MODE = 0o600


def runtime_root() -> Path:
    """The private runtime root seshat keeps streams and recordings under."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise core.ToolError("XDG_RUNTIME_DIR is not set")
    return Path(runtime_dir) / "seshat"


def stream_directory() -> Path:
    return runtime_root() / STREAM_DIRECTORY_NAME


def stream_path(source: str) -> Path:
    """Stream file for ``source``; the name must not escape the directory."""
    if not isinstance(source, str) or not source:
        raise core.ToolError("source must be a non-empty string")
    if "/" in source or "\\" in source or source in {".", ".."}:
        raise core.ToolError("source must not contain path separators")
    return stream_directory() / f"{source}{STREAM_SUFFIX}"


def append_event(
    source: str,
    tool: str,
    at_monotonic: float | None = None,
    ok: bool = True,
    payload: dict[str, Any] | None = None,
    path: Path | None = None,
) -> Path:
    """Append one event to ``source``'s stream, creating directory and file.

    The whole record is written with a single ``O_APPEND`` write, so emitters in
    separate processes cannot interleave inside a line.
    """
    if not isinstance(tool, str) or not tool:
        raise core.ToolError("tool must be a non-empty string")
    target = Path(path) if path is not None else stream_path(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=STREAM_DIR_MODE)
    record = {
        "at_monotonic": float(at_monotonic if at_monotonic is not None else time.monotonic()),
        "tool": tool,
        "ok": bool(ok),
        "payload": dict(payload or {}),
        "source": source,
    }
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(target, os.O_CREAT | os.O_APPEND | os.O_WRONLY, STREAM_FILE_MODE)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    try:
        os.chmod(target, STREAM_FILE_MODE)
    except OSError:
        pass
    return target


def discover_streams() -> list[Path]:
    """Every stream file currently present, in stable name order."""
    directory = stream_directory()
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob(f"*{STREAM_SUFFIX}") if path.is_file())


def source_name(path: Path) -> str:
    name = path.name
    if name.endswith(STREAM_SUFFIX):
        name = name[: -len(STREAM_SUFFIX)]
    return name or path.stem


@dataclass
class StreamRead:
    """One stream's contribution to a take, with its own honesty counters."""

    path: Path
    source: str
    events: list[dict[str, Any]] = field(default_factory=list)
    malformed: int = 0
    oversized: int = 0
    truncated: bool = False
    error: str | None = None

    def report(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "path": str(self.path),
            "source": self.source,
            "events": len(self.events),
            "malformed": self.malformed,
            "oversized": self.oversized,
            "truncated": self.truncated,
        }
        if self.error is not None:
            report["error"] = self.error
        return report


def parse_stream_line(raw: bytes, source: str) -> dict[str, Any] | None:
    """Parse one stream line into an internal event, or ``None`` when unusable."""
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    at_monotonic = record.get("at_monotonic")
    tool = record.get("tool")
    if isinstance(at_monotonic, bool) or not isinstance(at_monotonic, (int, float)):
        return None
    if not isinstance(tool, str) or not tool:
        return None
    payload = record.get("payload")
    record_source = record.get("source")
    return {
        "at_monotonic": float(at_monotonic),
        "tool": tool,
        "ok": bool(record.get("ok", True)),
        "payload": dict(payload) if isinstance(payload, dict) else {},
        "source": record_source if isinstance(record_source, str) and record_source else source,
    }


def read_stream(path: Path, since: float, until: float) -> StreamRead:
    """Read one stream, keeping only events inside ``[since, until]``."""
    source = source_name(path)
    read = StreamRead(path=path, source=source)
    try:
        data = path.read_bytes()
    except OSError as exc:
        read.error = f"unreadable: {exc.strerror or exc}"
        return read
    for raw in data.splitlines():
        if not raw.strip():
            continue
        if len(raw) > STREAM_MAX_LINE_BYTES:
            read.oversized += 1
            continue
        event = parse_stream_line(raw, source)
        if event is None:
            read.malformed += 1
            continue
        if not since <= event["at_monotonic"] <= until:
            continue
        if len(read.events) >= STREAM_MAX_EVENTS:
            read.truncated = True
            break
        read.events.append(event)
    return read


def _validated_paths(explicit: Any) -> list[Path]:
    if not isinstance(explicit, list) or not explicit:
        raise core.ToolError("timeline_sources must be a non-empty list of stream paths")
    paths: list[Path] = []
    for entry in explicit:
        if not isinstance(entry, str) or not entry:
            raise core.ToolError("timeline_sources entries must be non-empty strings")
        path = Path(entry)
        if not path.is_file():
            raise core.ToolError(f"timeline source is not a readable file: {path}")
        paths.append(path)
    return paths


def parse_timeline_sources(value: Any) -> list[str] | None:
    """Validate the ``timeline_sources`` argument; ``None`` means auto-discover.

    Validating at the start of a take means a mistyped source is reported then,
    not after the demonstration has already been performed.
    """
    if value is None:
        return None
    return [str(path) for path in _validated_paths(value)]


def resolve_sources(explicit: list[str] | None) -> list[Path]:
    """Paths to ingest for a take: the explicit ones, or every known stream."""
    if explicit is None:
        return discover_streams()
    return _validated_paths(explicit)


def read_sources(paths: list[Path], since: float, until: float) -> list[StreamRead]:
    """Read every source once, in order, for the window ``[since, until]``."""
    return [read_stream(path, since, until) for path in paths]


def merge_events(reads: list[StreamRead]) -> list[dict[str, Any]]:
    """Every ingested event, ordered by host monotonic time.

    ``sort`` is stable, so events that share a timestamp keep stream order.
    """
    merged = [dict(event) for read in reads for event in read.events]
    merged.sort(key=lambda event: event["at_monotonic"])
    return merged
