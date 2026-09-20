"""Web panel: JSON API plus the static single-page UI.

stdlib http.server on purpose - the API is a dozen dependency-free endpoints
with a small API-key check and no templating, keeping the image small and the
build reproducible.

The panel is trusted-network only: there is no authentication, and the API
can start encodes and rewrite the config file. Do not expose it to the
internet.
"""

from __future__ import annotations

import copy
import json
import logging
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import backup
from . import integrations
from . import config as config_mod
from . import notify as notify_mod
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


def _stages(mode: Any) -> dict[str, bool]:
    """The one-line summary of what a mode does, for a card in the panel."""
    return {
        "video": mode.video.enabled,
        "audio": mode.audio.enabled,
        "subtitles": mode.subtitles.enabled,
        "replace": mode.output.replace_original,
        "notify": mode.notify.enabled and bool(mode.notify.urls),
    }


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["reasons"] = json_loads(d.get("reasons"), [])
    if "plan" in d:
        d["plan"] = json_loads(d.get("plan"), None)
    if "detail" in d:
        d["detail"] = json_loads(d.get("detail"), None)
    return d


_ARR_IMPORT_EVENTS = frozenset({
    "download", "import", "upgrade", "manualimport", "manual_import",
})


def _as_id(value: Any) -> Any:
    """Keep Arr's numeric ids numeric while dropping empty placeholders."""
    if value in (None, ""):
        return None
    return value


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    """Return the first present value from a payload or one of its objects."""
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


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

    def _authorise(self, instance_secret: str = "") -> None:
        """Check the API key on /api/ routes, if one is configured.

        The key exists so Sonarr and Radarr have a credential to present. It
        is not a security boundary for the panel itself: index.html is served
        with the key embedded so the UI keeps working, which means anyone who
        can load the panel can read it. Keep the panel off the internet.
        """
        # The global key remains the normal credential.  A named Arr profile
        # may additionally carry a secret, which is handy when several Arr
        # instances share this process and no global key is desired.
        wanted = [v for v in (
            (self.engine.cfg.web.api_key or "").strip(),
            (instance_secret or "").strip(),
        ) if v]
        if not wanted:
            return
        got = (self.headers.get("X-Api-Key")
               or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
               or self._query().get("apikey", ""))
        if not any(secrets.compare_digest(str(got), want) for want in wanted):
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
            # Docker needs a credential-free liveness check. API routes may
            # require a key, so probing /api/status would mark a working
            # service unhealthy as soon as authentication is configured.
            if route == "/health":
                return self._json({"ok": True})
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
                "/api/backups": self.api_backups,
                "/api/integrations": self.api_integrations,
                "/api/workflow": self.api_workflow,
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
            native = self._native_route(route)
            if native is not None:
                # Native Arr requests need their body to select a named
                # profile (instanceName is part of the webhook payload).  A
                # request may use either the global key or that profile's
                # secret.
                body = self._body()
                provider, instance_id = native
                instance = self._arr_instance(
                    provider or self._payload_provider(body),
                    instance_id or self._payload_instance(body),
                )
                self._authorise(instance.secret if instance else "")
                self._json(self.api_webhook(body, provider, instance_id,
                                            instance=instance))
                return
            handler = {
                "/api/scan": self.api_scan,
                "/api/check": self.api_check,
                "/api/process": self.api_process,
                "/api/cancel": self.api_cancel,
                "/api/cancel-all": self.api_cancel_all,
                "/api/queue-pending": self.api_queue_pending,
                "/api/retry": self.api_retry,
                "/api/workflow/retry": self.api_workflow_retry,
                "/api/schedule": self.api_schedule,
                "/api/config": self.api_config_post,
                "/api/history/clear": self.api_history_clear,
                "/api/libraries/add": self.api_library_add,
                "/api/libraries/update": self.api_library_update,
                "/api/libraries/delete": self.api_library_delete,
                "/api/modes/add": self.api_mode_add,
                "/api/modes/update": self.api_mode_update,
                "/api/modes/delete": self.api_mode_delete,
                "/api/notify/test": self.api_notify_test,
                "/api/backups/run": self.api_backup_run,
                "/api/backups/restore": self.api_backup_restore,
                "/api/integrations/add": self.api_integration_add,
                "/api/integrations/update": self.api_integration_update,
                "/api/integrations/delete": self.api_integration_delete,
                "/api/integrations/autopulse": self.api_autopulse_update,
                "/api/integrations/test": self.api_integration_test,
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

    @staticmethod
    def _native_route(route: str) -> tuple[str | None, str | None] | None:
        """Recognise the canonical route and a couple of Arr-friendly aliases.

        ``/api/webhook/{provider}/{instance}`` is the canonical form.  The
        shorter ``/api/{provider}/webhook`` form is useful in Arr's UI, while
        ``/api/integrations/{provider}/webhook/{instance}`` leaves room for
        other integration families without changing this endpoint.
        """
        parts = [p for p in route.split("/") if p]
        if parts in (["api", "webhook"], ["api", "webhooks"]):
            return None, None
        if len(parts) >= 3 and parts[:2] in (["api", "webhook"],
                                             ["api", "webhooks"]):
            provider = parts[2].lower()
            if provider in ("sonarr", "radarr"):
                return provider, parts[3] if len(parts) > 3 else None
        if len(parts) >= 3 and parts[0:2] == ["api", "integrations"]:
            provider = parts[2].lower()
            if provider in ("sonarr", "radarr"):
                if len(parts) > 3 and parts[3].lower() == "webhook":
                    return provider, parts[4] if len(parts) > 4 else None
                # Also accept /api/integrations/sonarr[/<instance>] as a
                # compact form for clients that model the provider itself as
                # the integration resource.
                return provider, parts[3] if len(parts) > 3 else None
        if len(parts) == 3 and parts[0] == "api" and parts[2].lower() == "webhook":
            provider = parts[1].lower()
            if provider in ("sonarr", "radarr"):
                return provider, None
        return None

    def _static(self, name: str, ctype: str) -> None:
        f = STATIC / name
        if not f.exists():
            raise ApiError("not found", 404)
        if name == "index.html":
            # Substitute the quoted placeholder, not the bare token: the line
            # in index.html also names the variable __API_KEY__, and replacing
            # that too would leave the page setting window.<key> instead.
            page = f.read_text(encoding="utf-8").replace(
                '"__API_KEY__"', json.dumps(self.engine.cfg.web.api_key or ""))
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
            in_stage = now - (j.stage_started or j.started)
            encode_elapsed = now - (j.encode_started or j.started)
            copy_elapsed = now - (j.copy_started or now)
            encode_eta = ((encode_elapsed / j.encode_percent)
                          * (100 - j.encode_percent)
                          if j.encode_percent > 1 else 0)
            copy_eta = ((copy_elapsed / j.copy_percent)
                        * (100 - j.copy_percent)
                        if j.copy_percent > 1 else 0)
            # Keep the old stage-local fields for API clients that already
            # consume them, while exposing explicit encode/copy values for the
            # panel and newer integrations.
            eta = copy_eta if j.stage == "copying" else (
                encode_eta if j.stage == "encoding" else 0)
            active.append({
                "path": j.path,
                "name": Path(j.path).name,
                "stage": j.stage,
                "percent": round(j.percent, 2),
                "speed": round(j.speed, 2),
                "encode_percent": round(j.encode_percent, 2),
                "encode_speed": round(j.encode_speed, 2),
                "encode_eta": encode_eta,
                "copy_percent": round(j.copy_percent, 2),
                "copy_speed": round(j.copy_speed, 2),
                "copy_bytes": j.copy_bytes,
                "copy_total": j.copy_total,
                "copy_eta": copy_eta,
                "elapsed": elapsed,
                "stage_elapsed": in_stage,
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
            "notify": {
                "pending": eng.notifier.depth,
                "sent": eng.notifier.sent,
                "failed": eng.notifier.failed,
            },
            "workflow": eng.db.workflow_stats(),
            "backup": self._backup_status(),
            "queued": [{"path": p, "name": Path(p).name} for p in queued[:20]],
            "active": active,
            "recent": [_row(r) for r in recent],
            "now": now,
            **stats,
        }

    def _backup_status(self) -> dict[str, Any]:
        eng = self.engine
        cfg = eng.cfg.backup
        found = backup.list_backups(backup.backup_dir(eng.cfg))
        return {
            "enabled": cfg.enabled,
            "keep": cfg.keep,
            "interval_hours": cfg.interval_hours,
            "count": len(found),
            "last": found[-1].stat().st_mtime if found else 0.0,
            "next": eng.next_backup,
        }

    def api_backups(self) -> dict[str, Any]:
        directory = backup.backup_dir(self.engine.cfg)
        found = backup.list_backups(directory)
        return {
            "dir": str(directory),
            **self._backup_status(),
            # Newest first: the one anybody restoring is looking for.
            "backups": [
                {"name": f.name, "path": str(f), "size": f.stat().st_size,
                 "taken": f.stat().st_mtime}
                for f in reversed(found)
            ],
        }

    def api_backup_run(self, body: dict[str, Any]) -> dict[str, Any]:
        """Take a snapshot now. Replaces today's, it does not add to it."""
        try:
            path = self.engine.backup_now()
        except (OSError, sqlite3.Error) as exc:
            raise ApiError(f"backup failed: {exc}", 500)
        return {"ok": True, "path": str(path), **self.api_backups()}

    def api_backup_restore(self, body: dict[str, Any]) -> dict[str, Any]:
        """Put a snapshot back, under the running daemon.

        Only the snapshot's name is accepted, never a path: this replaces
        the state database, and the set of files it may read from is the
        backup directory and nothing else.
        """
        name = str(body.get("name") or "").strip()
        if not name:
            raise ApiError("name is required")
        try:
            kept = self.engine.restore_backup(name)
        except ValueError as exc:
            raise ApiError(str(exc), 404) from None
        except RuntimeError as exc:
            # Work in flight: a refusal the operator can act on, not a fault.
            raise ApiError(str(exc), 409) from None
        except (OSError, sqlite3.Error) as exc:
            raise ApiError(f"restore failed: {exc}", 500) from None
        message = f"restored {name}"
        if kept:
            message += f"; previous database kept as {Path(kept).name}"
        log.warning("%s", message)
        return {"ok": True, "message": message, **self.api_backups()}

    # --- imports ----------------------------------------------------------

    def api_workflow(self) -> dict[str, Any]:
        """The durable import queue: one entry per accepted Arr import.

        Jobs carry the encode; the outbox rows carry what happens after it
        (the Arr rescan/rename, then AutoPulse). Both are shown against the
        job, because a stuck import is almost always stuck in one stage and
        the panel's job is to say which.
        """
        q = self._query()
        limit = self._int(q, "limit", 50, 1, 500)
        status = (q.get("status") or "").strip() or None
        db = self.engine.db

        actions: dict[int, list[dict[str, Any]]] = {}
        for row in db.list_outbox(limit=2000):
            job_id = row["job_id"]
            if job_id is None:
                continue
            actions.setdefault(int(job_id), []).append({
                "id": row["id"], "action": row["action"],
                "status": row["status"], "retries": row["retries"],
                "next_attempt": row["next_attempt"], "error": row["error"],
                "updated_at": row["updated_at"],
            })

        jobs = []
        for row in db.list_jobs(status, limit=limit):
            meta = json_loads(row["source_metadata"], {}) or {}
            path = row["final_path"] or row["source_path"]
            jobs.append({
                "id": row["id"],
                "path": path,
                "name": Path(path).name,
                "source_path": row["source_path"],
                "final_path": row["final_path"],
                "provider": row["source_provider"] or "",
                "integration": str(meta.get("integration") or ""),
                "event": str(meta.get("event_type") or ""),
                "mode": row["mode"] or "",
                "stage": row["stage"],
                "status": row["status"],
                "retries": row["retries"],
                "next_attempt": row["next_attempt"],
                "error": row["error"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "finished_at": row["finished_at"],
                "actions": actions.get(int(row["id"]), []),
            })
        # Newest first: an import someone is asking about is a recent one.
        jobs.sort(key=lambda j: (j["created_at"] or 0, j["id"]), reverse=True)
        stats = db.workflow_stats()
        return {
            "jobs": jobs,
            "stats": stats,
            "failed": int(stats["jobs"].get("failed", 0)),
            "now": time.time(),
            "autopulse": self.engine.cfg.integrations.autopulse.enabled,
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
            "integrations": config_mod.integration_schema(self.engine.cfg),
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

    # --- native Sonarr/Radarr webhooks ----------------------------------

    def _payload_provider(self, body: dict[str, Any]) -> str | None:
        raw = body.get("provider") or body.get("application")
        if isinstance(raw, str):
            raw = raw.strip().lower()
            if raw in ("sonarr", "radarr"):
                return raw
        # A generic /api/webhook route can still identify native payloads
        # without asking Arr to add a non-standard provider field.
        if "series" in body or "episodeFile" in body:
            return "sonarr"
        if "movie" in body or "movieFile" in body:
            return "radarr"
        return None

    def _payload_instance(self, body: dict[str, Any]) -> str | None:
        raw = (body.get("integration") or body.get("instance")
               or body.get("instanceName"))
        return str(raw).strip() if raw not in (None, "") else None

    def _arr_instance(self, provider: str | None,
                      requested: str | None) -> Any:
        provider = (provider or "").strip().lower()
        if provider not in ("sonarr", "radarr"):
            if requested:
                raise ApiError("provider must be sonarr or radarr")
            return None
        instances = [i for i in self.engine.cfg.integrations.instances(provider)
                     if i.enabled]
        if requested:
            wanted = requested.casefold()
            found = next((i for i in instances
                          if i.id.casefold() == wanted
                          or i.name.casefold() == wanted), None)
            if found is None:
                raise ApiError(f"no such {provider} integration: {requested}", 404)
            return found
        if len(instances) == 1:
            return instances[0]
        if len(instances) > 1:
            raise ApiError(f"integration is required for {provider}")
        return None

    def _native_fields(self, body: dict[str, Any], provider: str) -> dict[str, Any]:
        """Extract stable fields from both current and older Arr payloads."""
        event = str(body.get("eventType") or body.get("event") or "").strip()
        lower = event.lower().replace(" ", "_")
        entity = body.get("series") if provider == "sonarr" else body.get("movie")
        entity = entity if isinstance(entity, dict) else {}
        file_key = "episodeFile" if provider == "sonarr" else "movieFile"
        file_obj = body.get(file_key)
        file_obj = file_obj if isinstance(file_obj, dict) else {}

        entity_id = _as_id(_nested(
            body,
            "seriesId" if provider == "sonarr" else "movieId",
            "entityId",
        ))
        if entity_id is None:
            entity_id = _as_id(entity.get("id"))
        file_id = _as_id(_nested(
            body,
            "episodeFileId" if provider == "sonarr" else "movieFileId",
            "fileId",
        ))
        if file_id is None:
            file_id = _as_id(file_obj.get("id"))

        final_path = _nested(
            file_obj, "path", "absolutePath", "finalPath", "destinationPath"
        )
        if final_path is None:
            final_path = _nested(
                body,
                "path", "finalPath", "destinationPath",
                "episodeFilePath" if provider == "sonarr" else "movieFilePath",
            )
        return {
            "event": event,
            "event_type": lower,
            "entity_id": entity_id,
            "file_id": file_id,
            "path": str(final_path) if final_path not in (None, "") else None,
        }

    def _enqueue_native(self, path: str, *, provider: str,
                        integration: Any, fields: dict[str, Any],
                        body: dict[str, Any]) -> tuple[bool, str]:
        """Call a future import-aware engine API, falling back to enqueue()."""
        mode = integration.mode if integration else ""
        is_upgrade = fields["event_type"] == "upgrade" or bool(
            body.get("isUpgrade") or body.get("isUpgradeFile"))
        kwargs = {
            "path": path,
            "provider": provider,
            "integration": integration.id if integration else None,
            "entity_id": fields["entity_id"],
            "file_id": fields["file_id"],
            "is_upgrade": is_upgrade,
            "event_type": fields["event_type"],
            "mode": mode or None,
            "payload": body,
        }
        method = next((getattr(self.engine, name, None) for name in (
            "enqueue_import", "enqueue_integration", "enqueue_native_event",
            "enqueue_arr",
        ) if callable(getattr(self.engine, name, None))), None)
        if callable(method):
            try:
                result = method(**kwargs)
            except TypeError:
                # Development compatibility for a positional implementation
                # while the engine API settles.
                try:
                    result = method(path=path, provider=provider,
                                    entity_id=fields["entity_id"],
                                    file_id=fields["file_id"],
                                    upgrade=is_upgrade, mode=mode or None)
                except TypeError:
                    try:
                        result = method(final_path=path, provider=provider,
                                        entity_id=fields["entity_id"],
                                        file_id=fields["file_id"],
                                        upgrade=is_upgrade, mode=mode or None)
                    except TypeError:
                        try:
                            result = method(path, provider=provider,
                                        entity_id=fields["entity_id"],
                                        file_id=fields["file_id"],
                                        is_upgrade=is_upgrade, mode=mode or None)
                        except TypeError:
                            result = method(path)
        else:
            result = self.engine.enqueue(path, force=is_upgrade,
                                         mode=mode or None)

        if isinstance(result, tuple):
            return bool(result[0]), str(result[1]) if len(result) > 1 else "queued"
        if isinstance(result, dict):
            accepted = result.get("accepted", result.get("ok", result.get("queued")))
            return bool(accepted), str(result.get("message", "queued"))
        return bool(result), "queued" if result else "not accepted"

    def api_webhook(self, body: dict[str, Any], provider_hint: str | None = None,
                    instance_hint: str | None = None,
                    *, instance: Any = None) -> dict[str, Any]:
        provider = (provider_hint or self._payload_provider(body) or "").lower()
        if provider not in ("sonarr", "radarr"):
            raise ApiError("native webhook route must name sonarr or radarr")
        instance = instance or self._arr_instance(
            provider, instance_hint or self._payload_instance(body))
        fields = self._native_fields(body, provider)
        if fields["event_type"] not in _ARR_IMPORT_EVENTS:
            return {
                "ok": True, "accepted": False, "ignored": True,
                "provider": provider, "integration": instance.id if instance else None,
                "entity_id": fields["entity_id"], "file_id": fields["file_id"],
                "entityId": fields["entity_id"], "fileId": fields["file_id"],
                "path": fields["path"], "final_path": fields["path"],
                "event": fields["event"],
                "message": "event ignored (only import/download/upgrade events are queued)",
            }
        if not fields["path"]:
            raise ApiError("native webhook has no final file path")
        if fields["entity_id"] is None:
            label = "series" if provider == "sonarr" else "movie"
            raise ApiError(f"native webhook has no {label} id")
        path = _norm(fields["path"])
        if instance and instance.path_from and instance.path_to:
            from .integrations import PathMapping
            path = _norm(PathMapping(instance.path_from,
                                     instance.path_to).to_local(path))
        ok, message = self._enqueue_native(
            path, provider=provider, integration=instance,
            fields=fields, body=body)
        if not ok:
            raise ApiError(f"enqueue was not accepted: {message}", 409)
        return {
            "ok": True, "accepted": True, "queued": True,
            "message": message, "provider": provider,
            "integration": instance.id if instance else None,
            "entity_id": fields["entity_id"], "file_id": fields["file_id"],
            "entityId": fields["entity_id"], "fileId": fields["file_id"],
            "path": path, "final_path": path, "event": fields["event"],
        }

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

    def api_workflow_retry(self, body: dict[str, Any]) -> dict[str, Any]:
        raw = body.get("job_id")
        try:
            job_id = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            raise ApiError("job_id must be a whole number") from None
        n = self.engine.retry_workflow(job_id)
        return {"ok": True, "message": f"retried {n} workflow job(s)"}

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
            "integrations": config_mod.integration_schema(self.engine.cfg),
            "toml": config_mod.dump_toml(self.engine.cfg),
        }

    def _library(self, lib_id: Any) -> Any:
        lib = self.engine.cfg.library(str(lib_id or ""))
        if lib is None:
            raise ApiError(f"no such library: {lib_id}", 404)
        return lib

    def api_libraries(self) -> dict[str, Any]:
        cfg = self.engine.cfg
        stats = self.engine.db.library_stats()
        out = []
        for l in cfg.libraries:
            mode = cfg.mode(l.mode)
            out.append({
                "id": l.id,
                "name": l.name,
                "enabled": l.enabled,
                "paths": l.paths,
                "mode": l.mode,
                "mode_name": mode.name if mode else l.mode,
                # What this library actually does is its mode's business, so
                # the summary on the card is read from there.
                "stages": _stages(mode) if mode else {},
                "schema": config_mod.library_schema(l, cfg),
                "stats": stats.get(l.id, {"total": 0, "bytes": 0,
                                          "counts": {}, "saved": 0}),
            })
        return {"libraries": out}

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

    # --- integrations -----------------------------------------------------

    def _origin(self) -> str:
        """Where this panel is being reached from.

        Used to print the webhook URL to paste into Sonarr. Built from the
        request's own headers so it is right whether the panel is reached by
        container name, by IP, or through a reverse proxy.
        """
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
        if not host:
            addr = self.server.server_address
            host = f"{addr[0]}:{addr[1]}"
        scheme = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{scheme}://{host}"

    def _arr_profile(self, body: dict[str, Any]) -> tuple[str, Any]:
        provider = str(body.get("provider") or "").strip().lower()
        if provider not in ("sonarr", "radarr"):
            raise ApiError("provider must be sonarr or radarr")
        wanted = str(body.get("id") or "")
        instance = config_mod.arr_instance(self.engine.cfg, provider, wanted)
        if instance is None:
            raise ApiError(f"no such {provider} integration: {wanted}", 404)
        return provider, instance

    def _integration_card(self, provider: str, instance: Any) -> dict[str, Any]:
        cfg = self.engine.cfg
        mode = cfg.mode(instance.mode) if instance.mode else None
        return {
            "provider": provider,
            "id": instance.id,
            "name": instance.name,
            "enabled": instance.enabled,
            "mode": instance.mode,
            "mode_name": mode.name if mode else "",
            "url": instance.url,
            "webhook": f"{self._origin()}/api/webhook/{provider}/{instance.id}",
            # Enough to reconcile with the Arr, or only enough to receive?
            "outbound_ready": bool(instance.url and instance.api_key),
            "api_key_configured": bool(instance.api_key),
            "secret_configured": bool(instance.secret),
            "schema": config_mod.arr_schema(instance, cfg),
        }

    def api_integrations(self) -> dict[str, Any]:
        cfg = self.engine.cfg
        auto = cfg.integrations.autopulse
        return {
            "sonarr": [self._integration_card("sonarr", i)
                       for i in cfg.integrations.sonarr],
            "radarr": [self._integration_card("radarr", i)
                       for i in cfg.integrations.radarr],
            "autopulse": {
                "enabled": auto.enabled,
                "url": auto.url,
                "configured": bool(auto.url),
                "schema": config_mod.autopulse_schema(cfg),
            },
            "modes": [{"id": m.id, "name": m.name} for m in cfg.modes],
            "api_key": cfg.web.api_key,
            "origin": self._origin(),
        }

    def api_integration_add(self, body: dict[str, Any]) -> dict[str, Any]:
        provider = str(body.get("provider") or "").strip().lower()
        try:
            instance = config_mod.add_arr_instance(
                self.engine.cfg, provider, str(body.get("name") or ""))
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        log.info("%s integration added: %s (%s)", provider, instance.name,
                 instance.id)
        return {"ok": True, "id": instance.id, **self.api_integrations()}

    def api_integration_update(self, body: dict[str, Any]) -> dict[str, Any]:
        provider, instance = self._arr_profile(body)
        updates = body.get("updates")
        if not isinstance(updates, dict):
            raise ApiError("updates must be an object of dotted keys")
        try:
            changed = config_mod.apply_arr_updates(
                self.engine.cfg, instance, updates)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        if changed:
            log.info("%s integration %s updated: %s", provider, instance.id,
                     ", ".join(changed))
        return {"ok": True, "changed": changed, **self.api_integrations()}

    def api_integration_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        provider, instance = self._arr_profile(body)
        try:
            config_mod.remove_arr_instance(self.engine.cfg, provider,
                                           instance.id)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        log.info("%s integration removed: %s", provider, instance.name)
        return {"ok": True, "message": f"removed {instance.name}",
                **self.api_integrations()}

    def api_autopulse_update(self, body: dict[str, Any]) -> dict[str, Any]:
        updates = body.get("updates")
        if not isinstance(updates, dict):
            raise ApiError("updates must be an object of dotted keys")
        try:
            changed = config_mod.apply_autopulse_updates(
                self.engine.cfg, updates)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        if changed:
            log.info("autopulse updated: %s", ", ".join(changed))
        return {"ok": True, "changed": changed, **self.api_integrations()}

    def api_integration_test(self, body: dict[str, Any]) -> dict[str, Any]:
        """Prove a profile's outbound credentials work, changing nothing.

        Sonarr and Radarr answer system/status, which is the cheapest call
        that still proves the URL, the port and the key. AutoPulse has no
        read-only endpoint and its only verb triggers a real scan, so it is
        checked as far as being configured and no further.
        """
        provider = str(body.get("provider") or "").strip().lower()
        if provider == "autopulse":
            auto = self.engine.cfg.integrations.autopulse
            if not auto.url.strip():
                raise ApiError("AutoPulse has no url configured")
            base = auto.url.rstrip("/")
            targets = {
                origin: base + "/" + auto.endpoint_for(origin).lstrip("/")
                for origin in ("sonarr", "radarr")
            }
            if len(set(targets.values())) == 1:
                message = ("AutoPulse will be called at "
                           + next(iter(targets.values())))
            else:
                message = "AutoPulse will be called at " + ", ".join(
                    f"{origin}: {url}" for origin, url in targets.items())
            return {"ok": True, "provider": "autopulse",
                    "targets": targets, "message": message}

        provider, instance = self._arr_profile(body)
        if not instance.url or not instance.api_key:
            raise ApiError(f"{instance.name} needs a url and an api_key "
                           "before it can be tested")
        cls = (integrations.SonarrClient if provider == "sonarr"
               else integrations.RadarrClient)
        client = cls(instance.url, instance.api_key,
                     timeout=instance.request_timeout)
        try:
            status = client.system_status()
        except (integrations.IntegrationError, OSError) as exc:
            raise ApiError(f"{instance.name}: {exc}", 502) from None
        version = str(status.get("version") or "") if isinstance(status, dict) else ""
        return {"ok": True, "provider": provider, "id": instance.id,
                "version": version,
                "message": f"{instance.name} answered"
                           + (f", version {version}" if version else "")}

    # --- modes ------------------------------------------------------------

    def api_modes(self) -> dict[str, Any]:
        cfg = self.engine.cfg
        return {
            "modes": [
                {
                    "id": m.id,
                    "name": m.name,
                    "description": m.description,
                    "stages": _stages(m),
                    # Which libraries run on this mode: the panel needs it to
                    # say what a change is about to affect, and deleting one
                    # that is in use is refused.
                    "libraries": [
                        {"id": l.id, "name": l.name}
                        for l in cfg.libraries if l.mode == m.id
                    ],
                    "schema": config_mod.mode_schema(m),
                }
                for m in cfg.modes
            ],
        }

    def _mode_cfg(self, mode_id: Any) -> Any:
        mode = self.engine.cfg.mode(str(mode_id or "").strip())
        if mode is None:
            raise ApiError(f"no such mode: {mode_id}", 404)
        return mode

    def api_mode_add(self, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        copy_from = body.get("copy_from")
        try:
            mode = config_mod.add_mode(self.engine.cfg, name,
                                       str(copy_from) if copy_from else None)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        return {"ok": True, "id": mode.id, "message": f"added {mode.name}",
                **self.api_modes()}

    def api_mode_update(self, body: dict[str, Any]) -> dict[str, Any]:
        mode = self._mode_cfg(body.get("id"))
        updates = body.get("updates")
        if not isinstance(updates, dict):
            raise ApiError("updates must be an object")
        before = copy.deepcopy(mode)
        try:
            changed = config_mod.apply_mode_updates(self.engine.cfg, mode, updates)
        except ConfigError as exc:
            # Put the mode back exactly as it was: a half-applied profile
            # would be live for every library pointing at it.
            self.engine.cfg.modes[self.engine.cfg.modes.index(mode)] = before
            raise ApiError(str(exc)) from None
        self._persist()
        return {"ok": True, "changed": changed,
                "message": f"updated {len(changed)} setting(s)" if changed
                           else "no changes",
                **self.api_modes()}

    def api_mode_delete(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            mode = config_mod.remove_mode(self.engine.cfg, str(body.get("id") or ""))
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        self._persist()
        return {"ok": True, "message": f"removed {mode.name}", **self.api_modes()}

    def api_notify_test(self, body: dict[str, Any]) -> dict[str, Any]:
        """Fire a library's webhooks now, with a sample payload.

        Worth having: a typo in a Jellyfin URL should show up while someone is
        looking at the panel, not silently at 3am when an import fires.
        """
        lib = self._library(body.get("library") or body.get("id"))
        mode = self._mode(body)
        try:
            profile = config_mod.resolve(self.engine.cfg, lib, mode)
        except ConfigError as exc:
            raise ApiError(str(exc)) from None
        hooks = notify_mod.hooks_for(profile.notify)
        if not hooks:
            raise ApiError("no notifications are configured for this library"
                           + (f" under mode {mode}" if mode else ""))
        sample = str(Path(body.get("path") or "/media/Example (2024)/"
                          "Example (2024) - Bluray-1080p.mkv"))
        payload = notify_mod.payload_for(
            sample, library=lib.id, mode=mode, status="done",
            in_size=8 * 2**30, out_size=3 * 2**30, elapsed=1800.0,
            reasons=["test notification"])
        payload["event"] = "test"
        n = self.engine.notifier.dispatch(hooks, payload)
        return {"ok": True, "message": f"queued {n} test call(s)",
                "urls": [notify_mod.expand(h.url, payload) for h in hooks]}

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
