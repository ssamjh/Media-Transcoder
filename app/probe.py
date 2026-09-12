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


def disposition(stream: dict[str, Any], flag: str) -> bool:
    return int((stream.get("disposition") or {}).get(flag) or 0) == 1


def is_default(stream: dict[str, Any]) -> bool:
    return disposition(stream, "default")


def is_attached_pic(stream: dict[str, Any]) -> bool:
    return disposition(stream, "attached_pic")


def is_comment(stream: dict[str, Any]) -> bool:
    return disposition(stream, "comment")


def is_visual_impaired(stream: dict[str, Any]) -> bool:
    return disposition(stream, "visual_impaired")


def codec_of(stream: dict[str, Any]) -> str:
    return str(stream.get("codec_name") or "").lower()


def channels_of(stream: dict[str, Any]) -> int:
    try:
        return int(stream.get("channels") or 0)
    except (TypeError, ValueError):
        return 0


def bitrate_of(stream: dict[str, Any]) -> int:
    """Bits per second, or 0 when the container does not record it.

    Matroska usually does not, which is why bitrate is only ever a tie-break:
    a missing one must not push a track below a genuinely worse one.
    """
    for key in ("bit_rate", "BPS", "BPS-eng"):
        value = stream.get(key) or (stream.get("tags") or {}).get(key)
        try:
            if value:
                return int(float(value))
        except (TypeError, ValueError):
            continue
    return 0
