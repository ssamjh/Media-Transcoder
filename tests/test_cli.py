"""Startup preflight.

A bind mount carries the host directory's ownership, so the container's uid
can easily find /config or /temp read-only. That has to be caught at startup
with something actionable, not per file with a traceback from inside ffmpeg.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from app import cli
from app.config import Config


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = logging.getLogger("test.preflight")
        self.cfg = Config()
        self.cfg.state_db = str(self.root / "config" / "state.db")
        self.cfg.output.temp_dir = str(self.root / "temp")

    def test_writable_directories_pass_and_are_created(self):
        self.assertTrue(cli._preflight(self.cfg, self.log, check_temp=True))
        self.assertTrue((self.root / "config").is_dir())
        self.assertTrue((self.root / "temp").is_dir())

    def test_an_unusable_scratch_directory_fails(self):
        blocker = self.root / "temp"
        blocker.write_text("not a directory", encoding="utf-8")
        with self.assertLogs("test.preflight", level="ERROR"):
            self.assertFalse(cli._preflight(self.cfg, self.log, check_temp=True))

    def test_the_scratch_directory_is_only_checked_when_encoding(self):
        """scan and check never write there, so they should not demand it."""
        (self.root / "temp").write_text("not a directory", encoding="utf-8")
        self.assertTrue(cli._preflight(self.cfg, self.log, check_temp=False))

    def test_an_unusable_state_directory_fails(self):
        self.cfg.state_db = str(self.root / "wall" / "state.db")
        (self.root / "wall").write_text("not a directory", encoding="utf-8")
        with self.assertLogs("test.preflight", level="ERROR"):
            self.assertFalse(cli._preflight(self.cfg, self.log, check_temp=False))
