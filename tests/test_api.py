"""Web API, driven against a real server on an ephemeral port.

No media is touched: the daemon's worker threads are never started, so these
exercise routing, validation, persistence and the state layer only.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as cfgmod      # noqa: E402
from app.config import Config         # noqa: E402
from app.db import Db                 # noqa: E402
from app.engine import ActiveJob, Engine  # noqa: E402
from app.web import serve             # noqa: E402


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.config_path = root / "config.toml"

        cfg = Config()
        cfg.state_db = str(root / "state.db")
        for name in ("TV", "Movies"):
            cfgmod.add_library(cfg, name, [str(root / "media" / name)]).enabled = True
        cfg.output.temp_dir = str(root / "temp")
        cfg.schedule.enabled = False
        cfg.web.host = "127.0.0.1"
        cfg.web.port = 0            # let the OS pick a free port
        cfgmod.save(cfg, cls.config_path)

        cls.cfg = cfg
        cls.db = Db(cfg.state_db)
        cls.engine = Engine(cfg, cls.db, config_path=str(cls.config_path))
        cls.httpd = serve(cfg, cls.engine)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

        # Fixture paths go through the same normalisation the API applies, so
        # these tests behave the same on POSIX and Windows.
        cls.A = str(Path("/media/TV/A.mkv"))
        cls.B = str(Path("/media/Movies/B.mkv"))

    def setUp(self):
        """Reset to two tracked files, with no media behind them.

        Several endpoints mutate state, so each test starts from the same
        place rather than depending on execution order.
        """
        with self.db._lock:
            self.db._conn.execute("DELETE FROM files")
            self.db._conn.execute("DELETE FROM history")
            self.db._conn.commit()
        with self.engine._lock:
            self.engine._queued.clear()
            self.engine._cancelled.clear()
        self.db.upsert(self.A, size=1000, mtime=1.0, status="pending",
                       library="tv",
                       reasons=["encode video h264 -> x265 crf 22"], height=1080,
                       video_codec="h264")
        self.db.upsert(self.B, size=500, mtime=1.0, status="failed",
                       library="movies", reasons=[], height=720,
                       video_codec="h264", error="boom", attempts=3)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.db.close()
        cls.tmp.cleanup()

    # --- helpers ----------------------------------------------------------

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read()), r.status

    def post(self, path, payload=None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read()), r.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read()), e.code

    def get_raw(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.read(), r.status, r.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.read(), e.code, None

    # --- static -----------------------------------------------------------

    def test_panel_and_assets_are_served(self):
        for path, ctype in [("/", "text/html"), ("/app.css", "text/css"),
                            ("/app.js", "text/javascript")]:
            body, status, got = self.get_raw(path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, got, path)
            self.assertGreater(len(body), 100, path)

    def test_unknown_route_is_404(self):
        _, status, _ = self.get_raw("/api/nope")
        self.assertEqual(status, 404)

    # --- reads ------------------------------------------------------------

    def test_status(self):
        job = ActiveJob(
            path=self.A, started=1.0, stage="copying",
            encode_percent=100.0, encode_speed=1.25,
            copy_percent=40.0, copy_speed=85.5,
            copy_bytes=400, copy_total=1000,
        )
        with self.engine._lock:
            self.engine._active[self.A] = job
        try:
            d, _ = self.get("/api/status")
            for key in ("counts", "active", "queued", "recent", "workers",
                        "schedule_enabled", "bytes_saved", "queue_depth"):
                self.assertIn(key, d)
            self.assertEqual(d["counts"]["pending"], 1)
            self.assertEqual(d["counts"]["failed"], 1)
            active = d["active"][0]
            self.assertEqual(active["encode_percent"], 100.0)
            self.assertEqual(active["copy_percent"], 40.0)
            self.assertEqual(active["copy_speed"], 85.5)
            self.assertEqual(active["copy_bytes"], 400)
            self.assertEqual(active["copy_total"], 1000)
        finally:
            with self.engine._lock:
                self.engine._active.pop(self.A, None)

    def test_files_filter_search_and_paging(self):
        d, _ = self.get("/api/files?status=all")
        self.assertEqual(d["total"], 2)

        d, _ = self.get("/api/files?status=failed")
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["files"][0]["name"], "B.mkv")

        d, _ = self.get("/api/files?q=A")
        self.assertEqual(d["total"], 1)

        d, _ = self.get("/api/files?status=all&limit=1&offset=1")
        self.assertEqual(len(d["files"]), 1)
        self.assertEqual(d["total"], 2)

    def test_files_reasons_come_back_parsed(self):
        d, _ = self.get("/api/files?q=A.mkv")
        self.assertEqual(d["files"][0]["reasons"],
                         ["encode video h264 -> x265 crf 22"])

    def test_file_detail(self):
        d, _ = self.get("/api/file?path=" + urllib.parse.quote(self.A))
        self.assertEqual(d["file"]["name"], "A.mkv")
        self.assertIn("history", d)
        self.assertIn("max_attempts", d)

    def test_completed_history_links_to_renamed_output(self):
        source = str(Path("/media/TV/old.avi"))
        final = str(Path("/media/TV/old.mkv"))
        self.db.upsert(final, size=600, mtime=2.0, status="done", library="tv")
        run_id = self.db.start_run(source, {"container": "mkv"})
        self.db.finish_run(run_id, "done", 1000, 600, 1.0,
                           detail={"container": "mkv"}, final_path=final)

        history, _ = self.get("/api/history")
        run = next(r for r in history["history"] if r["id"] == run_id)
        self.assertEqual(run["path"], final)
        self.assertEqual(run["name"], "old.mkv")

        detail, status = self.get(
            "/api/file?path=" + urllib.parse.quote(run["path"]))
        self.assertEqual(status, 200)
        self.assertEqual(detail["file"]["path"], final)

    def test_file_detail_requires_a_known_path(self):
        _, status, _ = self.get_raw("/api/file?path=" + urllib.parse.quote(str(Path("/media/TV/missing.mkv"))))
        self.assertEqual(status, 404)

    def test_file_detail_requires_a_path(self):
        _, status, _ = self.get_raw("/api/file")
        self.assertEqual(status, 400)

    # --- actions ----------------------------------------------------------

    def test_scan_is_accepted(self):
        d, status = self.post("/api/scan")
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])

    def test_process_requires_an_existing_file(self):
        d, status = self.post("/api/process", {"path": self.A})
        self.assertEqual(status, 200)
        self.assertFalse(d["ok"])          # tracked, but not on disk
        self.assertIn("does not exist", d["message"])

    def test_process_requires_a_path(self):
        d, status = self.post("/api/process", {})
        self.assertEqual(status, 400)
        self.assertIn("path is required", d["error"])

    def test_retry_resets_failures(self):
        d, status = self.post("/api/retry")
        self.assertEqual(status, 200)
        self.assertIn("reset 1", d["message"])
        row = self.db.get(self.B)
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["error"])
        # retry also queues what it reset, so nothing is left failed
        self.assertEqual(self.get("/api/status")[0]["counts"]["failed"], 0)

    def test_cancel_reports_when_not_queued(self):
        d, _ = self.post("/api/cancel", {"path": self.A})
        self.assertFalse(d["ok"])

    def test_bad_json_is_rejected(self):
        req = urllib.request.Request(
            self.base + "/api/config", data=b"{oops",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    # --- config -----------------------------------------------------------

    def test_config_get_returns_schema_and_file(self):
        d, _ = self.get("/api/config")
        self.assertTrue(d["schema"])
        self.assertIn("[modes.video]", d["toml"])
        self.assertEqual(d["path"], str(self.config_path))

    def test_config_update_persists_to_disk(self):
        d, status = self.post("/api/config",
                              {"updates": {"workers.pools": 9}})
        self.assertEqual(status, 200)
        self.assertEqual(d["changed"], ["workers.pools"])
        self.assertEqual(self.cfg.workers.pools, 9)
        self.assertEqual(cfgmod.load(self.config_path).workers.pools, 9)

    def test_files_can_be_filtered_by_library(self):
        d, _ = self.get("/api/files?status=all&library=tv")
        self.assertEqual(d["total"], 1)
        self.assertEqual(d["files"][0]["name"], "A.mkv")

    # --- libraries --------------------------------------------------------

    def test_libraries_listing(self):
        d, _ = self.get("/api/libraries")
        ids = [l["id"] for l in d["libraries"]]
        self.assertEqual(ids, ["tv", "movies"])
        tv = d["libraries"][0]
        self.assertTrue(tv["schema"])
        self.assertIn("video", tv["stages"])
        self.assertEqual(tv["stats"]["counts"].get("pending"), 1)

    def test_library_update_persists_and_is_independent(self):
        """Pointing one library at another mode leaves the other alone."""
        d, status = self.post("/api/libraries/update",
                              {"id": "movies", "updates": {"mode": "cleanup"}})
        self.assertEqual(status, 200)
        self.assertEqual(d["changed"], ["mode"])

        saved = cfgmod.load(self.config_path)
        self.assertEqual(saved.library("movies").mode, "cleanup")
        self.assertEqual(saved.library("tv").mode, "standard")

        self.post("/api/libraries/update",
                  {"id": "movies", "updates": {"mode": "standard"}})

    def test_library_update_rejects_bad_values_without_writing(self):
        before = self.config_path.read_text(encoding="utf-8")
        d, status = self.post("/api/libraries/update",
                              {"id": "tv", "updates": {"min_size_mb": -5}})
        self.assertEqual(status, 400)
        self.assertIn("at least 0", d["error"])
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_library_update_rejects_an_unknown_mode(self):
        d, status = self.post("/api/libraries/update",
                              {"id": "tv", "updates": {"mode": "nope"}})
        self.assertEqual(status, 400)
        self.assertIn("no such processing mode", d["error"])
        self.assertEqual(cfgmod.load(self.config_path).library("tv").mode,
                         "standard")

    def test_library_update_unknown_id(self):
        _, status = self.post("/api/libraries/update",
                              {"id": "nope", "updates": {}})
        self.assertEqual(status, 404)

    def test_library_add_and_delete(self):
        root = str(Path(self.tmp.name) / "media" / "Music")
        d, status = self.post("/api/libraries/add",
                              {"name": "Concerts", "paths": [root]})
        self.assertEqual(status, 200)
        self.assertEqual(d["id"], "concerts")
        self.assertIn("concerts", [l["id"] for l in d["libraries"]])
        self.assertTrue(cfgmod.load(self.config_path).library("concerts"))

        d, status = self.post("/api/libraries/delete", {"id": "concerts"})
        self.assertEqual(status, 200)
        self.assertNotIn("concerts", [l["id"] for l in d["libraries"]])
        self.assertIsNone(cfgmod.load(self.config_path).library("concerts"))

    def test_library_add_accepts_newline_separated_paths(self):
        base = Path(self.tmp.name) / "media"
        d, status = self.post("/api/libraries/add", {
            "name": "Kids",
            "paths": str(base / "Kids") + "\n" + str(base / "Cartoons"),
        })
        self.assertEqual(status, 200)
        lib = next(l for l in d["libraries"] if l["id"] == "kids")
        self.assertEqual(len(lib["paths"]), 2)
        self.post("/api/libraries/delete", {"id": "kids"})

    def test_library_add_rejects_overlap(self):
        d, status = self.post("/api/libraries/add", {
            "name": "Dupe", "paths": [str(Path(self.tmp.name) / "media" / "TV")],
        })
        self.assertEqual(status, 400)
        self.assertIn("overlaps", d["error"])

    def test_library_add_rejects_missing_name(self):
        _, status = self.post("/api/libraries/add",
                              {"name": "", "paths": ["/x"]})
        self.assertEqual(status, 400)

    def test_deleting_a_library_forgets_its_files(self):
        base = Path(self.tmp.name) / "media"
        self.post("/api/libraries/add",
                  {"name": "Temp", "paths": [str(base / "Temp")]})
        tracked = str(Path("/media/Temp/x.mkv"))
        self.db.upsert(tracked, size=1, mtime=1.0, status="pending",
                       library="temp")
        self.assertIsNotNone(self.db.get(tracked))
        self.post("/api/libraries/delete", {"id": "temp"})
        self.assertIsNone(self.db.get(tracked))

    def test_every_library_can_be_deleted(self):
        """Ending up with none is a legitimate state, not an error."""
        # The server is shared across tests, so put the libraries back.
        snapshot = copy.deepcopy(self.cfg.libraries)
        self.addCleanup(lambda: self.cfg.libraries.__setitem__(
            slice(None), copy.deepcopy(snapshot)))

        for lib_id in ("movies", "tv"):
            d, status = self.post("/api/libraries/delete", {"id": lib_id})
            self.assertEqual(status, 200, lib_id)
        self.assertEqual(d["libraries"], [])
        self.assertEqual(self.get("/api/libraries")[0]["libraries"], [])

        # The panel still has a mode editor to draw, and a scan still runs -
        # it just has nothing to walk.
        _, status = self.get("/api/modes")
        self.assertEqual(status, 200)
        _, status = self.post("/api/scan")
        self.assertEqual(status, 200)

    def test_scan_accepts_a_library(self):
        d, status = self.post("/api/scan", {"library": "tv"})
        self.assertEqual(status, 200)
        self.assertTrue(d["ok"])

    def test_scan_rejects_an_unknown_library(self):
        _, status = self.post("/api/scan", {"library": "nope"})
        self.assertEqual(status, 404)

    def test_status_lists_libraries(self):
        d, _ = self.get("/api/status")
        self.assertEqual([l["id"] for l in d["libraries"]], ["tv", "movies"])

    def test_config_update_reports_restart_only_fields(self):
        d, _ = self.post("/api/config", {"updates": {"workers.count": 7}})
        self.assertIn("workers.count", d["needs_restart"])

    def test_config_update_rejects_bad_values_without_writing(self):
        before = self.config_path.read_text(encoding="utf-8")
        d, status = self.post("/api/config", {"updates": {"web.port": 0}})
        self.assertEqual(status, 400)
        self.assertIn("at least 1", d["error"])
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_config_update_requires_an_object(self):
        _, status = self.post("/api/config", {"updates": "nope"})
        self.assertEqual(status, 400)

    def test_schedule_toggle_persists(self):
        d, _ = self.post("/api/schedule", {"enabled": True})
        self.assertTrue(d["enabled"])
        self.assertTrue(cfgmod.load(self.config_path).schedule.enabled)

        self.post("/api/schedule", {"enabled": False})
        self.assertFalse(cfgmod.load(self.config_path).schedule.enabled)

    def test_schedule_requires_enabled(self):
        _, status = self.post("/api/schedule", {})
        self.assertEqual(status, 400)

    # --- modes ------------------------------------------------------------

    def test_modes_listing(self):
        d, _ = self.get("/api/modes")
        self.assertEqual([m["id"] for m in d["modes"]], ["standard", "cleanup"])
        cleanup = d["modes"][1]
        self.assertFalse(cleanup["stages"]["video"])
        self.assertTrue(cleanup["schema"])
        keys = {f["key"] for block in cleanup["schema"] for f in block["fields"]}
        self.assertIn("video.enabled", keys)
        self.assertIn("output.container", keys)

    def test_a_mode_lists_the_libraries_using_it(self):
        d, _ = self.get("/api/modes")
        standard = d["modes"][0]
        self.assertEqual(sorted(l["id"] for l in standard["libraries"]),
                         ["movies", "tv"])
        self.assertEqual(d["modes"][1]["libraries"], [])

    def test_a_library_reports_the_mode_it_uses(self):
        d, _ = self.get("/api/libraries")
        tv = next(l for l in d["libraries"] if l["id"] == "tv")
        self.assertEqual(tv["mode"], "standard")
        self.assertEqual(tv["mode_name"], "Standard")
        self.assertTrue(tv["stages"]["video"])

    def test_process_accepts_a_mode(self):
        # No media behind the fixture, so it cannot actually queue - but the
        # mode is accepted and echoed rather than rejected.
        d, status = self.post("/api/process", {"path": self.A, "mode": "cleanup"})
        self.assertEqual(status, 200)
        self.assertEqual(d["mode"], "cleanup")

    def test_process_rejects_an_unknown_mode(self):
        d, status = self.post("/api/process", {"path": self.A, "mode": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("no such mode", d["error"])
        self.assertIn("cleanup", d["error"])

    def test_process_without_a_mode_is_unchanged(self):
        d, status = self.post("/api/process", {"path": self.A})
        self.assertEqual(status, 200)
        self.assertIsNone(d["mode"])

    def test_native_sonarr_webhook_extracts_ids_and_waits_for_enqueue(self):
        path = Path(self.tmp.name) / "media" / "TV" / "native.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        calls = []
        self.engine.enqueue_import = lambda **kwargs: (
            calls.append(kwargs) or (True, "accepted"))
        self.addCleanup(lambda: delattr(self.engine, "enqueue_import")
                        if hasattr(self.engine, "enqueue_import") else None)
        d, status = self.post("/api/webhook/sonarr", {
            "eventType": "Download", "isUpgrade": True,
            "series": {"id": 42},
            "episodeFile": {"id": 7, "path": str(path)},
        })
        self.assertEqual(status, 200, d)
        self.assertTrue(d["accepted"])
        self.assertEqual(d["provider"], "sonarr")
        self.assertEqual(d["entity_id"], 42)
        self.assertEqual(d["file_id"], 7)
        self.assertEqual(d["path"], str(path))
        self.assertTrue(calls[0]["is_upgrade"])

    def test_native_non_import_event_is_acknowledged_without_enqueue(self):
        calls = []
        self.engine.enqueue_import = lambda **kwargs: calls.append(kwargs)
        self.addCleanup(lambda: delattr(self.engine, "enqueue_import")
                        if hasattr(self.engine, "enqueue_import") else None)
        d, status = self.post("/api/webhook/radarr", {
            "eventType": "Test", "movie": {"id": 9},
            "movieFile": {"id": 3, "path": "/media/Movies/x.mkv"},
        })
        self.assertEqual(status, 200)
        self.assertTrue(d["ignored"])
        self.assertFalse(calls)

    def test_native_import_requires_arr_entity_id(self):
        d, status = self.post("/api/webhook/sonarr", {
            "eventType": "Download",
            "episodeFile": {"id": 7, "path": self.A},
        })
        self.assertEqual(status, 400)
        self.assertIn("series id", d["error"])

    def test_process_requires_a_path_before_a_mode(self):
        _, status = self.post("/api/process", {"mode": "cleanup"})
        self.assertEqual(status, 400)

    def test_check_rejects_an_unknown_mode(self):
        _, status = self.post("/api/check", {"path": self.A, "mode": "nope"})
        self.assertEqual(status, 400)

    def test_mode_add_update_and_delete(self):
        d, status = self.post("/api/modes/add",
                              {"name": "Subs only", "copy_from": "cleanup"})
        self.assertEqual(status, 200, d)
        self.assertEqual(d["id"], "subs-only")

        saved = cfgmod.load(self.config_path).mode("subs-only")
        self.assertFalse(saved.video.enabled)          # copied from cleanup

        d, status = self.post("/api/modes/update",
                              {"id": "subs-only",
                               "updates": {"audio.enabled": "false",
                                           "description": "Subtitles only."}})
        self.assertEqual(status, 200, d)
        saved = cfgmod.load(self.config_path).mode("subs-only")
        self.assertFalse(saved.audio.enabled)
        self.assertEqual(saved.description, "Subtitles only.")

        d, status = self.post("/api/modes/delete", {"id": "subs-only"})
        self.assertEqual(status, 200, d)
        self.assertIsNone(cfgmod.load(self.config_path).mode("subs-only"))

    def test_mode_update_changes_every_library_using_it(self):
        d, status = self.post("/api/modes/update",
                              {"id": "standard",
                               "updates": {"video.crf_1080p": 19}})
        self.assertEqual(status, 200, d)
        saved = cfgmod.load(self.config_path)
        for lib_id in ("tv", "movies"):
            self.assertEqual(
                cfgmod.resolve(saved, saved.library(lib_id)).video.crf_1080p, 19)
        self.post("/api/modes/update",
                  {"id": "standard", "updates": {"video.crf_1080p": 22}})

    def test_a_mode_in_use_cannot_be_deleted(self):
        d, status = self.post("/api/modes/delete", {"id": "standard"})
        self.assertEqual(status, 400)
        self.assertIn("in use", d["error"])
        self.assertIsNotNone(cfgmod.load(self.config_path).mode("standard"))

    def test_mode_add_rejects_an_unknown_source_without_writing(self):
        before = self.config_path.read_text(encoding="utf-8")
        _, status = self.post("/api/modes/add",
                              {"name": "Bad", "copy_from": "nope"})
        self.assertEqual(status, 400)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_mode_add_rejects_a_missing_name(self):
        _, status = self.post("/api/modes/add", {})
        self.assertEqual(status, 400)

    def test_mode_update_unknown_id(self):
        _, status = self.post("/api/modes/update",
                              {"id": "nope", "updates": {"name": "x"}})
        self.assertEqual(status, 404)

    def test_mode_update_rejects_bad_values_without_writing(self):
        before = self.config_path.read_text(encoding="utf-8")
        _, status = self.post("/api/modes/update",
                              {"id": "cleanup",
                               "updates": {"video.crf_1080p": 99}})
        self.assertEqual(status, 400)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)
        # The live mode is what every library on it runs, so a refused save
        # must leave it exactly as it was.
        live = self.engine.cfg.mode("cleanup")
        self.assertEqual(live.video.crf_1080p, 22)
        self.assertFalse(live.video.enabled)

    def test_mode_delete_unknown_id(self):
        _, status = self.post("/api/modes/delete", {"id": "nope"})
        self.assertEqual(status, 400)


class ApiKeyTest(unittest.TestCase):
    """With a key configured, /api/ needs it and the panel still works."""

    KEY = "test-key-1234567890"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cfg = Config()
        cfg.state_db = str(root / "state.db")
        cfgmod.add_library(cfg, "Media", [str(root / "media")]).enabled = True
        cfg.output.temp_dir = str(root / "temp")
        cfg.schedule.enabled = False
        cfg.web.host, cfg.web.port = "127.0.0.1", 0
        cfg.web.api_key = cls.KEY
        cls.db = Db(cfg.state_db)
        cls.engine = Engine(cfg, cls.db)
        cls.httpd = serve(cfg, cls.engine)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.db.close()
        cls.tmp.cleanup()

    def fetch(self, path, headers=None, method="GET"):
        req = urllib.request.Request(
            self.base + path, headers=headers or {}, method=method,
            data=b"{}" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.read(), r.status
        except urllib.error.HTTPError as e:
            return e.read(), e.code

    def test_api_without_a_key_is_rejected(self):
        _, status = self.fetch("/api/status")
        self.assertEqual(status, 401)

    def test_health_check_needs_no_key(self):
        body, status = self.fetch("/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_api_with_a_wrong_key_is_rejected(self):
        _, status = self.fetch("/api/status", {"X-Api-Key": "wrong"})
        self.assertEqual(status, 401)

    def test_api_with_the_header_is_accepted(self):
        _, status = self.fetch("/api/status", {"X-Api-Key": self.KEY})
        self.assertEqual(status, 200)

    def test_api_with_a_query_parameter_is_accepted(self):
        _, status = self.fetch("/api/status?apikey=" + self.KEY)
        self.assertEqual(status, 200)

    def test_api_with_a_bearer_token_is_accepted(self):
        _, status = self.fetch("/api/status",
                               {"Authorization": "Bearer " + self.KEY})
        self.assertEqual(status, 200)

    def test_posting_without_a_key_is_rejected(self):
        _, status = self.fetch("/api/process", method="POST",
                               headers={"Content-Type": "application/json"})
        self.assertEqual(status, 401)

    def test_posting_with_a_key_gets_past_auth(self):
        _, status = self.fetch(
            "/api/process", method="POST",
            headers={"Content-Type": "application/json", "X-Api-Key": self.KEY})
        self.assertEqual(status, 400)      # rejected for the missing path, not the key

    def test_the_panel_is_served_with_the_key_embedded(self):
        """Otherwise the UI could not call its own API.

        The key has to land as the *value*: index.html names __API_KEY__
        twice on that line, once as the window property and once as the
        placeholder, so substituting the bare token renames the property
        and the panel silently loses its credential on every request.
        """
        body, status = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn(f'window.__API_KEY__ = "{self.KEY}"', body.decode())

    def test_static_assets_need_no_key(self):
        for path in ("/", "/app.css", "/app.js"):
            _, status = self.fetch(path)
            self.assertEqual(status, 200, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
