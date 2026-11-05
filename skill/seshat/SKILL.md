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
   ingested, and both narration binaries: `binaries.piper` for offline speech
   and `binaries.edge-tts` for network-dependent speech. A null path means that
   engine is unavailable. Do this before promising anyone a narrated video.
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
- `recording_status(wait_seconds=30)` blocks until a terminal phase instead of
  forcing the agent to poll; the watchdog reaps a recorder that died on its own.
- **Desktop audio is opt-in**: pass `audio="monitor"` to record the desktop's
  own sound (application/system audio from the default sink), or `audio="mic"`
  for the host's default microphone. The default is silence. `audio="monitor"`
  resolves via `pactl info` (`Default Sink` + `.monitor`); an unresolvable
  default sink is a clear error. `seshat_info.audio` reports what would be
  captured. Do not assume recording captures audio implicitly: `wf-recorder`'s
  own bare `-a` records the *microphone* (PulseAudio default source), which is
  exactly why seshat resolves `<default-sink>.monitor` instead. A take can start
  with desktop audio and still get narration later — the voice lands as an
  additional stream on top of the captured track.
- Artifacts land in `$XDG_RUNTIME_DIR/seshat/recordings` while the take runs and
  move into the durable store `$XDG_STATE_HOME/seshat/takes/<id>/` when the take
  completes: master, timeline, manifest and narration journal. Takes survive a
  server restart and a logout; `recording_list` names them, `recording_export`
  copies them out, `recording_discard` deletes one.

## The capture target

`recording_start` can take a `window` argument: a class or title substring
resolved against the compositor's tree into a static rect on the output it
sits on (exclusive with `region`). The rect is fixed for the take — a window
that moves keeps its starting geometry, which is what makes the take
anchorable. Resolve the exact name with the compositor's own client
(`swaymsg -t get_tree`, `hyprctl -j clients`); a no-match is a clear error
naming the needle.

## Narration

Write the prose yourself — `recording_voiceover` synthesizes it and aligns it; it
invents nothing. Anchor every segment to an event id that `recording_timeline`
actually returned, or to an explicit `at_ms`:

```
# Plan first: dry_run answers synchronously with per-segment durations and
# overruns, so the demonstration can be paced to the script before recording.
recording_voiceover(segments=[...], dry_run=true)

recording_voiceover(segments=[
  {"anchor": {"event_id": 3}, "text": "..."},        # a real event id
  {"anchor": {"at_ms": 12000}, "text": "..."},       # an explicit offset
  {"anchor": {"match": {"tool": "write", "nth": 1}}, "text": "..."}
], fit="compress", subtitles=True, position="auto")
recording_status(wait_seconds=58)
```

- Anchors may be an event id, an explicit `at_ms`, or a content match
  (`{"tool", "payload subset", "nth"}`): exactly one real event, so a script
  survives a re-record. A match that finds nothing is refused, not invented.
- Captions are phrase-shaped and cue-timed; `<deliverable>.srt` and `.vtt` are
  written beside every render even when burn-in is off or unavailable.
  `position="auto"` keeps captions off the bottom third when the events acted
  there.
- The voice is tunable: `rate`/`volume`/`pitch` (edge) or `length_scale`
  (piper); the whole mix is normalized to −16 LUFS. The result reports
  `engine`, resolved `voice`, `network` and `character`.
- `variant="it"` renders `<id>.it.<fmt>` from the same master — narration in
  several languages from one take.

- Anchors are always the master's picture, so the same script can be narrated
  again after editing: the deliverable lands at `<id>.narrated.<fmt>` and the
  master never changes. Re-narration never compounds captions.
- A take recorded with `audio=monitor|mic` keeps its desktop sound: it is ducked
  under the voice and mixed with it in the deliverable's single audio stream.
- If the voice outlasts the picture, the picture is extended with cloned frames
  (`narration.extended_ms` in the result) instead of speech running past the last
  frame.
- `fit="compress"` time-compresses a segment so it cannot run into the next
  anchor. Use it when the prose is longer than the gap it sits in.
- `subtitles=true` (default) burns ASS captions and therefore re-encodes the
  video; `false` stream-copies it.
- A narration failure returns the take to `completed` with the artifact intact
  and the reason in `result.narration.error` — the recording is never lost.
