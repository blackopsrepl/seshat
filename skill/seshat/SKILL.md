---
name: seshat
description: Use when recording a screen into a narrated video through the seshat MCP server. Covers the take lifecycle, the timeline stream contract, anchor-safe narration, and the failure modes that produce silent or unanchored takes.
version: 1.0.0
author: Vittorio (blackopsrepl), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Recording, Screencast, Narration, MCP, Wayland]
    related_skills: [computer-use, linux-desktop-control]
---

# seshat — record a screen, narrate the take

seshat records an output of a live Sway or Hyprland session (Omarchy is
Hyprland; the compositor is detected, or forced with `SESHAT_COMPOSITOR`) into
a silent artifact, then muxes a synthesized
narration track aligned to the events that were published while the take ran.
It never drives the desktop; the tools that do publish a timeline stream for it.

## Before the take

1. `seshat_info` — confirm the detected compositor (`session.compositor`,
   `session.distribution`), the capture output, the streams that will be
   ingested, the binaries narration will use (`piper: null` means `edge-tts`
   only, which is network dependent). Do this before promising anyone a
   narrated video.
2. Decide the demonstration's driving tool server and check it publishes a
   stream: its file name shows up in `seshat_info.streams`. If it does not, the
   take will have no event anchors — plan `at_ms` anchors or `recording_scenes`.

## The take

```
recording_start(format="mp4", max_duration_seconds=120)
   <run the demonstration — every action goes through the driving tool server>
recording_stop()
recording_status()          # poll until completed | failed
recording_timeline()        # read before writing a single word of prose
```

- One take at a time per process; a second `recording_start` is an error.
- The watchdog stops the take at `max_duration_seconds` (default 60, gif 15) and
  reports `auto_stopped: true`, so a forgotten take cannot run forever.
- Artifacts land in `$XDG_RUNTIME_DIR/seshat/recordings` and do not survive
  logout. Copy out anything you need to keep.

## Narration

Write the prose yourself — `recording_voiceover` synthesizes it and aligns it; it
invents nothing. Anchor every segment to an event id that `recording_timeline`
actually returned, or to an explicit `at_ms`:

```
recording_voiceover(segments=[
  {"anchor": {"event_id": 3}, "text": "..."},
  {"anchor": {"at_ms": 12000}, "text": "..."}
], fit="compress", subtitles=True)
recording_status()   # poll until completed | failed
```

- `fit="compress"` time-compresses a segment so it cannot run into the next
  anchor. Use it when the prose is longer than the gap it sits in.
- `subtitles=true` (default) burns ASS captions and therefore re-encodes the
  video; `false` stream-copies it.
- A narration failure returns the take to `completed` with the artifact intact
  and the reason in `result.narration.error` — the recording is never lost.
- GIF cannot carry audio: `recording_voiceover` refuses `format=gif` outright.

## What the anchors guarantee, and what they do not

An anchor is validated against the **playable video**, not against the timeline or
the capture clock: a segment is refused when its event id or `at_ms` resolves past
the last frame, with both numbers in the message. That protects the placement. It
says nothing about the action: an event records that a call was *dispatched*, not
that it worked. The sibling project has open issues for exactly that gap — a
modified key that arrived unmodified, and a keystroke that landed in a window
which had stolen focus, both reported as sent. Write prose from what the take
shows; use the event only for its timing.

## Failure modes worth recognising

- **Timeline is empty.** The driving tool server did not publish a stream. Check
  `timeline.sources`, then anchor with `at_ms` or call `recording_scenes`.
- **`event_id ... is not in the take timeline`.** Event ids are positions in the
  time-ordered event list; an id read while a take was still recording can shift
  once more events land. Always read the timeline after the take completes.
- **`... past the end of the playable video`.** The event is real but the picture
  it points at is not in the artifact. Compare `latest_event_ms` with
  `media_duration_seconds`; a deadline-stopped take can lose its tail. Anchor
  earlier or drop the segment.
- **`capture_elapsed_seconds` and `media_duration_seconds` disagree.** They are
  supposed to: the first includes recorder shutdown, the second is playable. Do
  not treat the difference as corruption; do treat `events_beyond_media > 0` as
  anchors you must not use.
- **`capture produced an unreadable intermediate`.** The recorder left a
  truncated file, most often on a deadline stop. The intermediate and the capture
  log are kept and their paths are in the failure detail; the take is not
  recoverable as-is.
- **`narration failed: ... No such filter: 'subtitles'`.** The ffmpeg on `PATH`
  lacks libass, so captions cannot be burned. Point `PATH` at a full build
  (on mixed brew/Arch hosts: `/usr/bin/ffmpeg` first) and call
  `recording_voiceover` again, or pass `subtitles=false` for narration without
  captions. Either way the silent artifact survived.
- **`unknown recording id`.** Takes live in the server process: a restarted
  server cannot narrate, re-analyze, or even report an earlier take. Record,
  narrate and collect the artifact within one server session.
- **`piper is not installed` / no TTS engine.** Install `piper` and a voice
  model, or accept that `edge-tts` sends the narration prose to Microsoft.
- **Slow finalization.** WebM is AV1-encoded; poll `recording_status` rather than
  assuming a timeout means failure.

## Checking the deadline path for real

`make check` mocks the recorder, so it cannot see what a deadline stop really
leaves behind:

```
SESHAT_INTEGRATION=1 make integration
```

It records the live screen for a few seconds, publishes an event near the
deadline, and prints capture elapsed, playable duration, and how far the timeline
reaches. Run it after touching capture, finalization, or ingestion.
