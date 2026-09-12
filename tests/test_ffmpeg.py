"""Scratch-space housekeeping.

encode() cleans up after its own failures, so what is left in the scratch
directory is what a kill -9 or a host reboot left there - source-file sized,
and nothing else would ever remove it. No ffmpeg is involved.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import threading
import time

from app import ffmpeg
from app.config import Config, add_library
from app.db import Db
from app.engine import ActiveJob, Engine


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


class CopyBackTest(unittest.TestCase):
    """Putting the finished encode onto the library."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = Config()
        self.cfg.state_db = ":memory:"
        self.cfg.output.temp_dir = str(self.root / "temp")
        self.lib = add_library(self.cfg, "TV", [str(self.root / "media")])
        self.lib.enabled = True
        self.db = Db(self.cfg.state_db)
        self.addCleanup(self.db.close)
        self.engine = Engine(self.cfg, self.db)

    def test_copy_with_progress_copies_the_bytes_and_reports(self):
        src = self.root / "in.bin"
        dst = self.root / "out.bin"
        payload = bytes(range(256)) * (ffmpeg.COPY_CHUNK // 128)   # 2 chunks
        src.write_bytes(payload)

        seen: list[float] = []
        ffmpeg._copy_with_progress(src, dst, lambda pct, mbps: seen.append(pct))

        self.assertEqual(dst.read_bytes(), payload)
        self.assertEqual(seen, sorted(seen))
        self.assertAlmostEqual(seen[-1], 100.0, places=6)
        self.assertGreater(len(seen), 1)          # actually chunked
        self.assertEqual(int(src.stat().st_mtime), int(dst.stat().st_mtime))

    def test_only_one_file_is_copied_back_at_a_time(self):
        """A network share is one link; parallel copies just halve each other."""
        live = 0
        peak = 0
        guard = threading.Lock()

        def fake_replace(src, result, cfg, lib, on_progress=None):
            nonlocal live, peak
            with guard:
                live += 1
                peak = max(peak, live)
            if on_progress:
                on_progress(50.0, 12.5)
            time.sleep(0.05)
            with guard:
                live -= 1
            return True

        self.addCleanup(setattr, ffmpeg, "replace_original",
                        ffmpeg.replace_original)
        ffmpeg.replace_original = fake_replace

        jobs = [ActiveJob(path=f"/media/f{i}.mkv", started=time.time())
                for i in range(4)]
        threads = [threading.Thread(
            target=self.engine._copy_back,
            args=(j, Path(j.path), None, self.lib)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(peak, 1)
        for j in jobs:
            self.assertEqual(j.stage, "copying")
            self.assertEqual(j.percent, 50.0)

    def test_a_waiting_job_says_so(self):
        held = threading.Event()
        released = threading.Event()

        def fake_replace(src, result, cfg, lib, on_progress=None):
            held.set()
            released.wait(timeout=5)
            return True

        self.addCleanup(setattr, ffmpeg, "replace_original",
                        ffmpeg.replace_original)
        ffmpeg.replace_original = fake_replace

        first = ActiveJob(path="/media/a.mkv", started=time.time())
        second = ActiveJob(path="/media/b.mkv", started=time.time())
        t1 = threading.Thread(target=self.engine._copy_back,
                              args=(first, Path(first.path), None, self.lib))
        t1.start()
        self.assertTrue(held.wait(timeout=5))

        t2 = threading.Thread(target=self.engine._copy_back,
                              args=(second, Path(second.path), None, self.lib))
        t2.start()
        time.sleep(0.05)
        self.assertEqual(first.stage, "copying")
        self.assertEqual(second.stage, "waiting to copy")

        released.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
