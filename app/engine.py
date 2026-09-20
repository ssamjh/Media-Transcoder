"""The daemon: scan the libraries, queue what needs work, encode it.

Everything the web panel can do goes through this class - scanning, checking
one file, queueing, cancelling, pausing the schedule. Worker threads pull
paths off a queue rather than running as a one-shot batch, so work can be
added and cancelled while the daemon is live.
"""

from __future__ import annotations

import fnmatch
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import backup, ffmpeg, notify
from .config import Config, ConfigError, LibraryCfg, Profile, resolve
from .db import Db
from .plan import FilePlan, plan_file
from .probe import ProbeError, probe_file
from .workflow import Workflow

log = logging.getLogger("transcoder")

MAX_ATTEMPTS = 3

# The backup thread wakes often enough to notice a setting changed in the
# panel, and waits an hour before trying again after a failure so a
# read-only backup directory cannot fill the log a line at a time.
BACKUP_TICK = 60.0
BACKUP_RETRY = 3600.0


@dataclass
class ActiveJob:
    path: str
    started: float
    # Encode and copy progress are intentionally independent.  A completed
    # encode must stay at 100% while the verified result is copied back.
    encode_percent: float = 0.0
    encode_speed: float = 0.0
    encode_started: float = 0.0
    copy_percent: float = 0.0
    copy_speed: float = 0.0
    copy_bytes: int = 0
    copy_total: int = 0
    copy_started: float = 0.0
    # Kept as a small compatibility surface for callers that used the old
    # single-stage fields. They are updated to the current stage below.
    percent: float = 0.0
    speed: float = 0.0
    in_size: int = 0
    library: str = ""
    reasons: list[str] = field(default_factory=list)
    stage: str = "encoding"
    # When the current stage began, so an ETA for the copy is not computed
    # from time spent encoding.
    stage_started: float = 0.0
    mode: str = ""


@dataclass
class ScanResult:
    planned: list[FilePlan] = field(default_factory=list)
    cached: int = 0
    skipped: int = 0
    removed: int = 0
    unowned: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    elapsed: float = 0.0
    per_library: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def needs_work(self) -> list[FilePlan]:
        return [p for p in self.planned if p.needs_work]


