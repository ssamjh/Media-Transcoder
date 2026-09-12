"""Configuration: defaults, schema, validation, and TOML round-tripping.

The config file is *generated*, not hand-parsed and patched. Every field
carries its own description here, so writing the file back out reproduces the
explanatory comments rather than losing them. That makes it safe for the web
panel to rewrite the file whenever a setting changes.

Processing settings live on each library, not globally. A library owns a
complete profile - video, audio, subtitles, output - each with its own
`enabled` switch, so one library can clean audio and leave subtitles alone
while another does everything except re-encode. Nothing is inherited, so what
a library will do is always readable in one place.

TOML is read with the stdlib tomllib and written by the emitter at the bottom,
so the package needs no third-party dependencies.
"""

from __future__ import annotations

import copy
import os
import re
import secrets
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


# --- per-library processing profile ----------------------------------------

@dataclass
class VideoCfg:
    enabled: bool = True
    preset: str = "medium"
    crf_720p: int = 23
    crf_1080p: int = 22
    sd_max_height: int = 576
    h720_max_height: int = 800
    h1080_max_height: int = 1200
    skip_above_height: int = 1200
    already_encoded: list[str] = field(default_factory=lambda: ["hevc", "h265"])


@dataclass
class AudioCfg:
    enabled: bool = True
    keep_best_only: bool = True
    add_stereo_downmix: bool = True
    preferred_languages: list[str] = field(default_factory=lambda: ["eng", "und"])
    stereo_encoder: str = "aac"
    stereo_codec: str = "aac"
    stereo_bitrate: str = "160k"
    stereo_title: str = "Stereo"
    channel_score: dict[str, int] = field(
        default_factory=lambda: {"6": 30, "8": 22, "2": 16, "1": 6}
    )
    codec_score: dict[str, int] = field(
        default_factory=lambda: {
            "ac3": 14, "eac3": 14, "aac": 12, "dts": 10,
            "truehd": 8, "flac": 6, "opus": 6, "mp3": 4,
        }
    )
    commentary_pattern: str = r"commentary|descriptive|narration|audio description"


@dataclass
class SubtitlesCfg:
    enabled: bool = True
    keep_languages: list[str] = field(default_factory=lambda: ["eng", "en", "english"])
    undefined_languages: list[str] = field(
        default_factory=lambda: ["", "und", "unk", "unknown", "undefined", "zxx"]
    )
    keep_lone_undefined: bool = True
    drop_image_subs: bool = False
    image_codecs: list[str] = field(
        default_factory=lambda: [
            "hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "xsub",
        ]
    )


@dataclass
class LibOutputCfg:
    container: str = "mkv"
    drop_cover_art: bool = True
    image_codecs: list[str] = field(
        default_factory=lambda: ["mjpeg", "png", "bmp", "gif", "webp"]
    )
    replace_original: bool = True
    only_replace_if_smaller: bool = True


