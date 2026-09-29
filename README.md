# seshat

<img src="docs/assets/seshat-mascot.png" alt="Sesh, the seshat mascot: an ivory scribe-keeper crowned with Seshat's star and horns, a recording lens set in her chest and the goddess's notched measuring rod in her hand" width="208" align="right" />

An MCP server that **records a screen and narrates the take**.

seshat captures an output (or a region of one) into a silent artifact, ingests
the timeline streams that other tool servers publish while the take runs, and
then muxes a synthesized narration track — aligned to those real events, with
styled captions burned in — over the recorded video. The prose is the caller's;
the timing, the speech, the captions and the container are seshat's.

It is named after the Egyptian goddess of writing, measurement and record-keeping.

**Sesh** is the mascot: an ivory scribe-keeper crowned with Seshat's star and
horns, the recording lens set in her chest, the goddess's notched measuring rod —
the take timeline — in her hand, and the events other servers publish trailing
from her headcloth. She keeps the book; she never invents a word of it.

## What it is, and what it is not

- **It is a recorder and a narrator.** It owns capture, the take timeline, speech
  synthesis, caption layout and muxing.
- **It is not a desktop controller.** It never clicks, types, or focuses
  anything. The actions a take documents are performed by other tool servers;
  they reach seshat as published event streams. That split is deliberate: a
  recoder that also drives the desktop cannot record a desktop driven by
  anything else.
- **It embeds no model.** Narration prose is written by the calling agent and
  passed in. seshat is deterministic: same inputs, same timeline, same schedule.

## Requirements

- Python 3.10+ (standard library only — `dependencies = []` is a hard contract).
- A live **Sway** or **Hyprland** session (Omarchy is Hyprland). The compositor
  is detected from the session environment and can be forced with
  `SESHAT_COMPOSITOR=sway|hyprland`; a missing IPC variable (`SWAYSOCK`,
  `HYPRLAND_INSTANCE_SIGNATURE`) is recovered from the runtime directory, which
  is how a harness-launched server still finds the desktop.
- `wf-recorder` for capture, `ffmpeg` + `ffprobe` for finalization, muxing and
  probing; `hyprctl` or `swaymsg` to resolve output geometry. Caption burn-in
  additionally needs an ffmpeg built with libass — the `subtitles` filter —
  which minimal builds ship without. A missing binary is reported as a clear
  tool error, never an import failure.
- Optional: `edge-tts` (keyless, **network**) or `piper` (offline, needs a voice
  model) for narration; `tesseract` for OCR of scene keys.

On Arch or Omarchy:

```bash
sudo pacman -S --needed wf-recorder ffmpeg tesseract   # hyprctl ships with Hyprland
uv tool install edge-tts                               # or piper + a voice model
```

`make check` runs the unit tests, bytecode compilation, and the file-length check.
`make help` lists every target; `make ci-local` runs the whole gate plus a
distribution build, and `make runtime` shows the streams, recordings and active
take when something looks wrong.

## Install

```bash
uv venv
uv pip install -e .
seshat --doctor     # what this host can record and narrate with
seshat --self-test  # non-mutating checks
```

Register with an MCP harness:

```bash
hermes mcp add seshat --command /path/to/venv/bin/seshat
codex mcp add seshat -- /path/to/venv/bin/seshat
```

## Agent skill

`skill/seshat/SKILL.md` is the operating procedure for an agent driving this
server: the take lifecycle, the anchor rules, and the failure modes that produce
silent or unanchored takes. It ships in the source distribution, and the copy in
this repository is the source of truth — installed copies are copies.

```bash
# opencode
mkdir -p ~/.config/opencode/skill
cp -r skill/seshat ~/.config/opencode/skill/

# Hermes
mkdir -p ~/.hermes/skills/media
cp -r skill/seshat ~/.hermes/skills/media/
```

Re-run the copy after changing the skill; a stale installed copy is how an agent
ends up calling tools this server no longer has.

## Recording a take

```
recording_start(format="mp4", max_duration_seconds=120)
   ... the demonstration happens ...
recording_stop()
recording_status()        # poll until phase == completed | failed
recording_timeline()      # the ingested events, with recording-relative t_ms
```

One take may be active at a time. Artifacts are written to
`$XDG_RUNTIME_DIR/seshat/recordings` (0700, files 0600) and do not survive a
logout; copy anything you want to keep.

A completed take separates three lifecycle facts: `capture_elapsed_seconds` runs
from recorder launch to the stop request, `shutdown_latency_seconds` measures how
long the recorder then took to exit, and `media_duration_seconds` is the playable
picture. `termination_stage` and `recorder_returncode` expose how the process
ended. Capture is damage-independent, so static screen intervals still advance
the video; `latest_event_ms` and `events_beyond_media` identify any remaining
timeline/media mismatch rather than hiding it.

```bash
make check                         # unit tests, compilation, file-length ceiling
SESHAT_INTEGRATION=1 make integration   # records the live screen; opt in explicitly
```

