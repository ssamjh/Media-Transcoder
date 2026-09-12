"""Web panel: JSON API plus the static single-page UI.

stdlib http.server on purpose - the API is a dozen endpoints with no auth and
no templating, and staying dependency-free keeps the image small and the
build reproducible.

The panel is trusted-network only: there is no authentication, and the API
can start encodes and rewrite the config file. Do not expose it to the
internet.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import config as config_mod
from .config import Config, ConfigError
from .db import loads as json_loads
from .engine import MAX_ATTEMPTS, Engine
from .probe import ProbeError

log = logging.getLogger("transcoder.web")

STATIC = Path(__file__).parent / "static"
MAX_BODY = 1 << 20  # 1 MB is far more than any request here needs


class ApiError(Exception):
    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _norm(raw: Any) -> str:
    """Normalise an incoming path to the form the database stores.

    Paths are keys here, and a client may send either separator. str(Path(...))
    matches what the scanner wrote, which on Windows means backslashes.
    """
    if not raw:
        raise ApiError("path is required")
    return str(Path(str(raw)))


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["reasons"] = json_loads(d.get("reasons"), [])
    if "plan" in d:
        d["plan"] = json_loads(d.get("plan"), None)
    if "detail" in d:
        d["detail"] = json_loads(d.get("detail"), None)
    return d


class Handler(BaseHTTPRequestHandler):
    engine: Engine
    server_version = "transcoder"

    def log_message(self, fmt: str, *args) -> None:
        log.debug(fmt, *args)

    # --- plumbing ---------------------------------------------------------

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(json.dumps(payload).encode(), "application/json", code)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError("bad Content-Length") from None
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError("request body too large", 413)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ApiError(f"invalid JSON: {exc}") from None
        if not isinstance(data, dict):
            raise ApiError("body must be a JSON object")
        return data

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _authorise(self) -> None:
        """Check the API key on /api/ routes, if one is configured.

        The key exists so Sonarr and Radarr have a credential to present. It
        is not a security boundary for the panel itself: index.html is served
        with the key embedded so the UI keeps working, which means anyone who
        can load the panel can read it. Keep the panel off the internet.
        """
        want = (self.engine.cfg.web.api_key or "").strip()
        if not want:
            return
        got = (self.headers.get("X-Api-Key")
               or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
               or self._query().get("apikey", ""))
        if not secrets.compare_digest(str(got), want):
            raise ApiError("invalid or missing API key", 401)

    def _int(self, q: dict[str, str], key: str, default: int,
             lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(q.get(key, default))))
        except (TypeError, ValueError):
            return default

    # --- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if route == "/":
                return self._static("index.html", "text/html; charset=utf-8")
            if route == "/app.css":
                return self._static("app.css", "text/css; charset=utf-8")
            if route == "/app.js":
                return self._static("app.js", "text/javascript; charset=utf-8")

            handler = {
                "/api/status": self.api_status,
                "/api/files": self.api_files,
                "/api/file": self.api_file,
                "/api/history": self.api_history,
                "/api/config": self.api_config_get,
                "/api/libraries": self.api_libraries,
                "/api/modes": self.api_modes,
            }.get(route)
            if handler is None:
                raise ApiError("not found", 404)
            self._authorise()
            self._json(handler())
        except ApiError as exc:
            self._json({"error": exc.message}, exc.code)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("GET %s failed", route)
            self._json({"error": f"internal error: {exc}"}, 500)

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        route = urlparse(self.path).path.rstrip("/") or "/"
        try:
            handler = {
                "/api/scan": self.api_scan,
                "/api/check": self.api_check,
                "/api/process": self.api_process,
                "/api/cancel": self.api_cancel,
                "/api/cancel-all": self.api_cancel_all,
                "/api/queue-pending": self.api_queue_pending,
                "/api/retry": self.api_retry,
                "/api/schedule": self.api_schedule,
                "/api/config": self.api_config_post,
                "/api/history/clear": self.api_history_clear,
                "/api/libraries/add": self.api_library_add,
                "/api/libraries/update": self.api_library_update,
                "/api/libraries/delete": self.api_library_delete,
                "/api/modes/add": self.api_mode_add,
                "/api/modes/update": self.api_mode_update,
                "/api/modes/delete": self.api_mode_delete,
            }.get(route)
            if handler is None:
                raise ApiError("not found", 404)
            self._authorise()
            self._json(handler(self._body()))
        except ApiError as exc:
            self._json({"error": exc.message}, exc.code)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("POST %s failed", route)
            self._json({"error": f"internal error: {exc}"}, 500)

    def _static(self, name: str, ctype: str) -> None:
        f = STATIC / name
        if not f.exists():
            raise ApiError("not found", 404)
        if name == "index.html":
            page = f.read_text(encoding="utf-8").replace(
                "__API_KEY__", self.engine.cfg.web.api_key or "")
            return self._send(page.encode(), ctype)
        self._send(f.read_bytes(), ctype)

    # --- endpoints --------------------------------------------------------

    def api_status(self) -> dict[str, Any]:
        eng = self.engine
        now = time.time()
        stats = eng.db.stats()

        active = []
        for j in eng.active:
            elapsed = now - j.started
            eta = (elapsed / j.percent * (100 - j.percent)) if j.percent > 1 else 0
            active.append({
                "path": j.path,
                "name": Path(j.path).name,
                "percent": round(j.percent, 2),
                "speed": round(j.speed, 2),
                "elapsed": elapsed,
                "eta": eta,
                "in_size": j.in_size,
                "library": j.library,
                "reasons": j.reasons,
            })

        queued = eng.queued_paths()
        recent, _ = eng.db.history(limit=10)

        return {
            "scanning": eng.scanning,
            "scan_progress": eng.scan_progress,
            "last_scan": eng.last_scan,
            "last_scan_summary": eng.last_scan_summary,
            "next_scan": eng.next_scan,
            "schedule_enabled": eng.cfg.schedule.enabled,
            "scan_interval_hours": eng.cfg.schedule.scan_interval_hours,
            "dry_run": eng.cfg.dry_run,
            "workers": eng.cfg.workers.count,
            "uptime": now - eng.started_at,
            "libraries": [
                {"id": l.id, "name": l.name, "enabled": l.enabled}
                for l in eng.cfg.libraries
            ],
            "queue_depth": len(queued),
            "queued": [{"path": p, "name": Path(p).name} for p in queued[:20]],
            "active": active,
            "recent": [_row(r) for r in recent],
            "now": now,
            **stats,
        }

    def api_files(self) -> dict[str, Any]:
        q = self._query()
        limit = self._int(q, "limit", 50, 1, 500)
        offset = self._int(q, "offset", 0, 0, 10_000_000)
        rows, total = self.engine.db.list_files(
            status=q.get("status") or None,
            query=(q.get("q") or "").strip() or None,
            limit=limit, offset=offset, order=q.get("order", "size"),
            library=q.get("library") or None,
        )
        running = {j.path for j in self.engine.active}
        out = []
        for r in rows:
            d = _row(r)
            d["is_running"] = r["path"] in running
            out.append(d)
        return {"files": out, "total": total, "limit": limit, "offset": offset}

    def api_file(self) -> dict[str, Any]:
        path = _norm(self._query().get("path"))
        row = self.engine.db.get(path)
        if row is None:
            raise ApiError("file not tracked", 404)
        history, _ = self.engine.db.history(limit=25, path=path)
        return {
            "file": _row(row),
            "history": [_row(h) for h in history],
            "exists": Path(path).exists(),
            "max_attempts": MAX_ATTEMPTS,
        }

    def api_history(self) -> dict[str, Any]:
        q = self._query()
        limit = self._int(q, "limit", 50, 1, 500)
        offset = self._int(q, "offset", 0, 0, 10_000_000)
        rows, total = self.engine.db.history(limit=limit, offset=offset)
        return {"history": [_row(r) for r in rows], "total": total,
                "limit": limit, "offset": offset}

    def api_config_get(self) -> dict[str, Any]:
        return {
            "schema": config_mod.schema(self.engine.cfg),
            "path": self.engine.config_path,
            "toml": config_mod.dump_toml(self.engine.cfg),
        }

    # --- actions ----------------------------------------------------------

    def api_scan(self, body: dict[str, Any]) -> dict[str, Any]:
        paths = body.get("paths") or None
        if paths is not None and not isinstance(paths, list):
            raise ApiError("paths must be a list")
        lib_id = body.get("library")
        if lib_id and self.engine.cfg.library(str(lib_id)) is None:
            raise ApiError(f"no such library: {lib_id}", 404)
        ok = self.engine.request_scan(
            [str(p) for p in paths] if paths else None,
            library=str(lib_id) if lib_id else None,
        )
        return {"ok": ok,
                "message": "scan requested" if ok else "a scan is already running"}

    def api_check(self, body: dict[str, Any]) -> dict[str, Any]:
        path = _norm(body.get("path"))
        mode = self._mode(body)
        if not Path(path).exists():
            raise ApiError("file does not exist", 404)
        try:
            plan = self.engine.check_one(path, mode=mode)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        except LookupError as exc:
            raise ApiError(str(exc), 409) from None
        except ProbeError as exc:
            raise ApiError(f"probe failed: {exc}") from None
        return {"ok": True, "plan": plan.to_dict(), "mode": mode or None}

    def _mode(self, body: dict[str, Any]) -> str:
        """Validate the requested mode, defaulting to the library profile."""
        raw = body.get("mode")
        if raw in (None, ""):
            return ""
        mode = str(raw).strip()
        if self.engine.cfg.mode(mode) is None:
            known = ", ".join(m.id for m in self.engine.cfg.modes) or "none"
            raise ApiError(f"no such mode: {mode} (known modes: {known})")
        return mode

    def api_process(self, body: dict[str, Any]) -> dict[str, Any]:
        """Queue one file. This is the endpoint Sonarr and Radarr call."""
        path = _norm(body.get("path"))
        mode = self._mode(body)
        if not path:
            raise ApiError("path is required")
        try:
            ok, message = self.engine.enqueue(
                path, force=bool(body.get("force")), mode=mode)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        return {"ok": ok, "message": message, "path": path, "mode": mode or None}

    def api_cancel(self, body: dict[str, Any]) -> dict[str, Any]:
        path = _norm(body.get("path"))
        ok, message = self.engine.cancel(path)
        return {"ok": ok, "message": message}

    def api_cancel_all(self, body: dict[str, Any]) -> dict[str, Any]:
        n = self.engine.cancel_all()
        return {"ok": True, "message": f"cancelled {n}"}

    def api_queue_pending(self, body: dict[str, Any]) -> dict[str, Any]:
        n = self.engine.enqueue_pending()
        return {"ok": True, "message": f"queued {n} file(s)"}

    def api_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        raw = body.get("path")
        n = self.engine.db.reset_attempts(_norm(raw) if raw else None)
        self.engine.enqueue_pending()
        return {"ok": True, "message": f"reset {n} file(s)"}

    def api_schedule(self, body: dict[str, Any]) -> dict[str, Any]:
        if "enabled" not in body:
            raise ApiError("enabled is required")
        enabled = bool(body["enabled"])
        self.engine.set_schedule_enabled(enabled)
        self._persist()
        return {"ok": True, "enabled": enabled}

    def api_config_post(self, body: dict[str, Any]) -> dict[str, Any]:
        updates = body.get("updates")
        if not isinstance(updates, dict):
            raise ApiError("updates must be an object of dotted keys")
        try:
            changed = config_mod.apply_updates(self.engine.cfg, updates)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None

        self._persist()
        restart = sorted(
            k for k in changed if config_mod.META.get(k, {}).get("restart")
        )
        if "schedule.enabled" in changed or "schedule.scan_interval_hours" in changed:
            self.engine.set_schedule_enabled(self.engine.cfg.schedule.enabled)
        if changed:
            log.info("config updated: %s", ", ".join(changed))
        return {
            "ok": True,
            "changed": changed,
            "needs_restart": restart,
            "schema": config_mod.schema(self.engine.cfg),
            "toml": config_mod.dump_toml(self.engine.cfg),
        }

    def _library(self, lib_id: Any) -> Any:
        lib = self.engine.cfg.library(str(lib_id or ""))
        if lib is None:
            raise ApiError(f"no such library: {lib_id}", 404)
        return lib

    def api_libraries(self) -> dict[str, Any]:
        stats = self.engine.db.library_stats()
        return {
            "libraries": [
                {
                    "id": l.id,
                    "name": l.name,
                    "enabled": l.enabled,
                    "paths": l.paths,
                    "stages": {
                        "video": l.video.enabled,
                        "audio": l.audio.enabled,
                        "subtitles": l.subtitles.enabled,
                        "replace": l.output.replace_original,
                    },
                    "schema": config_mod.library_schema(l),
                    "stats": stats.get(l.id, {"total": 0, "bytes": 0,
                                              "counts": {}, "saved": 0}),
                }
                for l in self.engine.cfg.libraries
            ],
        }

    def api_library_add(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        raw = body.get("paths")
        if isinstance(raw, str):
            raw = [p.strip() for p in raw.splitlines() if p.strip()]
        if not isinstance(raw, list):
            raise ApiError("paths must be a list")
        try:
            lib = config_mod.add_library(self.engine.cfg, name,
                                         [str(p) for p in raw])
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        log.info("library added: %s (%s)", lib.name, lib.id)
        return {"ok": True, "id": lib.id, **self.api_libraries()}

    def api_library_update(self, body: dict[str, Any]) -> dict[str, Any]:
        lib = self._library(body.get("id"))
        updates = body.get("updates")
        if not isinstance(updates, dict):
            raise ApiError("updates must be an object of dotted keys")
        try:
            changed = config_mod.apply_library_updates(
                self.engine.cfg, lib, updates)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        if changed:
            log.info("library %s updated: %s", lib.id, ", ".join(changed))
        return {"ok": True, "changed": changed, **self.api_libraries()}

    def api_library_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        lib = self._library(body.get("id"))
        try:
            config_mod.remove_library(self.engine.cfg, lib.id)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        removed = self.engine.db.forget_library(lib.id)
        log.info("library removed: %s (%d tracked file(s) forgotten)",
                 lib.name, removed)
        return {"ok": True, "message": f"removed {lib.name}",
                **self.api_libraries()}

    # --- modes ------------------------------------------------------------

    def api_modes(self) -> dict[str, Any]:
        cfg = self.engine.cfg
        return {
            "modes": [
                {
                    "id": m.id,
                    "name": m.name,
                    "description": m.description,
                    "overrides": dict(m.overrides),
                    "schema": config_mod.mode_schema(m),
                }
                for m in cfg.modes
            ],
            "library_keys": sorted(
                f["key"] for block in config_mod.library_schema(cfg.libraries[0])
                for f in block["fields"]
                if f["key"] not in config_mod.MODE_FORBIDDEN
            ),
        }

    def _mode_cfg(self, mode_id: Any) -> Any:
        mode = self.engine.cfg.mode(str(mode_id or "").strip())
        if mode is None:
            raise ApiError(f"no such mode: {mode_id}", 404)
        return mode

    def api_mode_add(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        overrides = body.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise ApiError("overrides must be an object of dotted keys")
        try:
            mode = config_mod.add_mode(self.engine.cfg, name, overrides)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        return {"ok": True, "id": mode.id, "message": f"added {mode.name}"}

    def api_mode_update(self, body: dict[str, Any]) -> dict[str, Any]:
        mode = self._mode_cfg(body.get("id"))
        updates = body.get("updates")
        if not isinstance(updates, dict):
            raise ApiError("updates must be an object")
        before = (mode.name, mode.description, dict(mode.overrides))
        try:
            changed = config_mod.apply_mode_updates(self.engine.cfg, mode, updates)
        except ConfigError as exc:
            mode.name, mode.description, mode.overrides = before
            raise ApiError(str(exc)) from None
        self._persist()
        return {"ok": True, "changed": changed,
                "message": f"updated {len(changed)} setting(s)" if changed
                           else "no changes"}

    def api_mode_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            mode = config_mod.remove_mode(self.engine.cfg, str(body.get("id") or ""))
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        return {"ok": True, "message": f"removed {mode.name}"}

    def api_history_clear(self, body: dict[str, Any]) -> dict[str, Any]:
        n = self.engine.db.clear_history()
        return {"ok": True, "message": f"cleared {n} record(s)"}

    def _persist(self) -> None:
        if not self.engine.config_path:
            return
        try:
            config_mod.save(self.engine.cfg, self.engine.config_path)
        except OSError as exc:
            raise ApiError(f"could not write config file: {exc}", 500) from None


def serve(cfg: Config, engine: Engine) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    httpd = ThreadingHTTPServer((cfg.web.host, cfg.web.port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name="web").start()
    log.info("web panel on http://%s:%d", cfg.web.host, cfg.web.port)
    return httpd
