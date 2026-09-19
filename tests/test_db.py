"""Tracked state: what a scan forgets, and what it must keep.

Rows only survive because a library still routes to them, so narrowing or
deleting a library has to take its files with it - otherwise they keep
counting towards the library totals and keep getting queued. No media files
and no ffmpeg are involved.
"""

from __future__ import annotations

import os
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
