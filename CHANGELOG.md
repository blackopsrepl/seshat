# Changelog

All notable changes to this project will be documented in this file. See [commit-and-tag-version](https://github.com/absolute-version/commit-and-tag-version) for commit guidelines.

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
