# WIREFRAME — shipped MCP tool surface

Server name `seshat`, MCP protocol `2024-11-05`, stdio transport, ten tools.
This file describes what is shipped; it is not a roadmap.

## Tools

| tool | mutates | purpose |
|---|---|---|
| `seshat_info` | no | active take, runtime paths, durable store, streams to be ingested, capture outputs, narration binaries |
| `recording_start` | yes | begin a take on an output or region |
| `recording_status` | no | lifecycle phase, live progress, final artifact metadata; `wait_seconds` blocks for a terminal phase |
| `recording_stop` | yes | stop capture and begin finalization |
| `recording_timeline` | no | ingested events with recording-relative `t_ms`, plus the per-source report |
| `recording_voiceover` | yes | synthesize, align and mux a narration track (optionally burning captions) |
| `recording_scenes` | no | approximate scene cuts (+ optional OCR) as fallback anchors |
| `recording_list` | no | every take in memory and rehydrated from the store |
| `recording_discard` | yes | delete one inactive take's store directory |
| `recording_export` | yes | copy one completed take's published files to a directory |

### recording_start

```
format: "mp4" | "webm" | "gif"        default mp4
audio: "auto" | "monitor" | "mic" | "off" | null   default off
output: "<output name>"               inferred when exactly one output is active
region: {x, y, width, height}         must be fully contained in the output
max_duration_seconds: number          default 60 (mp4/webm), 15 (gif); gif capped at 15
timeline_sources: [path, ...]         default: every *.jsonl in the streams directory
```

Returns immediately with `phase: "recording"`. Rejects a second concurrent take.
Requires `wf-recorder`, `ffmpeg`, `ffprobe`.

`audio` captures desktop sound into the artifact (mp4/webm only; gif refuses it
at start): `monitor` records the default output sink's monitor — the desktop's
own application and system audio — resolved through `pactl info`; `mic` records
the host's default PulseAudio source (default input device); `auto` is
`monitor` where a default sink exists, `mic` otherwise; `off`/null is the
default silent artifact. A completed take with audio carries exactly one audio
stream of the container's codec (AAC in MP4, Opus in WebM).

### recording_status / recording_stop

`recording_status` accepts an optional `id` (default: the latest take) and reports
one of `idle`, `recording`, `stopping`, `processing`, `narrating`, `completed`,
`failed`. `completed` carries the artifact summary; `failed` carries `detail`
plus every path that survived the failure — `intermediate_path`,
`candidate_path` (a finalized artifact that did not validate), `log_path`.
`recording_stop` takes no
arguments, closes the event/capture window immediately, returns `stopping`, and
does not hold the manager lock while the recorder exits.

A completed summary separates lifecycle and media facts:

| field | meaning |
|---|---|
| `capture_elapsed_seconds` | recorder launch to stop request; excludes shutdown |
| `shutdown_latency_seconds` | stop request to observed process exit |
| `termination_stage` | `natural`, `sigint`, or `sigterm`; `sigkill` fails the take |
| `recorder_returncode` | exact recorder process return code |
| `media_duration_seconds` | playable extent of the video stream (container as fallback) |
| `latest_event_ms` | furthest ingested event, recording-relative; `null` when there are none |
| `events_beyond_media` | ingested events past the playable extent, i.e. unusable as anchors |
| `path` / `master_path` | the current deliverable and the finalized capture it was built from; narration changes only `path` |

### recording_timeline

```
{"id", "phase", "format", "output", "region", "started_utc",
 "capture_elapsed_seconds", "media_duration_seconds", "event_count",
 "latest_event_ms", "events_beyond_media",
 "sources": [{"path", "source", "events", "malformed", "oversized", "truncated"}],
 "events": [{"id", "t_ms", "tool", "ok", "payload", "source"}]}
```

`t_ms` is recording-relative and never negative. `id` is the event's 1-based
position in the take's time-ordered event list: **provisional while recording**
(the `note` field says so), final once the take stops. `media_duration_seconds`
is `null` until finalization has measured the artifact. A sidecar copy is written
next to the artifact as `<id>.timeline.json` on completion.

### recording_voiceover

```
segments: [{anchor: {event_id: n} | {at_ms: n}, text: "..."}]   required
engine: "auto" | "edge" | "piper"      default auto (edge preferred, then piper)
voice: "<engine voice>"                optional
offset_ms: number                      default 0, shifts every resolved anchor
fit: "natural" | "compress"            default natural
tail_ms: number                        default 300, max 2000
subtitles: boolean                     default true
id: "<take id>"                        default the latest take
```

Refuses `format=gif` — including a take that recorded with audio — and requires
`phase == "completed"`. Rejects an `event_id` that is not in the take timeline
**and** an anchor — either kind — that resolves past the playable video extent,
unknown keys, and non-boolean `subtitles`. Runs asynchronously as
`phase: "narrating"`. A narration failure returns the take to `completed` with
the artifact intact and the reason in `result.narration.error`. Caption burn-in
additionally requires an ffmpeg built with libass (the `subtitles` filter); a
build without one fails narration exactly this way, and the take can be
re-narrated with `subtitles=false`. Narration always builds from the take's
finalized master and republishes a `<id>.narrated.<fmt>` deliverable, so
re-narration never compounds captions or generations; `recording_status`
reports `path` (the current deliverable) and `master_path`. A take recorded with
`audio=monitor|mic` keeps its own sound: the track builder ducks it under the
voice (sidechaincompress) and mixes both into the deliverable's single audio
stream; over a silent master the stream is the voice alone. A schedule that
runs past the master's extent extends the picture with cloned frames (`tpad`),
reported as `narration.extended_ms`. `seshat_info.audio` reports `default_sink`,
`default_source` and `monitor_source` (what `audio="monitor"` resolves to), or
an `error` field on a host without a usable audio server.

