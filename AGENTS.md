# Repository Guidelines

Guidance for coding agents working in this repository.

## Product Contract

- `seshat` is a zero-runtime-dependency Python MCP stdio server that records a
  screen and narrates the take. `dependencies = []` in `pyproject.toml` is a
  hard contract; capture, TTS and OCR binaries are optional and runtime-detected,
  and a missing binary is a clear `ToolError`, never a hard import failure.
- **seshat never drives the desktop.** It records a screen. The actions a take
  documents are performed by whatever tool server the harness called, and reach
  the timeline as published event streams (`streams.py`). There is exactly one
  way into a timeline, and it is ingestion.
- The silent recording contract is frozen: silent takes carry no audio, the
  artifact has exactly one video stream (H.264 MP4 by default, AV1 WebM, or
  GIF), and `validate_recording_artifact` keeps rejecting unexpected audio.
- Only one take may be active per process; `RecordingManager` is the single
  owner. `recording_start` must reject a second concurrent take.
- GIF cannot carry audio; `recording_voiceover` refuses `format=gif`.
- Narration is strictly opt-in and is never invented: an anchor is either a real
  event id from the take timeline or an explicit `at_ms`. The calling agent
  writes the narration prose; seshat owns timestamping, speech synthesis,
  alignment, captions and muxing.
- Event ids are positions in the take's ingested, time-ordered event list. They
  are provisional while a take records and final once it stops. Narration is
  only accepted on a completed take, so anchors are always final.

## Project Structure

- `src/seshat/core.py`: tool errors, subprocess execution, session environment
  recovery, strict parsing helpers.
- `streams.py`: the cross-server event stream contract — append, discover,
  parse, window-filter, merge.
- `outputs.py`: the only session fact capture needs — active output geometry.
- `media.py`: `ffprobe`-backed media probing.
- `recording.py`: take model, path/argument construction, artifact validation,
  finalization.
- `timeline.py`: recording-relative timeline projection and sidecar.
- `tts.py`: pluggable `edge-tts`/`piper` engines and audio parsing.
- `narration.py`: narration contract, anchor resolution, scheduling, muxing.
- `subtitles.py`: styled ASS captions and the burn-in mux argv.
- `scenes.py`: approximate scene-cut and OCR fallback anchors.
- `manager.py`: the single take lifecycle owner and `RECORDINGS`.
- `tools.py` / `specs.py`: MCP tool wrappers and JSON schemas.
- `server.py`: MCP protocol loop and CLI facade.
- `tests/`: deterministic unit tests plus `support.py` helpers.

## Engineering Rules

- Any file that reaches **500 lines** must be split into multiple files. This is
  enforced by `scripts/check_file_length.py` through `make check`. Move code into
  a new focused module instead of growing an existing one; `CHANGELOG.md` is
  exempt.
- Do not hand-edit `CHANGELOG.md` or version numbers. `.versionrc.js` owns
  `pyproject.toml` and `SERVER_VERSION`, and releases run through
  `commit-and-tag-version`.
- **The emitter decides what is safe to publish; seshat decides when and how it
  is narrated.** Never move per-tool payload curation back into this repository:
  it belongs to the server that knows what its own arguments mean.
- When a task is simple, do the simple thing. Do not expand scope into unrelated
  critical paths.

## Validation

Run:

```bash
make check
```

That runs the unit tests, bytecode compilation, and the file-length check.

## Documentation Surfaces

Keep these synchronized with shipped behavior:

- `README.md`: public overview, requirements, recording and narration workflows,
  and the stream contract.
- `WIREFRAME.md`: shipped MCP tool surface and runtime contract.
- `docs/architecture.md`: layers, boundaries, and lifecycle.
- `skill/seshat/SKILL.md`: agent operating procedure.
- `AGENTS.md`: repository rules and validation.
