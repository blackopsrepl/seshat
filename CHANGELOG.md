# Changelog

All notable changes to this project will be documented in this file. See [commit-and-tag-version](https://github.com/absolute-version/commit-and-tag-version) for commit guidelines.

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
