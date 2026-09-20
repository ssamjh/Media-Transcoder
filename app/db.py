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

-- Durable work state.  These tables intentionally do not depend on the
-- in-memory Engine queue: a process can be stopped between any two stages
-- and recover the work on its next start.
CREATE TABLE IF NOT EXISTS processing_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key        TEXT NOT NULL,
    source_path       TEXT NOT NULL,
    source_provider   TEXT,
    source_metadata   TEXT,
    mode              TEXT,
    stage             TEXT NOT NULL DEFAULT 'queued',
    status            TEXT NOT NULL DEFAULT 'pending',
    retries           INTEGER NOT NULL DEFAULT 0,
    next_attempt      REAL,
    error             TEXT,
    final_path        TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL,
    import_request_id INTEGER
);

CREATE TABLE IF NOT EXISTS import_requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key        TEXT NOT NULL,
    source_path       TEXT NOT NULL,
    source_provider   TEXT,
    source_metadata   TEXT,
    mode              TEXT,
    stage             TEXT NOT NULL DEFAULT 'received',
    status            TEXT NOT NULL DEFAULT 'pending',
    retries           INTEGER NOT NULL DEFAULT 0,
    next_attempt      REAL,
    error             TEXT,
    final_path        TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL,
    job_id            INTEGER
);

CREATE TABLE IF NOT EXISTS outbox_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key   TEXT NOT NULL,
    action       TEXT NOT NULL,
    job_id       INTEGER,
    payload      TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    retries      INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL,
    error        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    claimed_at   REAL,
    finished_at  REAL
);
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


# Columns are listed separately from SCHEMA so a database created by an older
# build (or by an interrupted migration) can be upgraded without dropping
# any state.  SQLite has no ``ADD COLUMN IF NOT EXISTS``; checking PRAGMA
# first gives us the same safe behaviour.
_DURABLE_COLUMNS: dict[str, dict[str, str]] = {
    "processing_jobs": {
        "dedupe_key": "TEXT",
        "source_path": "TEXT",
        "source_provider": "TEXT",
        "source_metadata": "TEXT",
        "mode": "TEXT",
        "stage": "TEXT NOT NULL DEFAULT 'queued'",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "retries": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt": "REAL",
        "error": "TEXT",
        "final_path": "TEXT",
        "created_at": "REAL",
        "updated_at": "REAL",
        "started_at": "REAL",
        "finished_at": "REAL",
        "import_request_id": "INTEGER",
    },
    "import_requests": {
        "dedupe_key": "TEXT",
        "source_path": "TEXT",
        "source_provider": "TEXT",
        "source_metadata": "TEXT",
        "mode": "TEXT",
        "stage": "TEXT NOT NULL DEFAULT 'received'",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "retries": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt": "REAL",
        "error": "TEXT",
        "final_path": "TEXT",
        "created_at": "REAL",
        "updated_at": "REAL",
        "started_at": "REAL",
        "finished_at": "REAL",
        "job_id": "INTEGER",
    },
    "outbox_actions": {
        "dedupe_key": "TEXT",
        "action": "TEXT",
        "job_id": "INTEGER",
        "payload": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "retries": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt": "REAL",
        "error": "TEXT",
        "created_at": "REAL",
        "updated_at": "REAL",
        "claimed_at": "REAL",
        "finished_at": "REAL",
    },
}


