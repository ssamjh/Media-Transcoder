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

from . import ffmpeg
from .config import Config, ConfigError, LibraryCfg, resolve_library
from .db import Db
from .plan import FilePlan, plan_file
from .probe import ProbeError, probe_file

log = logging.getLogger("transcoder")

MAX_ATTEMPTS = 3


@dataclass
class ActiveJob:
    path: str
    started: float
    percent: float = 0.0
    speed: float = 0.0
    in_size: int = 0
    library: str = ""
    reasons: list[str] = field(default_factory=list)
    stage: str = "encoding"
    mode: str = ""


@dataclass
class ScanResult:
    planned: list[FilePlan] = field(default_factory=list)
    cached: int = 0
    skipped: int = 0
    removed: int = 0
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
        # Modes are one-shot: a path's mode lives only until it is processed,
        # and is deliberately not persisted. A restart drops them, which is
        # the same outcome as the scan that follows.
        self._modes: dict[str, str] = {}
        self._cancelled: set[str] = set()
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
        self.started_at = time.time()

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        for i in range(max(1, self.cfg.workers.count)):
            t = threading.Thread(target=self._worker_loop, name=f"worker-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._scheduler_loop, name="scheduler",
                             daemon=True)
        t.start()
        self._threads.append(t)
        log.info("started %d worker(s)", self.cfg.workers.count)

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
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

        `mode` names a set of overrides from the config, applied to the
        owning library's profile for this run only. Raises ConfigError if the
        mode is unknown, so a typo from a Sonarr hook is a loud 400 rather
        than a silent full re-encode.
        """
        p = Path(path)
        if not p.exists():
            return False, "file does not exist"
        if mode:
            lib = self.cfg.library_for(path)
            # Resolve now so an unknown mode or a bad override is rejected
            # while the caller is still listening.
            resolve_library(self.cfg, lib or LibraryCfg(), mode)
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
        plan = plan_file(probe, lib, self.cfg)
        self._record(Path(path), plan)
        if mode:
            return plan_file(probe, resolve_library(self.cfg, lib, mode), self.cfg)
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

                plan = plan_file(probe, lib, self.cfg)
                res.planned.append(plan)
                if self._record(p, plan) == "skip":
                    res.skipped += 1
                    tally["already_fine"] += 1
                else:
                    tally["need_work"] += 1

            res.removed = self.db.forget_missing(seen, roots_scanned)
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
            "errors": len(res.errors),
            "elapsed": res.elapsed,
            "roots": roots_scanned,
            "libraries": res.per_library,
        }
        log.info(
            "scan done in %.1fs: %d need work, %d already fine, %d cached, "
            "%d gone, %d error(s)",
            res.elapsed, len(res.needs_work), res.skipped, res.cached,
            res.removed, len(res.errors),
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
                    self.enqueue_pending()
                except Exception:
                    log.exception("scan failed")
                self.next_scan = (
                    time.time() + self._interval()
                    if self.cfg.schedule.enabled else 0.0
                )
            self._stop.wait(1.0)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    cancelled = path in self._cancelled
                if cancelled:
                    self.db.set_status(path, "pending")
                    log.info("skipped (cancelled before start): %s", path)
                else:
                    self._process_one(path)
            except Exception:
                log.exception("unexpected error on %s", path)
                self.db.upsert(path, status="failed", error="internal error")
            finally:
                self._queue.task_done()
                with self._lock:
                    self._queued.discard(path)
                    self._cancelled.discard(path)

    def _process_one(self, path: str) -> str:
        p = Path(path)
        if not p.exists():
            self.db.upsert(path, status="skip", error="file disappeared")
            return "skip"

        lib = self.cfg.library_for(path)
        if lib is None:
            # The library was deleted, disabled or re-pointed since the scan.
            self.db.upsert(path, status="skip",
                           error="no enabled library covers this path")
            return "skip"

        # Re-plan immediately before encoding: a scan may be hours old and the
        # file could have been replaced, or the library reconfigured.
        try:
            probe = probe_file(p)
        except ProbeError as exc:
            self.db.upsert(path, status="failed", error=f"probe: {exc}")
            return "failed"

        with self._lock:
            mode = self._modes.pop(path, "")

        # A mode overrides the library's profile for this run only. The plan
        # that decides the encode is the mode's; the plan that is *stored*
        # is always the library's own, so a "cleanup" run can never settle a
        # file as done when the library still wants it re-encoded.
        try:
            profile = resolve_library(self.cfg, lib, mode)
        except ConfigError as exc:
            self.db.upsert(path, status="failed", error=f"mode: {exc}")
            return "failed"

        plan = plan_file(probe, profile, self.cfg)
        if not plan.needs_work:
            self._record(p, plan_file(probe, lib, self.cfg) if mode else plan)
            return "skip"

        if self.cfg.dry_run:
            log.info("[dry-run] would process %s%s: %s", path,
                     f" [{mode}]" if mode else "", "; ".join(plan.reasons))
            self.db.upsert(path, status="pending",
                           error="dry run: not processed")
            return "pending"

        job = ActiveJob(path=path, started=time.time(), in_size=plan.size,
                        library=lib.name, reasons=list(plan.reasons), mode=mode)
        with self._lock:
            self._active[path] = job
        self.db.set_status(path, "running")
        self.db.bump_attempts(path)
        detail = plan.to_dict()
        detail["mode"] = mode
        run_id = self.db.start_run(path, detail)

        def progress(pct: float, speed: float) -> None:
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
                log.info("cancelled: %s", p.name)
                return "cancelled"
            row = self.db.get(path)
            if row and int(row["attempts"]) >= MAX_ATTEMPTS:
                msg = f"{msg} (gave up after {MAX_ATTEMPTS} attempts)"
            self.db.upsert(path, status="failed", error=msg)
            self.db.finish_run(run_id, "failed", error=msg)
            log.error("encode failed for %s: %s", p.name, msg)
            return "failed"
        finally:
            with self._lock:
                self._active.pop(path, None)
                self._modes.pop(path, None)

        detail["in_size"] = result.in_size
        detail["out_size"] = result.out_size

        if not lib.output.replace_original:
            note = "replace_original is off for this library, encode discarded"
            ffmpeg.discard(result)
            self.db.upsert(path, status="skip", error=note)
            self.db.finish_run(run_id, "rejected", result.in_size, result.out_size,
                               result.elapsed, note, detail)
            return "skip"

        if not ffmpeg.replace_original(p, result, self.cfg, lib):
            note = "output was not smaller, original kept"
            self.db.upsert(path, status="skip", error=note,
                           in_size=result.in_size, out_size=result.out_size)
            self.db.finish_run(run_id, "rejected", result.in_size, result.out_size,
                               result.elapsed, note, detail)
            log.info("%s: %s", p.name, note)
            return "skip"

        final = p.with_suffix("." + plan.container)
        if final != p:
            self.db.forget(path)
        st = final.stat()
        status, reasons = "done", plan.reasons
        if mode:
            # The mode is spent. Re-plan the result under the library's own
            # profile, so the next scan sees the truth without re-probing: a
            # cleanup run leaves a file still owing an x265 encode.
            try:
                after = plan_file(probe_file(final), lib, self.cfg)
                status = "done" if not after.needs_work else "pending"
                reasons = after.reasons
            except ProbeError:
                status, reasons = "done", plan.reasons
        self.db.upsert(
            str(final), size=st.st_size, mtime=st.st_mtime, status=status,
            library=lib.id, last_run=time.time(), in_size=result.in_size,
            out_size=result.out_size, error=None, plan=detail,
            reasons=reasons,
        )
        self.db.finish_run(run_id, "done", result.in_size, result.out_size,
                           result.elapsed, None, detail)
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
