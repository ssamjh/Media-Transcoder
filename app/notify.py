"""Outbound webhooks: tell other applications a file is finished.

Sonarr or Radarr calls `/api/process` on import; when the encode has verified
and been copied back over the original, whatever else cares - Jellyfin, Plex,
a notification service - gets a call of its own. Those calls are queued and
delivered on a single background thread, so a mass import that finishes
twenty files at once never blocks a worker on somebody else's slow HTTP
server, and a downstream service that is down does not fail the encode.

Delivery is best effort by design: a webhook that never succeeds is logged
and dropped. The file is already correct on disk; the transcoder's own state
must not depend on a third party answering.

stdlib urllib only - no third-party HTTP client, same as everywhere else.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("transcoder.notify")

# Statuses worth another attempt: the far end is overloaded or broken, not
# refusing us. A 404 or a 401 will still be a 404 or a 401 in eight seconds.
RETRY_CODES = {408, 425, 429, 500, 502, 503, 504}
BACKOFF = (2.0, 5.0, 15.0, 30.0)


@dataclass
class Webhook:
    url: str
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 15.0
    retries: int = 3


def parse_headers(lines: list[str]) -> dict[str, str]:
    """`["X-Api-Key: abc"]` -> `{"X-Api-Key": "abc"}`.

    Headers are configured as a list of lines rather than a table because the
    config schema's map type is integer-valued (it exists for the audio
    scoring tables) and an API key is not a number.
    """
    out: dict[str, str] = {}
    for line in lines:
        name, sep, value = str(line).partition(":")
        if not sep or not name.strip():
            continue
        out[name.strip()] = value.strip()
    return out


_TOKEN = re.compile(r"\{(path|name|stem|dir|library|mode|status)\}")


def expand(template: str, payload: dict[str, Any]) -> str:
    """Substitute the handful of known {tokens}, URL-quoting each value.

    Only these names are recognised, so a brace that is really part of the URL
    is left exactly as it was found.
    """
    def sub(m: re.Match[str]) -> str:
        return urllib.parse.quote(str(payload.get(m.group(1), "")), safe="")
    return _TOKEN.sub(sub, template)


class Notifier:
    """A queue of pending webhook calls plus the thread that drains it."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[Webhook, dict[str, Any]]] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._inflight = 0
        self.sent = 0
        self.failed = 0

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the delivery thread. Idempotent, and safe to call late.

        The engine starts it with the workers, but `dispatch` starts it too,
        so a one-shot CLI run or a test that never calls `Engine.start` still
        delivers.
        """
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="notify",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 30.0) -> None:
        """Wait for the backlog to drain, then for the thread to finish."""
        deadline = time.monotonic() + timeout
        while self.depth and time.monotonic() < deadline:
            time.sleep(0.1)
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=max(0.1, deadline - time.monotonic()))

    @property
    def depth(self) -> int:
        """Calls still owed - waiting, or in the middle of a retry.

        In-flight counts, so `join` does not cut short a call that is between
        attempts and then report the backlog as empty.
        """
        return self._queue.qsize() + self._inflight

    # --- submission -------------------------------------------------------

    def dispatch(self, hooks: list[Webhook], payload: dict[str, Any]) -> int:
        """Queue one payload against every hook. Never raises, never blocks."""
        if not hooks:
            return 0
        self.start()
        for hook in hooks:
            self._queue.put((hook, payload))
        log.info("queued %d webhook(s) for %s", len(hooks),
                 payload.get("name") or payload.get("path"))
        return len(hooks)

    # --- delivery ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set() or self.depth:
            try:
                hook, payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._inflight += 1
            try:
                self._deliver(hook, payload)
            except Exception:  # pragma: no cover - defensive
                log.exception("webhook dispatch failed")
            finally:
                self._inflight -= 1
                self._queue.task_done()

    def _deliver(self, hook: Webhook, payload: dict[str, Any]) -> bool:
        url = expand(hook.url, payload)
        attempts = max(1, hook.retries)
        for attempt in range(1, attempts + 1):
            code, note = self._once(hook, url, payload)
            if code and 200 <= code < 300:
                self.sent += 1
                log.info("webhook %s %s -> %d", hook.method, url, code)
                return True
            retryable = code is None or code in RETRY_CODES
            if not retryable or attempt == attempts or self._stop.is_set():
                self.failed += 1
                log.warning("webhook %s %s failed: %s (gave up after %d attempt(s))",
                            hook.method, url, note, attempt)
                return False
            delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            log.warning("webhook %s %s failed: %s, retrying in %.0fs",
                        hook.method, url, note, delay)
            if self._stop.wait(delay):
                # Shutting down mid-backoff: drop it rather than hold the
                # process open for a service that is not answering.
                self.failed += 1
                return False
        return False

    def _once(self, hook: Webhook, url: str,
              payload: dict[str, Any]) -> tuple[int | None, str]:
        method = (hook.method or "POST").upper()
        data = None
        headers = dict(hook.headers)
        if method not in ("GET", "HEAD", "DELETE"):
            data = json.dumps(payload).encode()
            headers.setdefault("Content-Type", "application/json")
        headers = {k: expand(v, payload) for k, v in headers.items()}
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=hook.timeout) as resp:
                resp.read(2048)
                return int(resp.status), "ok"
        except urllib.error.HTTPError as exc:
            return int(exc.code), f"HTTP {exc.code}"
        except Exception as exc:
            return None, str(exc) or exc.__class__.__name__


def hooks_for(notify: Any) -> list[Webhook]:
    """Turn a library's (or a mode's resolved) NotifyCfg into Webhooks."""
    if notify is None or not getattr(notify, "enabled", False):
        return []
    headers = parse_headers(list(notify.headers))
    return [
        Webhook(url=str(u).strip(), method=notify.method, headers=headers,
                timeout=notify.timeout, retries=notify.retries)
        for u in notify.urls if str(u).strip()
    ]


def jellyfin_hooks(cfg: Any) -> list[Webhook]:
    """Build the direct Jellyfin update hook used by non-import/manual work."""
    target = getattr(getattr(cfg, "integrations", None), "jellyfin", None)
    if not target or not target.enabled or not str(target.url).strip() or not target.api_key:
        return []
    return [Webhook(
        url=str(target.url).rstrip("/") + "/Library/Media/Updated",
        headers={"X-Emby-Token": str(target.api_key)},
        timeout=float(target.timeout), retries=max(1, int(target.max_retries)),
    )]


def jellyfin_payload(path: str) -> dict[str, Any]:
    return {"Updates": [{"Path": str(path), "UpdateType": "Modified"}]}


def payload_for(path: str, *, library: str, mode: str, status: str,
                original: str = "", in_size: int = 0, out_size: int = 0,
                elapsed: float = 0.0,
                reasons: list[str] | None = None) -> dict[str, Any]:
    p = Path(path)
    return {
        "event": "processed",
        "status": status,
        "path": str(p),
        "name": p.name,
        "stem": p.stem,
        "dir": str(p.parent),
        "original_path": original or str(p),
        "library": library,
        "mode": mode or "",
        "in_size": in_size,
        "out_size": out_size,
        "saved": max(0, in_size - out_size),
        "elapsed": round(elapsed, 2),
        "reasons": list(reasons or []),
        "at": time.time(),
    }
