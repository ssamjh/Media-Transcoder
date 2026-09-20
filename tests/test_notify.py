"""Webhook delivery: queueing, retries, token expansion, mode overrides.

A real http.server on an ephemeral port stands in for Jellyfin. No media
files and no ffmpeg are involved.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import notify
from app.config import (Config, ConfigError, ModeCfg, add_library,
                        add_mode, apply_mode_updates, loads, dump_toml,
                        resolve)


class _Recorder(BaseHTTPRequestHandler):
    """Records every request; replies with whatever the test queued up."""

    received: list[dict]
    codes: list[int]
    lock = threading.Lock()

    def log_message(self, *a) -> None:
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with self.lock:
            self.received.append({
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(raw) if raw else None,
            })
            code = self.codes.pop(0) if self.codes else 200
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PUT = _handle


class WebhookServerCase(unittest.TestCase):
    def setUp(self) -> None:
        received: list[dict] = []
        codes: list[int] = []
        handler = type("Bound", (_Recorder,),
                       {"received": received, "codes": codes})
        self.received = received
        self.codes = codes
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.notifier = notify.Notifier()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.notifier.stop)

    def deliver(self, hooks, payload, timeout: float = 10.0) -> None:
        self.notifier.dispatch(hooks, payload)
        self.notifier.join(timeout=timeout)


class TestDelivery(WebhookServerCase):
    def test_post_carries_the_file_details_as_json(self):
        hook = notify.Webhook(url=self.base + "/hook")
        payload = notify.payload_for("/media/Show/ep.mkv", library="tv",
                                     mode="cleanup", status="done",
                                     in_size=100, out_size=40)
        self.deliver([hook], payload)

        self.assertEqual(len(self.received), 1)
        got = self.received[0]
        self.assertEqual(got["method"], "POST")
        self.assertEqual(got["body"]["name"], "ep.mkv")
        self.assertEqual(got["body"]["library"], "tv")
        self.assertEqual(got["body"]["mode"], "cleanup")
        self.assertEqual(got["body"]["saved"], 60)
        self.assertEqual(self.notifier.sent, 1)

    def test_get_sends_no_body_and_expands_tokens_in_the_url(self):
        hook = notify.Webhook(url=self.base + "/refresh?p={path}&lib={library}",
                              method="GET")
        payload = notify.payload_for("/media/Show/ep one.mkv", library="tv",
                                     mode="", status="done")
        self.deliver([hook], payload)

        got = self.received[0]
        self.assertEqual(got["method"], "GET")
        self.assertIsNone(got["body"])
        self.assertIn("lib=tv", got["path"])
        # Spaces and separators are quoted, so the query survives intact.
        self.assertIn("ep%20one.mkv", got["path"])
        self.assertNotIn(" ", got["path"])

    def test_headers_are_sent(self):
        hook = notify.Webhook(url=self.base + "/hook",
                              headers=notify.parse_headers(
                                  ["X-Api-Key: abc123", "X-Lib: {library}"]))
        self.deliver([hook], notify.payload_for("/media/a.mkv", library="tv",
                                                mode="", status="done"))
        self.assertEqual(self.received[0]["headers"]["X-Api-Key"], "abc123")
        self.assertEqual(self.received[0]["headers"]["X-Lib"], "tv")

    def test_every_url_is_called(self):
        hooks = [notify.Webhook(url=self.base + "/one"),
                 notify.Webhook(url=self.base + "/two")]
        self.deliver(hooks, notify.payload_for("/media/a.mkv", library="tv",
                                               mode="", status="done"))
        self.assertEqual({r["path"] for r in self.received}, {"/one", "/two"})

    def test_a_burst_is_queued_and_all_of_it_is_delivered(self):
        hook = notify.Webhook(url=self.base + "/hook")
        for i in range(25):
            self.notifier.dispatch(
                [hook], notify.payload_for(f"/media/{i}.mkv", library="tv",
                                           mode="", status="done"))
        self.notifier.join(timeout=30)
        self.assertEqual(len(self.received), 25)
        self.assertEqual(self.notifier.sent, 25)

    def test_a_4xx_is_not_retried(self):
        self.codes.extend([404, 404, 404])
        hook = notify.Webhook(url=self.base + "/hook", retries=3)
        self.deliver([hook], notify.payload_for("/media/a.mkv", library="tv",
                                                mode="", status="done"))
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.notifier.failed, 1)

    def test_a_5xx_is_retried_then_succeeds(self):
        self.codes.append(503)
        hook = notify.Webhook(url=self.base + "/hook", retries=3)
        self.deliver([hook], notify.payload_for("/media/a.mkv", library="tv",
                                                mode="", status="done"),
                     timeout=30)
        self.assertEqual(len(self.received), 2)
        self.assertEqual(self.notifier.sent, 1)

    def test_an_unreachable_host_never_raises(self):
        # Nothing is listening on the shut-down port; delivery must simply
        # give up, because the file on disk is already correct.
        hook = notify.Webhook(url="http://127.0.0.1:1/hook", retries=1,
                              timeout=1.0)
        self.deliver([hook], notify.payload_for("/media/a.mkv", library="tv",
                                                mode="", status="done"))
        self.assertEqual(self.notifier.failed, 1)


class TestExpansion(unittest.TestCase):
    def test_unknown_braces_are_left_alone(self):
        payload = notify.payload_for("/media/a.mkv", library="tv", mode="",
                                     status="done")
        self.assertEqual(notify.expand("http://x/{weird}?n={name}", payload),
                         "http://x/{weird}?n=a.mkv")

    def test_headers_without_a_colon_are_ignored(self):
        self.assertEqual(notify.parse_headers(["nonsense", "A: b"]), {"A": "b"})


class TestConfigSurface(unittest.TestCase):
    """Hooks belong to a mode, like everything else that happens to a file."""

    def setUp(self):
        self.cfg = Config()
        self.lib = add_library(self.cfg, "Media", ["/media"])
        self.mode = self.cfg.mode(self.lib.mode)

    def test_notifications_are_off_by_default(self):
        self.assertEqual(notify.hooks_for(ModeCfg().notify), [])

    def test_jellyfin_hook_uses_direct_api_schema(self):
        target = self.cfg.integrations.autopulse
        target.enabled = True
        target.url = "http://jellyfin:8096"
        target.api_key = "key"
        hooks = notify.jellyfin_hooks(self.cfg)
        self.assertEqual(hooks[0].url,
                         "http://jellyfin:8096/Library/Media/Updated")
        self.assertEqual(hooks[0].headers["Authorization"],
                         'MediaBrowser Token="key"')
        self.assertEqual(notify.jellyfin_payload("/media/a.mkv"), {
            "Updates": [{"Path": "/media/a.mkv", "UpdateType": "Modified"}]})

    def test_a_mode_carries_its_own_hooks(self):
        apply_mode_updates(self.cfg, self.mode, {
            "notify.enabled": True,
            "notify.urls": ["http://jellyfin:8096/Library/Refresh"],
            "notify.headers": ["X-Api-Key: k"],
        })
        hooks = notify.hooks_for(resolve(self.cfg, self.lib).notify)
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0].headers, {"X-Api-Key": "k"})

    def test_a_url_without_a_scheme_is_rejected(self):
        with self.assertRaises(ConfigError):
            apply_mode_updates(self.cfg, self.mode, {
                "notify.enabled": True, "notify.urls": ["jellyfin:8096/x"]})

    def test_a_malformed_header_is_rejected(self):
        with self.assertRaises(ConfigError):
            apply_mode_updates(self.cfg, self.mode,
                               {"notify.headers": ["no colon here"]})

    def test_settings_survive_a_toml_round_trip(self):
        apply_mode_updates(self.cfg, self.mode, {
            "notify.enabled": True,
            "notify.urls": ["http://jellyfin:8096/Library/Media/Updated"],
            "notify.headers": ["X-Api-Key: k"],
            "notify.method": "PUT",
            "notify.retries": 5,
        })
        back = loads(dump_toml(self.cfg)).mode(self.mode.id).notify
        self.assertTrue(back.enabled)
        self.assertEqual(back.method, "PUT")
        self.assertEqual(back.retries, 5)
        self.assertEqual(back.headers, ["X-Api-Key: k"])


class TestModeOverrides(unittest.TestCase):
    def test_an_import_mode_can_add_its_own_callback(self):
        cfg = Config()
        lib = add_library(cfg, "Media", ["/media"])
        mode = add_mode(cfg, "Import", copy_from=lib.mode)
        apply_mode_updates(cfg, mode, {
            "video.enabled": False,
            "notify.enabled": True,
            "notify.urls": ["http://jellyfin:8096/Library/Media/Updated"],
        })
        profile = resolve(cfg, lib, mode.id)
        self.assertEqual(len(notify.hooks_for(profile.notify)), 1)
        # ...and the mode the library normally runs on is untouched, so a
        # scheduled scan of the same file calls nobody.
        self.assertEqual(notify.hooks_for(resolve(cfg, lib).notify), [])

    def test_a_bad_mode_url_is_caught_when_the_mode_is_saved(self):
        cfg = Config()
        mode = add_mode(cfg, "Import")
        with self.assertRaises(ConfigError):
            apply_mode_updates(cfg, mode, {
                "overrides": {"notify.enabled": True,
                              "notify.urls": ["not-a-url"]}})


if __name__ == "__main__":
    unittest.main()
