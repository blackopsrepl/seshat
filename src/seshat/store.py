"""The durable take store: where finalized takes outlive this server process.

Everything a completed take is was written to `$XDG_RUNTIME_DIR` — tmpfs, wiped
at logout, invisible to the next server session — so a restart forgot every take
and a logout deleted every artifact. The store keeps them in
`$XDG_STATE_HOME/seshat/takes/<id>/` (0700, files 0600):

    master.<fmt>          the finalized capture
    narrated.<fmt>        when a narration was published
    timeline.json         the take's timeline sidecar
    take.json             the take manifest
    narration.jsonl       one line per narration request and its schedule

Adoption moves the finalized files into the store and rewrites the job's paths;
rehydration rebuilds a completed job from the manifest at startup. Files leave
the store only through export, and are deleted only through discard.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import recording, timeline as _timeline


STORE_STATE_VARIABLE = "XDG_STATE_HOME"
STORE_DEFAULT_STATE = ".local/state"
STORE_ROOT_NAME = "seshat"
STORE_TAKES_NAME = "takes"
STORE_MANIFEST_NAME = "take.json"
STORE_NARRATION_NAME = "narration.jsonl"
STORE_TIMELINE_NAME = "timeline.json"


def state_takes_root() -> Path:
    """The store under `$XDG_STATE_HOME` (default `~/.local/state`)."""
    state_home = os.environ.get(STORE_STATE_VARIABLE)
    if not state_home:
        state_home = os.path.join(os.path.expanduser("~"), *(STORE_DEFAULT_STATE.split("/")))
    return Path(state_home) / STORE_ROOT_NAME / STORE_TAKES_NAME


def take_directory(take_id: str) -> Path:
    if not isinstance(take_id, str) or not take_id:
        raise recording.core.ToolError("take id must be a non-empty string")
    return state_takes_root() / take_id


def manifest_path(take_id: str) -> Path:
    return take_directory(take_id) / STORE_MANIFEST_NAME


def narration_journal_path(take_id: str) -> Path:
    return take_directory(take_id) / STORE_NARRATION_NAME


def timeline_path(take_id: str) -> Path:
    return take_directory(take_id) / f"{take_id}{_timeline.TIMELINE_SUFFIX}"


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=recording.RECORDING_DIR_MODE)
    os.chmod(path.parent, recording.RECORDING_DIR_MODE)
    part = path.with_name(path.name + ".part")
    fd = os.open(part, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, recording.RECORDING_FILE_MODE)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(part, path)
    os.chmod(path, recording.RECORDING_FILE_MODE)


def manifest_document(job) -> dict:
    """The manifest for a job: facts the next session cannot re-derive."""
    return {
        "id": job.id,
        "format": job.fmt,
        "output": job.output,
        "region": job.region,
        "width": job.width,
        "height": job.height,
        "audio": job.audio,
        "started_utc": job.started_utc,
        "rehydrated_from_store": True,
        "result": job.result,
        "narration": job.narration,
        "stream_sources": job.stream_sources,
    }


def write_manifest(job) -> Path:
    _write_private(manifest_path(job.id), json.dumps(manifest_document(job), indent=2).encode("utf-8"))
    return manifest_path(job.id)


def append_narration_record(take_id: str, arguments: dict, meta: dict) -> None:
    """One line per narration: the request and the schedule it produced."""
    record = {"arguments": dict(arguments), "meta": dict(meta)}
    path = narration_journal_path(take_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=recording.RECORDING_DIR_MODE)
    os.chmod(path.parent, recording.RECORDING_DIR_MODE)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    existing = path.read_bytes() if path.exists() else b""
    _write_private(path, existing + line)


def read_manifest(take_id: str) -> dict | None:
    path = manifest_path(take_id)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def scan_store() -> list[dict]:
    """Every readable manifest in the store, oldest take first."""
    root = state_takes_root()
    if not root.is_dir():
        return []
    manifest_take_ids = sorted(take_dir.name for take_dir in root.iterdir() if take_dir.is_dir())
    documents = []
    for take_id in manifest_take_ids:
        document = read_manifest(take_id)
        if document is not None:
            documents.append(document)
    return documents


def adopt(
    job,
    timeline_sidecar: Path | None,
) -> Path:
    """Move a finalized take's published files into its store directory.

    Returns the job's new home. The master, sidecar and any existing narration
    move; `job`'s path fields are rewritten to the store by the caller.
    """
    directory = take_directory(job.id)
    directory.mkdir(parents=True, exist_ok=True, mode=recording.RECORDING_DIR_MODE)
    os.chmod(directory, recording.RECORDING_DIR_MODE)
    moves = [
        (job.artifact, directory / job.artifact.name),
    ]
    if timeline_sidecar is not None and timeline_sidecar.exists():
        moves.append((timeline_sidecar, directory / timeline_sidecar.name))
    if job.narrated_path is not None and job.narrated_path.exists():
        moves.append((job.narrated_path, directory / job.narrated_path.name))
    for source, destination in moves:
        if source.is_file():
            os.replace(source, destination)
            os.chmod(destination, recording.RECORDING_FILE_MODE)
    return directory


def delete_take(take_id: str) -> None:
    """Remove one take's store directory, by its explicit id only."""
    directory = take_directory(take_id)
    if directory.exists():
        shutil.rmtree(directory)


def export_take(job, destination: str) -> list[dict]:
    """Copy the take's published files into a destination directory."""
    if not isinstance(destination, str) or not destination.strip():
        raise recording.core.ToolError("destination must be a non-empty path")
    destination_dir = Path(destination).expanduser()
    destination_dir.mkdir(parents=True, exist_ok=True)
    files = [job.artifact, job.timeline_path]
    if job.narrated_path is not None and job.narrated_path.exists():
        files.append(job.narrated_path)
    manifest = manifest_path(job.id)
    if manifest.exists():
        files.append(manifest)
    journal_files = narration_journal_path(job.id)
    if journal_files.exists():
        files.append(journal_files)
    copied = []
    for source in files:
        if source is None or not Path(source).exists():
            continue
        name = Path(source).name
        if name in (STORE_MANIFEST_NAME, STORE_NARRATION_NAME):
            # The store names these per-directory ("take.json"); one exported
            # destination can hold several takes, so the copy carries the id.
            name = f"{job.id}.{name}"
        target = destination_dir / name
        shutil.copy2(source, target)
        copied.append({"file": name, "path": str(target), "bytes": target.stat().st_size})
    if not copied:
        raise recording.core.ToolError(f"take {job.id} has no published files to export")
    return copied