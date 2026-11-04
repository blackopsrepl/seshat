# Changelog

All notable changes to this project will be documented in this file. See [commit-and-tag-version](https://github.com/absolute-version/commit-and-tag-version) for commit guidelines.

## [0.4.0](https://github.com/blackopsrepl/seshat/compare/v0.3.0...v0.4.0) (2026-11-04)


### Features

* **render:** timeline-driven recording_render over the finalized master 1e68bdd


### Bug Fixes

* **render:** re-time the captured audio with the picture, and fail loudly on an empty trim 0c71ad7
* **render:** stitch leaves the manager lock before it runs 4a40f4b


### Documentation

* recording_render, the window capture target, and the derived-variant contract on every doc surface f488ed5

## [0.3.0](https://github.com/blackopsrepl/seshat/compare/v0.2.0...v0.3.0) (2026-11-04)


### Features

* **narration:** dry_run answers synchronously with the plan it computed e319e49
* **narration:** phrase captions with sidecars and placement, one loudness, plan mode, content anchors, variants 64292f2


### Documentation

* phrase captions with soft sidecars and placement, plan mode, content anchors, voice character, variants 975a7b8

## [0.2.0](https://github.com/blackopsrepl/seshat/compare/v0.1.5...v0.2.0) (2026-11-04)


### Features

* **takes:** finalized takes survive restarts through a durable store, surfaced by list/discard/export d8a01f8
* **tts:** engine=auto is offline-only; edge is an explicit opt-in with a pinned default voice 0e06e14


### Bug Fixes

* **store:** adopt across the tmpfs/store device boundary, and derive beside the take 77cf18b


### Documentation

* the durable take store, the offline engine policy and the status wait on every surface ffffea7

## [0.1.5](https://github.com/blackopsrepl/seshat/compare/v0.1.4...v0.1.5) (2026-11-04)


### Features

* **scripts:** a narration integration check that records the live screen 757a77f


### Bug Fixes

* **narration:** build from the finalized master so re-narration cannot compound b11a799
* **narration:** mix the captured desktop track under the voice and extend the picture to cover it ae40a80
* **server:** survive any message a client can send, and answer ping 1934507
* **tools:** reject unknown arguments on every tool, and remove the hidden audio_source 1785179


### Documentation

* **readme:** state the narration mix and answer the audio questions users ask 3ba4c60
* state the master/derived narration mix and clear every drift found by the surface audit 29428dc

## [0.1.4](https://github.com/blackopsrepl/seshat/compare/v0.1.3...v0.1.4) (2026-10-03)


### Features

* **encoding:** carry the capture's own audio track through finalization f0df194
* **manager:** report audio_included from the take, not as a constant 7a37c81
* **recording:** resolve an opt-in desktop-audio source per take b02d7c3
* **specs:** expose the audio argument and the host's audio defaults 5877a2e


### Documentation

* document the opt-in desktop-audio contract end to end a38a07f

## [0.1.3](https://github.com/blackopsrepl/seshat/compare/v0.1.2...v0.1.3) (2026-09-29)


### Features

* **capture:** request frames continuously so a static tail is recorded 8cce9c8
* **diagnostics:** surface the compositor and Omarchy distribution da48cdd
* **lifecycle:** separate capture, shutdown and artifact validation 7a0964c
* **session:** detect the compositor instead of assuming Sway 061ec68


### Bug Fixes

* **outputs:** project Hyprland monitors into logical geometry 0d373d2
* **release:** link issue references in the changelog d05ee16


### Build System

* **manifest:** ship docs assets in the sdist 5baad59


### Documentation

* describe the take's lifecycle facts and validated publication 5b1fb98
* document Hyprland and Omarchy support bdcbf02
* fold the Omarchy live-fire run into every surface ([#3](https://github.com/blackopsrepl/seshat/issues/3)) 3d52de3
* **readme:** add the Sesh mascot 17b8865
* **wireframe:** describe the compositor override as a force 1e536af

## [0.1.2](https://github.com/blackopsrepl/seshat/compare/v0.1.1...v0.1.2) (2026-09-29)


### Build System

* **release:** show docs-only releases in the changelog b6856ec


### Documentation

* **readme:** say how to install the agent skill 31c2271

## [0.1.1](https://github.com/blackopsrepl/seshat/compare/v0.1.0...v0.1.1) (2026-09-28)


### Build System

* **make:** dress the build system in the SolverForge idiom bfa25b2
* **release:** keep tooling-only releases visible in the changelog 9b445eb

## 0.1.0 (2026-09-28)


### Features

* **capture:** record an output into a silent artifact with a take window 0edf9be
* **narration:** synthesize, align, caption and mux a narration track 9732346
* **server:** own the take lifecycle and expose it as MCP tools 5e03947
* **streams:** publish and ingest the take timeline as an event stream 14b332b


### Bug Fixes

* **anchors:** validate and report against the playable video, not the capture clock 9d4fb77
* **test:** stop racing the finalization worker in the shutdown tests 82232cb
