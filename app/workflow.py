"""Durable post-import workflow for Arr-managed media.

The encoder and remote applications have deliberately separate lifetimes.  An
Arr webhook is committed as a processing job before it is acknowledged; once
the file is settled, the targeted Jellyfin update moves to the durable outbox.
Retrying that remote call can therefore never re-run an encode.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import integrations
from .db import Db, loads

log = logging.getLogger("transcoder.workflow")


def _value(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, default)
    return default if value is None else value


class Workflow:
    """Persist imports and drain their post-processing outbox."""

    def __init__(self, cfg: Any, db: Db) -> None:
        self.cfg = cfg
        self.db = db
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="integration-outbox", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def join(self, timeout: float = 30.0) -> None:
        thread = self._thread
        if thread:
            thread.join(timeout=max(0.0, timeout))

    def wake(self) -> None:
        self._wake.set()

    # --- imports --------------------------------------------------------

    @staticmethod
    def dedupe_key(path: str, provider: str, integration: str | None,
                   entity_id: Any, file_id: Any, event_type: str,
                   payload: dict[str, Any] | None = None) -> str:
        """Stable key for webhook redelivery, distinct for real upgrades."""
        body = payload or {}
        release = body.get("downloadId") or body.get("downloadClientId")
        if not release and isinstance(body.get("release"), dict):
            release = body["release"].get("releaseTitle")
        identity: dict[str, Any] = {
            "provider": provider, "integration": integration or "",
            "entity": entity_id, "file": file_id, "event": event_type,
            "path": str(Path(path)), "release": release,
        }
        # Old/custom webhook payloads sometimes omit both the file and
        # download ids.  In that case the imported file's stat separates a
        # later replacement at the same path from a webhook retry.
        if file_id in (None, "") and not release:
            try:
                stat = Path(path).stat()
                identity.update(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
            except OSError:
                pass
        raw = json.dumps(identity, sort_keys=True, separators=(",", ":"),
                         default=str).encode()
        return f"{provider}:{hashlib.sha256(raw).hexdigest()}"

    def accept(self, *, path: str, provider: str,
               integration: str | None = None, entity_id: Any = None,
               file_id: Any = None, is_upgrade: bool = False,
               event_type: str = "download", mode: str | None = None,
               payload: dict[str, Any] | None = None, **_: Any) -> tuple[Any, bool]:
        """Commit an import and its processing job.

        Returns ``(job, should_queue)``.  Redelivery of a completed or active
        event is accepted idempotently without starting a second encode.
        """
        provider = str(provider).strip().lower()
        metadata = {
            "provider": provider, "integration": integration or "",
            "entity_id": entity_id, "file_id": file_id,
            "is_upgrade": bool(is_upgrade), "event_type": event_type,
            "payload": payload or {},
        }
        key = self.dedupe_key(path, provider, integration, entity_id, file_id,
                              event_type, payload)
        existing = self.db.get_job(dedupe_key=key)
        if existing is not None:
            return existing, existing["status"] in {"pending", "retry"}

        request = self.db.create_import_request(
            key, path, source_provider=provider, source_metadata=metadata,
            mode=mode or "")
        job = self.db.create_job(
            key, path, source_provider=provider, source_metadata=metadata,
            mode=mode or "", stage="processing", status="pending",
            import_request_id=request["id"])
        self.db.update_import_request(
            request["id"], job_id=job["id"], stage="processing",
            status="queued")
        return job, True

    def recoverable(self) -> list[Any]:
        # ``accept`` writes the request before the job.  If the process dies
        # in that tiny gap, the acknowledged import is still reconstructible
        # from its request row on startup (and before any queue sweep).
        for request in self.db.list_import_requests(status="pending",
                                                    limit=100_000):
            if request["job_id"]:
                continue
            job = self.db.create_job(
                request["dedupe_key"], request["source_path"],
                source_provider=request["source_provider"],
                source_metadata=loads(request["source_metadata"], {}),
                mode=request["mode"], stage="processing", status="pending",
                import_request_id=request["id"])
            self.db.update_import_request(
                request["id"], job_id=job["id"], stage="processing",
                status="queued")
        return [row for row in self.db.recoverable_jobs(limit=100_000)
                if row["stage"] == "processing"
                and self.db.get_outbox(dedupe_key=row["dedupe_key"]) is None]

    def claim(self, job_id: int) -> Any:
        return self.db.claim_job(job_id)

    def processing_succeeded(self, job_id: int, final_path: str,
                             *, changed: bool) -> None:
        """Move a settled import directly to its Jellyfin update."""
        job = self.db.get_job(job_id)
        if job is None:
            return
        metadata = loads(job["source_metadata"], {})
        provider = str(job["source_provider"] or "")
        if not self._autopulse_enabled():
            self._complete_job(job_id, final_path)
            return
        action = "jellyfin"
        stage = "jellyfin"
        out = {
            "provider": provider,
            "integration": metadata.get("integration") or "",
            "entity_id": metadata.get("entity_id"),
            "file_id": metadata.get("file_id"),
            "original_path": job["source_path"],
            "final_path": final_path,
        }
        self.db.transition_job_to_outbox(
            job_id, stage=stage, final_path=final_path,
            action=action, payload=out)
        self._wake.set()

    def processing_failed(self, job_id: int, error: str) -> None:
        job = self.db.finish_job(job_id, "failed", error=error)
        if job is not None:
            self._update_import(job, stage="processing", status="failed",
                                error=error)

    # --- outbox ---------------------------------------------------------

    def drain_once(self) -> bool:
        row = self.db.claim_outbox()
        if row is None:
            return False
        payload = loads(row["payload"], {})
        try:
            if row["action"] == "arr_reconcile":
                final_path = self._reconcile(payload)
                job = self.db.update_job(
                    row["job_id"], final_path=final_path,
                    stage="autopulse" if self._autopulse_enabled() else "complete",
                    status="waiting" if self._autopulse_enabled() else "done")
                if self._autopulse_enabled():
                    next_payload = dict(payload, final_path=final_path)
                    self.db.enqueue_outbox(
                        row["dedupe_key"], "autopulse", next_payload,
                        job_id=row["job_id"])
                    if job is not None:
                        self._update_import(job, final_path=final_path,
                                            stage="autopulse", status="waiting")
                else:
                    self._complete_job(row["job_id"], final_path)
            elif row["action"] in {"jellyfin", "autopulse"}:
                self._send_jellyfin(str(payload["final_path"]),
                                     payload.get("provider"))
                self._complete_job(row["job_id"], str(payload["final_path"]))
            else:
                raise integrations.IntegrationError(
                    f"unknown outbox action: {row['action']}")
        except Exception as exc:
            self._retry_or_fail(row, exc)
            return True
        self.db.complete_outbox(row["id"])
        self._wake.set()
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.drain_once():
                self._wake.wait(1.0)
                self._wake.clear()

    def _reconcile(self, payload: dict[str, Any]) -> str:
        provider = str(payload.get("provider") or "").lower()
        cfg = self._arr_instance(provider, payload.get("integration"))
        if cfg is None:
            raise integrations.IntegrationError(
                f"no enabled {provider} integration")
        if not _value(cfg, "url", "") or not _value(cfg, "api_key", ""):
            raise integrations.IntegrationError(
                f"{provider} integration {cfg.id} needs url and api_key")
        cls = integrations.SonarrClient if provider == "sonarr" else integrations.RadarrClient
        mappings = []
        if _value(cfg, "path_from", "") and _value(cfg, "path_to", ""):
            mappings.append(integrations.PathMapping(cfg.path_from, cfg.path_to))
        client = cls(
            cfg.url, cfg.api_key, timeout=float(_value(cfg, "request_timeout", 30)),
            command_timeout=float(_value(cfg, "command_timeout", 300)),
            poll_interval=float(_value(cfg, "poll_interval", 2)),
            path_mappings=mappings)
        result = client.reconcile(
            payload.get("entity_id"), source_file_id=payload.get("file_id"),
            original_path=payload.get("original_path"),
            final_path=payload.get("final_path"))
        return result.final_path

    def _send_jellyfin(self, path: str, provider: str | None = None) -> None:
        cfg = self.cfg.integrations.autopulse
        if not _value(cfg, "url", ""):
            raise integrations.IntegrationError("Jellyfin url is not configured")
        if not _value(cfg, "api_key", ""):
            raise integrations.IntegrationError("Jellyfin api_key is not configured")
        integrations.JellyfinClient(
            cfg.url, cfg.api_key,
            timeout=float(_value(cfg, "timeout", 15))).update(path)

    def _retry_or_fail(self, row: Any, exc: Exception) -> None:
        limit = self._retry_limit(row)
        attempts = int(row["retries"] or 0)
        message = str(exc) or exc.__class__.__name__
        if attempts >= limit:
            self.db.fail_outbox(row["id"], message)
            job = self.db.update_job(
                row["job_id"], stage=row["action"], status="failed",
                error=f"{message} (gave up after {attempts} attempts)")
            if job is not None:
                self._update_import(job, stage=row["action"], status="failed",
                                    error=job["error"])
            log.error("%s failed for job %s: %s", row["action"],
                      row["job_id"], message)
            return
        delay = min(3600.0, 5.0 * (2 ** max(0, attempts - 1)))
        self.db.fail_outbox(row["id"], message,
                            next_attempt=time.time() + delay)
        log.warning("%s failed for job %s: %s; retrying in %.0fs",
                    row["action"], row["job_id"], message, delay)

    def _retry_limit(self, row: Any) -> int:
        if row["action"] in {"jellyfin", "autopulse"}:
            return max(1, int(_value(self.cfg.integrations.autopulse,
                                     "max_retries", 5)))
        payload = loads(row["payload"], {})
        cfg = self._arr_instance(payload.get("provider", ""),
                                 payload.get("integration"))
        return max(1, int(_value(cfg, "max_retries", 5)))

    # --- helpers --------------------------------------------------------

    def _arr_instance(self, provider: str, requested: str | None) -> Any:
        if provider not in {"sonarr", "radarr"}:
            return None
        candidates = [i for i in self.cfg.integrations.instances(provider)
                      if i.enabled]
        if requested:
            wanted = str(requested).casefold()
            return next((i for i in candidates if i.id.casefold() == wanted
                         or i.name.casefold() == wanted), None)
        return candidates[0] if len(candidates) == 1 else None

    def _autopulse_enabled(self) -> bool:
        cfg = self.cfg.integrations.autopulse
        return bool(cfg.enabled and str(cfg.url).strip())

    def _complete_job(self, job_id: int, final_path: str) -> None:
        job = self.db.finish_job(job_id, "done", final_path=final_path)
        if job is not None:
            self.db.update_job(job_id, stage="complete")
            self._update_import(job, stage="complete", status="done",
                                final_path=final_path, error=None)

    def _update_import(self, job: Any, **changes: Any) -> None:
        request_id = job["import_request_id"]
        if request_id:
            self.db.update_import_request(request_id, **changes)


__all__ = ["Workflow"]
