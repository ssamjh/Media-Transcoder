"""Tracked state: what a scan forgets, and what it must keep.

Rows only survive because a library still routes to them, so narrowing or
deleting a library has to take its files with it - otherwise they keep
counting towards the library totals and keep getting queued. No media files
and no ffmpeg are involved.
"""

from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.config import Config, add_library, apply_library_updates
from app.db import Db
from app.engine import Engine


def p(*parts: str) -> str:
    return str(Path(os.sep.join(("", "media") + parts)))


class ForgettingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config()
        self.cfg.state_db = str(Path(self.tmp.name) / "state.db")
        add_library(self.cfg, "TV", [p("TV")]).enabled = True
        self.db = Db(self.cfg.state_db)
        self.addCleanup(self.db.close)
        self.engine = Engine(self.cfg, self.db)

        for path in (p("TV", "a.mkv"), p("TV", "sub", "b.mkv")):
            self.db.upsert(path, size=100, mtime=1.0, status="pending",
                           library="tv", reasons=["encode video"])

    def tracked(self) -> set[str]:
        return {r["path"] for r in self.db._conn.execute("SELECT path FROM files")}

    def test_narrowing_a_library_forgets_what_it_no_longer_covers(self):
        apply_library_updates(self.cfg, self.cfg.libraries[0],
                              {"paths": [p("TV", "sub")]})
        dropped = self.db.forget_unowned(self.engine._owned)
        self.assertEqual(dropped, 1)
        self.assertEqual(self.tracked(), {p("TV", "sub", "b.mkv")})

    def test_a_disabled_library_keeps_its_state(self):
        """Disabling is "leave it out of scans", not "forget everything"."""
        apply_library_updates(self.cfg, self.cfg.libraries[0], {"enabled": False})
        self.assertEqual(self.db.forget_unowned(self.engine._owned), 0)
        self.assertEqual(len(self.tracked()), 2)

    def test_another_librarys_files_are_left_alone(self):
        add_library(self.cfg, "Movies", [p("Movies")]).enabled = True
        self.db.upsert(p("Movies", "c.mkv"), size=100, mtime=1.0,
                       status="done", library="movies")
        self.assertEqual(self.db.forget_unowned(self.engine._owned), 0)
        self.assertEqual(len(self.tracked()), 3)

    def test_forget_missing_matches_roots_by_segment(self):
        """/media/TV must not claim rows under a sibling /media/TV-4K."""
        ghost = p("TV-4K", "d.mkv")
        self.db.upsert(ghost, size=100, mtime=1.0, status="pending",
                       library="tv4k")
        self.db.forget_missing(seen=set(), roots=[p("TV")])
        self.assertIn(ghost, self.tracked())

    def test_forget_missing_drops_files_that_are_gone(self):
        self.db.forget_missing(seen=set(), roots=[p("TV")])
        self.assertEqual(self.tracked(), set())


class HistoryPathRepairTest(unittest.TestCase):
    def test_old_history_is_relinked_to_its_tracked_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            source = p("TV", "Film.avi")
            final = p("TV", "Film.mkv")

            db = Db(db_path)
            db.upsert(final, size=60, mtime=1.0, status="done", library="tv")
            run_id = db.start_run(source, {"container": "mkv"})
            db.finish_run(run_id, "done", 100, 60, 1.0,
                          detail={"container": "mkv"})
            db.close()

            reopened = Db(db_path)
            try:
                rows, _ = reopened.history()
                run = next(r for r in rows if r["id"] == run_id)
                self.assertEqual(run["path"], final)
                self.assertEqual(run["name"], "Film.mkv")
            finally:
                reopened.close()


class DurableWorkTest(unittest.TestCase):
    """Import/work state survives duplicate hooks and process restarts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.db"
        self.db = Db(self.path)
        self.addCleanup(self.db.close)

    def test_job_is_deduplicated_and_updated_by_path(self):
        job = self.db.create_job(
            "sonarr:episode-1", "/media/TV/episode.mkv", provider="sonarr",
            metadata={"series_id": 42}, mode_id="import", now=10.0,
        )
        same = self.db.create_job(
            "sonarr:episode-1", "/media/TV/episode.mkv", metadata={"retry": 1},
            now=11.0,
        )
        self.assertEqual(job["id"], same["id"])
        self.assertEqual(self.db.get_job(source_path="/media/TV/episode.mkv")["id"],
                         job["id"])
        self.assertEqual(same["mode"], "import")
        self.assertEqual(json.loads(same["source_metadata"]), {"retry": 1})

        claimed = self.db.claim_job(dedupe_key="sonarr:episode-1", now=12.0)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["retries"], 1)
        finished = self.db.finish_job(
            claimed["id"], final_path="/media/TV/episode-clean.mkv"
        )
        self.assertEqual(finished["status"], "done")
        self.assertEqual(finished["final_path"], "/media/TV/episode-clean.mkv")

    def test_import_request_and_outbox_are_durable(self):
        request = self.db.create_import_request(
            "radarr:movie-1", "/media/Movies/movie.mkv", provider="radarr",
            metadata={"movie_id": 1}, mode="import", now=20.0,
        )
        self.assertEqual(self.db.get_import_request("radarr:movie-1")["id"],
                         request["id"])
        action = self.db.enqueue_outbox(
            "radarr:movie-1", "refresh", {"path": "/media/Movies/movie.mkv"},
            now=21.0,
        )
        self.assertEqual(
            self.db.enqueue_outbox("radarr:movie-1", "refresh")["id"], action["id"]
        )
        claimed = self.db.claim_outbox(now=22.0)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["retries"], 1)
        completed = self.db.complete_outbox(claimed["id"], now=23.0)
        self.assertEqual(completed["status"], "done")
        self.assertEqual(json.loads(completed["payload"])["path"],
                         "/media/Movies/movie.mkv")

    def test_running_work_is_retryable_after_reopen(self):
        job = self.db.create_job("restart-job", "/media/a.mkv")
        self.db.claim_job(job["id"], now=30.0)
        action = self.db.enqueue_outbox("restart-job", "notify")
        self.db.claim_outbox(now=30.0)
        self.db.close()
        self.db = Db(self.path)
        self.addCleanup(self.db.close)
        self.assertEqual(self.db.get_job(job["id"])["status"], "pending")
        self.assertEqual(self.db.get_outbox(action["id"])["status"], "pending")
        self.assertIsNotNone(self.db.get_job(job["id"])["next_attempt"])

    def test_partial_durable_tables_migrate_without_dropping_rows(self):
        self.db.close()
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + "-wal").unlink(missing_ok=True)
        Path(str(self.path) + "-shm").unlink(missing_ok=True)
        conn = sqlite3.connect(self.path)
        conn.executescript(
            "CREATE TABLE processing_jobs (id INTEGER PRIMARY KEY, "
            "dedupe_key TEXT, source_path TEXT);"
            "INSERT INTO processing_jobs VALUES (1, 'old', '/media/old.mkv');"
            "CREATE TABLE import_requests (id INTEGER PRIMARY KEY, "
            "dedupe_key TEXT, source_path TEXT);"
            "CREATE TABLE outbox_actions (id INTEGER PRIMARY KEY, "
            "dedupe_key TEXT, action TEXT);"
        )
        conn.commit()
        conn.close()
        self.db = Db(self.path)
        self.addCleanup(self.db.close)
        row = self.db.get_job("old")
        self.assertIsNotNone(row)
        self.assertEqual(row["source_path"], "/media/old.mkv")
        self.assertIn("next_attempt", row.keys())
        self.assertIsNotNone(self.db.create_job("new", "/media/new.mkv"))