### The timeline stream contract

seshat cannot timestamp actions it does not perform. Any tool server that wants
its actions to be narratable publishes them — one JSON object per line — to
`$XDG_RUNTIME_DIR/seshat/streams/<source>.jsonl`:

```json
{"at_monotonic": 12345.678, "tool": "click", "ok": true,
 "payload": {"x": 640, "y": 360}, "source": "computer-use-sway"}
```

- `at_monotonic` is required: CLOCK_MONOTONIC seconds, the value of
  `time.monotonic()` in the emitting process. That clock is host-wide, which is
  what makes an unrelated process's timestamps directly comparable with the
  recording epoch — no handshake, no session id.
- `tool` is required. `ok` (default true), `payload` (default `{}`) and `source`
  (default: the file stem) are optional.
- Each emitter owns its file and should truncate it when a new session starts.
- Events are filtered to the take's own capture window. Anything published
  before `recording_start` or after capture stopped describes something the
  video does not contain, and is not an anchor.
- Malformed, oversized or unreadable lines are counted in the timeline's
  `sources` report and skipped. A broken stream can never destroy a take.

The contract is driver-agnostic. On Sway,
[`computer-use-sway`](https://github.com/blackopsrepl/computer-use-sway) plays
this emitter role; on Hyprland or Omarchy, whatever drives the desktop can
publish the same lines from the process that performs the actions. A driver
that publishes nothing still records fine, but the take has no event anchors —
narrate with `at_ms` or fall back to `recording_scenes`.

Pass `timeline_sources` to `recording_start` to ingest specific files instead of
everything in the streams directory; explicit paths are validated when the take
starts, not when it is finalized.

## Narrating a take

Read the timeline first, then write prose against the event ids that are really
there:

```
recording_timeline()
recording_voiceover(segments=[
  {"anchor": {"event_id": 1}, "text": "..."},
  {"anchor": {"at_ms": 8200},  "text": "..."}
])
recording_status()        # poll until phase == completed | failed
```

`recording_voiceover` synthesizes each segment, builds one audio track placed at
the resolved anchors (with `offset_ms`, optional tempo compression via
`fit="compress"`, and lead-silence trimming), muxes it over the existing video,
and — unless `subtitles=false` — burns styled ASS captions. The video is only
re-encoded when captions are burned; otherwise it is stream-copied. The result is
re-validated: exactly one video stream plus exactly one audio stream. A
narration failure never destroys the take: the silent artifact stays in place
and the reason lands in `result.narration.error` — for example, an ffmpeg build
without the `subtitles` filter cannot burn captions. Retry that take with
`subtitles=false`. To burn captions, start the server with a libass-enabled
ffmpeg on `PATH` before recording; restarting the server to change `PATH`
forgets its in-memory takes.

**Anchors are checked against the picture, not the timeline.** Both `event_id`
and `at_ms` are validated against the playable video extent — the video stream's
own duration, falling back to the container's — so speech cannot be placed after
the last frame. An event id that exists but resolves past the end of the video is
refused, with both numbers in the message, rather than anchored into silence.

If `recording_timeline` reports no events, the demonstration was driven by a tool
server that does not publish a stream. Anchor segments with `at_ms`, or use
`recording_scenes` for approximate cuts. Scene cuts are secondary evidence;
they are never the sync source.

## What the events do and do not prove

An event records that a tool call was **dispatched**, not that it visibly worked.
The driving server is the only party that knows whether its action had the
intended effect; a driver that returns `sent: true` without verifying the result
will publish an event for an action that did nothing. Two open issues in the
sibling project are exactly this shape: a modified key that arrived as an
unmodified one, and a keystroke delivered to a window that had stolen focus.

So narrate what the take shows. Use event ids to place a line in time, and ground
its content in the video, in the driver's own verified payload, or in something
you observed yourself — never in the mere existence of an event.

## Security notes

- `edge-tts` sends the narration prose to Microsoft. It is keyless but network
  dependent. Install `piper` and a voice model (`voice`, or
  `SESHAT_PIPER_MODEL`) to keep narration on the host.
- Narration text is the caller's; seshat never reads it from the screen, so
  nothing visible on screen becomes narration unless the agent says so.
- Recordings and streams live under `$XDG_RUNTIME_DIR/seshat` with 0700 / 0600
  permissions.

## Related

- [`computer-use-sway`](https://github.com/blackopsrepl/computer-use-sway) —
  drives a Sway session and publishes the timeline stream that this server
  ingests. Recording and narration were extracted from that project so that both
  could stand on their own.
- [`omarchy-computer-use`](https://github.com/enricofranke/omarchy-computer-use) —
  gives an agent its own nested Hyprland session on Omarchy. It does not publish
  a timeline stream, so takes it drives narrate via `at_ms` or
  `recording_scenes` unless something else publishes for them.