@dataclass
class LibraryCfg:
    id: str = "media"
    name: str = "Media"
    enabled: bool = True
    paths: list[str] = field(default_factory=lambda: ["/media"])
    extensions: list[str] = field(
        default_factory=lambda: [".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".wmv"]
    )
    exclude: list[str] = field(
        default_factory=lambda: ["*/.recycle/*", "*/@eaDir/*", "*/.@__thumb/*"]
    )
    min_size_mb: int = 50
    video: VideoCfg = field(default_factory=VideoCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    subtitles: SubtitlesCfg = field(default_factory=SubtitlesCfg)
    output: LibOutputCfg = field(default_factory=LibOutputCfg)

    def contains(self, path: str) -> str | None:
        """Return the matching root, or None. Used to route a file to a library."""
        target = os.path.normcase(os.path.normpath(str(path)))
        best = None
        for root in self.paths:
            r = os.path.normcase(os.path.normpath(str(root)))
            if target == r or target.startswith(r.rstrip(os.sep) + os.sep):
                if best is None or len(r) > len(best):
                    best = r
        return best


# --- processing modes -------------------------------------------------------

@dataclass
class ModeCfg:
    """A named set of overrides applied on top of a library's profile.

    A mode is a *one-shot* override for a single process request - typically
    from Sonarr or Radarr on import. It is never stored against the file, so
    the next scheduled scan plans that file under its library's normal
    profile again.

    `overrides` uses the same dotted keys as a library's own settings, so
    {"video.enabled": false} means "do everything except re-encode video".
    """

    id: str = "all"
    name: str = "All"
    description: str = ""
    overrides: dict[str, Any] = field(default_factory=dict)


def default_modes() -> list[ModeCfg]:
    return [
        ModeCfg(
            id="all", name="All", overrides={},
            description="The library's full profile: video, audio, subtitles "
                        "and container.",
        ),
        ModeCfg(
            id="cleanup", name="Cleanup", overrides={"video.enabled": False},
            description="Everything except re-encoding video. Audio and "
                        "subtitles are cleaned and the container normalised, "
                        "while every video stream is copied as-is. Fast, and "
                        "a good fit for an on-import hook.",
        ),
    ]


# --- global settings --------------------------------------------------------

@dataclass
class ScheduleCfg:
    enabled: bool = True
    scan_interval_hours: float = 6.0
    scan_on_start: bool = True


@dataclass
class WorkersCfg:
    count: int = 4
    pools: int = 6


@dataclass
class OutputCfg:
    temp_dir: str = "/temp"
    min_duration_ratio: float = 0.98
    chown_uid: int = -1
    chown_gid: int = -1


@dataclass
class WebCfg:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    api_key: str = ""


@dataclass
class Config:
    state_db: str = "/config/state.db"
    dry_run: bool = False
    schedule: ScheduleCfg = field(default_factory=ScheduleCfg)
    workers: WorkersCfg = field(default_factory=WorkersCfg)
    output: OutputCfg = field(default_factory=OutputCfg)
    web: WebCfg = field(default_factory=WebCfg)
    libraries: list[LibraryCfg] = field(default_factory=lambda: [LibraryCfg()])
    modes: list[ModeCfg] = field(default_factory=default_modes)

    def mode(self, mode_id: str) -> ModeCfg | None:
        return next((m for m in self.modes if m.id == mode_id), None)

    def library(self, lib_id: str) -> LibraryCfg | None:
        return next((l for l in self.libraries if l.id == lib_id), None)

    def library_for(self, path: str) -> LibraryCfg | None:
        """The enabled library that owns this path, longest root wins."""
        best: tuple[int, LibraryCfg] | None = None
        for lib in self.libraries:
            if not lib.enabled:
                continue
            root = lib.contains(path)
            if root is not None and (best is None or len(root) > best[0]):
                best = (len(root), lib)
        return best[1] if best else None

    @property
    def active_libraries(self) -> list[LibraryCfg]:
        return [l for l in self.libraries if l.enabled]


# --- field metadata ---------------------------------------------------------
# Drives both the generated file comments and the settings panel. "restart"
# marks fields that only take effect when the process restarts.

SECTIONS: dict[str, str] = {
    "": "General",
    "schedule": "Schedule",
    "workers": "Workers",
    "output": "Encoding",
    "web": "Web panel",
}

MODE_SECTIONS: dict[str, str] = {"": "Mode"}

LIB_SECTIONS: dict[str, str] = {
    "": "Library",
    "video": "Video",
    "audio": "Audio",
    "subtitles": "Subtitles",
    "output": "Output",
}

META: dict[str, dict[str, Any]] = {
    "state_db": {"desc": "Path to the SQLite state database.", "restart": True},
    "dry_run": {"desc": "Plan and log work, but never actually encode anything."},

    "schedule.enabled": {
        "desc": "Run periodic scans. Turn off to only ever scan on demand."},
    "schedule.scan_interval_hours": {
        "desc": "Hours between automatic scans.", "min": 0.05, "max": 720},
    "schedule.scan_on_start": {
        "desc": "Scan immediately on startup instead of waiting a full interval."},

    "workers.count": {
        "desc": "How many files to encode at once, across all libraries.",
        "min": 1, "max": 64, "restart": True},
    "workers.pools": {
        "desc": "x265 thread pool size per worker. count x pools should land "
                "near the host CPU thread count; x265 scales poorly past about "
                "12 threads, so throughput comes from several concurrent "
                "encodes rather than one wide one.",
        "min": 1, "max": 128},

    "output.temp_dir": {
        "desc": "Scratch directory for encodes. Use local disk, not a network "
                "share.", "restart": True},
    "output.min_duration_ratio": {
        "desc": "Output must be at least this fraction of the source duration. "
                "Catches truncated encodes that still exit 0.",
        "min": 0.0, "max": 1.0},
    "output.chown_uid": {
        "desc": "Set owner uid on replaced files. -1 leaves ownership alone.",
        "min": -1, "max": 65535},
    "output.chown_gid": {
        "desc": "Set owner gid on replaced files. -1 leaves ownership alone.",
        "min": -1, "max": 65535},

    "web.enabled": {"desc": "Serve the web panel.", "restart": True},
    "web.host": {"desc": "Address to bind.", "restart": True},
    "web.port": {"desc": "Port to bind.", "min": 1, "max": 65535, "restart": True},
    "web.api_key": {
        "desc": "Key required on every /api/ request, sent as an X-Api-Key "
                "header or an ?apikey= parameter. Generated on first start. "
                "The panel itself is served with the key embedded, so anyone "
                "who can load the panel can read it - this authenticates "
                "Sonarr and Radarr, it does not make the panel safe to expose."},
}


MODE_META: dict[str, dict[str, Any]] = {
    "id": {"desc": "Stable identifier, used as \"mode\" in the API call.",
           "readonly": True},
    "name": {"desc": "Display name for this mode."},
    "description": {"desc": "What this mode does, shown in the panel."},
    "overrides": {
        "desc": "Library settings this mode overrides, as dotted keys - for "
                "example video.enabled = false. Anything not listed is taken "
                "from the library's own profile.",
        "hint": "One library.setting = value per line"},
}

LIB_META: dict[str, dict[str, Any]] = {
    "id": {"desc": "Stable identifier. Generated from the name; leave it alone "
                   "once files are tracked against it.", "readonly": True},
    "name": {"desc": "Display name for this library."},
    "enabled": {"desc": "Include this library in scans."},
    "paths": {"desc": "Directories to scan, as seen from inside the container."},
    "extensions": {"desc": "File extensions considered video files."},
    "exclude": {"desc": "Glob patterns matched against the full path; matches "
                        "are ignored."},
    "min_size_mb": {"desc": "Ignore files smaller than this. Skips samples, "
                            "extras and stray fragments.", "min": 0, "max": 100_000},

    "video.enabled": {
        "desc": "Re-encode video. Turn off to leave every video stream exactly "
                "as it is while still cleaning the other streams."},
    "video.preset": {
        "desc": "x265 preset. Slower is smaller and takes longer.",
        "choices": ["ultrafast", "superfast", "veryfast", "faster", "fast",
                    "medium", "slow", "slower", "veryslow"]},
    "video.crf_720p": {
        "desc": "Quality for 720p sources. Lower is bigger and better.",
        "min": 0, "max": 51},
    "video.crf_1080p": {
        "desc": "Quality for 1080p sources. Lower is bigger and better.",
        "min": 0, "max": 51},
    "video.sd_max_height": {
        "desc": "At or below this height a file is cleaned and remuxed but "
                "never re-encoded.", "min": 0, "max": 4320},
    "video.h720_max_height": {
        "desc": "Upper bound of the 720p band.", "min": 0, "max": 4320},
    "video.h1080_max_height": {
        "desc": "Upper bound of the 1080p band.", "min": 0, "max": 4320},
    "video.skip_above_height": {
        "desc": "Files taller than this are left completely alone.",
        "min": 0, "max": 4320},
    "video.already_encoded": {
        "desc": "Codecs treated as already done and copied, never re-encoded."},

    "audio.enabled": {
        "desc": "Touch audio at all. Turn off to copy every audio track "
                "unchanged."},
    "audio.keep_best_only": {
        "desc": "Drop every audio track except the best one. Turn off to keep "
                "them all."},
    "audio.add_stereo_downmix": {
        "desc": "Add an AAC 2.0 downmix as the default track, for players that "
                "handle surround badly. An existing one is re-used, not rebuilt."},
    "audio.preferred_languages": {
        "desc": "Language tags preferred when choosing the main track, best first."},
    "audio.stereo_encoder": {"desc": "Encoder used for the stereo downmix."},
    "audio.stereo_codec": {
        "desc": "Codec name the encoder produces, used to recognise an existing "
                "downmix so it is re-used instead of rebuilt."},
    "audio.stereo_bitrate": {"desc": "Bitrate for the stereo downmix."},
    "audio.stereo_title": {"desc": "Title tag written on the stereo downmix."},
    "audio.channel_score": {
        "desc": "Channel count to score, when picking the main track."},
    "audio.codec_score": {"desc": "Codec to score, when picking the main track."},
    "audio.commentary_pattern": {
        "desc": "Regex matched against track titles to detect commentary, which "
                "is never chosen as the main or stereo track."},

    "subtitles.enabled": {
        "desc": "Touch subtitles at all. Turn off to copy every subtitle track "
                "unchanged."},
    "subtitles.keep_languages": {
        "desc": "Subtitle language tags to keep. Everything else is dropped."},
    "subtitles.undefined_languages": {"desc": "Tags that count as unlabelled."},
    "subtitles.keep_lone_undefined": {
        "desc": "Keep a single unlabelled subtitle track when no track is "
                "tagged with a kept language."},
    "subtitles.drop_image_subs": {
        "desc": "Drop image-based subtitles (PGS/VOBSUB) as well."},
    "subtitles.image_codecs": {
        "desc": "Codecs considered image-based subtitles."},

    "output.container": {
        "desc": "Container to write. \"keep\" leaves the extension alone, which "
                "means a file needing no other work is never remuxed.",
        "choices": ["mkv", "keep"]},
    "output.drop_cover_art": {
        "desc": "Drop embedded cover art and thumbnail streams."},
    "output.image_codecs": {
        "desc": "Video-stream codecs that are really cover art."},
    "output.replace_original": {
        "desc": "Replace the source file once an encode verifies. Off means "
                "encodes are produced and then discarded, which is useful for "
                "testing settings against real files."},
    "output.only_replace_if_smaller": {
        "desc": "Throw the encode away if it came out bigger than the source."},
}


class ConfigError(ValueError):
    pass


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "map"
    return "str"


def _describe(holder: Any, section: str, meta: dict[str, dict[str, Any]],
              skip: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    entries = []
    for f in fields(holder):
        if f.name in skip:
            continue
        value = getattr(holder, f.name)
        if is_dataclass(value) or isinstance(value, list) and value and \
                is_dataclass(value[0]):
            continue
        key = f"{section}.{f.name}" if section else f.name
        m = meta.get(key, {})
        entries.append({
            "key": key,
            "name": f.name,
            "value": value,
            "type": _kind(value),
            "desc": m.get("desc", ""),
            "min": m.get("min"),
            "max": m.get("max"),
            "choices": m.get("choices"),
            "restart": bool(m.get("restart")),
            "readonly": bool(m.get("readonly")),
            "hint": m.get("hint"),
        })
    return entries


def schema(cfg: Config) -> list[dict[str, Any]]:
    """Describe the global settings, for rendering the settings panel."""
    out = []
    for section, title in SECTIONS.items():
        holder = cfg if section == "" else getattr(cfg, section)
        entries = _describe(holder, section, META, skip=("libraries",))
        if entries:
            out.append({"section": section, "title": title, "fields": entries})
    return out


def library_schema(lib: LibraryCfg) -> list[dict[str, Any]]:
    """Describe one library's settings."""
    out = []
    for section, title in LIB_SECTIONS.items():
        holder = lib if section == "" else getattr(lib, section)
        entries = _describe(holder, section, LIB_META)
        if entries:
            out.append({"section": section, "title": title, "fields": entries})
    return out


def mode_schema(mode: ModeCfg) -> list[dict[str, Any]]:
    """Describe one mode's settings."""
    out = []
    for section, title in MODE_SECTIONS.items():
        holder = mode if section == "" else getattr(mode, section)
        entries = _describe(holder, section, MODE_META)
        if entries:
            out.append({"section": section, "title": title, "fields": entries})
    return out


# --- validation -------------------------------------------------------------

def _coerce(key: str, current: Any, incoming: Any,
            meta: dict[str, dict[str, Any]]) -> Any:
    m = meta.get(key, {})

    if isinstance(current, bool):
        if isinstance(incoming, bool):
            return incoming
        if isinstance(incoming, str):
            return incoming.strip().lower() in ("1", "true", "yes", "on")
        raise ConfigError(f"{key}: expected true or false")

    if isinstance(current, int):
        try:
            value: Any = int(str(incoming).strip())
        except (TypeError, ValueError):
            raise ConfigError(f"{key}: expected a whole number") from None
    elif isinstance(current, float):
        try:
            value = float(str(incoming).strip())
        except (TypeError, ValueError):
            raise ConfigError(f"{key}: expected a number") from None
    elif isinstance(current, list):
        if isinstance(incoming, str):
            value = [p.strip() for p in incoming.split(",")]
        elif isinstance(incoming, list):
            value = [str(p).strip() for p in incoming]
        else:
            raise ConfigError(f"{key}: expected a list")
        # An empty entry is meaningful for subtitles.undefined_languages, where
        # "" stands for a missing language tag, so only trailing blanks go.
        while value and value[-1] == "":
            value.pop()
    elif isinstance(current, dict):
        if not isinstance(incoming, dict):
            raise ConfigError(f"{key}: expected a mapping")
        value = {}
        for k, v in incoming.items():
            try:
                value[str(k)] = int(v)
            except (TypeError, ValueError):
                raise ConfigError(f"{key}: {k} must be a whole number") from None
    else:
        value = str(incoming)

    lo, hi = m.get("min"), m.get("max")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if lo is not None and value < lo:
            raise ConfigError(f"{key}: must be at least {lo}")
        if hi is not None and value > hi:
            raise ConfigError(f"{key}: must be at most {hi}")

    choices = m.get("choices")
    if choices and value not in choices:
        raise ConfigError(f"{key}: must be one of {', '.join(choices)}")

    return value


def _apply(target: Any, updates: dict[str, Any],
           meta: dict[str, dict[str, Any]]) -> list[str]:
    """Validate everything, then apply. A bad field applies nothing."""
    staged: list[tuple[Any, str, Any]] = []
    changed: list[str] = []

    for key, incoming in updates.items():
        section, _, name = key.rpartition(".")
        holder = target if not section else getattr(target, section, None)
        if holder is None or not any(f.name == name for f in fields(holder)):
            raise ConfigError(f"unknown setting: {key}")
        if meta.get(key, {}).get("readonly"):
            raise ConfigError(f"{key} cannot be changed")
        current = getattr(holder, name)
        value = _coerce(key, current, incoming, meta)
        if value != current:
            staged.append((holder, name, value))
            changed.append(key)

    for holder, name, value in staged:
        setattr(holder, name, value)
    return changed


def apply_updates(cfg: Config, updates: dict[str, Any]) -> list[str]:
    changed = _apply(cfg, updates, META)
    _validate_global(cfg)
    return changed


def apply_library_updates(cfg: Config, lib: LibraryCfg,
                          updates: dict[str, Any]) -> list[str]:
    changed = _apply(lib, updates, LIB_META)
    _validate_library(cfg, lib)
    return changed


def _validate_global(cfg: Config) -> None:
    if not cfg.libraries:
        raise ConfigError("at least one library is required")
    ids = [l.id for l in cfg.libraries]
    if len(set(ids)) != len(ids):
        raise ConfigError("library ids must be unique")
    mode_ids = [m.id for m in cfg.modes]
    if len(set(mode_ids)) != len(mode_ids):
        raise ConfigError("mode ids must be unique")


def _validate_profile(lib: LibraryCfg) -> None:
    """The processing rules only. Shared with mode override validation."""
    v = lib.video
    if not (v.sd_max_height <= v.h720_max_height <= v.h1080_max_height):
        raise ConfigError(
            "video height bands must increase: sd_max_height <= "
            "h720_max_height <= h1080_max_height"
        )


def _validate_library(cfg: Config, lib: LibraryCfg) -> None:
    _validate_profile(lib)
    if not lib.paths:
        raise ConfigError("a library needs at least one path")
    if not lib.name.strip():
        raise ConfigError("a library needs a name")

    seen: set[str] = set()
    for root in lib.paths:
        norm = os.path.normcase(os.path.normpath(root))
        if norm in seen:
            raise ConfigError(f"{root} is listed twice")
        seen.add(norm)

    # Overlapping libraries would make routing a file ambiguous: a file under
    # both would get two different profiles depending on match order.
    for other in cfg.libraries:
        if other is lib:
            continue
        for root in lib.paths:
            for other_root in other.paths:
                if _overlaps(root, other_root):
                    raise ConfigError(
                        f"{root} overlaps {other_root} in library "
                        f"{other.name!r}"
                    )


def _overlaps(a: str, b: str) -> bool:
    """True if either path contains the other, or they are the same."""
    na = os.path.normcase(os.path.normpath(a))
    nb = os.path.normcase(os.path.normpath(b))
    if na == nb:
        return True
    return (na.startswith(nb.rstrip(os.sep) + os.sep)
            or nb.startswith(na.rstrip(os.sep) + os.sep))


def slugify(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "library"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def add_library(cfg: Config, name: str, paths: list[str]) -> LibraryCfg:
    if not name.strip():
        raise ConfigError("a library needs a name")
    if not paths:
        raise ConfigError("a library needs at least one path")
    lib = LibraryCfg(
        id=slugify(name, {l.id for l in cfg.libraries}),
        name=name.strip(),
        paths=[str(p).strip() for p in paths if str(p).strip()],
    )
    cfg.libraries.append(lib)
    try:
        _validate_library(cfg, lib)
        _validate_global(cfg)
    except ConfigError:
        cfg.libraries.remove(lib)
        raise
    return lib


def remove_library(cfg: Config, lib_id: str) -> LibraryCfg:
    lib = cfg.library(lib_id)
    if lib is None:
        raise ConfigError(f"no such library: {lib_id}")
    if len(cfg.libraries) == 1:
        raise ConfigError("the last library cannot be removed")
    cfg.libraries.remove(lib)
    return lib


# --- modes ------------------------------------------------------------------

# Identity and routing are the library's business, not a mode's. Letting a
# mode rewrite these would let one API call re-point a library at a different
# directory.
MODE_FORBIDDEN = ("id", "name", "enabled", "paths")


def _validate_mode(cfg: Config, mode: ModeCfg) -> None:
    if not mode.name.strip():
        raise ConfigError("a mode needs a name")
    if not isinstance(mode.overrides, dict):
        raise ConfigError("mode overrides must be a table of dotted keys")

    for key in mode.overrides:
        if key in MODE_FORBIDDEN:
            raise ConfigError(f"a mode cannot override {key}")

    # Apply to a throwaway default library: a typo or an out-of-range value is
    # caught now, when the mode is saved, rather than when Sonarr calls in.
    probe = LibraryCfg()
    _apply(probe, dict(mode.overrides), LIB_META)
    _validate_profile(probe)

    ids = [m.id for m in cfg.modes]
    if len(set(ids)) != len(ids):
        raise ConfigError("mode ids must be unique")


def apply_mode_updates(cfg: Config, mode: ModeCfg,
                       updates: dict[str, Any]) -> list[str]:
    """Update a mode. `overrides` is replaced wholesale, not merged."""
    updates = dict(updates)
    changed: list[str] = []

    if "overrides" in updates:
        incoming = updates.pop("overrides")
        if not isinstance(incoming, dict):
            raise ConfigError("overrides must be an object of dotted keys")
        before = dict(mode.overrides)
        probe = LibraryCfg()
        for key in incoming:
            if key in MODE_FORBIDDEN:
                raise ConfigError(f"a mode cannot override {key}")
        # Coerce through the library schema so "false" becomes False and the
        # stored overrides are typed the same as the settings they replace.
        _apply(probe, dict(incoming), LIB_META)
        _validate_profile(probe)
        typed = {}
        for key in incoming:
            section, _, name = key.rpartition(".")
            holder = probe if not section else getattr(probe, section)
            typed[key] = getattr(holder, name)
        if typed != before:
            mode.overrides = typed
            changed.append("overrides")

    changed += _apply(mode, updates, MODE_META)
    _validate_mode(cfg, mode)
    return changed


def add_mode(cfg: Config, name: str,
             overrides: dict[str, Any] | None = None) -> ModeCfg:
    if not name.strip():
        raise ConfigError("a mode needs a name")
    mode = ModeCfg(id=slugify(name, {m.id for m in cfg.modes}), name=name.strip())
    cfg.modes.append(mode)
    try:
        if overrides:
            apply_mode_updates(cfg, mode, {"overrides": overrides})
        else:
            _validate_mode(cfg, mode)
    except ConfigError:
        cfg.modes.remove(mode)
        raise
    return mode


def remove_mode(cfg: Config, mode_id: str) -> ModeCfg:
    mode = cfg.mode(mode_id)
    if mode is None:
        raise ConfigError(f"no such mode: {mode_id}")
    if len(cfg.modes) == 1:
        raise ConfigError("the last mode cannot be removed")
    cfg.modes.remove(mode)
    return mode


def resolve_library(cfg: Config, lib: LibraryCfg,
                    mode_id: str | None) -> LibraryCfg:
    """The profile to plan with: the library, or a copy under a mode.

    The returned library is a detached copy, so nothing a mode changes leaks
    back into the live config or into any other file being processed.
    """
    if not mode_id:
        return lib
    mode = cfg.mode(mode_id)
    if mode is None:
        known = ", ".join(m.id for m in cfg.modes) or "none"
        raise ConfigError(f"no such mode: {mode_id} (known modes: {known})")
    if not mode.overrides:
        return lib
    derived = copy.deepcopy(lib)
    _apply(derived, dict(mode.overrides), LIB_META)
    _validate_profile(derived)
    return derived


# --- TOML round-trip --------------------------------------------------------

def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f'"{k}" = {_fmt(v)}' for k, v in value.items()) + " }"
    escaped = (
        str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )
    return f'"{escaped}"'


def _wrap_comment(desc: str, out: list[str]) -> None:
    line = "#"
    for word in desc.split():
        if len(line) + len(word) + 1 > 76:
            out.append(line)
            line = "#"
        line += " " + word
    if line != "#":
        out.append(line)


def _emit(blocks: list[dict[str, Any]], header: str, out: list[str]) -> None:
    for block in blocks:
        name = ".".join(p for p in (header, block["section"]) if p)
        if name:
            out.append(f"[{name}]")
        for entry in block["fields"]:
            if entry["desc"]:
                _wrap_comment(entry["desc"], out)
            if entry["restart"]:
                out.append("# (takes effect on restart)")
            out.append(f"{entry['name']} = {_fmt(entry['value'])}")
            out.append("")
        while out and out[-1] == "":
            out.pop()
        out.append("")


def dump_toml(cfg: Config) -> str:
    out = [
        "# Generated by the transcoder web panel - read on startup, and",
        "# rewritten whenever a setting is changed in the UI.",
        "",
    ]
    _emit(schema(cfg), "", out)

    for lib in cfg.libraries:
        out.append("")
        out.append("# " + "-" * 70)
        out.append(f"# Library: {lib.name}")
        out.append("# " + "-" * 70)
        blocks = library_schema(lib)
        for block in blocks:
            if block["section"] == "":
                out.append("[[libraries]]")
            else:
                out.append(f"[libraries.{block['section']}]")
            for entry in block["fields"]:
                if entry["desc"]:
                    _wrap_comment(entry["desc"], out)
                out.append(f"{entry['name']} = {_fmt(entry['value'])}")
                out.append("")
            while out and out[-1] == "":
                out.pop()
            out.append("")

    if cfg.modes:
        out.append("")
        out.append("# " + "-" * 70)
        out.append("# Processing modes")
        out.append("#")
        out.append("# A mode is a one-shot override for a single /api/process")
        out.append("# call - typically from Sonarr or Radarr on import. It is")
        out.append("# not remembered against the file, so the next scheduled")
        out.append("# scan plans it under its library's normal profile again.")
        out.append("# " + "-" * 70)
        for mode in cfg.modes:
            out.append("[[modes]]")
            for entry in mode_schema(mode)[0]["fields"]:
                if entry["desc"]:
                    _wrap_comment(entry["desc"], out)
                out.append(f"{entry['name']} = {_fmt(entry['value'])}")
                out.append("")
            while out and out[-1] == "":
                out.pop()
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def save(cfg: Config, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(dump_toml(cfg), encoding="utf-8")
    tmp.replace(p)


# --- loading ----------------------------------------------------------------

def _fill(obj: Any, data: dict[str, Any], where: str) -> None:
    known = {f.name for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"unknown config key: {where}{key}")
        current = getattr(obj, key)
        if is_dataclass(current) and isinstance(value, dict):
            _fill(current, value, f"{where}{key}.")
        else:
            setattr(obj, key, value)


def _from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    data = dict(data)
    raw_libs = data.pop("libraries", None)
    raw_modes = data.pop("modes", None)

    _fill(cfg, data, "")

    if raw_libs is not None:
        cfg.libraries = []
        for i, raw in enumerate(raw_libs):
            lib = LibraryCfg()
            _fill(lib, raw, f"libraries[{i}].")
            if not raw.get("id"):
                lib.id = slugify(lib.name, {l.id for l in cfg.libraries})
            cfg.libraries.append(lib)

    if raw_modes is not None:
        cfg.modes = []
        for i, raw in enumerate(raw_modes):
            mode = ModeCfg()
            _fill(mode, raw, f"modes[{i}].")
            if not raw.get("id"):
                mode.id = slugify(mode.name, {m.id for m in cfg.modes})
            cfg.modes.append(mode)

    _validate_global(cfg)
    return cfg


def load(path: str | Path | None) -> Config:
    if path is None:
        return Config()
    p = Path(path)
    if not p.exists():
        return Config()
    with p.open("rb") as fh:
        return _from_dict(tomllib.load(fh))


def loads(text: str) -> Config:
    return _from_dict(tomllib.loads(text))