class Db:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        with self._lock:
            self._connect()

    def _connect(self) -> None:
        """Connect and bring the file up to date. Call with the lock held.

        Separate from __init__ because restoring a backup replaces the file
        under a live process: every other object holds this Db, so the
        connection is swapped inside it rather than the Db being rebuilt.
        """
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._migrate_durable_schema()
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
        self._repair_history_paths()
        self._recover_durable_state()
        self._conn.commit()

    def _migrate_durable_schema(self) -> None:
        """Add durable-work columns without disturbing an existing DB.

        ``CREATE TABLE IF NOT EXISTS`` handles new installations.  The PRAGMA
        pass is needed for databases that briefly existed with an earlier,
        smaller durable schema; every added column is nullable or has a safe
        default so ALTER TABLE remains valid on SQLite.
        """
        for table, columns in _DURABLE_COLUMNS.items():
            existing = {
                row["name"] for row in self._conn.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            for name, definition in columns.items():
                if name not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )
        # Index creation is deliberately after ALTER TABLE.  A hand-created
        # legacy table may have had only its primary key columns.  A duplicate
        # dedupe key should remain readable and writable, so the unique index
        # is best-effort; new tables have idempotency enforced by the methods.
        for sql in (
            "CREATE INDEX IF NOT EXISTS processing_jobs_status_attempt "
            "ON processing_jobs(status, next_attempt)",
            "CREATE INDEX IF NOT EXISTS processing_jobs_source_path "
            "ON processing_jobs(source_path)",
            "CREATE INDEX IF NOT EXISTS import_requests_status_attempt "
            "ON import_requests(status, next_attempt)",
            "CREATE INDEX IF NOT EXISTS import_requests_source_path "
            "ON import_requests(source_path)",
            "CREATE INDEX IF NOT EXISTS outbox_actions_status_attempt "
            "ON outbox_actions(status, next_attempt)",
            "CREATE INDEX IF NOT EXISTS outbox_actions_job_id "
            "ON outbox_actions(job_id)",
        ):
            self._conn.execute(sql)
        try:
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS outbox_actions_dedupe_action "
                "ON outbox_actions(dedupe_key, action)"
            )
        except sqlite3.IntegrityError:
            # Keep a legacy database usable.  enqueue_outbox performs its own
            # lookup before inserting and therefore does not require this
            # optimisation index.
            pass

    def _recover_durable_state(self) -> None:
        """Make work owned by a crashed process eligible after a restart."""
        now = time.time()
        for table in ("processing_jobs", "import_requests"):
            self._conn.execute(
                f"UPDATE {table} SET status = 'pending', next_attempt = ?, "
                "updated_at = ?, error = COALESCE(error, "
                "'interrupted by restart') WHERE status = 'running'",
                (now, now),
            )
        self._conn.execute(
            "UPDATE outbox_actions SET status = 'pending', next_attempt = ?, "
            "updated_at = ?, error = COALESCE(error, 'interrupted by restart') "
            "WHERE status = 'running'",
            (now, now),
        )

    def _repair_history_paths(self) -> None:
        """Relink old successful runs whose output changed extension.

        Earlier versions left history pointing at the source path even after
        replacing (for example) ``Film.avi`` with the tracked ``Film.mkv``.
        The plan saved with the run contains the output container, so repair
        those unambiguous records when the database is opened.
        """
        rows = self._conn.execute(
            "SELECT h.id, h.path, h.detail FROM history h "
            "LEFT JOIN files f ON f.path = h.path "
            "WHERE h.status = 'done' AND f.path IS NULL"
        ).fetchall()
        for row in rows:
            detail = loads(row["detail"], {})
            container = detail.get("container") if isinstance(detail, dict) else None
            if not isinstance(container, str) or not container:
                continue
            final = str(Path(row["path"]).with_suffix("." + container.lstrip(".")))
            if final == row["path"]:
                continue
            tracked = self._conn.execute(
                "SELECT 1 FROM files WHERE path = ?", (final,)
            ).fetchone()
            if tracked:
                self._conn.execute(
                    "UPDATE history SET path = ?, name = ? WHERE id = ?",
                    (final, Path(final).name, row["id"]),
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def swap(self, replace: Callable[[], Any]) -> Any:
        """Close, let `replace` put a different file at `path`, open again.

        The whole swap happens under the lock every other statement takes,
        so a worker or the workflow thread waits for the new connection
        instead of meeting the closed one - which would otherwise kill that
        thread for the rest of the process's life. Opening again performs
        the same 'running' row reset a restart does, applied to whatever
        the restored file was holding.
        """
        with self._lock:
            self._conn.close()
            try:
                return replace()
            finally:
                self._connect()

    def backup_to(self, dest: str | Path) -> None:
        """Write a consistent snapshot of this database to `dest`.

        SQLite's own backup API, not a file copy: with WAL journalling the
        file on disk is only half the state, so copying it while anything
        is mid-transaction produces exactly the corrupt database a backup
        is supposed to insure against. This also means the snapshot needs
        no sidecar files of its own. The write happens under the same lock
        every other statement takes, so a worker cannot commit into the
        middle of it.
        """
        target = sqlite3.connect(str(dest))
        try:
            with self._lock:
                self._conn.backup(target)
            # The snapshot inherits this database's WAL mode, and a WAL
            # database is not one file: anything that reads it drops a -wal
            # and a -shm beside it, and copying it away without them loses
            # data. A snapshot is an archive, never a live database, so it
            # is switched to a rollback journal and stays self-contained.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()

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

    # --- durable imports and processing jobs -----------------------------

    @staticmethod
    def _durable_args(source_provider: str | None, source_metadata: Any,
                      mode: str | None, provider: str | None,
                      metadata: Any, mode_id: str | None) -> tuple[str | None, str | None, str | None]:
        """Accept the short aliases used by webhook integrations."""
        if source_provider is None:
            source_provider = provider
        if source_metadata is None:
            source_metadata = metadata
        if mode is None:
            mode = mode_id
        return source_provider, _json(source_metadata), mode

    @staticmethod
    def _durable_selector(identifier: int | str | None,
                          dedupe_key: str | None,
                          source_path: str | None) -> tuple[str, Any] | None:
        if dedupe_key is not None:
            return "dedupe_key = ?", dedupe_key
        if source_path is not None:
            return "source_path = ?", source_path
        if identifier is None:
            return None
        if isinstance(identifier, int):
            return "id = ?", identifier
        return "dedupe_key = ?", identifier

    def create_job(
        self, dedupe_key: str | None = None, source_path: str | None = None,
        *, source_provider: str | None = None, source_metadata: Any = None,
        mode: str | None = None, stage: str = "queued",
        status: str = "pending", retries: int = 0,
        next_attempt: float | None = None, import_request_id: int | None = None,
        provider: str | None = None, metadata: Any = None,
        mode_id: str | None = None, path: str | None = None,
        now: float | None = None,
    ) -> sqlite3.Row:
        """Insert or return a processing job, keyed by ``dedupe_key``.

        Repeating an import is deliberately idempotent: an existing job is
        refreshed with the request's source/mode metadata, but its status and
        retry count are left intact unless the caller explicitly updates them
        through :meth:`update_job`.
        """
        if source_path is None:
            source_path = path
        if source_path is None:
            source_path = dedupe_key
        if not source_path:
            raise ValueError("source_path is required")
        if dedupe_key is None:
            dedupe_key = source_path
        source_provider, source_metadata, mode = self._durable_args(
            source_provider, source_metadata, mode, provider, metadata, mode_id
        )
        stamp = time.time() if now is None else float(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM processing_jobs WHERE dedupe_key = ? "
                "ORDER BY id LIMIT 1", (dedupe_key,)
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE processing_jobs SET source_path = ?, "
                    "source_provider = COALESCE(?, source_provider), "
                    "source_metadata = COALESCE(?, source_metadata), "
                    "mode = COALESCE(?, mode), updated_at = ? WHERE id = ?",
                    (source_path, source_provider, source_metadata, mode,
                     stamp, row["id"]),
                )
                job_id = row["id"]
            else:
                cur = self._conn.execute(
                    "INSERT INTO processing_jobs "
                    "(dedupe_key, source_path, source_provider, source_metadata, "
                    "mode, stage, status, retries, next_attempt, created_at, "
                    "updated_at, import_request_id) VALUES (?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?)",
                    (dedupe_key, source_path, source_provider, source_metadata,
                     mode, stage, status, retries, next_attempt, stamp, stamp,
                     import_request_id),
                )
                job_id = int(cur.lastrowid or 0)
            self._conn.commit()
            return self._conn.execute(
                "SELECT * FROM processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()

    # Names used by integrations and older callers; all share the same
    # implementation and therefore the same idempotency guarantees.
    enqueue_job = create_job
    create_processing_job = create_job
    enqueue_processing_job = create_job
    upsert_processing_job = create_job

    def get_job(self, identifier: int | str | None = None, *,
                dedupe_key: str | None = None,
                source_path: str | None = None) -> sqlite3.Row | None:
        selector = self._durable_selector(identifier, dedupe_key, source_path)
        if selector is None:
            return None
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM processing_jobs WHERE {selector[0]} "
                "ORDER BY id DESC LIMIT 1", (selector[1],)
            ).fetchone()

    def jobs_for_path(self, source_path: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM processing_jobs WHERE source_path = ? "
                "ORDER BY created_at DESC, id DESC", (source_path,)
            ).fetchall()

    def list_jobs(self, status: str | None = None, *, limit: int = 100,
                  offset: int = 0, due: bool = False,
                  now: float | None = None) -> list[sqlite3.Row]:
        where, params = [], []
        if status and status != "all":
            where.append("status = ?")
            params.append(status)
        if due:
            where.append("status IN ('pending', 'queued', 'retry')")
            where.append("(next_attempt IS NULL OR next_attempt <= ?)")
            params.append(time.time() if now is None else float(now))
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM processing_jobs" + clause +
                " ORDER BY COALESCE(next_attempt, 0), created_at, id "
                "LIMIT ? OFFSET ?", [*params, limit, offset]
            ).fetchall()

    def recoverable_jobs(self, *, limit: int = 100,
                         now: float | None = None) -> list[sqlite3.Row]:
        return self.list_jobs(limit=limit, due=True, now=now)

    list_recoverable_jobs = recoverable_jobs
    get_processing_job = get_job
    jobs_for_source_path = jobs_for_path

    def claim_job(self, identifier: int | str | None = None, *,
                  dedupe_key: str | None = None,
                  source_path: str | None = None,
                  now: float | None = None) -> sqlite3.Row | None:
        """Atomically claim one due job and increment its retry counter."""
        stamp = time.time() if now is None else float(now)
        selector = self._durable_selector(identifier, dedupe_key, source_path)
        with self._lock:
            # Lock the SQLite write transaction before selecting.  The
            # process-local mutex protects threads; BEGIN IMMEDIATE also
            # prevents two Db connections from claiming the same row.
            self._conn.execute("BEGIN IMMEDIATE")
            params: list[Any] = [stamp]
            extra = ""
            if selector:
                extra = f" AND {selector[0]}"
                params.append(selector[1])
            row = self._conn.execute(
                "SELECT id FROM processing_jobs WHERE status IN "
                "('pending', 'queued', 'retry') AND "
                "(next_attempt IS NULL OR next_attempt <= ?)" + extra +
                " ORDER BY COALESCE(next_attempt, 0), created_at, id LIMIT 1",
                params,
            ).fetchone()
            if not row:
                self._conn.rollback()
                return None
            self._conn.execute(
                "UPDATE processing_jobs SET status = 'running', retries = "
                "retries + 1, started_at = ?, updated_at = ?, error = NULL "
                "WHERE id = ?", (stamp, stamp, row["id"]),
            )
            self._conn.commit()
            return self._conn.execute(
                "SELECT * FROM processing_jobs WHERE id = ?", (row["id"],)
            ).fetchone()

    claim_processing_job = claim_job

    def update_job(self, identifier: int | str, **changes: Any) -> sqlite3.Row | None:
        """Persist stage/status/progress fields and return the updated job."""
        if "mode_id" in changes and "mode" not in changes:
            changes["mode"] = changes.pop("mode_id")
        if "provider" in changes and "source_provider" not in changes:
            changes["source_provider"] = changes.pop("provider")
        if "metadata" in changes and "source_metadata" not in changes:
            changes["source_metadata"] = changes.pop("metadata")
        for key in ("source_metadata",):
            if key in changes:
                changes[key] = _json(changes[key])
        allowed = {
            "source_path", "source_provider", "source_metadata", "mode",
            "stage", "status", "retries", "next_attempt", "error",
            "final_path", "started_at", "finished_at", "import_request_id",
        }
        updates = [(key, value) for key, value in changes.items()
                   if key in allowed]
        if not updates:
            return self.get_job(identifier)
        stamp = time.time()
        if "status" in changes and changes["status"] in {
            "done", "failed", "cancelled", "complete"
        } and "finished_at" not in changes:
            updates.append(("finished_at", stamp))
        updates.append(("updated_at", stamp))
        selector = self._durable_selector(identifier, None, None)
        assert selector is not None
        with self._lock:
            self._conn.execute(
                f"UPDATE processing_jobs SET " +
                ", ".join(f"{key} = ?" for key, _ in updates) +
                f" WHERE {selector[0]}",
                [value for _, value in updates] + [selector[1]],
            )
            self._conn.commit()
            return self._conn.execute(
                f"SELECT * FROM processing_jobs WHERE {selector[0]} "
                "ORDER BY id DESC LIMIT 1", (selector[1],)
            ).fetchone()

    def set_job_stage(self, identifier: int | str, stage: str, **changes: Any) -> sqlite3.Row | None:
        changes["stage"] = stage
        return self.update_job(identifier, **changes)

    update_processing_job = update_job
    set_processing_job_stage = set_job_stage

    def set_job_final_path(self, identifier: int | str,
                           final_path: str | None) -> sqlite3.Row | None:
        return self.update_job(identifier, final_path=final_path)

    set_processing_job_final_path = set_job_final_path

    def finish_job(self, identifier: int | str, status: str = "done",
                   *, final_path: str | None = None,
                   error: str | None = None) -> sqlite3.Row | None:
        return self.update_job(identifier, status=status, final_path=final_path,
                               error=error)

    def create_import_request(
        self, dedupe_key: str | None = None, source_path: str | None = None,
        *, source_provider: str | None = None, source_metadata: Any = None,
        mode: str | None = None, stage: str = "received",
        status: str = "pending", retries: int = 0,
        next_attempt: float | None = None, job_id: int | None = None,
        provider: str | None = None, metadata: Any = None,
        mode_id: str | None = None, path: str | None = None,
        now: float | None = None,
    ) -> sqlite3.Row:
        """Persist an import webhook/request using the same durable fields."""
        if source_path is None:
            source_path = path
        if source_path is None:
            source_path = dedupe_key
        if not source_path:
            raise ValueError("source_path is required")
        if dedupe_key is None:
            dedupe_key = source_path
        source_provider, source_metadata, mode = self._durable_args(
            source_provider, source_metadata, mode, provider, metadata, mode_id
        )
        stamp = time.time() if now is None else float(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM import_requests WHERE dedupe_key = ? "
                "ORDER BY id LIMIT 1", (dedupe_key,)
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE import_requests SET source_path = ?, "
                    "source_provider = COALESCE(?, source_provider), "
                    "source_metadata = COALESCE(?, source_metadata), "
                    "mode = COALESCE(?, mode), job_id = COALESCE(?, job_id), "
                    "updated_at = ? WHERE id = ?",
                    (source_path, source_provider, source_metadata, mode, job_id,
                     stamp, row["id"]),
                )
                request_id = row["id"]
            else:
                cur = self._conn.execute(
                    "INSERT INTO import_requests "
                    "(dedupe_key, source_path, source_provider, source_metadata, "
                    "mode, stage, status, retries, next_attempt, created_at, "
                    "updated_at, job_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (dedupe_key, source_path, source_provider, source_metadata,
                     mode, stage, status, retries, next_attempt, stamp, stamp,
                     job_id),
                )
                request_id = int(cur.lastrowid or 0)
            self._conn.commit()
            return self._conn.execute(
                "SELECT * FROM import_requests WHERE id = ?", (request_id,)
            ).fetchone()

    enqueue_import = create_import_request
    create_import = create_import_request
    upsert_import_request = create_import_request

    def get_import_request(self, identifier: int | str | None = None, *,
                           dedupe_key: str | None = None,
                           source_path: str | None = None) -> sqlite3.Row | None:
        selector = self._durable_selector(identifier, dedupe_key, source_path)
        if selector is None:
            return None
        with self._lock:
            return self._conn.execute(
                f"SELECT * FROM import_requests WHERE {selector[0]} "
                "ORDER BY id DESC LIMIT 1", (selector[1],)
            ).fetchone()

    def list_import_requests(self, status: str | None = None, *,
                             limit: int = 100, offset: int = 0) -> list[sqlite3.Row]:
        clause, params = "", []
        if status and status != "all":
            clause, params = " WHERE status = ?", [status]
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM import_requests" + clause +
                " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()

    get_import = get_import_request
    list_imports = list_import_requests

    def update_import_request(self, identifier: int | str, **changes: Any) -> sqlite3.Row | None:
        """Update an import request without requiring a separate ORM object."""
        if "mode_id" in changes and "mode" not in changes:
            changes["mode"] = changes.pop("mode_id")
        if "provider" in changes and "source_provider" not in changes:
            changes["source_provider"] = changes.pop("provider")
        if "metadata" in changes and "source_metadata" not in changes:
            changes["source_metadata"] = changes.pop("metadata")
        if "source_metadata" in changes:
            changes["source_metadata"] = _json(changes["source_metadata"])
        allowed = {
            "source_path", "source_provider", "source_metadata", "mode",
            "stage", "status", "retries", "next_attempt", "error",
            "final_path", "started_at", "finished_at", "job_id",
        }
        updates = [(key, value) for key, value in changes.items()
                   if key in allowed]
        if not updates:
            return self.get_import_request(identifier)
        stamp = time.time()
        if "status" in changes and changes["status"] in {
            "done", "failed", "cancelled", "complete"
        } and "finished_at" not in changes:
            updates.append(("finished_at", stamp))
        updates.append(("updated_at", stamp))
        selector = self._durable_selector(identifier, None, None)
        assert selector is not None
        with self._lock:
            self._conn.execute(
                f"UPDATE import_requests SET " +
                ", ".join(f"{key} = ?" for key, _ in updates) +
                f" WHERE {selector[0]}",
                [value for _, value in updates] + [selector[1]],
            )
            self._conn.commit()
            return self._conn.execute(
                f"SELECT * FROM import_requests WHERE {selector[0]} "
                "ORDER BY id DESC LIMIT 1", (selector[1],)
            ).fetchone()

    def enqueue_outbox(
        self, dedupe_key: str, action: str | None = None, payload: Any = None, *,
        job_id: int | None = None, status: str = "pending",
        next_attempt: float | None = None, now: float | None = None,
        action_type: str | None = None, kind: str | None = None,
    ) -> sqlite3.Row:
        """Insert one outbound action, deduplicated by key and action."""
        action = action or action_type or kind
        if not action:
            raise ValueError("action is required")
        stamp = time.time() if now is None else float(now)
        encoded = _json(payload)
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM outbox_actions WHERE dedupe_key = ? AND action = ? "
                "ORDER BY id LIMIT 1", (dedupe_key, action)
            ).fetchone()
            if row:
                # A delivered notification must remain delivered when the
                # originating job is retried; do not resurrect it.
                existing = self._conn.execute(
                    "SELECT status FROM outbox_actions WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                if existing and existing["status"] not in {"done", "complete"}:
                    self._conn.execute(
                        "UPDATE outbox_actions SET payload = COALESCE(?, payload), "
                        "job_id = COALESCE(?, job_id), updated_at = ? WHERE id = ?",
                        (encoded, job_id, stamp, row["id"]),
                    )
                action_id = row["id"]
            else:
                cur = self._conn.execute(
                    "INSERT INTO outbox_actions "
                    "(dedupe_key, action, job_id, payload, status, next_attempt, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (dedupe_key, action, job_id, encoded, status, next_attempt,
                     stamp, stamp),
                )
                action_id = int(cur.lastrowid or 0)
            self._conn.commit()
            return self._conn.execute(
                "SELECT * FROM outbox_actions WHERE id = ?", (action_id,)
            ).fetchone()

    add_outbox_action = enqueue_outbox
    enqueue_outbox_action = enqueue_outbox

    def transition_job_to_outbox(self, job_id: int, *, stage: str,
                                 final_path: str, action: str,
                                 payload: Any) -> sqlite3.Row:
        """Atomically park a processed job and create its first outbox row.

        This is the encode/outbox boundary.  A crash can leave the work on
        either side, never in the gap between them where recovery would run
        FFmpeg again or lose the downstream hand-off.
        """
        stamp = time.time()
        encoded = _json(payload)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            job = self._conn.execute(
                "SELECT * FROM processing_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                self._conn.rollback()
                raise LookupError(f"no such processing job: {job_id}")
            row = self._conn.execute(
                "SELECT id, status FROM outbox_actions WHERE dedupe_key = ? "
                "AND action = ? ORDER BY id LIMIT 1",
                (job["dedupe_key"], action),
            ).fetchone()
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO outbox_actions "
                    "(dedupe_key, action, job_id, payload, status, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                    (job["dedupe_key"], action, job_id, encoded, stamp, stamp),
                )
                action_id = int(cur.lastrowid or 0)
            else:
                action_id = int(row["id"])
                if row["status"] not in {"done", "complete"}:
                    self._conn.execute(
                        "UPDATE outbox_actions SET payload = ?, job_id = ?, "
                        "updated_at = ? WHERE id = ?",
                        (encoded, job_id, stamp, action_id),
                    )
            self._conn.execute(
                "UPDATE processing_jobs SET stage = ?, status = 'waiting', "
                "final_path = ?, error = NULL, updated_at = ? WHERE id = ?",
                (stage, final_path, stamp, job_id),
            )
            if job["import_request_id"]:
                self._conn.execute(
                    "UPDATE import_requests SET stage = ?, status = 'waiting', "
                    "final_path = ?, error = NULL, updated_at = ? WHERE id = ?",
                    (stage, final_path, stamp, job["import_request_id"]),
                )
            self._conn.commit()
            return self._conn.execute(
                "SELECT * FROM outbox_actions WHERE id = ?", (action_id,)
            ).fetchone()

    def get_outbox(self, identifier: int | None = None, *,
                   dedupe_key: str | None = None,
                   action: str | None = None) -> sqlite3.Row | None:
        with self._lock:
            if identifier is not None:
                return self._conn.execute(
                    "SELECT * FROM outbox_actions WHERE id = ?", (identifier,)
                ).fetchone()
            if dedupe_key is None:
                return None
            if action is None:
                return self._conn.execute(
                    "SELECT * FROM outbox_actions WHERE dedupe_key = ? "
                    "ORDER BY id DESC LIMIT 1", (dedupe_key,)
                ).fetchone()
            return self._conn.execute(
                "SELECT * FROM outbox_actions WHERE dedupe_key = ? AND action = ? "
                "ORDER BY id DESC LIMIT 1", (dedupe_key, action)
            ).fetchone()

    def list_outbox(self, status: str | None = None, *, limit: int = 100,
                    offset: int = 0, due: bool = False,
                    now: float | None = None) -> list[sqlite3.Row]:
        where, params = [], []
        if status and status != "all":
            where.append("status = ?")
            params.append(status)
        if due:
            where.append("status IN ('pending', 'queued', 'retry')")
            where.append("(next_attempt IS NULL OR next_attempt <= ?)")
            params.append(time.time() if now is None else float(now))
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM outbox_actions" + clause +
                " ORDER BY COALESCE(next_attempt, 0), created_at, id "
                "LIMIT ? OFFSET ?", [*params, limit, offset]
            ).fetchall()

    get_outbox_action = get_outbox
    list_outbox_actions = list_outbox

    def claim_outbox(self, limit: int = 1, *,
                     now: float | None = None) -> sqlite3.Row | list[sqlite3.Row] | None:
        """Atomically mark due outbox rows running and increment retries.

        The default returns one row for the common worker-loop case.  A
        positive ``limit`` greater than one returns a list for batch delivery.
        """
        if limit < 1:
            return [] if limit != 1 else None
        stamp = time.time() if now is None else float(now)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                "SELECT id FROM outbox_actions WHERE status IN "
                "('pending', 'queued', 'retry') AND "
                "(next_attempt IS NULL OR next_attempt <= ?) "
                "ORDER BY COALESCE(next_attempt, 0), created_at, id LIMIT ?",
                (stamp, limit),
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE outbox_actions SET status = 'running', retries = "
                    "retries + 1, claimed_at = ?, updated_at = ?, error = NULL "
                    "WHERE id = ?", (stamp, stamp, row["id"]),
                )
            self._conn.commit()
            claimed = [self._conn.execute(
                "SELECT * FROM outbox_actions WHERE id = ?", (row["id"],)
            ).fetchone() for row in rows]
        if limit == 1:
            return claimed[0] if claimed else None
        return claimed

    def complete_outbox(self, identifier: int, *, now: float | None = None,
                        payload: Any = None) -> sqlite3.Row | None:
        stamp = time.time() if now is None else float(now)
        updates = ["status = 'done'", "finished_at = ?", "updated_at = ?",
                   "next_attempt = NULL", "error = NULL"]
        values: list[Any] = [stamp, stamp]
        if payload is not None:
            updates.append("payload = ?")
            values.append(_json(payload))
        values.append(identifier)
        with self._lock:
            self._conn.execute(
                "UPDATE outbox_actions SET " + ", ".join(updates) +
                " WHERE id = ?", values
            )
            self._conn.commit()
            return self._conn.execute(
                "SELECT * FROM outbox_actions WHERE id = ?", (identifier,)
            ).fetchone()

    def fail_outbox(self, identifier: int, error: str,
                    *, next_attempt: float | None = None,
                    retry_at: float | None = None,
                    now: float | None = None) -> sqlite3.Row | None:
        stamp = time.time() if now is None else float(now)
        due = next_attempt if next_attempt is not None else retry_at
        status = "pending" if due is not None else "failed"
        with self._lock:
            self._conn.execute(
                "UPDATE outbox_actions SET status = ?, error = ?, "
                "next_attempt = ?, updated_at = ?, finished_at = "
                "CASE WHEN ? IS NULL THEN ? ELSE NULL END WHERE id = ?",
                (status, error, due, stamp, due, stamp, identifier),
            )
            self._conn.commit()
            return self._conn.execute(
                "SELECT * FROM outbox_actions WHERE id = ?", (identifier,)
            ).fetchone()

    claim_outbox_action = claim_outbox
    complete_outbox_action = complete_outbox
    fail_outbox_action = fail_outbox

    def retry_outbox(self, job_id: int | None = None) -> int:
        """Make failed delivery actions eligible again."""
        stamp = time.time()
        clause, params = "status = 'failed'", []
        if job_id is not None:
            clause += " AND job_id = ?"
            params.append(job_id)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE outbox_actions SET status = 'pending', retries = 0, "
                "next_attempt = ?, error = NULL, finished_at = NULL, "
                "updated_at = ? WHERE " + clause,
                (stamp, stamp, *params),
            )
            self._conn.commit()
            return cur.rowcount

    def workflow_stats(self) -> dict[str, Any]:
        """Small status summary for the panel/API."""
        with self._lock:
            jobs = {r["status"]: int(r["n"]) for r in self._conn.execute(
                "SELECT status, COUNT(*) n FROM processing_jobs GROUP BY status"
            )}
            outbox = {r["status"]: int(r["n"]) for r in self._conn.execute(
                "SELECT status, COUNT(*) n FROM outbox_actions GROUP BY status"
            )}
        return {"jobs": jobs, "outbox": outbox}

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
                   error: str | None = None, detail: Any = None,
                   final_path: str | None = None) -> None:
        updates = ["finished = ?", "status = ?", "in_size = ?",
                   "out_size = ?", "elapsed = ?", "error = ?"]
        values: list[Any] = [time.time(), status, in_size, out_size, elapsed, error]
        if detail is not None:
            updates.append("detail = ?")
            values.append(_json(detail))
        if final_path is not None:
            updates.extend(["path = ?", "name = ?"])
            values.extend([final_path, Path(final_path).name])
        values.append(run_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE history SET {', '.join(updates)} WHERE id = ?", values
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
