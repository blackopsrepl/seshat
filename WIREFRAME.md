# WIREFRAME — shipped MCP tool surface

Server name `seshat`, MCP protocol `2024-11-05`, stdio transport, seven tools.
This file describes what is shipped; it is not a roadmap.

## Tools

| tool | mutates | purpose |
|---|---|---|
| `seshat_info` | no | active take, runtime paths, streams to be ingested, capture outputs, narration binaries |
| `recording_start` | yes | begin a take on an output or region |
| `recording_status` | no | lifecycle phase, live progress, final artifact metadata |
| `recording_stop` | yes | stop capture and begin finalization |
| `recording_timeline` | no | ingested events with recording-relative `t_ms`, plus the per-source report |
| `recording_voiceover` | yes | synthesize, align and mux a narration track (optionally burning captions) |
| `recording_scenes` | no | approximate scene cuts (+ optional OCR) as fallback anchors |

### recording_start

```
format: "mp4" | "webm" | "gif"        default mp4
output: "<output name>"               inferred when exactly one output is active
region: {x, y, width, height}         must be fully contained in the output
max_duration_seconds: number          default 60 (mp4/webm), 15 (gif); gif capped at 15
timeline_sources: [path, ...]         default: every *.jsonl in the streams directory
```

Returns immediately with `phase: "recording"`. Rejects a second concurrent take.
Requires `wf-recorder`, `ffmpeg`, `ffprobe`.

### recording_status / recording_stop

`recording_status` accepts an optional `id` (default: the latest take) and reports
one of `idle`, `recording`, `stopping`, `processing`, `narrating`, `completed`,
`failed`. `completed` carries the artifact summary; `failed` carries `detail`
plus any surviving `intermediate_path` / `log_path`. `recording_stop` takes no
arguments and errors when no take is recording.

A completed summary separates the two durations on purpose:

| field | meaning |
|---|---|
| `capture_elapsed_seconds` | how long capture ran, including recorder shutdown escalation |
| `media_duration_seconds` | playable extent of the video stream (container as fallback) |
| `latest_event_ms` | furthest ingested event, recording-relative; `null` when there are none |
| `events_beyond_media` | ingested events past the playable extent, i.e. unusable as anchors |

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
offset_ms: number                      default 0, auto-shifts by genre/tone preferences
fit: "natural" | "compress"            default natural
tail_ms: number                        default 300, max 2000
subtitles: boolean                     default true
id: "<take id>"                        default the latest take
```

Refuses `format=gif`. Requires `phase == "completed"`. Rejects an `event_id` that
is not in the take timeline **and** an anchor — either kind — that resolves past
the playable video extent, unknown keys, and non-boolean `subtitles`. Runs
asynchronously as `phase: "narrating"`. A narration failure returns the take to
`completed` with the artifact intact and the reason in
`result.narration.error`.

### recording_scenes

```
threshold: number     default 0.30 (SCENE_DEFAULT_THRESHOLD)
max_scenes: integer   default 12, max SCENE_MAX_CUTS
ocr: boolean          default false, needs tesseract
```

Always reports `approximate: true` and states that scene cuts are not the sync
source.

## Runtime contract

- Runtime root `$XDG_RUNTIME_DIR/seshat` (0700): `streams/` for ingestion,
  `recordings/` for takes. Artifacts are 0600 and do not survive logout.
- One take per process. `RECORDINGS` is the single owner; the server stops an
  active capture on stdin EOF.
- Two clocks, reported separately and never conflated: `capture_elapsed_seconds`
  is wall time during which capture ran (shutdown escalation included),
  `media_duration_seconds` is the playable picture measured by ffprobe. Anchors
  are validated against the latter.
- Silent artifact contract: exactly one video stream, no audio —
  MP4 = H.264 (`libx264`, CRF 23), WebM = AV1 (`libsvtav1` preferred, then
  `libaom-av1`), GIF = 12 fps, max 960 px wide, works only with no stream.
- Narrated artifact contract: exactly one video stream **and** exactly one audio
  stream — MP4 = AAC 128k, WebM = Opus 96k, both 48 kHz stereo. The middle of the
  take is written to `<id>.narrated.<fmt>.part` and moved into place only after
  validation.
- Error contract: every expected failure is an MCP tool error (`isError: true`)
  with a specific message. `server.py` logs unexpected exceptions to stderr and
  returns a generic `internal tool error`.
- `--doctor` and `--self-test` print JSON diagnostics; neither records.
- `make integration` records the live screen for a real deadline and checks that
  the artifact is playable and that the ingested timeline is accounted for. It
  refuses to run without `SESHAT_INTEGRATION=1` and is never part of `make check`.

## Environment

| variable | effect |
|---|---|
| `SESHAT_COMMAND` | command argv used for MCP self-registration |
| `SESHAT_COMPOSITOR` | forces the compositor (`sway` or `hyprland`) when detection is ambiguous |
| `SESHAT_PIPER_MODEL` | default piper voice model path |
| `XDG_RUNTIME_DIR` | required: runtime root for streams and recordings |
| `SWAYSOCK`, `HYPRLAND_INSTANCE_SIGNATURE`, `WAYLAND_DISPLAY` | recovered when missing, needed to resolve output geometry and to capture |
