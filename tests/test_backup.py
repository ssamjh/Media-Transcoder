"""Daily snapshots of the state database.

The promise is "the last 7 days", so the tests that matter are the ones
about counting: a second snapshot on the same day must not cost a day, and
the retention window must drop the oldest rather than the newest. Nothing
here needs media files or ffmpeg - a snapshot is pure SQLite.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app import backup
from app.config import Config, ConfigError, apply_updates, dump_toml, loads
from app.db import Db
from app.engine import Engine


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config()
        self.cfg.state_db = str(Path(self.tmp.name) / "config" / "state.db")
        self.db = Db(self.cfg.state_db)
        self.addCleanup(self.db.close)
        self.db.upsert("/media/a.mkv", size=10, mtime=1.0, status="done",
                       library="media")

    @property
    def dir(self) -> Path:
        return backup.backup_dir(self.cfg)

    def day(self, offset: int) -> float:
        return (datetime.now() + timedelta(days=offset)).timestamp()

    def test_snapshot_lands_beside_the_database_and_is_readable(self):
        path = backup.run(self.db, self.cfg)
        self.assertEqual(path.parent, Path(self.cfg.state_db).parent / "backups")
        self.assertIsNone(backup.verify(path))

        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT path, status FROM files").fetchall()
        self.assertEqual(rows, [("/media/a.mkv", "done")])

    def test_a_snapshot_is_taken_while_the_database_is_being_written(self):
        """The backup API, not a file copy: a live WAL must not matter."""
        self.db.upsert("/media/b.mkv", size=20, mtime=1.0, status="running",
                       library="media")
        path = backup.run(self.db, self.cfg)
        self.assertIsNone(backup.verify(path))
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
        # A snapshot carries its own state entirely: no sidecars to ship.
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()), [path.name])

    def test_a_second_run_the_same_day_replaces_that_days_snapshot(self):
        first = backup.run(self.db, self.cfg)
        self.db.upsert("/media/b.mkv", size=20, mtime=1.0, status="pending",
                       library="media")
        second = backup.run(self.db, self.cfg)

        self.assertEqual(first, second)
        self.assertEqual(len(backup.list_backups(self.dir)), 1)
        conn = sqlite3.connect(second)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)

    def test_only_the_last_seven_days_are_kept(self):
        for offset in range(-9, 1):
            backup.run(self.db, self.cfg, when=self.day(offset))

        kept = [p.name for p in backup.list_backups(self.dir)]
        self.assertEqual(len(kept), 7)
        expected = [f"{backup.PREFIX}{backup.stamp(self.day(o))}{backup.SUFFIX}"
                    for o in range(-6, 1)]
        self.assertEqual(kept, expected)

    def test_retention_follows_the_configured_number(self):
        self.cfg.backup.keep = 2
        for offset in range(-4, 1):
            backup.run(self.db, self.cfg, when=self.day(offset))
        self.assertEqual(len(backup.list_backups(self.dir)), 2)

    def test_due_is_decided_by_the_newest_snapshot_on_disk(self):
        """A restart must not buy an extra snapshot."""
        self.assertTrue(backup.due(self.dir, 24.0))
        path = backup.run(self.db, self.cfg)
        self.assertFalse(backup.due(self.dir, 24.0))

        old = time.time() - 25 * 3600
        os.utime(path, (old, old))
        self.assertTrue(backup.due(self.dir, 24.0))

    def test_an_interrupted_snapshot_is_never_mistaken_for_a_good_one(self):
        class Exploding:
            def backup_to(self, dest):
                Path(dest).write_bytes(b"half a database")
                raise OSError("disk full")

        with self.assertRaises(OSError):
            backup.create(Exploding(), self.dir)
        self.assertEqual(backup.list_backups(self.dir), [])

        # And the leftover is swept by the next successful run.
        backup.run(self.db, self.cfg)
        self.assertEqual(
            [p.name for p in self.dir.iterdir()],
            [f"{backup.PREFIX}{backup.stamp()}{backup.SUFFIX}"])

    def test_restore_puts_the_snapshot_back_and_keeps_what_it_replaced(self):
        path = backup.run(self.db, self.cfg)
        self.db.upsert("/media/later.mkv", size=30, mtime=1.0, status="pending",
                       library="media")
        self.db.close()

        kept = backup.restore(path, self.cfg.state_db)
        self.assertIsNotNone(kept)
        self.assertTrue(kept.exists())

        restored = Db(self.cfg.state_db)
        self.addCleanup(restored.close)
        self.assertIsNotNone(restored.get("/media/a.mkv"))
        self.assertIsNone(restored.get("/media/later.mkv"))

    def test_a_damaged_snapshot_is_refused_before_anything_is_overwritten(self):
        bad = Path(self.tmp.name) / "bad.db"
        bad.write_bytes(b"not a database at all")
        self.db.close()

        with self.assertRaises(ValueError):
            backup.restore(bad, self.cfg.state_db)

        survived = Db(self.cfg.state_db)
        self.addCleanup(survived.close)
        self.assertIsNotNone(survived.get("/media/a.mkv"))

    def test_a_configured_directory_wins_over_the_default(self):
        elsewhere = Path(self.tmp.name) / "somewhere"
        self.cfg.backup.dir = str(elsewhere)
        path = backup.run(self.db, self.cfg)
        self.assertEqual(path.parent, elsewhere)


class EngineBackupTest(unittest.TestCase):
    """The daemon's view: one call, and when the next one is owed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config()
        self.cfg.state_db = str(Path(self.tmp.name) / "state.db")
        self.db = Db(self.cfg.state_db)
        self.addCleanup(self.db.close)
        self.engine = Engine(self.cfg, self.db)

    def test_backup_now_records_when_the_next_one_is_due(self):
        before = time.time()
        path = self.engine.backup_now()
        self.assertTrue(path.is_file())
        self.assertGreaterEqual(self.engine.last_backup, before)
        self.assertAlmostEqual(
            self.engine.next_backup - self.engine.last_backup, 24 * 3600,
            delta=5)

    def test_nothing_is_owed_immediately_after_one_is_taken(self):
        directory = backup.backup_dir(self.cfg)
        self.assertTrue(backup.due(directory, self.cfg.backup.interval_hours))
        self.engine.backup_now()
        self.assertFalse(backup.due(directory, self.cfg.backup.interval_hours))


