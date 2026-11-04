"""The durable take store: adoption, rehydration and the list/discard/export surface.

Real filesystem, fake media: the media tier proves the pipelines, these prove
that a take survives the server process ending.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seshat import core, manager as manager_module, outputs, recording, store, streams, takes

from support import completed_job, event, single_output


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.runtime_rig = patch.dict(
            os.environ,
            {"XDG_RUNTIME_DIR": self.tmp.name, "XDG_STATE_HOME": str(self.state)},
        )
        self.runtime_rig.start()
        self.addCleanup(self.runtime_rig.stop)

    def store_job(self, **kwargs) -> recording.RecordingJob:
        job = completed_job(self.tmp.name, **kwargs)
        return job

    def test_adopt_moves_the_take_into_a_private_store_directory(self) -> None:
        job = self.store_job(events=[event(500.0)])
        job.result = {
            "id": job.id, "phase": "completed", "format": job.fmt,
            "path": str(job.artifact), "master_path": str(job.artifact),
        }
        sidecar = job.timeline_path
        assert sidecar is not None
        sidecar.write_text(json.dumps({"id": job.id, "events": []}), encoding="utf-8")
        directory = store.adopt(job, sidecar)
        store.write_manifest(job)
        self.assertEqual(directory, store.take_directory(job.id))
        self.assertTrue((directory / job.artifact.name).exists())
        self.assertTrue((directory / sidecar.name).exists())
        manifest = json.loads((directory / "take.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], job.id)
        self.assertTrue(manifest["result"])
        self.assertEqual(store.scan_store()[0]["id"], job.id)

    def test_narration_journal_appends_and_manages_permissions(self) -> None:
        job = self.store_job()
        store.append_narration_record(job.id, {"segments": [{"anchor": {"at_ms": 1}, "text": "a"}]}, {"engine": "sine"})
        store.append_narration_record(job.id, {"segments": [{"anchor": {"at_ms": 2}, "text": "b"}]}, {"engine": "sine"})
        journal = store.narration_journal_path(job.id)
        lines = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["arguments"]["segments"][0]["anchor"]["at_ms"], 1)
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_manifest_never_leaves_a_part_file(self) -> None:
        job = self.store_job()
        store.write_manifest(job)
        self.assertEqual(sorted(p.name for p in store.take_directory(job.id).iterdir()),
                         ["take.json"])

    def test_roundtrip_through_a_fresh_manager(self) -> None:
        job = self.store_job(events=[event(700.0)], fmt="mp4")
        job.result = {
            "id": job.id, "phase": "completed", "format": job.fmt,
            "path": str(job.artifact), "master_path": str(job.artifact),
            "media_duration_seconds": 2.0, "audio_included": False,
        }
        sidecar = job.timeline_path
        assert sidecar is not None, "completed_job always allocates a timeline path"
        sidecar.write_text(json.dumps(timeline_document_stub(job)), encoding="utf-8")
        directory = store.adopt(job, sidecar)
        job.artifact = directory / job.artifact.name
        job.timeline_path = directory / sidecar.name
        store.write_manifest(job)
        fresh = manager_module.RecordingManager()
        self.assertIn(job.id, fresh._jobs)
        rehydrated = fresh._jobs[job.id]
        self.assertTrue(rehydrated.rehydrated)
        self.assertEqual(rehydrated.phase, "completed")
        self.assertEqual(fresh.timeline({"id": job.id})["events"][0]["t_ms"], 700.0)
        listing = fresh.list_takes()
        self.assertEqual([t["id"] for t in listing["takes"]], [job.id])
        self.assertFalse(listing["takes"][0]["in_memory"])

    def test_discard_removes_only_that_take(self) -> None:
        job = self.store_job()
        job.result = {"id": job.id, "phase": "completed", "format": job.fmt}
        store.adopt(job, None)
        store.write_manifest(job)
        other = self.store_job()
        other.result = {"id": other.id, "phase": "completed", "format": other.fmt}
        store.adopt(other, None)
        store.write_manifest(other)
        fresh = manager_module.RecordingManager()
        result = fresh.discard({"id": job.id})
        self.assertTrue(result["discarded"])
        self.assertFalse(store.take_directory(job.id).exists())
        self.assertTrue(store.take_directory(other.id).exists())
        self.assertNotIn(job.id, fresh._jobs)

    def test_export_copies_every_published_file(self) -> None:
        job = self.store_job()
        job.result = {"id": job.id, "phase": "completed", "format": job.fmt,
                      "path": str(job.narrated_path), "master_path": str(job.artifact)}
        job.narrated_path.write_bytes(b"n")
        store.adopt(job, None)
        store.write_manifest(job)
        destination = Path(self.tmp.name) / "exported"
        fresh = manager_module.RecordingManager()
        result = fresh.export({"id": job.id, "destination": str(destination)})
        self.assertEqual(result["count"], 3)
        names = {f["file"] for f in result["files"]}
        self.assertIn(f"{job.id}.{job.fmt}", names)
        self.assertIn(f"{job.id}.narrated.{job.fmt}", names)
        self.assertIn(f"{job.id}.take.json", names)
        self.assertTrue((destination / f"{job.id}.{job.fmt}").exists())


def timeline_document_stub(job) -> dict:
    document = {
        "id": job.id,
        "format": job.fmt,
        "started_utc": job.started_utc,
        "sources": [],
        "events": [
            {"id": index, "t_ms": 700.0, "tool": "click", "ok": True, "payload": {}, "source": "seshat"}
            for index, _ in enumerate(job.events, 1)
        ],
    }
    return document


if __name__ == "__main__":
    unittest.main()