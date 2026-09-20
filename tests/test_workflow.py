"""Durable Arr -> transcode -> rename -> AutoPulse workflow."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.config import ArrInstanceCfg, AutoPulseCfg, Config, add_library
from app.db import Db
from app.engine import Engine
from app.integrations import RenameResult
from app.workflow import Workflow


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Db(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.db.close)
        self.cfg = Config()
        self.cfg.integrations.sonarr = [ArrInstanceCfg(
            id="tv", name="TV", url="http://sonarr:8989", api_key="arr",
            mode="cleanup", poll_interval=0.01)]
        self.cfg.integrations.autopulse = AutoPulseCfg(
            enabled=True, url="http://autopulse:2875",
            username="user", password="pass")
        self.workflow = Workflow(self.cfg, self.db)

    def accept(self):
        return self.workflow.accept(
            path="/media/TV/Show/episode.mkv", provider="sonarr",
            integration="tv", entity_id=42, file_id=7,
            event_type="download", mode="cleanup",
            payload={"downloadId": "abc"})

    def test_import_is_committed_and_redelivery_is_idempotent(self):
        first, queue_first = self.accept()
        second, queue_second = self.accept()

        self.assertTrue(queue_first)
        self.assertTrue(queue_second)  # active engine path dedupe prevents a second queue item
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["mode"], "cleanup")
        self.assertEqual(len(self.db.list_import_requests()), 1)
        self.assertEqual(len(self.db.list_jobs()), 1)

    def test_redelivery_is_accepted_after_original_path_was_replaced(self):
        job, _ = self.accept()
        self.db.update_job(job["id"], status="done", stage="complete",
                           final_path="/media/TV/Show/episode.mkv")
        engine = Engine(self.cfg, self.db)

        accepted, message = engine.enqueue_import(
            "/media/TV/Show/episode.mkv", provider="sonarr",
            integration="tv", entity_id=42, file_id=7,
            event_type="download", mode="cleanup",
            payload={"downloadId": "abc"})

        self.assertTrue(accepted)
        self.assertEqual(message, "already done")
        self.assertEqual(len(self.db.list_jobs()), 1)

    def test_startup_repairs_import_committed_before_its_job(self):
        request = self.db.create_import_request(
            "sonarr:orphan", "/media/TV/Show/episode.mkv",
            source_provider="sonarr",
            source_metadata={"integration": "tv", "entity_id": 42,
                             "file_id": 7},
            mode="cleanup")

        rows = self.workflow.recoverable()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dedupe_key"], "sonarr:orphan")
        repaired = self.db.get_import_request(request["id"])
        self.assertEqual(repaired["status"], "queued")
        self.assertEqual(repaired["job_id"], rows[0]["id"])

    def test_engine_noop_import_is_still_handed_to_autopulse(self):
        media = Path(self.tmp.name) / "TV"
        media.mkdir()
        path = media / "episode.mkv"
        path.write_bytes(b"media")
        lib = add_library(self.cfg, "TV", [str(media)])
        lib.enabled = True
        engine = Engine(self.cfg, self.db)

        accepted, _ = engine.enqueue_import(
            str(path), provider="sonarr", integration="tv",
            entity_id=42, file_id=7, mode="cleanup", payload={})
        self.assertTrue(accepted)
        job = self.db.jobs_for_path(str(path))[0]
        engine.workflow.claim(job["id"])
        plan = SimpleNamespace(
            manual_review="", needs_work=False, size=path.stat().st_size,
            library=lib.id, reasons=[], height=0, src_video_codec="hevc",
            to_dict=lambda: {})
        with mock.patch("app.engine.probe_file", return_value=object()), \
                mock.patch("app.engine.plan_file", return_value=plan):
            self.assertEqual(
                engine._process_one(str(path), durable_id=job["id"]), "skip")

        action = self.db.get_outbox(
            dedupe_key=job["dedupe_key"], action="autopulse")
        self.assertIsNotNone(action)
        self.assertEqual(self.db.get_job(job["id"])["status"], "waiting")

    def test_changed_file_rescans_renames_then_notifies_autopulse(self):
        job, _ = self.accept()
        self.workflow.claim(job["id"])
        self.workflow.processing_succeeded(
            job["id"], "/media/TV/Show/episode.mkv", changed=True)
        self.assertEqual(self.workflow.recoverable(), [])

        renamed = "/media/TV/Show/episode [HEVC AAC].mkv"
        arr = mock.Mock()
        arr.reconcile.return_value = RenameResult(renamed, 7, {}, {})
        pulse = mock.Mock()
        with mock.patch("app.workflow.integrations.SonarrClient",
                        return_value=arr), mock.patch(
                            "app.workflow.integrations.AutoPulseClient",
                            return_value=pulse):
            self.assertTrue(self.workflow.drain_once())
            self.assertTrue(self.workflow.drain_once())

        arr.reconcile.assert_called_once()
        pulse.trigger.assert_called_once_with(renamed)
        done = self.db.get_job(job["id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["stage"], "complete")
        self.assertEqual(done["final_path"], renamed)
        self.assertEqual(
            [r["status"] for r in self.db.list_outbox()], ["done", "done"])

    def test_unchanged_file_goes_straight_to_autopulse(self):
        job, _ = self.accept()
        self.workflow.claim(job["id"])
        path = "/media/TV/Show/episode.mkv"
        self.workflow.processing_succeeded(job["id"], path, changed=False)

        pulse = mock.Mock()
        with mock.patch("app.workflow.integrations.SonarrClient") as arr, \
                mock.patch("app.workflow.integrations.AutoPulseClient",
                           return_value=pulse):
            self.assertTrue(self.workflow.drain_once())

        arr.assert_not_called()
        pulse.trigger.assert_called_once_with(path)
        self.assertEqual(self.db.get_job(job["id"])["status"], "done")

    def test_exhausted_delivery_is_failed_without_reencoding(self):
        self.cfg.integrations.autopulse.max_retries = 1
        job, _ = self.accept()
        self.workflow.claim(job["id"])
        self.workflow.processing_succeeded(
            job["id"], "/media/TV/Show/episode.mkv", changed=False)

        pulse = mock.Mock()
        pulse.trigger.side_effect = RuntimeError("offline")
        with mock.patch("app.workflow.integrations.AutoPulseClient",
                        return_value=pulse):
            self.assertTrue(self.workflow.drain_once())

        failed = self.db.get_job(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stage"], "autopulse")
        self.assertEqual(len(self.db.list_jobs()), 1)

        engine = Engine(self.cfg, self.db)
        self.assertEqual(engine.retry_workflow(job["id"]), 1)
        self.assertEqual(self.db.get_job(job["id"])["status"], "waiting")
        self.assertEqual(self.db.get_outbox(
            dedupe_key=job["dedupe_key"], action="autopulse")["status"],
            "pending")


if __name__ == "__main__":
    unittest.main()