class BackupConfigTest(unittest.TestCase):
    def test_settings_round_trip_through_the_generated_file(self):
        cfg = Config()
        cfg.backup.keep = 14
        cfg.backup.interval_hours = 12.0
        cfg.backup.dir = "/config/snapshots"
        again = loads(dump_toml(cfg))
        self.assertEqual(again.backup.keep, 14)
        self.assertEqual(again.backup.interval_hours, 12.0)
        self.assertEqual(again.backup.dir, "/config/snapshots")

    def test_defaults_are_a_daily_snapshot_kept_for_a_week(self):
        cfg = Config()
        self.assertTrue(cfg.backup.enabled)
        self.assertEqual(cfg.backup.interval_hours, 24.0)
        self.assertEqual(cfg.backup.keep, 7)

    def test_nonsense_retention_is_rejected(self):
        cfg = Config()
        with self.assertRaises(ConfigError):
            apply_updates(cfg, {"backup.keep": "0"})
        with self.assertRaises(ConfigError):
            loads("[backup]\nkeep = 0\n")

    def test_the_panel_can_change_retention(self):
        cfg = Config()
        changed = apply_updates(cfg, {"backup.keep": "3"})
        self.assertEqual(changed, ["backup.keep"])
        self.assertEqual(cfg.backup.keep, 3)


if __name__ == "__main__":
    unittest.main()