class Engine:
    def __init__(self, cfg: Config, db: Db, config_path: str | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.config_path = config_path

        self._queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        # Ordinary one-shot modes live here. Native import modes are also
        # persisted in Workflow jobs and restored into this map on recovery.
        self._modes: dict[str, str] = {}
        self._durable_jobs: dict[str, int] = {}
        self._cancelled: set[str] = set()
        # Serialises the copy back onto the library. See _process_one.
        self._copy_lock = threading.Lock()
        # Outbound webhooks are queued on their own thread: a mass import can
        # finish a dozen files at once, and none of them should wait on
        # somebody else's HTTP server.
        self.notifier = notify.Notifier()
        self.workflow = Workflow(cfg, db)
        self._active: dict[str, ActiveJob] = {}
        self._lock = threading.RLock()

        self._stop = threading.Event()
        self._scan_now = threading.Event()
        self._scan_paths: list[str] | None = None
        self._scan_library: str | None = None
        self._threads: list[threading.Thread] = []

        self.scanning = False
        self.scan_progress = (0, 0)
        self.last_scan: float = 0.0
        self.last_scan_summary: dict[str, Any] = {}
        self.next_scan: float = 0.0
        self.last_backup: float = 0.0
        self.next_backup: float = 0.0
        self.started_at = time.time()

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        # Nothing of ours is encoding yet, so anything in the scratch space is
        # left over from a kill -9 or a host reboot, and it is source-file
        # sized. The database does the same thing for 'running' rows.
        removed, freed = ffmpeg.sweep_scratch(self.cfg)
        if removed:
            log.info("swept %d stale scratch director%s (%.1f GB reclaimed)",
                     removed, "y" if removed == 1 else "ies",
                     freed / (1024 ** 3))

        # Rehydrate accepted imports before workers start consuming.  The
        # database reset running jobs to pending when it opened.
        self.rehydrate()

        for i in range(max(1, self.cfg.workers.count)):
            t = threading.Thread(target=self._worker_loop, name=f"worker-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._scheduler_loop, name="scheduler",
                             daemon=True)
        t.start()
        self._threads.append(t)
        # Always started, even with backups switched off: the loop reads the
        # setting each tick, so turning them on in the panel takes effect
        # without a restart.
        t = threading.Thread(target=self._backup_loop, name="backup",
                             daemon=True)
        t.start()
        self._threads.append(t)
        self.notifier.start()
        self.workflow.start()
        log.info("started %d worker(s)", self.cfg.workers.count)

    def stop(self) -> None:
        self._stop.set()
        self.notifier.stop()
        self.workflow.stop()

    def join(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        self.notifier.join(timeout=max(0.1, deadline - time.monotonic()))
        self.workflow.join(timeout=max(0.1, deadline - time.monotonic()))
        for t in self._threads:
            t.join(timeout=max(0.1, deadline - time.monotonic()))

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # --- introspection ----------------------------------------------------

    @property
    def active(self) -> list[ActiveJob]:
        with self._lock:
            return sorted(self._active.values(), key=lambda j: j.started)

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queued)

    def queued_paths(self) -> list[str]:
        with self._lock:
            running = set(self._active)
            return [p for p in self._queued if p not in running]

    # --- control surface --------------------------------------------------

    def request_scan(self, paths: list[str] | None = None,
                     library: str | None = None) -> bool:
        """Ask for a scan as soon as the scheduler comes round. Non-blocking."""
        if self.scanning:
            return False
        self._scan_paths = paths
        self._scan_library = library
        self._scan_now.set()
        return True

    def set_schedule_enabled(self, enabled: bool) -> None:
        self.cfg.schedule.enabled = enabled
        if enabled:
            self.next_scan = time.time() + self._interval()
        else:
            self.next_scan = 0.0

    def enqueue(self, path: str, force: bool = False,
                mode: str | None = None) -> tuple[bool, str]:
        """Queue one file for processing. Returns (queued, message).

        `mode` names a processing mode to treat this one file with,
        instead of the mode its library normally uses. Raises ConfigError if
        the mode is unknown, so a typo from a Sonarr hook is a loud 400
        rather than a silent full re-encode.
        """
        p = Path(path)
        if not p.exists():
            return False, "file does not exist"
        if mode:
            lib = self.cfg.library_for(path)
            # Resolve now so an unknown mode or a bad override is rejected
            # while the caller is still listening.
            resolve(self.cfg, lib or LibraryCfg(), mode)
        with self._lock:
            if path in self._queued:
                return False, "already queued"
            if mode:
                self._modes[path] = mode
            else:
                self._modes.pop(path, None)
        if force:
            self.db.reset_attempts(path)
        else:
            self.db.set_status(path, "pending")
        self._push(path)
        return True, f"queued ({mode})" if mode else "queued"

    def enqueue_import(self, path: str, *, provider: str,
                       integration: str | None = None,
                       entity_id: Any = None, file_id: Any = None,
                       is_upgrade: bool = False,
                       event_type: str = "download",
                       mode: str | None = None,
                       payload: dict[str, Any] | None = None,
                       **kwargs: Any) -> tuple[bool, str]:
        """Durably accept one native Arr import before acknowledging it."""
        # A webhook may be redelivered after a successful container change
        # removed its original path.  Resolve idempotency before touching the
        # filesystem so that completed imports still acknowledge cleanly.
        key = self.workflow.dedupe_key(
            path, provider, integration, entity_id, file_id, event_type,
            payload)
        existing = self.db.get_job(dedupe_key=key)
        if existing is not None:
            return True, f"already {existing['status']}"
        p = Path(path)
        if not p.exists():
            return False, "file does not exist"
        lib = self.cfg.library_for(path)
        if lib is None:
            return False, "no enabled library covers this path"
        if mode:
            resolve(self.cfg, lib, mode)

        job, should_queue = self.workflow.accept(
            path=path, provider=provider, integration=integration,
            entity_id=entity_id, file_id=file_id, is_upgrade=is_upgrade,
            event_type=event_type, mode=mode, payload=payload, **kwargs)
        st = p.stat()
        if self.db.get(path) is None:
            self.db.upsert(path, size=st.st_size, mtime=st.st_mtime,
                           status="pending", library=lib.id, reasons=[])
        if not should_queue:
            return True, f"already {job['status']}"
        if not self._push_durable(job):
            return True, "persisted behind active work"
        return True, f"queued ({mode})" if mode else "queued"

    def cancel(self, path: str) -> tuple[bool, str]:
        """Cancel a running or queued file."""
        with self._lock:
            if path not in self._queued:
                return False, "not queued"
            self._cancelled.add(path)
            running = path in self._active
        if not running:
            self.db.set_status(path, "pending")
            with self._lock:
                self._modes.pop(path, None)
        return True, "cancelling" if running else "removed from queue"

    def cancel_all(self) -> int:
        with self._lock:
            paths = list(self._queued)
        for path in paths:
            self.cancel(path)
        return len(paths)

    def retry_workflow(self, job_id: int | None = None) -> int:
        """Retry failed native imports from their exact failed stage."""
        rows = self.db.list_jobs(status="failed", limit=100_000)
        if job_id is not None:
            rows = [row for row in rows if int(row["id"]) == int(job_id)]
        retried = 0
        for row in rows:
            if row["stage"] == "processing":
                fresh = self.db.update_job(
                    row["id"], status="pending", retries=0,
                    next_attempt=None, error=None, finished_at=None)
                if fresh is not None:
                    self._push_durable(fresh)
            else:
                count = self.db.retry_outbox(int(row["id"]))
                if count:
                    self.db.update_job(row["id"], status="waiting",
                                       error=None, finished_at=None)
                elif row["final_path"]:
                    self.workflow.processing_succeeded(
                        int(row["id"]), str(row["final_path"]),
                        changed=row["stage"] == "arr_reconcile")
                else:
                    continue
            retried += 1
        self.workflow.wake()
        return retried

    def check_one(self, path: str, mode: str | None = None) -> FilePlan:
        """Probe and plan a single file right now, and store the result.

        With a mode, the returned plan is what that mode *would* do, while the
        state written to the database stays the library's normal verdict - a
        mode must never settle a file as "nothing to do" when the library
        still has work for it.
        """
        lib = self.cfg.library_for(path)
        if lib is None:
            raise LookupError("no enabled library covers this path")
        probe = probe_file(path)
        plan = plan_file(probe, resolve(self.cfg, lib), self.cfg)
        self._record(Path(path), plan)
        if mode:
            return plan_file(probe, resolve(self.cfg, lib, mode), self.cfg)
        return plan

    # --- internals --------------------------------------------------------

    def _interval(self) -> float:
        return max(0.05, self.cfg.schedule.scan_interval_hours) * 3600

    def _push(self, path: str) -> None:
        with self._lock:
            if path in self._queued:
                return
            self._queued.add(path)
        self.db.set_status(path, "queued")
        self._queue.put(path)

    def _record(self, p: Path, plan: FilePlan) -> str:
        """Store a plan against a file and return the status it implies."""
        if plan.manual_review:
            log.warning("%s needs a look: %s", p.name, plan.manual_review)
        try:
            st = p.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        status = "pending" if plan.needs_work else "skip"
        self.db.upsert(
            str(p), size=size, mtime=mtime, status=status, library=plan.library,
            reasons=plan.reasons, plan=plan.to_dict(), height=plan.height,
            video_codec=plan.src_video_codec, error=None,
        )
        return status

    def _candidates(self, lib: LibraryCfg,
                    roots: list[str] | None = None) -> list[Path]:
        exts = {e.lower() for e in lib.extensions}
        min_bytes = lib.min_size_mb * 1024 * 1024
        out: list[Path] = []

        for root in (roots if roots is not None else lib.paths):
            base = Path(root)
            if not base.exists():
                log.warning("library path does not exist: %s", base)
                continue
            if base.is_file():
                out.append(base)
                continue
            for p in base.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in exts:
                    continue
                if any(fnmatch.fnmatch(p.as_posix(), pat) for pat in lib.exclude):
                    continue
                if p.name.endswith(".transcoding.tmp"):
                    continue
                try:
                    if p.stat().st_size < min_bytes:
                        continue
                except OSError:
                    continue
                out.append(p)
        return out

    def _owned(self, path: str) -> bool:
        """Does any library still cover this path?

        Routing, not enablement: disabling a library is "leave it out of
        scans", so it keeps what it knows and a re-enable costs no re-probe.
        A path no library covers any more is state nobody will consult again.
        """
        return any(lib.contains(path) is not None for lib in self.cfg.libraries)

    def scan(self, paths: list[str] | None = None,
             use_cache: bool = True,
             library: str | None = None) -> ScanResult:
        """Probe and plan candidate files. Touches nothing on disk.

        Each library is walked with its own extensions, exclusions and size
        floor, and every file is planned against its owning library profile.
        """
        started = time.monotonic()
        self.scanning = True
        res = ScanResult()
        roots_scanned: list[str] = []
        try:
            if library:
                libs = [l for l in self.cfg.libraries if l.id == library]
                if not libs:
                    raise LookupError(f"no such library: {library}")
            else:
                libs = self.cfg.active_libraries

            seen: set[str] = set()
            work: list[tuple[LibraryCfg, Path]] = []
            for lib in libs:
                subset = None
                if paths:
                    # Only the requested paths that this library actually owns.
                    subset = [p for p in paths if lib.contains(p)]
                    if not subset:
                        continue
                found = self._candidates(lib, subset)
                roots_scanned.extend(subset if subset is not None else lib.paths)
                work.extend((lib, p) for p in found)
                res.per_library.setdefault(
                    lib.id, {"name": lib.name, "examined": 0, "need_work": 0,
                             "already_fine": 0, "cached": 0}
                )

            if not libs:
                log.warning("no libraries configured: nothing to scan")
            log.info("scan: %d candidate file(s) across %d librar%s",
                     len(work), len(libs), "y" if len(libs) == 1 else "ies")
            self.scan_progress = (0, len(work))

            for i, (lib, p) in enumerate(work, 1):
                if self._stop.is_set():
                    break
                self.scan_progress = (i, len(work))
                sp = str(p)
                seen.add(sp)
                tally = res.per_library[lib.id]
                tally["examined"] += 1
                try:
                    st = p.stat()
                except OSError as exc:
                    res.errors.append((sp, str(exc)))
                    continue

                if use_cache and self.db.is_cached(sp, st.st_size, st.st_mtime):
                    res.cached += 1
                    tally["cached"] += 1
                    continue

                try:
                    probe = probe_file(p)
                except ProbeError as exc:
                    res.errors.append((sp, str(exc)))
                    self.db.upsert(sp, size=st.st_size, mtime=st.st_mtime,
                                   library=lib.id, status="failed",
                                   error=f"probe: {exc}")
                    continue

                plan = plan_file(probe, resolve(self.cfg, lib), self.cfg)
                res.planned.append(plan)
                if self._record(p, plan) == "skip":
                    res.skipped += 1
                    tally["already_fine"] += 1
                else:
                    tally["need_work"] += 1

            res.removed = self.db.forget_missing(seen, roots_scanned)
            # Only a full scan can tell that a file belongs to nobody: a
            # library-scoped or path-scoped scan never looks at the rest.
            if paths is None and library is None:
                res.unowned = self.db.forget_unowned(self._owned)
            self.last_scan = time.time()
        finally:
            self.scanning = False
            self.scan_progress = (0, 0)
            res.elapsed = time.monotonic() - started

        self.last_scan_summary = {
            "examined": len(res.planned) + res.cached,
            "need_work": len(res.needs_work),
            "already_fine": res.skipped,
            "cached": res.cached,
            "removed": res.removed,
            "unowned": res.unowned,
            "errors": len(res.errors),
            "elapsed": res.elapsed,
            "roots": roots_scanned,
            "libraries": res.per_library,
        }
        log.info(
            "scan done in %.1fs: %d need work, %d already fine, %d cached, "
            "%d gone, %d no longer in a library, %d error(s)",
            res.elapsed, len(res.needs_work), res.skipped, res.cached,
            res.removed, res.unowned, len(res.errors),
        )
        return res

    def enqueue_pending(self) -> int:
        rows = self.db.queueable(MAX_ATTEMPTS)
        n = 0
        for r in rows:
            with self._lock:
                if r["path"] in self._queued:
                    continue
            self._push(r["path"])
            n += 1
        if n:
            log.info("queued %d file(s)", n)
        return n

    def _scheduler_loop(self) -> None:
        if self.cfg.schedule.enabled and self.cfg.schedule.scan_on_start:
            self._scan_now.set()
        elif self.cfg.schedule.enabled:
            self.next_scan = time.time() + self._interval()

        while not self._stop.is_set():
            due = (
                self.cfg.schedule.enabled
                and self.next_scan
                and time.time() >= self.next_scan
            )
            if self._scan_now.is_set() or due:
                self._scan_now.clear()
                paths, self._scan_paths = self._scan_paths, None
                lib_id, self._scan_library = self._scan_library, None
                try:
                    self.scan(paths, library=lib_id)
                    # A scan only plans. Turning what it found into encodes is
                    # a separate decision, off by default, so a scan can never
                    # surprise anyone with work they did not ask for.
                    if self.cfg.schedule.process_after_scan:
                        self.enqueue_pending()
                except Exception:
                    log.exception("scan failed")
                self.next_scan = (
                    time.time() + self._interval()
                    if self.cfg.schedule.enabled else 0.0
                )
            self._stop.wait(1.0)

    def rehydrate(self) -> int:
        """Push durable work the database still owes back onto the queue."""
        pushed = 0
        for row in self.workflow.recoverable():
            if self._push_durable(row):
                pushed += 1
        return pushed

    def backup_now(self) -> Path:
        """Snapshot the state database and prune old ones. Returns its path."""
        path = backup.run(self.db, self.cfg)
        self.last_backup = time.time()
        self.next_backup = backup.next_due(
            backup.backup_dir(self.cfg), self.cfg.backup.interval_hours)
        return path

    def restore_backup(self, name: str) -> Path | None:
        """Put a snapshot back under the running daemon.

        Restoring replaces the file every thread here is reading, so this
        refuses while anything is queued or encoding rather than trying to
        be clever about it - the panel says as much, and cancelling first
        is a second's work. With the queue empty, the swap is: close the
        connection, move the live file aside, copy the snapshot in, open it
        again (which resets 'running' rows exactly as a restart would), and
        push back whatever durable imports the snapshot still owes.

        Returns where the replaced database was kept, or None if there was
        nothing to keep.
        """
        directory = backup.backup_dir(self.cfg)
        candidate = Path(name).name          # a name, never a path to anywhere
        source = directory / candidate
        if candidate != str(name) and Path(name) != source:
            raise ValueError(f"{name} is not a snapshot in {directory}")
        if source not in backup.list_backups(directory):
            raise ValueError(f"no such snapshot: {candidate}")

        with self._lock:
            busy = len(self._queued)
        if busy or self.scanning:
            raise RuntimeError(
                "cancel the queue and let the current scan finish before "
                "restoring - the database is in use")

        # Db.swap holds the database lock across the whole exchange, so a
        # worker or the workflow thread waits for the restored file instead
        # of meeting a closed connection - and a refused restore (a snapshot
        # that will not open) leaves the daemon on the database it had.
        kept = self.db.swap(lambda: backup.restore(source, self.cfg.state_db))
        self.rehydrate()
        log.warning("state database restored from %s", source)
        return kept

    def _backup_loop(self) -> None:
        """One snapshot of the state database per configured interval.

        Its own thread rather than a step in the scheduler, because a scan
        can run for hours and the backup should not queue behind it. What is
        owed is decided from the newest file on disk (see backup.next_due),
        so restarting the daemon does not take an extra snapshot and a
        daemon that is never up at midnight still gets one a day.
        """
        retry_after = 0.0
        while not self._stop.is_set():
            cfg = self.cfg.backup
            directory = backup.backup_dir(self.cfg)
            if cfg.enabled:
                self.next_backup = backup.next_due(directory, cfg.interval_hours)
                now = time.time()
                if now >= self.next_backup and now >= retry_after:
                    try:
                        path = self.backup_now()
                        log.info("state database backed up to %s", path)
                    except Exception:
                        log.exception("state database backup failed")
                        retry_after = time.time() + BACKUP_RETRY
            else:
                self.next_backup = 0.0
            self._stop.wait(BACKUP_TICK)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    durable_id = self._durable_jobs.get(path)
                durable = self.workflow.claim(durable_id) if durable_id else None
                if durable is not None:
                    with self._lock:
                        if durable["mode"]:
                            self._modes[path] = durable["mode"]
                with self._lock:
                    cancelled = path in self._cancelled
                if cancelled:
                    self.db.set_status(path, "pending")
                    if durable_id:
                        self.db.update_job(durable_id, status="cancelled",
                                           error="cancelled")
                    log.info("skipped (cancelled before start): %s", path)
                else:
                    self._process_one(path, durable_id=durable_id)
            except Exception as exc:
                log.exception("unexpected error on %s", path)
                self.db.upsert(path, status="failed", error="internal error")
                if durable_id:
                    self.workflow.processing_failed(durable_id, str(exc))
            finally:
                self._queue.task_done()
                with self._lock:
                    self._queued.discard(path)
                    self._cancelled.discard(path)
                    # Cleared here rather than when the encode ends: the job
                    # stays visible through the copy back, which on a network
                    # share is the slowest part of the whole run.
                    self._active.pop(path, None)
                    self._modes.pop(path, None)
                    self._durable_jobs.pop(path, None)
                self._queue_next_durable(path)

    def _push_durable(self, row: Any) -> bool:
        path = str(row["source_path"])
        with self._lock:
            if path in self._queued:
                return False
            self._durable_jobs[path] = int(row["id"])
            if row["mode"]:
                self._modes[path] = str(row["mode"])
        self._push(path)
        return True

    def _queue_next_durable(self, path: str) -> None:
        for row in self.workflow.recoverable():
            if str(row["source_path"]) == path:
                self._push_durable(row)
                return

    def _copy_back(self, job: ActiveJob, src: Path,
                   result: ffmpeg.EncodeResult, profile: Profile) -> bool:
        """Put a finished encode onto the library - one file at a time.

        The library is typically a network share, and two copies over one
        link do not go twice as fast: they halve each other and leave both
        files in flight for longer. Encoding carries on in the other workers
        while this one waits its turn, and the job stays on the panel the
        whole time with the copy's own progress.
        """
        def copied(pct: float, mbps: float) -> None:
            job.copy_percent, job.copy_speed = pct, mbps
            job.copy_bytes = min(job.copy_total,
                                 int(job.copy_total * pct / 100))
            job.percent, job.speed = pct, mbps

        job.encode_percent = 100.0
        job.stage = "waiting to copy"
        job.copy_percent = job.copy_bytes = 0
        job.copy_speed = 0.0
        job.copy_total = getattr(result, "out_size", 0)
        job.percent = job.speed = 0.0
        job.stage_started = job.copy_started = time.time()
        with self._copy_lock:
            job.stage = "copying"
            job.stage_started = job.copy_started = time.time()
            return ffmpeg.replace_original(src, result, self.cfg, profile,
                                           on_progress=copied)

    def _rebuild_without_encode(self, job: ActiveJob, src: Path, plan: FilePlan,
                                profile: Profile, progress: Any,
                                cancelled: Any) -> ffmpeg.EncodeResult | None:
        """Run the job again with the source video copied. None if that failed.

        Reached when the x265 pass came back bigger than the file it would
        replace. Only the encode failed to pay off - the stereo track and the
        subtitle cleaning still have, and the source video stream is the best
        video this file is going to get - so the run is rebuilt around it
        instead of being thrown away whole.
        """
        job.stage = "encoding"
        job.encode_percent = job.percent = 0.0
        job.encode_speed = job.speed = 0.0
        job.encode_started = time.time()
        job.stage_started = time.time()
        try:
            result = ffmpeg.encode(plan, self.cfg, on_progress=progress,
                                   cancel=cancelled)
        except ffmpeg.EncodeError as exc:
            log.error("rebuild without the x265 pass failed for %s: %s",
                      src.name, exc)
            return None
        # replace_original cleans up after itself when it refuses, so a
        # rejection here needs nothing more than the original left in place.
        if not self._copy_back(job, src, result, profile):
            return None
        return result

    def _process_one(self, path: str, durable_id: int | None = None) -> str:
        p = Path(path)
        if not p.exists():
            self.db.upsert(path, status="skip", error="file disappeared")
            if durable_id:
                self.workflow.processing_failed(durable_id, "file disappeared")
            return "skip"

        lib = self.cfg.library_for(path)
        if lib is None:
            # The library was deleted, disabled or re-pointed since the scan.
            self.db.upsert(path, status="skip",
                           error="no enabled library covers this path")
            if durable_id:
                self.workflow.processing_failed(
                    durable_id, "no enabled library covers this path")
            return "skip"

        # Re-plan immediately before encoding: a scan may be hours old and the
        # file could have been replaced, or the library reconfigured.
        try:
            probe = probe_file(p)
        except ProbeError as exc:
            self.db.upsert(path, status="failed", error=f"probe: {exc}")
            if durable_id:
                self.workflow.processing_failed(durable_id, f"probe: {exc}")
            return "failed"

        with self._lock:
            mode = self._modes.pop(path, "")

        # A mode named on the request treats this one file differently. The
        # plan that decides the encode is that mode's; the plan that is
        # *stored* is always the library's own mode, so a "cleanup" run can
        # never settle a file as done when the library still wants it
        # re-encoded.
        try:
            profile = resolve(self.cfg, lib, mode)
        except ConfigError as exc:
            self.db.upsert(path, status="failed", error=f"mode: {exc}")
            if durable_id:
                self.workflow.processing_failed(durable_id, f"mode: {exc}")
            return "failed"

        plan = plan_file(probe, profile, self.cfg)
        if plan.manual_review:
            log.warning("%s needs a look: %s", p.name, plan.manual_review)
        if not plan.needs_work:
            self._record(p, plan_file(probe, resolve(self.cfg, lib), self.cfg)
                         if mode else plan)
            if durable_id:
                self.workflow.processing_succeeded(
                    durable_id, str(p), changed=False)
            return "skip"

        if self.cfg.dry_run:
            log.info("[dry-run] would process %s%s: %s", path,
                     f" [{mode}]" if mode else "", "; ".join(plan.reasons))
            self.db.upsert(path, status="pending",
                           error="dry run: not processed")
            if durable_id:
                self.workflow.processing_failed(
                    durable_id, "dry run: not processed")
            return "pending"

        job = ActiveJob(path=path, started=time.time(), in_size=plan.size,
                        library=lib.name, reasons=list(plan.reasons), mode=mode,
                        stage_started=time.time(), encode_started=time.time())
        with self._lock:
            self._active[path] = job
        self.db.set_status(path, "running")
        self.db.bump_attempts(path)
        detail = plan.to_dict()
        detail["mode"] = mode
        run_id = self.db.start_run(path, detail)

        def progress(pct: float, speed: float) -> None:
            job.encode_percent, job.encode_speed = pct, speed
            job.percent, job.speed = pct, speed

        def cancelled() -> bool:
            with self._lock:
                return self._stop.is_set() or path in self._cancelled

        try:
            result = ffmpeg.encode(plan, self.cfg, on_progress=progress,
                                   cancel=cancelled)
        except ffmpeg.EncodeError as exc:
            msg = str(exc)
            if msg == "cancelled":
                self.db.upsert(path, status="pending", error="cancelled")
                self.db.finish_run(run_id, "cancelled", error="cancelled")
                if durable_id:
                    self.db.update_job(durable_id, status="cancelled",
                                       error="cancelled")
                log.info("cancelled: %s", p.name)
                return "cancelled"
            row = self.db.get(path)
            if row and int(row["attempts"]) >= MAX_ATTEMPTS:
                msg = f"{msg} (gave up after {MAX_ATTEMPTS} attempts)"
            self.db.upsert(path, status="failed", error=msg)
            self.db.finish_run(run_id, "failed", error=msg)
            if durable_id:
                self.workflow.processing_failed(durable_id, msg)
            log.error("encode failed for %s: %s", p.name, msg)
            return "failed"

        detail["in_size"] = result.in_size
        detail["out_size"] = result.out_size

        if not profile.output.replace_original:
            note = "replace_original is off for this library, encode discarded"
            ffmpeg.discard(result)
            self.db.upsert(path, status="skip", error=note)
            self.db.finish_run(run_id, "rejected", result.in_size, result.out_size,
                               result.elapsed, note, detail)
            if durable_id:
                self.workflow.processing_succeeded(
                    durable_id, path, changed=False)
            return "skip"

        # Set once the x265 pass was rejected for size and the run was rebuilt
        # around the source video. It is also the signal that the file has to
        # settle rather than stay pending: the encode would be rejected again
        # on every scan from now until someone changes the settings.
        rebuilt = ""
        if not self._copy_back(job, p, result, profile):
            note = ffmpeg.size_verdict(result.in_size, result.out_size,
                                       profile.output,
                                       result.video_encoded) or "output was rejected"
            retry = None
            if result.video_encoded and ffmpeg.over_ceiling(
                    result.in_size, result.out_size, profile.output):
                log.info("%s: %s; rebuilding with the source video stream",
                         p.name, note)
                plan = plan.without_video_encode()
                retry = self._rebuild_without_encode(job, p, plan, profile,
                                                     progress, cancelled)
            if retry is None:
                note += ", original kept"
                self.db.upsert(path, status="skip", error=note,
                               in_size=result.in_size, out_size=result.out_size)
                self.db.finish_run(run_id, "rejected", result.in_size,
                                   result.out_size, result.elapsed, note, detail)
                log.info("%s: %s", p.name, note)
                if durable_id:
                    self.workflow.processing_succeeded(
                        durable_id, path, changed=False)
                return "skip"
            rebuilt = note + ", kept the source video stream"
            result = retry
            detail = plan.to_dict()
            detail["mode"] = mode
            detail["in_size"], detail["out_size"] = result.in_size, result.out_size

        final = p.with_suffix("." + plan.container)
        if final != p:
            self.db.forget(path)
        st = final.stat()
        status, reasons = "done", plan.reasons
        if rebuilt:
            # Settled, not done: the library still wants an x265 encode this
            # file will never accept, and leaving it pending would re-run the
            # whole rejected pass on every scan.
            status, reasons = "skip", plan.reasons
        elif mode:
            # The mode is spent. Re-plan the result under the library's own
            # profile, so the next scan sees the truth without re-probing: a
            # cleanup run leaves a file still owing an x265 encode.
            try:
                after = plan_file(probe_file(final), resolve(self.cfg, lib),
                                  self.cfg)
                status = "done" if not after.needs_work else "pending"
                reasons = after.reasons
            except ProbeError:
                status, reasons = "done", plan.reasons
        self.db.upsert(
            str(final), size=st.st_size, mtime=st.st_mtime, status=status,
            library=lib.id, last_run=time.time(), in_size=result.in_size,
            out_size=result.out_size, error=rebuilt or None, plan=detail,
            reasons=reasons,
        )
        self.db.finish_run(run_id, "done", result.in_size, result.out_size,
                           result.elapsed, rebuilt or None, detail,
                           final_path=str(final))
        if durable_id:
            self.workflow.processing_succeeded(
                durable_id, str(final), changed=True)
        else:
            # Files discovered by a scan or queued manually do not pass
            # through the durable import workflow, but Jellyfin still needs
            # the same targeted path update once replacement is complete.
            self.notifier.dispatch(
                notify.jellyfin_hooks(self.cfg),
                notify.jellyfin_payload(str(final)),
            )
        # Only now, with the verified encode in place of the original, is it
        # true to tell anyone else the file changed. `profile` and not `lib`,
        # so a mode can add or replace the callbacks for this one request -
        # which is how an import hook points at Jellyfin without every
        # scheduled scan doing the same.
        self.notifier.dispatch(
            notify.hooks_for(profile.notify),
            notify.payload_for(
                str(final), library=lib.id, mode=mode, status=status,
                original=path, in_size=result.in_size,
                out_size=result.out_size, elapsed=result.elapsed,
                reasons=reasons,
            ),
        )
        log.info(
            "%s: %.2f GB -> %.2f GB (%.0f%%) in %s",
            final.name, result.in_size / 2**30, result.out_size / 2**30,
            result.ratio * 100, hms(result.elapsed),
        )
        return "done"

    # --- one-shot (CLI) ---------------------------------------------------

    def process_all(self) -> dict[str, int]:
        """Process everything pending and return when the queue drains."""
        self.enqueue_pending()
        if not self.queue_depth:
            log.info("nothing pending")
            return {}
        self.start()
        while self.queue_depth and not self._stop.is_set():
            time.sleep(0.5)
        self.stop()
        self.join()
        return self.db.stats()["counts"]


def hms(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"