### recording_scenes

```
threshold: number     default 0.30 (SCENE_DEFAULT_THRESHOLD)
max_scenes: integer   default 40, max SCENE_MAX_CUTS
ocr: boolean          default false, needs tesseract
```

Always reports `approximate: true` and states that scene cuts are not the sync
source.

## Runtime contract

- Runtime root `$XDG_RUNTIME_DIR/seshat` (0700) holds `streams/` for ingestion
  and `recordings/` for the live intermediate and capture log. A completed take
  is adopted into the durable store `$XDG_STATE_HOME/seshat/takes/<id>/`
  (0700/0600): `master` (`<id>.<fmt>`), `narrated.<fmt>` when narration ran,
  the timeline sidecar, `take.json` (the manifest, including the completion
  result), and `narration.jsonl` (one line per voiceover request and schedule).
  Startup rehydrates completed takes from the store; `recording_export` is the
  only way files leave it, `recording_discard` the only way they are deleted.
- One capture at a time per process. `RECORDINGS` is the single owner; the
  server stops an active capture on stdin EOF. The watchdog reaps a recorder
  that died between polls; `recording_status(wait_seconds≤60)` blocks for a
  terminal phase instead of forcing a poll per agent turn.
- Tools answer `structuredContent` alongside their text content, and carry MCP
  `annotations` (`readOnlyHint` on the read-only tools, `destructiveHint` on
  `recording_discard`). `engine="auto"` resolves to the offline `piper` engine
  only; `engine="edge"` is the explicit opt-in to send narration prose to
  Microsoft, and the narration result always reports engine, resolved `voice`
  and `network`.
- Three lifecycle/media facts stay separate: `capture_elapsed_seconds` spans
  recorder launch through the stop request, `shutdown_latency_seconds` spans the
  stop request through process exit, and `media_duration_seconds` is the playable
  picture measured by ffprobe. Anchors are validated against the latter.
- Capture requests frames continuously (`wf-recorder -D`) so a static tail still
  advances to the stop boundary. A stop that reaches `SIGKILL`, or any non-zero
  recorder return code, fails instead of publishing a possibly truncated take.
- Finalization writes a `.part` candidate, validates stream/container shape,
  decodes the complete video stream with ffmpeg, then atomically replaces the
  public artifact.
- Silent artifact contract: exactly one video stream, no audio —
  MP4 = H.264 (`libx264`, CRF 23), WebM = AV1 (`libsvtav1` preferred, then
  `libaom-av1`), GIF = 12 fps, max 960 px wide, works only with no stream.
- Audio-capture artifact contract (`audio=monitor|mic`): exactly one video
  stream **and** exactly one audio stream — MP4 = AAC 128k, WebM = Opus 96k,
  both 48 kHz stereo — captured by `wf-recorder -a [source]` into the silent
  intermediate and re-encoded beside the picture during finalization, with the
  artifact validated for exactly that shape. Narration later re-adds its own
  speech-encoded stream per the narrated contract below.
- Narrated artifact contract: exactly one video stream **and** exactly one audio
  stream — MP4 = AAC 128k, WebM = Opus 96k, both 48 kHz stereo. The deliverable
  is written to `<id>.narrated.<fmt>.part` and moved into place only after
  validation; the finalized master at `<id>.<fmt>` is never rewritten, and every
  narration starts from it again.
- Error contract: every expected failure is an MCP tool error (`isError: true`)
  with a specific message, and every tool rejects argument keys it does not
  declare. `server.py` answers `ping`, answers non-object messages and non-object
  arguments as protocol errors (-32600/-32602) instead of dying, logs unexpected
  exceptions to stderr, and returns a generic `internal tool error`.
- `--doctor` and `--self-test` print JSON diagnostics; neither records.
- `make integration` records the live screen for a real deadline and asserts that
  the take auto-stopped, that the recorder exited cleanly, that the playable
  picture reaches the stop boundary, and that no ingested event resolves past it.
  It refuses to run without `SESHAT_INTEGRATION=1` and is never part of `make check`.

## Environment

| variable | effect |
|---|---|
| `SESHAT_COMMAND` | command argv used for MCP self-registration |
| `SESHAT_COMPOSITOR` | forces the compositor (`sway` or `hyprland`) instead of detecting it |
| `SESHAT_PIPER_MODEL` | default piper voice model path |
| `XDG_RUNTIME_DIR` | required: runtime root for streams and recordings |
| `SWAYSOCK`, `HYPRLAND_INSTANCE_SIGNATURE`, `WAYLAND_DISPLAY` | recovered when missing, needed to resolve output geometry and to capture |
