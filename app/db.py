"""SQLite state.

Three jobs: remember what every file looked like so an unchanged file is never
re-probed, record what was done to it, and answer the queries the web panel
asks (filter, search, paginate, per-file detail).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    name          TEXT,
    library       TEXT,
    size          INTEGER NOT NULL DEFAULT 0,
    mtime         REAL    NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL,   -- pending|queued|running|skip|done|failed
    reasons       TEXT,               -- json list
    plan          TEXT,               -- json dict, full stream plan
    height        INTEGER,
    video_codec   TEXT,
    last_checked  REAL,
    last_run      REAL,
    in_size       INTEGER,
    out_size      INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE INDEX IF NOT EXISTS files_name   ON files(name);
CREATE INDEX IF NOT EXISTS files_library ON files(library);

CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT NOT NULL,
    name      TEXT,
    started   REAL NOT NULL,
    finished  REAL,
    status    TEXT NOT NULL,          -- running|done|rejected|failed|cancelled
    in_size   INTEGER,
    out_size  INTEGER,
    elapsed   REAL,
    detail    TEXT,                   -- json dict of what was done
    error     TEXT
);
CREATE INDEX IF NOT EXISTS history_finished ON history(finished DESC);
CREATE INDEX IF NOT EXISTS history_path ON history(path);
"""

STATUSES = ("pending", "queued", "running", "skip", "done", "failed")


def _json(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value)


def _under(path: str, root: str) -> bool:
    """Is path inside root? Compared by path segment, not by prefix.

    A bare startswith would put /media/TV-4K under the root /media/TV.
    """
    target = os.path.normcase(os.path.normpath(path))
    r = os.path.normcase(os.path.normpath(root))
    return target == r or target.startswith(r.rstrip(os.sep) + os.sep)


def loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