- GIF cannot carry audio: `recording_voiceover` refuses `format=gif` outright,
  and `recording_start(format="gif", audio=...)` is refused at start.

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
  `media_duration_seconds`. Anchor earlier or drop the segment.
- **`capture_elapsed_seconds` and `media_duration_seconds` disagree.** They are
  supposed to: capture is wall time from recorder launch to the stop request,
  media is the playable picture. Do not treat a small gap as corruption; treat
  `events_beyond_media > 0` as event ids you must not anchor to. A large gap means
  the recorder's own timeline is not the take's, and the take is not trustworthy
  for narration.
- **`wf-recorder required SIGKILL; capture may be truncated`.** The recorder
  ignored both graceful signals, so the take failed instead of publishing a
  possibly malformed artifact. The intermediate is kept for inspection; re-record.
- **`wf-recorder exited during shutdown (code N)`.** The recorder died while
  stopping, so its tail is not trustworthy. Read the returned log tail before
  re-recording.
- **`capture produced an unreadable intermediate`.** The recorder left a
  truncated file, most often on a deadline stop. The intermediate and the capture
  log are kept and their paths are in the failure detail; the take is not
  recoverable as-is.
- **`narration failed: ... No such filter: 'subtitles'`.** The ffmpeg on `PATH`
  lacks libass, so captions cannot be burned. Retry the current take with
  `subtitles=false`. To burn captions, start the server with a full ffmpeg on
  `PATH` before recording (on mixed brew/Arch hosts: `/usr/bin/ffmpeg` first);
  restarting to change `PATH` forgets the server's in-memory takes.
- **`unknown recording id`.** A restarted server rehydrates completed takes,
  but a lost id is best recovered from `recording_list`, and an active take
  still lives only in one server process.
- **`piper is not installed` / no TTS engine.** `engine="auto"` is offline-only:
  install `piper` and a voice model, and the error names the `engine="edge"`
  opt-in that sends the narration prose to Microsoft.
- **`could not resolve a default audio sink from pactl info`.** `audio="monitor"`
  and `audio="auto"` need a default sink; a headless or audio-less session has
  none. Use `audio="mic"` or record silent.
- **`wf-recorder exited during startup ... (the take asked for audio=...)`.**
  This binary build has no audio support or no reachable audio server: retry
  without the `audio` argument and read the log tail for the backend error.
- **Slow finalization.** WebM is AV1-encoded; poll `recording_status` rather than
  assuming a timeout means failure.

## Post-production (recording_render)

Run after the take completes, before narration; the render answers
synchronously with its own event map:

```
recording_render(id=..., trim={"pad_ms": 400}, idle={"min_gap_ms": 1000, "speed": 4},
                 zoom={"scale": 2, "hold_ms": 900}, markers=true,
                 chapters=true, poster=true, teaser=true, profile="native")
```

- Every operation is anchored in the take's ingested events: `zoom`/`markers`
  need payloads with coordinates, `chapters` needs payloads with a `chapter`
  label; a need that cannot be met is a clear error, not a silent skip.
- The result's `event_map` carries every event's remapped `t_ms`; narrate a
  render with `at_ms` anchors from that table — or pass the prose to the render
  itself in `narrate` and take `<id>.render.narrated.<fmt>` from the same call,
  with the cut's own audio as the ducked bed. The master is untouched, so a
  re-render or a re-narration of the master never compounds anything.
- `stitch: ["<id>", ...]` concatenates whole masters of the same shape and
  codec at the container level; a mismatch is refused. Stitching takes of
  different profiles is a refusal by design.

## Checking the deadline path for real

`make check` mocks the recorder, so it cannot see what a deadline stop really
leaves behind:

```
SESHAT_INTEGRATION=1 make integration
```

It records the live screen for a few seconds, publishes an event inside the take's
own window, and asserts that the deadline stopped the take, that the recorder
exited cleanly, that the playable picture reached the stop boundary, and that no
ingested event resolves past it. `SESHAT_INTEGRATION_SECONDS` sets the deadline
and `SESHAT_INTEGRATION_TOLERANCE_SECONDS` (default 2.5) the gap allowed for the
recorder attaching to the compositor after launch. Run it after touching capture,
lifecycle, finalization, or ingestion.
