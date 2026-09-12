"""ffprobe wrapper.

Everything downstream works off the parsed dict, so planning can be unit
tested with hand-written probe fixtures and never needs a real file.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FFPROBE = "ffprobe"


class ProbeError(RuntimeError):
    pass


@dataclass
class Probe:
    path: str
    streams: list[dict[str, Any]]
    fmt: dict[str, Any]

    @property
    def duration(self) -> float:
        try:
            return float(self.fmt.get("duration") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def size(self) -> int:
        try:
            return int(self.fmt.get("size") or 0)
        except (TypeError, ValueError):
            return 0

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [s for s in self.streams if s.get("codec_type") == kind]


def probe_file(path: str | Path, timeout: int = 120) -> Probe:
    cmd = [
        FFPROBE, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe not found on PATH") from exc

    if res.returncode != 0:
        raise ProbeError((res.stderr or "ffprobe failed").strip().splitlines()[-1][:400])

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"could not parse ffprobe output: {exc}") from exc

    streams = data.get("streams") or []
    if not streams:
        raise ProbeError("file contains no streams")

    return Probe(path=str(path), streams=streams, fmt=data.get("format") or {})


# --- small helpers shared by planning -------------------------------------

def lang_of(stream: dict[str, Any], default: str = "und") -> str:
    return str((stream.get("tags") or {}).get("language") or default).lower().strip()


def title_of(stream: dict[str, Any]) -> str:
    return str((stream.get("tags") or {}).get("title") or "")


def is_default(stream: dict[str, Any]) -> bool:
    return int((stream.get("disposition") or {}).get("default") or 0) == 1


def is_attached_pic(stream: dict[str, Any]) -> bool:
    return int((stream.get("disposition") or {}).get("attached_pic") or 0) == 1


def codec_of(stream: dict[str, Any]) -> str:
    return str(stream.get("codec_name") or "").lower()


def channels_of(stream: dict[str, Any]) -> int:
    try:
        return int(stream.get("channels") or 0)
    except (TypeError, ValueError):
        return 0
