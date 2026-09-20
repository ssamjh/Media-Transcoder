"""Daily snapshots of the state database.

The state database is the one thing here that cannot be rebuilt from the
files on disk: per-file verdicts, history and the durable work queue all
live in it, and a bad shutdown or a dying disk can leave it unreadable. So
a snapshot is kept beside it in the config directory and the last few days
survive.

Two details carry the whole design:

- A snapshot is named for the *day* it was taken, so a second run on the
  same day replaces that day's file instead of eating a retention slot.
  ``keep = 7`` therefore means seven days of history no matter how often
  the daemon restarts or how the interval is set - which is what anyone
  asking for "the last 7 days" actually wants.
- A snapshot is written through SQLite's own backup API (``Db.backup_to``),
  never by copying the file. A copy taken while a WAL transaction is in
  flight is precisely the corrupt database this exists to protect against.

Names are ISO dates, so sorting them lexically sorts them chronologically,
which is the only ordering anything here needs.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("standardisarr.backup")

PREFIX = "state-"
SUFFIX = ".db"
# Snapshots are written under this suffix and renamed into place, so a run
# killed mid-copy never leaves a half-written file that looks like a good
# one. Leftovers are swept the next time one is taken - the same reasoning
# as ffmpeg.sweep_scratch.
PARTIAL = ".partial"


def backup_dir(cfg: Any) -> Path:
    """Where snapshots live: beside the state database unless told otherwise."""
    configured = (cfg.backup.dir or "").strip()
    if configured:
        return Path(configured)
    return Path(cfg.state_db).parent / "backups"


def stamp(when: float | None = None) -> str:
    return datetime.fromtimestamp(
        time.time() if when is None else when).strftime("%Y-%m-%d")


def list_backups(directory: str | Path) -> list[Path]:
    """Existing snapshots, oldest first."""
    d = Path(directory)
    if not d.is_dir():
        return []
    return sorted(
        (p for p in d.iterdir()
         if p.is_file() and p.name.startswith(PREFIX) and p.name.endswith(SUFFIX)),
        key=lambda p: p.name,
    )


def newest(directory: str | Path) -> Path | None:
    found = list_backups(directory)
    return found[-1] if found else None


def next_due(directory: str | Path, interval_hours: float,
             now: float | None = None) -> float:
    """When the next snapshot is owed.

    Judged from the newest snapshot on disk rather than from anything held
    in memory, so a daemon that is restarted six times a day still takes
    one backup a day instead of six.
    """
    now = time.time() if now is None else now
    latest = newest(directory)
    if latest is None:
        return now
    try:
        return latest.stat().st_mtime + max(0.0, interval_hours) * 3600
    except OSError:
        return now


def due(directory: str | Path, interval_hours: float,
        now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now >= next_due(directory, interval_hours, now)


def _sweep_partials(directory: Path) -> None:
    for leftover in directory.glob(f"{PREFIX}*{SUFFIX}{PARTIAL}"):
        try:
            leftover.unlink()
        except OSError:
            pass


def create(db: Any, directory: str | Path, when: float | None = None) -> Path:
    """Take one snapshot, named for its day, and return its path."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    _sweep_partials(d)
    dest = d / f"{PREFIX}{stamp(when)}{SUFFIX}"
    tmp = dest.with_name(dest.name + PARTIAL)
    try:
        db.backup_to(tmp)
        os.replace(tmp, dest)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return dest


def prune(directory: str | Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` snapshots. Returns what went."""
    found = list_backups(directory)
    removed: list[Path] = []
    for path in found[:max(0, len(found) - max(1, int(keep)))]:
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            log.warning("could not remove old backup %s: %s", path, exc)
    return removed


def run(db: Any, cfg: Any, when: float | None = None) -> Path:
    """Snapshot, then prune to the configured retention."""
    directory = backup_dir(cfg)
    path = create(db, directory, when)
    for gone in prune(directory, cfg.backup.keep):
        log.info("removed expired backup %s", gone.name)
    return path


def verify(path: str | Path) -> str | None:
    """None if the file is a readable database, else why it is not.

    A backup nobody has ever opened is a guess, not a backup, so restoring
    one checks it first - the whole point is that the file being replaced
    may itself be damaged, and there is no going back from overwriting a
    good database with a bad snapshot.
    """
    p = Path(path)
    if not p.is_file():
        return f"{p} does not exist"
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return str(exc)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return f"integrity check failed: {row[0] if row else 'no result'}"
        conn.execute("SELECT 1 FROM files LIMIT 1").fetchone()
    except sqlite3.DatabaseError as exc:
        return str(exc)
    finally:
        conn.close()
    return None


def restore(backup_path: str | Path, db_path: str | Path) -> Path | None:
    """Put a snapshot back. Returns where the current database was kept.

    The database being replaced is moved aside rather than deleted: if the
    snapshot turns out to be older than expected, the only other copy of
    that state is the file this is overwriting. Its WAL and shared-memory
    sidecars go with it - they describe the database that was moved away,
    and leaving them beside the restored file would corrupt it.

    The caller must not have the database open; the CLI closes it first.
    """
    problem = verify(backup_path)
    if problem:
        raise ValueError(f"refusing to restore {backup_path}: {problem}")

    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    kept: Path | None = None
    if target.exists():
        kept = target.with_name(
            f"{target.name}.replaced-{time.strftime('%Y%m%d-%H%M%S')}")
        os.replace(target, kept)
    for sidecar in ("-wal", "-shm"):
        try:
            Path(f"{target}{sidecar}").unlink(missing_ok=True)
        except OSError:
            pass
    shutil.copy2(backup_path, target)
    return kept
