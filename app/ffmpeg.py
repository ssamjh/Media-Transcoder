"""Turn a FilePlan into an ffmpeg invocation, run it, and verify the result.

Every codec is written out explicitly. Nothing is left to ffmpeg defaults:
an output stream with no -c: option falls back to the container default
encoder, which silently produces H.264 for mkv.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config, LibraryCfg
from .plan import FilePlan
from .probe import ProbeError, probe_file

log = logging.getLogger("transcoder.ffmpeg")

FFMPEG = "ffmpeg"

# Every encode gets its own directory under the scratch root, named with this
# prefix so a later sweep can tell our debris from anything else living there.
WORK_PREFIX = "transcode-"

ProgressCb = Callable[[float, float], None]  # (percent 0-100, speed multiplier)


class EncodeError(RuntimeError):
    pass


@dataclass
class EncodeResult:
    out_path: Path
    in_size: int
    out_size: int
    elapsed: float

    @property
    def saved(self) -> int:
        return self.in_size - self.out_size

    @property
    def ratio(self) -> float:
        return (self.out_size / self.in_size) if self.in_size else 1.0


def build_args(plan: FilePlan, dest: str | Path) -> list[str]:
    """Build the full ffmpeg argument list for a plan."""
    args = [
        FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-i", str(plan.path),
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-max_muxing_queue_size", "4096",
    ]

    for out_index, sp in enumerate(plan.streams):
        args += ["-map", f"0:{sp.src_index}"]
        args += [f"-c:{out_index}", sp.codec]
        args += [a.replace("{i}", str(out_index)) for a in sp.extra]
        if sp.disposition is not None:
            args += [f"-disposition:{out_index}", sp.disposition]
        if sp.title is not None:
            args += [f"-metadata:s:{out_index}", f"title={sp.title}"]

    args += ["-progress", "pipe:1", "-nostats", str(dest)]
    return args


_TIME_RE = re.compile(r"out_time_us=(\d+)")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


def _run(args: list[str], duration: float, on_progress: ProgressCb | None,
         cancel: Callable[[], bool] | None) -> None:
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    # stderr must be drained concurrently with stdout. Reading it only after
    # the progress loop finishes deadlocks the moment ffmpeg writes more than
    # the ~64KB pipe buffer - it blocks on the write, stops emitting progress,
    # and both processes wait on each other forever.
    tail: deque[str] = deque(maxlen=50)

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip()
            if line:
                tail.append(line)

    reader = threading.Thread(target=drain, daemon=True, name="ffmpeg-stderr")
    reader.start()

    speed = 0.0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel is not None and cancel():
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise EncodeError("cancelled")
            if on_progress is None:
                continue
            m = _SPEED_RE.search(line)
            if m:
                speed = float(m.group(1))
            m = _TIME_RE.search(line)
            if m and duration > 0:
                pct = min(100.0, (int(m.group(1)) / 1_000_000) / duration * 100)
                on_progress(pct, speed)
    finally:
        if proc.stdout:
            proc.stdout.close()

    rc = proc.wait()
    reader.join(timeout=10)
    if proc.stderr:
        proc.stderr.close()
    if rc != 0:
        msg = " | ".join(list(tail)[-4:])[:500]
        raise EncodeError(f"ffmpeg exited {rc}: {msg}")


def _verify(src_plan: FilePlan, dest: Path, cfg: Config) -> None:
    """Catch truncated or empty encodes that still exited 0."""
    if not dest.exists() or dest.stat().st_size == 0:
        raise EncodeError("output file is missing or empty")

    try:
        out = probe_file(dest)
    except ProbeError as exc:
        raise EncodeError(f"output failed to probe: {exc}") from exc

    if not out.of_type("video"):
        raise EncodeError("output has no video stream")

    want = len(src_plan.streams)
    got = len(out.streams)
    if got != want:
        raise EncodeError(f"expected {want} output streams, got {got}")

    if src_plan.duration > 0:
        ratio = out.duration / src_plan.duration
        if ratio < cfg.output.min_duration_ratio:
            raise EncodeError(
                f"output is {ratio:.1%} of source duration "
                f"({out.duration:.0f}s vs {src_plan.duration:.0f}s)"
            )


def sweep_scratch(cfg: Config) -> tuple[int, int]:
    """Delete work directories left behind by a hard kill.

    encode() cleans up after itself on failure, but SIGKILL or a host reboot
    mid-encode leaves a source-sized directory in the scratch space that
    nothing would ever remove. Called when the engine starts, at which point
    no encode of ours is running, so everything matching the prefix is
    debris. Returns (entries removed, bytes reclaimed).
    """
    root = Path(cfg.output.temp_dir)
    removed = freed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return (0, 0)

    for entry in entries:
        if not entry.name.startswith(WORK_PREFIX):
            continue
        try:
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())                 if entry.is_dir() else entry.stat().st_size
        except OSError:
            size = 0
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            log.warning("could not remove stale scratch %s: %s", entry, exc)
            continue
        removed += 1
        freed += size
    return (removed, freed)


def encode(plan: FilePlan, cfg: Config, on_progress: ProgressCb | None = None,
           cancel: Callable[[], bool] | None = None) -> EncodeResult:
    """Encode to a temp file, verify it, and return where it landed.

    The source is never touched here; replacing it is a separate, explicit
    step so a failed or rejected encode always leaves the library intact.
    """
    src = Path(plan.path)
    in_size = src.stat().st_size

    temp_root = Path(cfg.output.temp_dir)
    temp_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=WORK_PREFIX, dir=str(temp_root)))
    dest = work / f"{src.stem}.{plan.container}"

    started = time.monotonic()
    try:
        _run(build_args(plan, dest), plan.duration, on_progress, cancel)
        _verify(plan, dest, cfg)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise

    return EncodeResult(
        out_path=dest,
        in_size=in_size,
        out_size=dest.stat().st_size,
        elapsed=time.monotonic() - started,
    )


def discard(result: EncodeResult) -> None:
    """Throw away an encode without touching the source."""
    shutil.rmtree(result.out_path.parent, ignore_errors=True)


def replace_original(src: Path, result: EncodeResult, cfg: Config,
                     lib: LibraryCfg) -> bool:
    """Move the encode over the original. Returns False if it was rejected.

    The new file is staged alongside the original first so the final step is
    an os.replace on one filesystem - if the process dies mid-copy the
    library still holds a complete file, never a half-written one.
    """
    if lib.output.only_replace_if_smaller and result.out_size >= result.in_size:
        shutil.rmtree(result.out_path.parent, ignore_errors=True)
        return False

    final = src.with_suffix("." + result.out_path.suffix.lstrip("."))
    staged = src.with_name(src.name + ".transcoding.tmp")

    try:
        shutil.copy2(result.out_path, staged)
        with open(staged, "rb+") as fh:
            os.fsync(fh.fileno())

        if cfg.output.chown_uid >= 0 and hasattr(os, "chown"):
            try:
                os.chown(staged, cfg.output.chown_uid, cfg.output.chown_gid)
            except PermissionError:
                pass  # not running as root; leave ownership as-is

        os.replace(staged, final)
        # A source with a different extension is a separate inode from the
        # file we just wrote, so it has to go explicitly.
        if src.resolve() != final.resolve() and src.exists():
            src.unlink()
    finally:
        if staged.exists():
            staged.unlink(missing_ok=True)
        shutil.rmtree(result.out_path.parent, ignore_errors=True)

    return True
