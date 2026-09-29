# Architecture

## The one boundary that matters

seshat records a screen and narrates it. It does not drive the desktop.

That sentence is the whole architecture. An agent driving a desktop is already
talking to a tool server that performs the actions — a Sway or Hyprland desktop
server, a browser driver, a session service. The recorder cannot be the thing
that performs them
if it is also the thing that records whatever else the machine does. So the
actions are **published** rather than captured:

```
driver tool server ──appends──> $XDG_RUNTIME_DIR/seshat/streams/<source>.jsonl
                                            │
seshat: recording_start                     │ ingest (window-filtered, merged)
        capture (wf-recorder)               ▼
        recording_stop ──────────────> timeline (events, t_ms, ids)
                                            │
        recording_voiceover ────────────────┘
        synthesize → schedule → mux → validate
```

Corollary rules that fall out of the boundary:

- **The emitter decides what is safe to publish; seshat decides when and how it
  is narrated.** Per-tool payload curation (which argument keys are meaningful
  and non-sensitive) lives with the server that knows its own arguments. seshat
  passes payloads through verbatim.
- **There is one way into a timeline: ingestion.** seshat contributes no events
  of its own, because none of its own actions are part of a demonstration.
- **A take with no streams still works.** It has no event anchors; narration
  falls back to explicit `at_ms`, or to `recording_scenes`. This is the honest
  consequence of decoupling, and it is why the fallback exists.

## Layers

| layer | modules | responsibility |
|---|---|---|
| primitives | `core`, `media` | subprocess execution, strict parsing, ffprobe |
| session | `session` | compositor detection (Sway, Hyprland), IPC environment recovery |
| contract | `streams` | the published event format: append, discover, parse, window-filter, merge |
| session fact | `outputs` | the only thing capture needs from the session: output geometry |
| capture | `recording`, `encoding` | take model, artifact validation, finalization; encoder selection and every ffmpeg argv |
| timeline | `timeline` | recording-relative projection, event ids, media extent, sidecar |
| narration | `tts`, `narration`, `subtitles`, `scenes` | synthesis, anchor resolution, scheduling, caption layout, burn-in argv |
| lifecycle | `manager` | the single take owner; ingestion, phase transitions, shutdown |
| surface | `tools`, `specs`, `server` | tool wrappers, JSON schemas, MCP loop, CLI facade |

`server.py` star-imports the package: the composition root re-exports the
function names the tests and the tool layer use, matching the convention of the
sibling project this was extracted from.

## Take lifecycle

```
idle ──recording_start──> recording ──recording_stop──> stopping ──> processing ──> completed
                              │  watchdog: max_duration / size limit ─────────────────┘
                              └──shutdown (stdin EOF)─────────────────────────────────>
completed ──recording_voiceover──> narrating ──> completed   (failure returns to completed + error)
```

- `RecordingManager` holds one lock, one current take, and a history of takes by
  id — all in process memory, so a server restart forgets every take, completed
  or not. Every mutation is under that lock.
- Finalization is a worker thread: it re-encodes to a private candidate, validates
  the stream/container contract, fully decodes the video, atomically publishes
  the artifact, ingests the timeline streams, writes the sidecar, then discards
  the intermediate and capture log.
- Ingestion is idempotent by construction: the event list is rebuilt from the
  streams on every read, so `recording_timeline` during a take, finalization, and
  anchor resolution before narration all see a consistent, duplicate-free list.
- The ingestion window closes at the take's own end (`ended_monotonic`). An event
  published after capture stopped describes something the video does not contain.

## Failure policy

- Missing binary, unusable session, bad argument, unknown id: `ToolError`, which
  the protocol loop returns as a tool error with the specific message.
- A malformed, oversized or unreadable stream line is counted in the timeline's
  `sources` report and skipped. No stream can destroy a take.
- A failed capture deletes the artifact and keeps the intermediate, candidate, and
  log when present so the cause is inspectable. `SIGKILL` and non-zero recorder
  exits are failures and never enter finalization.
- A failed narration leaves the previous artifact untouched: the narrated file is
  written to a `.part` path and validated before `os.replace`.

## Lifecycle facts and one picture

Three facts, never conflated:

- `capture_elapsed_seconds` is the capture window: recorder launch to the stop
  request. Encoder discovery and allocation happen before launch and are not
  capture; signal escalation happens after the request and is not capture either.
  Ingestion closes on the same instant, so an event published while the recorder
  is shutting down cannot become an anchor for a picture that already ended.
- `shutdown_latency_seconds` is the stop request to the observed process exit,
  reported beside `termination_stage` and the exact `recorder_returncode`.
- `media_duration_seconds` is the playable picture ffprobe measures.

Capture requests frames continuously (`wf-recorder -D`), so a static tail still
reaches the stop boundary instead of ending at the last damaged frame. Where the
two still disagree the pipeline reports the gap instead of hiding it:
`events_beyond_media` counts ingested events with no picture, and every narration
anchor — event id or explicit offset — is validated against the playable video
extent rather than against the clock the events were recorded on. An event that
exists is not thereby an anchor.

## What an event proves

An event records that a tool call was dispatched by the server that published it.
It does not prove the action had its intended visible effect: the driver is the
only party that knows, and a driver that reports success without verifying the
result will publish an event for an action that did nothing. That distinction
belongs in how narration is written, not in the recorder — which is why seshat
publishes payloads verbatim and makes no claim about them.
