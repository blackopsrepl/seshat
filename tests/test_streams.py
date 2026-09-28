from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, streams


class AppendEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_writes_one_json_object_per_line(self) -> None:
        path = streams.append_event(
            "computer-use-sway", "click", at_monotonic=101.5, payload={"x": 4}
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(record["at_monotonic"], 101.5)
        self.assertEqual(record["tool"], "click")
        self.assertEqual(record["payload"], {"x": 4})
        self.assertEqual(record["source"], "computer-use-sway")
        self.assertTrue(record["ok"])

    def test_appends_without_truncating(self) -> None:
        path = streams.append_event("emitter", "click", at_monotonic=1.0)
        streams.append_event("emitter", "key", at_monotonic=2.0)
        self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)

    def test_directory_and_file_are_private(self) -> None:
        path = streams.append_event("emitter", "click", at_monotonic=1.0)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_defaults_to_now_on_the_host_clock(self) -> None:
        path = streams.append_event("emitter", "click")
        record = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreater(record["at_monotonic"], 0.0)

    def test_rejects_a_source_that_escapes_the_directory(self) -> None:
        for source in ("../escape", "sub/dir", "", 7):
            with self.assertRaises(core.ToolError, msg=repr(source)):
                streams.append_event(source, "click")

    def test_rejects_an_empty_tool(self) -> None:
        with self.assertRaises(core.ToolError):
            streams.append_event("emitter", "")


class StreamReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "emitter.jsonl"

    def write(self, *lines: str) -> Path:
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.path

    def line(self, at: float, tool: str = "click", **extra) -> str:
        record = {"at_monotonic": at, "tool": tool, **extra}
        return json.dumps(record)

    def test_filters_to_the_capture_window(self) -> None:
        self.write(self.line(99.0), self.line(100.0), self.line(105.0), self.line(111.0))
        read = streams.read_stream(self.path, 100.0, 110.0)
        self.assertEqual([event["at_monotonic"] for event in read.events], [100.0, 105.0])

    def test_counts_malformed_and_oversized_lines(self) -> None:
        self.write(
            self.line(101.0),
            "{not json}",
            json.dumps(["not", "an", "object"]),
            json.dumps({"tool": "click"}),
            json.dumps({"at_monotonic": True, "tool": "click"}),
            json.dumps({"at_monotonic": 102.0}),
            "x" * (streams.STREAM_MAX_LINE_BYTES + 1),
            "",
        )
        read = streams.read_stream(self.path, 0.0, 200.0)
        self.assertEqual(len(read.events), 1)
        self.assertEqual(read.malformed, 5)
        self.assertEqual(read.oversized, 1)

    def test_source_comes_from_the_record_when_present(self) -> None:
        self.write(self.line(101.0, source="driver"), self.line(102.0))
        read = streams.read_stream(self.path, 0.0, 200.0)
        self.assertEqual([event["source"] for event in read.events], ["driver", "emitter"])

    def test_non_dict_payload_is_replaced_not_propagated(self) -> None:
        self.write(self.line(101.0, payload="text"))
        read = streams.read_stream(self.path, 0.0, 200.0)
        self.assertEqual(read.events[0]["payload"], {})

    def test_unreadable_file_is_reported(self) -> None:
        read = streams.read_stream(Path(self.tmp.name) / "missing.jsonl", 0.0, 10.0)
        self.assertEqual(read.events, [])
        self.assertIsNotNone(read.error)
        self.assertIn("error", read.report())

    def test_event_cap_flags_truncation(self) -> None:
        with patch.object(streams, "STREAM_MAX_EVENTS", 2):
            self.write(self.line(101.0), self.line(102.0), self.line(103.0))
            read = streams.read_stream(self.path, 0.0, 200.0)
        self.assertEqual(len(read.events), 2)
        self.assertTrue(read.truncated)
        self.assertTrue(read.report()["truncated"])


class DiscoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_no_stream_directory_yet(self) -> None:
        self.assertEqual(streams.discover_streams(), [])

    def test_returns_streams_in_stable_order_ignoring_others(self) -> None:
        streams.append_event("zulu", "click")
        streams.append_event("alpha", "click")
        (streams.stream_directory() / "notes.txt").write_text("ignore me", encoding="utf-8")
        self.assertEqual(
            [path.name for path in streams.discover_streams()],
            ["alpha.jsonl", "zulu.jsonl"],
        )


class SourceResolveTests(unittest.TestCase):
    def test_missing_explicit_source_is_an_error(self) -> None:
        with self.assertRaises(core.ToolError):
            streams.parse_timeline_sources(["/nonexistent/stream.jsonl"])

    def test_non_list_is_an_error(self) -> None:
        for value in ("stream.jsonl", [], 7):
            with self.assertRaises(core.ToolError, msg=repr(value)):
                streams.parse_timeline_sources(value)

    def test_none_means_discover(self) -> None:
        self.assertIsNone(streams.parse_timeline_sources(None))

    def test_valid_paths_round_trip_as_strings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "stream.jsonl"
            path.write_text("", encoding="utf-8")
            self.assertEqual(streams.parse_timeline_sources([str(path)]), [str(path)])


class MergeTests(unittest.TestCase):
    def test_orders_by_host_monotonic_time_across_streams(self) -> None:
        reads = [
            streams.StreamRead(
                path=Path("b.jsonl"),
                source="b",
                events=[
                    {"at_monotonic": 3.0, "tool": "b1", "ok": True, "payload": {}, "source": "b"},
                    {"at_monotonic": 1.0, "tool": "b2", "ok": True, "payload": {}, "source": "b"},
                ],
            ),
            streams.StreamRead(
                path=Path("a.jsonl"),
                source="a",
                events=[
                    {"at_monotonic": 2.0, "tool": "a1", "ok": True, "payload": {}, "source": "a"},
                ],
            ),
        ]
        self.assertEqual(
            [event["tool"] for event in streams.merge_events(reads)], ["b2", "a1", "b1"]
        )


if __name__ == "__main__":
    unittest.main()
