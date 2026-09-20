"""Contract tests for the dependency-free outbound integrations."""

from __future__ import annotations

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app.integrations import (
    AutoPulseClient,
    CommandFailedError,
    IntegrationHTTPError,
    PathMapping,
    SonarrClient,
)


class _FakeArr(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[tuple[str, str, object, dict[str, str]]] = []
    command_reads = 0
    statuses = {"1": ["queued", "completed"], "2": ["queued", "completed"]}
    rename_records = [{
        "id": 99,
        "existingPath": "/arr/tv/Show/episode.mkv",
        "newPath": "/arr/tv/Show/01 - episode.mkv",
    }]

    def log_message(self, *_args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length)) if length else None

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        _FakeArr.requests.append(("GET", parsed.path, query, dict(self.headers)))
        if parsed.path == "/api/v3/rename":
            self._json(200, _FakeArr.rename_records)
            return
        if parsed.path.startswith("/api/v3/command/"):
            command_id = parsed.path.rsplit("/", 1)[1]
            values = _FakeArr.statuses[command_id]
            index = min(_FakeArr.command_reads, len(values) - 1)
            _FakeArr.command_reads += 1
            self._json(200, {"id": int(command_id), "status": values[index]})
            return
        self._json(404, {"error": "missing"})

    def do_POST(self):
        parsed = urlparse(self.path)
        payload = self._body()
        _FakeArr.requests.append(("POST", parsed.path, payload, dict(self.headers)))
        if parsed.path == "/api/v3/command":
            command_id = 1 if payload.get("name") == "RescanSeries" else 2
            self._json(201, {"id": command_id, "status": "queued"})
            return
        self._json(404, {"error": "missing"})


class _FakePulse(BaseHTTPRequestHandler):
    payload = None
    auth = None

    def log_message(self, *_args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        values = parse_qs(parsed.query)
        _FakePulse.payload = {key: value[0] for key, value in values.items()}
        _FakePulse.auth = self.headers.get("Authorization")
        body = b'{"accepted": true}'
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _FakeArr.requests = []
        _FakeArr.command_reads = 0
        cls.arr = ThreadingHTTPServer(("127.0.0.1", 0), _FakeArr)
        cls.arr_thread = threading.Thread(target=cls.arr.serve_forever, daemon=True)
        cls.arr_thread.start()
        cls.pulse = ThreadingHTTPServer(("127.0.0.1", 0), _FakePulse)
        cls.pulse_thread = threading.Thread(target=cls.pulse.serve_forever, daemon=True)
        cls.pulse_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.arr.shutdown()
        cls.pulse.shutdown()
        cls.arr.server_close()
        cls.pulse.server_close()

    def setUp(self):
        _FakeArr.requests.clear()
        _FakeArr.command_reads = 0

    def test_reconcile_rescans_then_renames_only_selected_file(self):
        client = SonarrClient(
            f"http://127.0.0.1:{self.arr.server_port}",
            "arr-key",
            path_mappings=[PathMapping("/arr", "D:/media")],
            poll_interval=0,
            sleep=lambda _seconds: None,
        )
        result = client.reconcile(42, original_path="D:/media/tv/Show/episode.mkv")
        self.assertEqual(result.source_file_id, 99)
        self.assertEqual(result.final_path, "D:/media/tv/Show/01 - episode.mkv")
        posts = [item for item in _FakeArr.requests if item[0] == "POST"]
        self.assertEqual(posts[0][2], {"name": "RescanSeries", "seriesId": 42})
        self.assertEqual(posts[1][2], {"name": "RenameFiles", "seriesId": 42, "files": [99]})
        self.assertEqual(posts[0][3]["X-Api-Key"], "arr-key")

    def test_manual_trigger_uses_basic_auth_and_optional_hash(self):
        client = AutoPulseClient(
            f"http://127.0.0.1:{self.pulse.server_port}",
            username="user",
            password="pass",
        )
        self.assertEqual(client.trigger("D:/media/file.mkv", "abc123"), {"accepted": True})
        self.assertEqual(_FakePulse.payload, {"path": "D:/media/file.mkv", "hash": "abc123"})
        expected = "Basic " + base64.b64encode(b"user:pass").decode()
        self.assertEqual(_FakePulse.auth, expected)

    def test_http_errors_are_explicit(self):
        client = SonarrClient(
            f"http://127.0.0.1:{self.arr.server_port}",
            "key",
            opener=lambda request, timeout: _error_response(503, b"temporarily down"),
        )
        with self.assertRaises(IntegrationHTTPError) as raised:
            client.start_rescan(1)
        self.assertEqual(raised.exception.status, 503)

    def test_failed_command_is_reported(self):
        statuses = _FakeArr.statuses
        _FakeArr.statuses = {"1": ["failed"]}
        try:
            client = SonarrClient(
                f"http://127.0.0.1:{self.arr.server_port}",
                "key",
                poll_interval=0,
                sleep=lambda _seconds: None,
            )
            with self.assertRaises(CommandFailedError):
                client.start_rescan(1)
        finally:
            _FakeArr.statuses = statuses

    def test_changed_container_falls_back_from_old_file_id_to_new_path(self):
        client = SonarrClient("http://unused", "key")
        record = {
            "episodeFileId": 88,
            "existingPath": "Season 01/episode.mkv",
            "newPath": "Season 01/episode [HEVC].mkv",
        }
        chosen = client._choose_preview(
            [record], source_file_id=7,
            existing_path="/media/Show/Season 01/episode.mkv")
        self.assertIs(chosen, record)
        self.assertEqual(client._record_file_id(chosen, requested=7), 88)

    def test_episode_file_id_and_relative_preview_path(self):
        old_records = _FakeArr.rename_records
        _FakeArr.rename_records = [{
            "episodeFileId": 123,
            "existingPath": "episode.mkv",
            "newPath": "renamed.mkv",
        }]
        try:
            client = SonarrClient(
                f"http://127.0.0.1:{self.arr.server_port}",
                "arr-key",
                path_mappings=[PathMapping("/arr", "D:/media")],
                poll_interval=0,
                sleep=lambda _seconds: None,
            )
            result = client.rename_affected(
                42,
                source_file_id=123,
                existing_path="D:/media/tv/Show/episode.mkv",
            )
            self.assertEqual(result.final_path, "D:/media/tv/Show/renamed.mkv")
            post = [item for item in _FakeArr.requests if item[0] == "POST"][-1]
            self.assertEqual(post[2], {"name": "RenameFiles", "seriesId": 42, "files": [123]})
        finally:
            _FakeArr.rename_records = old_records

    def test_empty_preview_is_a_successful_noop(self):
        old_records = _FakeArr.rename_records
        _FakeArr.rename_records = []
        try:
            client = SonarrClient(f"http://127.0.0.1:{self.arr.server_port}", "arr-key")
            result = client.rename_affected(42, source_file_id=99, final_path="D:/media/tv/Show/episode.mkv")
            self.assertEqual(result.final_path, "D:/media/tv/Show/episode.mkv")
            self.assertEqual([item for item in _FakeArr.requests if item[0] == "POST"], [])
        finally:
            _FakeArr.rename_records = old_records


def _error_response(status, body):
    class Response:
        def __init__(self):
            self.status = status

        def read(self):
            return body

        def close(self):
            pass

    return Response()


if __name__ == "__main__":
    unittest.main()
