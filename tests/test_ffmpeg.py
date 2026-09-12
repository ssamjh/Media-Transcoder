"""Scratch-space housekeeping.

encode() cleans up after its own failures, so what is left in the scratch
directory is what a kill -9 or a host reboot left there - source-file sized,
and nothing else would ever remove it. No ffmpeg is involved.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import ffmpeg
from app.config import Config
from app.db import Db
from app.engine import Engine


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Config()
        self.cfg.output.temp_dir = str(self.root)

    def debris(self, name: str, size: int = 1024) -> Path:
        d = self.root / name
        d.mkdir()
        (d / "part.mkv").write_bytes(b"x" * size)
        return d

    def test_stale_work_directories_are_removed(self):
        a = self.debris(ffmpeg.WORK_PREFIX + "abc123", 2048)
        b = self.debris(ffmpeg.WORK_PREFIX + "def456", 1024)
        removed, freed = ffmpeg.sweep_scratch(self.cfg)
        self.assertEqual(removed, 2)
        self.assertEqual(freed, 3072)
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())

    def test_anything_else_in_the_directory_is_left_alone(self):
        """The scratch directory may well be someone's general temp space."""
        keep_dir = self.root / "important"
        keep_dir.mkdir()
        keep_file = self.root / "notes.txt"
        keep_file.write_text("hello", encoding="utf-8")
        self.debris(ffmpeg.WORK_PREFIX + "abc123")

        removed, _ = ffmpeg.sweep_scratch(self.cfg)
        self.assertEqual(removed, 1)
        self.assertTrue(keep_dir.exists())
        self.assertTrue(keep_file.exists())

    def test_a_missing_scratch_directory_is_not_an_error(self):
        self.cfg.output.temp_dir = str(self.root / "absent")
        self.assertEqual(ffmpeg.sweep_scratch(self.cfg), (0, 0))

    def test_the_engine_sweeps_when_it_starts(self):
        """Startup is the moment nothing of ours is encoding."""
        self.cfg.state_db = str(self.root / "state.db")
        self.cfg.schedule.enabled = False
        debris = self.debris(ffmpeg.WORK_PREFIX + "abc123")

        db = Db(self.cfg.state_db)
        self.addCleanup(db.close)
        engine = Engine(self.cfg, db)
        engine.start()
        try:
            self.assertFalse(debris.exists())
        finally:
            engine.stop()
            engine.join(timeout=5)