class Db:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            # Nothing can still be running after a restart.
            self._conn.execute(
                "UPDATE files SET status = 'pending' "
                "WHERE status IN ('running', 'queued')"
            )
            self._conn.execute(
                "UPDATE history SET status = 'cancelled', finished = ?, "
                "error = 'interrupted by restart' WHERE finished IS NULL",
                (time.time(),),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- file state -------------------------------------------------------

    def get(self, path: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM files WHERE path = ?", (path,)
            ).fetchone()

    def is_cached(self, path: str, size: int, mtime: float) -> sqlite3.Row | None:
        """Row for an unchanged, already-settled file, else None.

        Size and mtime together are the cache key. A file that changed on disk
        gets re-probed; one that did not, and that already settled as done or
        skip, needs no further thought.
        """
        row = self.get(path)
        if row is None or row["status"] not in ("done", "skip"):
            return None
        if int(row["size"]) != int(size):
            return None
        if abs(float(row["mtime"]) - float(mtime)) > 1.0:
            return None
        return row

    def upsert(self, path: str, **cols: Any) -> None:
        cols.setdefault("name", Path(path).name)
        for key in ("reasons", "plan"):
            if key in cols:
                cols[key] = _json(cols[key])
        cols.setdefault("last_checked", time.time())
        keys = list(cols)
        sets = ", ".join(f"{k} = excluded.{k}" for k in keys)
        sql = (
            f"INSERT INTO files (path, {', '.join(keys)}) "
            f"VALUES (?, {', '.join('?' * len(keys))}) "
            f"ON CONFLICT(path) DO UPDATE SET {sets}"
        )
        with self._lock:
            self._conn.execute(sql, [path, *[cols[k] for k in keys]])
            self._conn.commit()

    def set_status(self, path: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE files SET status = ? WHERE path = ?", (status, path)
            )
            self._conn.commit()

    def bump_attempts(self, path: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE files SET attempts = attempts + 1 WHERE path = ?", (path,)
            )
            self._conn.commit()

    def reset_attempts(self, path: str | None = None) -> int:
        with self._lock:
            if path:
                cur = self._conn.execute(
                    "UPDATE files SET attempts = 0, error = NULL, "
                    "status = 'pending' WHERE path = ?", (path,)
                )
            else:
                cur = self._conn.execute(
                    "UPDATE files SET attempts = 0, error = NULL, "
                    "status = 'pending' WHERE status = 'failed'"
                )
            self._conn.commit()
            return cur.rowcount

    def queueable(self, max_attempts: int, limit: int = 100_000) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM files WHERE status = 'pending' AND attempts < ? "
                "ORDER BY size DESC LIMIT ?",
                (max_attempts, limit),
            ).fetchall()

    def list_files(self, status: str | None = None, query: str | None = None,
                   limit: int = 50, offset: int = 0, order: str = "size",
                   library: str | None = None) -> tuple[list[sqlite3.Row], int]:
        where, params = [], []
        if status and status != "all":
            where.append("status = ?")
            params.append(status)
        if library and library != "all":
            where.append("library = ?")
            params.append(library)
        if query:
            where.append("name LIKE ?")
            params.append(f"%{query}%")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        orders = {
            "size": "size DESC",
            "name": "name COLLATE NOCASE ASC",
            "checked": "last_checked DESC",
            "saved": "(in_size - out_size) DESC",
        }
        order_by = orders.get(order, orders["size"])
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) n FROM files {clause}", params
            ).fetchone()["n"]
            rows = self._conn.execute(
                f"SELECT * FROM files {clause} ORDER BY {order_by} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return rows, int(total)

    def forget_missing(self, seen: set[str], roots: list[str]) -> int:
        """Drop rows for files under the scanned roots that no longer exist."""
        removed = 0
        with self._lock:
            rows = self._conn.execute("SELECT path FROM files").fetchall()
            for r in rows:
                path = r["path"]
                if path in seen:
                    continue
                if roots and not any(_under(path, root) for root in roots):
                    continue
                if not Path(path).exists():
                    self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
                    removed += 1
            self._conn.commit()
        return removed

    def forget_unowned(self, is_owned: Callable[[str], bool]) -> int:
        """Drop rows for files no longer covered by any library.

        A row survives a scan only because something still routes to it.
        Narrow a library's paths, or delete a library, and its files stop
        being scanned but keep counting towards the tracked totals and keep
        getting queued, so the state has to go with them.
        """
        with self._lock:
            gone = [r["path"] for r
                    in self._conn.execute("SELECT path FROM files")
                    if not is_owned(r["path"])]
            for path in gone:
                self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
            self._conn.commit()
        return len(gone)

    def forget_library(self, lib_id: str) -> int:
        """Drop tracked state for a library that no longer exists."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM files WHERE library = ?", (lib_id,))
            self._conn.commit()
            return cur.rowcount

    def forget(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
            self._conn.commit()

    # --- history ----------------------------------------------------------

    def start_run(self, path: str, detail: Any = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO history (path, name, started, status, detail) "
                "VALUES (?, ?, ?, 'running', ?)",
                (path, Path(path).name, time.time(), _json(detail)),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, status: str, in_size: int = 0,
                   out_size: int = 0, elapsed: float = 0.0,
                   error: str | None = None, detail: Any = None) -> None:
        with self._lock:
            if detail is None:
                self._conn.execute(
                    "UPDATE history SET finished = ?, status = ?, in_size = ?, "
                    "out_size = ?, elapsed = ?, error = ? WHERE id = ?",
                    (time.time(), status, in_size, out_size, elapsed, error, run_id),
                )
            else:
                self._conn.execute(
                    "UPDATE history SET finished = ?, status = ?, in_size = ?, "
                    "out_size = ?, elapsed = ?, error = ?, detail = ? WHERE id = ?",
                    (time.time(), status, in_size, out_size, elapsed, error,
                     _json(detail), run_id),
                )
            self._conn.commit()

    def history(self, limit: int = 50, offset: int = 0,
                path: str | None = None) -> tuple[list[sqlite3.Row], int]:
        clause, params = "WHERE finished IS NOT NULL", []
        if path:
            clause += " AND path = ?"
            params.append(path)
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) n FROM history {clause}", params
            ).fetchone()["n"]
            rows = self._conn.execute(
                f"SELECT * FROM history {clause} ORDER BY finished DESC "
                f"LIMIT ? OFFSET ?", [*params, limit, offset],
            ).fetchall()
        return rows, int(total)

    def clear_history(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM history WHERE finished IS NOT NULL")
            self._conn.commit()
            return cur.rowcount

    def library_stats(self) -> dict[str, dict[str, Any]]:
        """Counts and reclaimed bytes per library, for the Libraries tab."""
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for r in self._conn.execute(
                "SELECT COALESCE(library, '') lib, status, COUNT(*) n, "
                "COALESCE(SUM(size), 0) bytes FROM files GROUP BY lib, status"
            ):
                entry = out.setdefault(
                    r["lib"], {"total": 0, "bytes": 0, "counts": {}, "saved": 0}
                )
                entry["counts"][r["status"]] = r["n"]
                entry["total"] += r["n"]
                entry["bytes"] += r["bytes"]
            for r in self._conn.execute(
                "SELECT COALESCE(f.library, '') lib, "
                "COALESCE(SUM(h.in_size - h.out_size), 0) saved "
                "FROM history h LEFT JOIN files f ON f.path = h.path "
                "WHERE h.status = 'done' GROUP BY lib"
            ):
                if r["lib"] in out:
                    out[r["lib"]]["saved"] = int(r["saved"])
        return out

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts = {
                r["status"]: r["n"]
                for r in self._conn.execute(
                    "SELECT status, COUNT(*) n FROM files GROUP BY status"
                )
            }
            row = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(in_size - out_size), 0) saved, "
                "COALESCE(SUM(elapsed), 0) secs FROM history WHERE status = 'done'"
            ).fetchone()
            pending_bytes = self._conn.execute(
                "SELECT COALESCE(SUM(size), 0) b FROM files WHERE status = 'pending'"
            ).fetchone()["b"]
        return {
            "counts": {s: counts.get(s, 0) for s in STATUSES},
            "total": sum(counts.values()),
            "encoded": int(row["n"]),
            "bytes_saved": int(row["saved"]),
            "encode_seconds": float(row["secs"]),
            "pending_bytes": int(pending_bytes),
        }
