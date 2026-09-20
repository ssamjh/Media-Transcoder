"""Managing Sonarr/Radarr profiles and AutoPulse from the panel.

These drive the same endpoints the Integrations tab calls, against a real
server on an ephemeral port. No worker threads, no outbound HTTP: the one
test that touches the network asserts the failure path, because a Test
button that hangs is worse than one that says no.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as cfgmod      # noqa: E402
from app.config import Config         # noqa: E402
from app.db import Db                 # noqa: E402
from app.engine import Engine         # noqa: E402
from app.web import serve             # noqa: E402


class IntegrationApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config_path = root / "config.toml"

        cfg = Config()
        cfg.state_db = str(root / "state.db")
        cfg.schedule.enabled = False
        cfg.web.host = "127.0.0.1"
        cfg.web.port = 0
        cfgmod.save(cfg, self.config_path)

        self.cfg = cfg
        self.db = Db(cfg.state_db)
        self.addCleanup(self.db.close)
        self.engine = Engine(cfg, self.db, config_path=str(self.config_path))
        self.httpd = serve(cfg, self.engine)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    # --- helpers ----------------------------------------------------------

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read()), r.status

    def post(self, path, payload=None):
        req = urllib.request.Request(
            self.base + path, method="POST",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read()), r.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read()), e.code

    def add(self, provider="sonarr", name="TV Sonarr"):
        d, status = self.post("/api/integrations/add",
                              {"provider": provider, "name": name})
        self.assertEqual(status, 200)
        return d

    # --- listing ----------------------------------------------------------

    def test_a_fresh_install_has_no_profiles(self):
        d, _ = self.get("/api/integrations")
        self.assertEqual(d["sonarr"], [])
        self.assertEqual(d["radarr"], [])
        self.assertFalse(d["autopulse"]["enabled"])
        self.assertIn("standard", [m["id"] for m in d["modes"]])

    def test_added_profile_reports_its_webhook_url(self):
        d = self.add()
        card = d["sonarr"][0]
        self.assertEqual(card["id"], "tv-sonarr")
        self.assertTrue(card["webhook"].endswith("/api/webhook/sonarr/tv-sonarr"))
        # Created with no credentials, so it can receive but not reconcile,
        # and the panel says so rather than looking finished.
        self.assertFalse(card["outbound_ready"])
        self.assertFalse(card["api_key_configured"])

    def test_the_profile_is_written_to_the_config_file(self):
        self.add()
        text = self.config_path.read_text(encoding="utf-8")
        self.assertIn("[[integrations.sonarr]]", text)
        self.assertIn('id = "tv-sonarr"', text)

    def test_the_schema_carries_mode_choices_and_marks_credentials(self):
        d = self.add()
        fields = {f["key"]: f
                  for b in d["sonarr"][0]["schema"] for f in b["fields"]}
        self.assertIn("cleanup", fields["mode"]["choices"])
        self.assertIn("", fields["mode"]["choices"])   # the library's own mode
        self.assertTrue(fields["api_key"]["secret"])
        self.assertTrue(fields["secret"]["secret"])
        self.assertTrue(fields["id"]["readonly"])
        self.assertFalse(fields["url"]["secret"])

    # --- updating ---------------------------------------------------------

    def test_credentials_and_mode_can_be_saved(self):
        self.add()
        d, status = self.post("/api/integrations/update", {
            "provider": "sonarr", "id": "tv-sonarr",
            "updates": {"url": "http://sonarr:8989", "api_key": "abc",
                        "mode": "cleanup"},
        })
        self.assertEqual(status, 200)
        self.assertEqual(sorted(d["changed"]), ["api_key", "mode", "url"])
        card = d["sonarr"][0]
        self.assertTrue(card["outbound_ready"])
        self.assertEqual(card["mode_name"], "Cleanup")
        self.assertIn('api_key = "abc"',
                      self.config_path.read_text(encoding="utf-8"))

    def test_a_rejected_update_changes_nothing(self):
        self.add()
        self.post("/api/integrations/update", {
            "provider": "sonarr", "id": "tv-sonarr",
            "updates": {"url": "http://sonarr:8989"}})
        before = self.config_path.read_text(encoding="utf-8")

        # path_from without path_to is refused for the whole profile.
        d, status = self.post("/api/integrations/update", {
            "provider": "sonarr", "id": "tv-sonarr",
            "updates": {"path_from": "/arr/media", "max_retries": 9}})
        self.assertEqual(status, 400)
        self.assertIn("path_from and path_to", d["error"])

        live, _ = self.get("/api/integrations")
        fields = {f["key"]: f["value"]
                  for b in live["sonarr"][0]["schema"] for f in b["fields"]}
        self.assertEqual(fields["path_from"], "")
        self.assertEqual(fields["max_retries"], 3)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_an_unknown_mode_is_refused(self):
        self.add()
        d, status = self.post("/api/integrations/update", {
            "provider": "sonarr", "id": "tv-sonarr",
            "updates": {"mode": "nope"}})
        self.assertEqual(status, 400)
        self.assertIn("does not exist", d["error"])

    def test_the_id_cannot_be_changed(self):
        self.add()
        _, status = self.post("/api/integrations/update", {
            "provider": "sonarr", "id": "tv-sonarr",
            "updates": {"id": "other"}})
        self.assertEqual(status, 400)

    def test_unknown_profile_is_404(self):
        _, status = self.post("/api/integrations/update", {
            "provider": "sonarr", "id": "ghost", "updates": {"url": "x"}})
        self.assertEqual(status, 404)

    def test_unknown_provider_is_rejected(self):
        _, status = self.post("/api/integrations/add",
                              {"provider": "lidarr", "name": "Music"})
        self.assertEqual(status, 400)

    # --- deleting ---------------------------------------------------------

    def test_deleting_a_profile_removes_it_from_the_file(self):
        self.add()
        d, status = self.post("/api/integrations/delete",
                              {"provider": "sonarr", "id": "tv-sonarr"})
        self.assertEqual(status, 200)
        self.assertEqual(d["sonarr"], [])
        self.assertNotIn("[[integrations.sonarr]]",
                         self.config_path.read_text(encoding="utf-8"))

    def test_two_instances_of_one_provider_get_distinct_ids(self):
        self.add(name="Radarr HD")
        d = self.add(provider="sonarr", name="Radarr HD")
        self.assertEqual([c["id"] for c in d["sonarr"]],
                         ["radarr-hd", "radarr-hd-2"])

    # --- autopulse --------------------------------------------------------

    def test_autopulse_can_be_configured_from_the_panel(self):
        d, status = self.post("/api/integrations/autopulse", {"updates": {
            "enabled": True, "url": "http://autopulse:2875",
            "username": "user", "password": "pw"}})
        self.assertEqual(status, 200)
        self.assertTrue(d["autopulse"]["enabled"])
        self.assertEqual(d["autopulse"]["url"], "http://autopulse:2875")
        self.assertIn("[integrations.autopulse]",
                      self.config_path.read_text(encoding="utf-8"))

    def test_autopulse_rejects_a_url_without_a_scheme(self):
        self.post("/api/integrations/autopulse",
                  {"updates": {"url": "http://autopulse:2875"}})
        d, status = self.post("/api/integrations/autopulse",
                              {"updates": {"url": "autopulse:2875"}})
        self.assertEqual(status, 400)
        self.assertIn("http://", d["error"])
        live, _ = self.get("/api/integrations")
        self.assertEqual(live["autopulse"]["url"], "http://autopulse:2875")

    def test_enabling_autopulse_without_a_url_is_refused(self):
        _, status = self.post("/api/integrations/autopulse",
                              {"updates": {"enabled": True}})
        self.assertEqual(status, 400)

    def test_autopulse_password_is_marked_secret(self):
        d, _ = self.get("/api/integrations")
        fields = {f["key"]: f
                  for b in d["autopulse"]["schema"] for f in b["fields"]}
        self.assertTrue(fields["password"]["secret"])
        # api_key is read by nothing, so the panel shows it as untouchable
        # rather than offering a setting that does nothing.
        self.assertTrue(fields["api_key"]["readonly"])

    # --- test button ------------------------------------------------------

    def test_testing_a_profile_without_credentials_explains_why(self):
        self.add()
        d, status = self.post("/api/integrations/test",
                              {"provider": "sonarr", "id": "tv-sonarr"})
        self.assertEqual(status, 400)
        self.assertIn("url and an api_key", d["error"])

    def test_testing_an_unreachable_arr_reports_the_failure(self):
        self.add()
        # Port 1 on localhost refuses immediately, so this never waits on a
        # real timeout.
        self.post("/api/integrations/update", {
            "provider": "sonarr", "id": "tv-sonarr",
            "updates": {"url": "http://127.0.0.1:1", "api_key": "abc",
                        "request_timeout": 1.0}})
        d, status = self.post("/api/integrations/test",
                              {"provider": "sonarr", "id": "tv-sonarr"})
        self.assertEqual(status, 502)
        self.assertIn("TV Sonarr", d["error"])

    def test_testing_autopulse_needs_a_url(self):
        _, status = self.post("/api/integrations/test",
                              {"provider": "autopulse"})
        self.assertEqual(status, 400)

        self.post("/api/integrations/autopulse",
                  {"updates": {"url": "http://autopulse:2875"}})
        d, status = self.post("/api/integrations/test",
                              {"provider": "autopulse"})
        self.assertEqual(status, 200)
        self.assertIn("/triggers/manual", d["message"])

    # --- the webhook still works ------------------------------------------

    def test_a_profile_made_in_the_panel_receives_its_own_webhook(self):
        self.add()
        self.post("/api/integrations/update", {
            "provider": "sonarr", "id": "tv-sonarr",
            "updates": {"mode": "cleanup"}})
        d, status = self.post("/api/webhook/sonarr/tv-sonarr", {
            "eventType": "Test", "series": {"id": 1},
            "episodeFile": {"id": 2, "path": "/media/TV/x.mkv"}})
        self.assertEqual(status, 200)
        self.assertTrue(d["ignored"])
        self.assertEqual(d["integration"], "tv-sonarr")

    # --- the imports list -------------------------------------------------

    def job(self, path, **changes):
        """One durable import, as the webhook would have written it."""
        row = self.db.create_job(
            path, path, source_provider="sonarr",
            source_metadata={"integration": "tv-sonarr",
                             "event_type": "download"},
            mode="cleanup", stage="processing", status="pending")
        if changes:
            row = self.db.update_job(row["id"], **changes)
        return row

    def test_imports_list_is_empty_on_a_fresh_install(self):
        d, status = self.get("/api/workflow")
        self.assertEqual(status, 200)
        self.assertEqual(d["jobs"], [])
        self.assertEqual(d["failed"], 0)

    def test_an_import_is_listed_with_its_stage_and_origin(self):
        self.job("/media/TV/x.mkv")
        d, _ = self.get("/api/workflow")
        self.assertEqual(len(d["jobs"]), 1)
        job = d["jobs"][0]
        self.assertEqual(job["name"], "x.mkv")
        self.assertEqual(job["provider"], "sonarr")
        self.assertEqual(job["integration"], "tv-sonarr")
        self.assertEqual(job["mode"], "cleanup")
        self.assertEqual(job["stage"], "processing")

    def test_the_outbox_stage_a_job_is_stuck_in_is_shown_against_it(self):
        row = self.job("/media/TV/x.mkv", stage="arr_reconcile",
                       status="failed", error="sonarr refused")
        self.db.enqueue_outbox(row["dedupe_key"], "arr_reconcile",
                               job_id=row["id"], payload={})
        self.db.fail_outbox(
            self.db.get_outbox(dedupe_key=row["dedupe_key"])["id"],
            "sonarr refused")

        d, _ = self.get("/api/workflow")
        job = d["jobs"][0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "sonarr refused")
        self.assertEqual([a["action"] for a in job["actions"]],
                         ["arr_reconcile"])
        self.assertEqual(d["failed"], 1)

    def test_the_list_can_be_filtered_by_status(self):
        self.job("/media/TV/ok.mkv", status="done")
        self.job("/media/TV/bad.mkv", status="failed")
        d, _ = self.get("/api/workflow?status=failed")
        self.assertEqual([j["name"] for j in d["jobs"]], ["bad.mkv"])

    def test_a_failed_import_can_be_retried_from_the_panel(self):
        row = self.job("/media/TV/x.mkv", status="failed", error="boom")
        d, status = self.post("/api/workflow/retry", {"job_id": row["id"]})
        self.assertEqual(status, 200)
        self.assertIn("1", d["message"])
        self.assertEqual(self.db.get_job(row["id"])["status"], "pending")

    def test_retrying_everything_leaves_finished_imports_alone(self):
        done = self.job("/media/TV/ok.mkv", status="done")
        bad = self.job("/media/TV/bad.mkv", status="failed", error="boom")
        self.post("/api/workflow/retry")
        self.assertEqual(self.db.get_job(done["id"])["status"], "done")
        self.assertEqual(self.db.get_job(bad["id"])["status"], "pending")

    def test_a_bad_job_id_is_refused(self):
        d, status = self.post("/api/workflow/retry", {"job_id": "soon"})
        self.assertEqual(status, 400)
        self.assertIn("whole number", d["error"])


if __name__ == "__main__":
    unittest.main()
