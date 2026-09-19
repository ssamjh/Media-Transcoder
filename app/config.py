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
    crf_sd: int = 23
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
    add_stereo_downmix: bool = True
    keep_stereo_only: bool = False
    preferred_languages: list[str] = field(default_factory=lambda: ["eng"])
    downmix_channels: list[str] = field(default_factory=lambda: ["6", "8"])
    # Codecs whose bitstream carries the mix engineer's own Lo/Ro downmix
    # coefficients. For these the decoder is asked for stereo directly, which
    # is a better fold-down than any matrix this tool could apply.
    downmix_metadata_codecs: list[str] = field(
        default_factory=lambda: ["ac3", "eac3", "dts", "truehd"]
    )
    downmix_request: str = "-downmix stereo"
    stereo_encoder: str = "auto"
    stereo_codec: str = "aac"
    stereo_bitrate: str = "192k"
    # A track that is already 2.0 but in the wrong codec is re-encoded only at
    # a rate below its source rate: lifting a 128k mp3 to 192k AAC buys nothing
    # but size. Source rates at or below the two thresholds get the matching
    # target; an equal rung steps down, while no safe rung or an unknown source
    # rate leaves the original track copied. Either threshold at "0" switches
    # its band off.
    stereo_bitrate_mid: str = "128k"
    stereo_bitrate_low: str = "96k"
    mid_max_source_bitrate: str = "160k"
    low_max_source_bitrate: str = "112k"
    stereo_title: str = "Stereo"
    commentary_pattern: str = (
        r"commentary|comment|director|cast|crew|isolated|descriptive"
        r"|audio description|narration|sign language"
    )


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
    # An accepted encode has to land inside this window, as a fraction of the
    # source size. The ceiling catches work that did not pay off; the floor
    # catches an encode that came out impossibly small - which, now that a
    # run can *add* a stereo track, is a far more useful pair of questions
    # than "is it smaller".
    min_size_ratio: float = 0.30
    max_size_ratio: float = 1.10


@dataclass
class NotifyCfg:
    """Webhooks fired once a processed file has been copied back.

    Part of the library profile rather than global config, because whether
    Jellyfin should be told about a file is a property of the library the
    file is in - and because a mode can then override it for one request,
    which is how an import hook adds its own callback.
    """

    enabled: bool = False
    urls: list[str] = field(default_factory=list)
    method: str = "POST"
    headers: list[str] = field(default_factory=list)
    timeout: float = 15.0
    retries: int = 3


@dataclass
class LibraryCfg:
    """Where files are and which of them count - not what happens to them.

    What happens to a file is a processing mode, named here and defined once
    in `Config.modes`. Two libraries that should be treated the same point at
    the same mode instead of carrying two copies of the same settings that
    drift apart.
    """

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
    mode: str = "standard"

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
    """A complete, named set of processing settings.

    One mode says everything about what a run does to a file: which video it
    re-encodes, how audio and subtitles are cleaned, what container comes
    out, and who gets told afterwards. A library names the mode it uses, and
    a single /api/process call can name a different one for that file only -
    which is how an import hook does less work than a scheduled scan without
    a second copy of the settings existing anywhere.
    """

    id: str = "standard"
    name: str = "Standard"
    description: str = ""
    video: VideoCfg = field(default_factory=VideoCfg)
    audio: AudioCfg = field(default_factory=AudioCfg)
    subtitles: SubtitlesCfg = field(default_factory=SubtitlesCfg)
    output: LibOutputCfg = field(default_factory=LibOutputCfg)
    notify: NotifyCfg = field(default_factory=NotifyCfg)


@dataclass
class Profile:
    """What one file gets: a library's identity with a mode's settings.

    Planning works from one of these, so nothing downstream needs to know
    whether the settings came from the library's own mode or from a one-shot
    override on an import call. `id` and `name` are the library's, because
    that is what the file's tracked state belongs to.
    """

    id: str
    name: str
    mode: str
    mode_name: str
    video: VideoCfg
    audio: AudioCfg
    subtitles: SubtitlesCfg
    output: LibOutputCfg
    notify: NotifyCfg


def default_modes() -> list[ModeCfg]:
    cleanup = ModeCfg(
        id="cleanup", name="Cleanup",
        description="Everything except re-encoding video. Audio and subtitles "
                    "are cleaned and the container normalised, while every "
                    "video stream is copied as-is. Fast, and a good fit for "
                    "an on-import hook.",
    )
    cleanup.video.enabled = False
    return [
        ModeCfg(
            id="standard", name="Standard",
            description="Re-encode to x265, keep the best audio track plus an "
                        "AAC stereo downmix, keep the configured subtitle "
                        "languages, normalise the container.",
        ),
        cleanup,
    ]


# --- global settings --------------------------------------------------------

@dataclass
class ScheduleCfg:
    enabled: bool = True
    scan_interval_hours: float = 6.0
    # Both default to off, so nothing is ever encoded that the user did not
    # ask for: a scan plans and reports, and the work waits in `pending`
    # until it is queued from the panel or this switch is turned on.
    scan_on_start: bool = False
    process_after_scan: bool = False


@dataclass
class WorkersCfg:
    # Sized for an ordinary 8-thread host: 2 x 4 keeps every thread busy
    # without oversubscribing. Raise both together on a bigger box.
    count: int = 2
    pools: int = 4


@dataclass
class OutputCfg:
    # Inside the container by default, so it needs no mount and no
    # permissions of its own. Point it at real disk if /tmp is small.
    temp_dir: str = "/tmp/transcoder"
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
    # No library by default: a fresh install has nothing to scan until
    # someone points it at a path, and guessing /media would start a scan
    # of whatever happened to be mounted there.
    libraries: list[LibraryCfg] = field(default_factory=list)
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

# A library is routing; a mode is everything that happens to a file.
LIB_SECTIONS: dict[str, str] = {"": "Library"}

MODE_SECTIONS: dict[str, str] = {
    "": "Mode",
    "video": "Video",
    "audio": "Audio",
    "subtitles": "Subtitles",
    "output": "Output",
    "notify": "Notifications",
}

META: dict[str, dict[str, Any]] = {
    "state_db": {"desc": "Path to the SQLite state database.", "restart": True},
    "dry_run": {"desc": "Plan and log work, but never actually encode anything."},

    "schedule.enabled": {
        "desc": "Run periodic scans. Turn off to only ever scan on demand."},
    "schedule.scan_interval_hours": {
        "desc": "Hours between automatic scans.", "min": 0.05, "max": 720},
    "schedule.scan_on_start": {
        "desc": "Scan immediately on startup instead of waiting a full "
                "interval."},
    "schedule.process_after_scan": {
        "desc": "Queue everything a scan finds, instead of leaving it pending "
                "for you to review and queue yourself. Off means no file is "
                "ever encoded without being asked for."},

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
                "share. Needs room for one in-progress encode per worker.",
        "restart": True},
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
    "mode": {"desc": "Processing mode this library is treated with. Defined "
                     "under Modes, and shared with any other library that "
                     "should behave the same way.",
             "choices": []},
}


MODE_META: dict[str, dict[str, Any]] = {
    "id": {"desc": "Stable identifier, used as \"mode\" in the API call.",
           "readonly": True},
    "name": {"desc": "Display name for this mode."},
    "description": {"desc": "What this mode does, shown in the panel."},

    "video.enabled": {
        "desc": "Re-encode video. Turn off to leave every video stream exactly "
                "as it is while still cleaning the other streams."},
    "video.preset": {
        "desc": "x265 preset. Slower is smaller and takes longer.",
        "choices": ["ultrafast", "superfast", "veryfast", "faster", "fast",
                    "medium", "slow", "slower", "veryslow"]},
    "video.crf_sd": {
        "desc": "Quality for SD sources. Separate from the HD bands because "
                "x265 at an HD CRF is wasteful on a 480p source.",
        "min": 0, "max": 51},
    "video.crf_720p": {
        "desc": "Quality for 720p sources. Lower is bigger and better.",
        "min": 0, "max": 51},
    "video.crf_1080p": {
        "desc": "Quality for 1080p sources. Lower is bigger and better.",
        "min": 0, "max": 51},
    "video.sd_max_height": {
        "desc": "At or below this height a file is treated as SD and encoded "
                "with video.crf_sd.", "min": 0, "max": 4320},
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
    "audio.add_stereo_downmix": {
        "desc": "Make sure the file ends up with an AAC 2.0 track, set default, "
                "for players that handle surround badly. An existing stereo "
                "track is re-used - converted to AAC if a lower configured "
                "bitrate makes it smaller, otherwise copied unchanged - "
                "and only a file with none gets one folded down from its "
                "surround mix. Off copies every track exactly as it arrived."},
    "audio.keep_stereo_only": {
        "desc": "Once the stereo track exists, drop every other audio track. "
                "Commentary and described audio are always kept, because "
                "nothing else in the file stands in for them. Off keeps every "
                "original track alongside the stereo one."},
    "audio.preferred_languages": {
        "desc": "Language tags a track must carry to be considered for the "
                "stereo track, either as one already there or as the surround "
                "mix folded down."},
    "audio.downmix_channels": {
        "desc": "Channel counts eligible to be folded down to stereo. The "
                "widest track wins, then the highest bitrate, then the lowest "
                "stream index."},
    "audio.downmix_request": {
        "desc": "Decoder option asking for a stereo fold-down, applied to the "
                "codecs listed above. This was -request_channel_layout stereo "
                "until ffmpeg 7 removed it in favour of -downmix; both mean "
                "\"decode to stereo using the coefficients in the bitstream\". "
                "Blank falls back to the matrix downmix for every codec."},
    "audio.downmix_metadata_codecs": {
        "desc": "Codecs that carry their own Lo/Ro downmix coefficients. These "
                "are folded down by the decoder, which applies the mix "
                "engineer's own settings; anything else uses the standard "
                "matrix, normalised so the sum cannot clip."},
    "audio.stereo_encoder": {
        "desc": "AAC encoder. \"auto\" uses libfdk_aac when this ffmpeg build "
                "has it and the native encoder otherwise.",
        "choices": ["auto", "aac", "libfdk_aac"]},
    "audio.stereo_codec": {
        "desc": "Codec name the encoder produces, used to recognise an existing "
                "stereo track so it is re-used instead of rebuilt."},
    "audio.stereo_bitrate": {
        "desc": "Bitrate for the stereo track. Every fold-down from a surround "
                "mix gets this; a track that was already 2.0 gets it only when "
                "it is below the source bitrate. Equal or larger choices step "
                "down to a safe configured rung; if none exists, or the source "
                "rate is unknown, the original stereo track is copied."},
    "audio.stereo_bitrate_mid": {
        "desc": "Bitrate for an existing 2.0 track whose own bitrate is at or "
                "below mid_max_source_bitrate."},
    "audio.stereo_bitrate_low": {
        "desc": "Bitrate for an existing 2.0 track whose own bitrate is at or "
                "below low_max_source_bitrate."},
    "audio.mid_max_source_bitrate": {
        "desc": "Source bitrate at or below which an existing 2.0 track is "
                "re-encoded at stereo_bitrate_mid. \"0\" turns the band off."},
    "audio.low_max_source_bitrate": {
        "desc": "Source bitrate at or below which an existing 2.0 track is "
                "re-encoded at stereo_bitrate_low. \"0\" turns the band off."},
    "audio.stereo_title": {"desc": "Title tag written on the stereo track."},
    "audio.commentary_pattern": {
        "desc": "Regex matched against track titles to detect commentary and "
                "described audio, which is never folded down to stereo and is "
                "never dropped by keep_stereo_only. The comment and "
                "visual_impaired dispositions are honoured as well."},

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
    "output.min_size_ratio": {
        "desc": "Reject an encode smaller than this fraction of the source - "
                "an implausible shrink usually means something was lost. 0 "
                "turns the floor off.",
        "min": 0.0, "max": 1.0},
    "output.max_size_ratio": {
        "desc": "Reject an encode larger than this fraction of the source. "
                "Only asked of a run that actually re-encoded the video - a "
                "run that copied it has no way to shrink the file and every "
                "reason to grow it slightly. A rejected x265 pass is rebuilt "
                "around the source video stream rather than thrown away, so "
                "the audio and subtitle work still lands. 10 effectively "
                "turns the ceiling off.",
        "min": 0.1, "max": 10.0},

    "notify.enabled": {
        "desc": "Call other applications once a processed file has been "
                "copied back over the original. Nothing is called for a file "
                "that needed no work, or for one that failed."},
    "notify.urls": {
        "desc": "URLs to call, one per file, in order. {path} {name} {stem} "
                "{dir} {library} {mode} {status} are substituted and URL "
                "encoded - for example "
                "http://jellyfin:8096/Library/Media/Updated?api_key=KEY. "
                "Calls are queued and delivered in the background, so a slow "
                "or dead service never holds up an encode.",
        "hint": "One URL per line"},
    "notify.method": {
        "desc": "HTTP method. POST and PUT send the file's details as a JSON "
                "body; GET, HEAD and DELETE send no body, so put everything "
                "the far end needs in the URL.",
        "choices": ["POST", "PUT", "GET", "HEAD", "DELETE"]},
    "notify.headers": {
        "desc": "Extra request headers, one \"Name: value\" per line. The "
                "same {tokens} are substituted here, so an API key header is "
                "written literally: X-Api-Key: abc123.",
        "hint": "One Name: value per line"},
    "notify.timeout": {
        "desc": "Seconds to wait for each call.", "min": 0.5, "max": 300},
    "notify.retries": {
        "desc": "Attempts per URL. A timeout, a connection error or a 5xx is "
                "retried with a growing delay; a 4xx is not, because it will "
                "not start working. Delivery is best effort - a webhook that "
                "never succeeds is logged and dropped, never re-runs the "
                "encode.", "min": 1, "max": 10},
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


def library_schema(lib: LibraryCfg,
                   cfg: Config | None = None) -> list[dict[str, Any]]:
    """Describe one library's settings.

    The mode field's choices are whatever modes exist, so the panel offers a
    picker rather than a free-text id nobody can verify.
    """
    out = []
    for section, title in LIB_SECTIONS.items():
        holder = lib if section == "" else getattr(lib, section)
        entries = _describe(holder, section, LIB_META)
        if cfg is not None:
            for entry in entries:
                if entry["key"] == "mode":
                    entry["choices"] = [m.id for m in cfg.modes]
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

def parse_bitrate(text: str) -> int | None:
    """Bits per second from an ffmpeg-style rate, or None if it is not one.

    Bitrates are written the way ffmpeg takes them - "192k", "1.5M", or a
    bare count of bits - because they are handed straight to it. Planning has
    to compare them against a probed rate, so the same reading is used to
    validate what is saved and to place a track on the ladder.
    """
    raw = str(text).strip()
    if not raw:
        return None
    scale = {"k": 1_000, "m": 1_000_000}.get(raw[-1].lower())
    try:
        value = float(raw[:-1] if scale else raw) * (scale or 1)
    except ValueError:
        return None
    return int(value) if value >= 0 else None


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


def _transactional(target: Any, apply: Any, validate: Any) -> list[str]:
    """Apply then validate, restoring the target if validation refuses.

    _apply coerces before it writes, so a bad *value* never lands - but a
    rule about the whole object (height bands, a size window, a mode that
    does not exist) can only be checked afterwards, and a half-applied
    profile would be live for every library pointing at it.
    """
    before = copy.deepcopy(target)
    try:
        changed = apply()
        validate()
    except ConfigError:
        for f in fields(target):
            setattr(target, f.name, getattr(before, f.name))
        raise
    return changed


def apply_library_updates(cfg: Config, lib: LibraryCfg,
                          updates: dict[str, Any]) -> list[str]:
    return _transactional(lib,
                          lambda: _apply(lib, updates, LIB_META),
                          lambda: _validate_library(cfg, lib))


def apply_mode_updates(cfg: Config, mode: ModeCfg,
                       updates: dict[str, Any]) -> list[str]:
    return _transactional(mode,
                          lambda: _apply(mode, updates, MODE_META),
                          lambda: _validate_mode(cfg, mode))


def _validate_global(cfg: Config) -> None:
    ids = [l.id for l in cfg.libraries]
    if len(set(ids)) != len(ids):
        raise ConfigError("library ids must be unique")
    mode_ids = [m.id for m in cfg.modes]
    if len(set(mode_ids)) != len(mode_ids):
        raise ConfigError("mode ids must be unique")
    for lib in cfg.libraries:
        if cfg.mode(lib.mode) is None:
            known = ", ".join(mode_ids) or "none"
            raise ConfigError(
                f"library {lib.id} names mode {lib.mode!r}, which does not "
                f"exist (known modes: {known})")


def _validate_profile(lib: ModeCfg | Profile) -> None:
    """The processing rules, on a mode or on a resolved profile."""
    v = lib.video
    if not (v.sd_max_height <= v.h720_max_height <= v.h1080_max_height):
        raise ConfigError(
            "video height bands must increase: sd_max_height <= "
            "h720_max_height <= h1080_max_height"
        )

    a = lib.audio
    rates: dict[str, int] = {}
    for name in ("stereo_bitrate", "stereo_bitrate_mid", "stereo_bitrate_low",
                 "mid_max_source_bitrate", "low_max_source_bitrate"):
        parsed = parse_bitrate(getattr(a, name))
        if parsed is None:
            raise ConfigError(
                f"audio.{name}: {getattr(a, name)!r} is not a bitrate "
                f"(try \"192k\", \"1.5M\", or a count of bits)")
        rates[name] = parsed
    if not (rates["stereo_bitrate_low"] <= rates["stereo_bitrate_mid"]
            <= rates["stereo_bitrate"]):
        raise ConfigError(
            "audio bitrate bands must increase: stereo_bitrate_low <= "
            "stereo_bitrate_mid <= stereo_bitrate"
        )
    if rates["low_max_source_bitrate"] > rates["mid_max_source_bitrate"]:
        raise ConfigError(
            "audio.low_max_source_bitrate must not be above "
            "audio.mid_max_source_bitrate, or the low band could never be hit"
        )

    o = lib.output
    if o.min_size_ratio and o.max_size_ratio and o.min_size_ratio > o.max_size_ratio:
        raise ConfigError(
            "output.min_size_ratio must not be above output.max_size_ratio, "
            "or no encode could ever be accepted"
        )

    for url in lib.notify.urls:
        if not str(url).strip().lower().startswith(("http://", "https://")):
            raise ConfigError(
                f"notify.urls: {url!r} must start with http:// or https://")
    for line in lib.notify.headers:
        name, sep, _ = str(line).partition(":")
        if not sep or not name.strip():
            raise ConfigError(
                f"notify.headers: {line!r} must be in the form \"Name: value\"")


def _validate_library(cfg: Config, lib: LibraryCfg) -> None:
    if cfg.mode(lib.mode) is None:
        known = ", ".join(m.id for m in cfg.modes) or "none"
        raise ConfigError(
            f"no such processing mode: {lib.mode} (known modes: {known})")
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
        # Off until someone turns it on: a new library is a set of paths
        # nobody has reviewed a profile for yet, and enabling it is the one
        # deliberate step between "I typed a path" and "it started work".
        enabled=False,
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
    cfg.libraries.remove(lib)
    return lib


# --- modes ------------------------------------------------------------------

def _validate_mode(cfg: Config, mode: ModeCfg) -> None:
    if not mode.name.strip():
        raise ConfigError("a mode needs a name")
    _validate_profile(mode)
    ids = [m.id for m in cfg.modes]
    if len(set(ids)) != len(ids):
        raise ConfigError("mode ids must be unique")


def add_mode(cfg: Config, name: str, copy_from: str | None = None) -> ModeCfg:
    """Create a mode, optionally starting from an existing one.

    Copying is the usual way in: a new mode is nearly always an existing one
    with two settings changed, and starting from the defaults would mean
    re-entering the rest by hand.
    """
    if not name.strip():
        raise ConfigError("a mode needs a name")
    if copy_from:
        source = cfg.mode(copy_from)
        if source is None:
            raise ConfigError(f"no such mode: {copy_from}")
        mode = copy.deepcopy(source)
        mode.description = ""
    else:
        mode = ModeCfg()
    mode.id = slugify(name, {m.id for m in cfg.modes})
    mode.name = name.strip()
    cfg.modes.append(mode)
    try:
        _validate_mode(cfg, mode)
    except ConfigError:
        cfg.modes.remove(mode)
        raise
    return mode


def remove_mode(cfg: Config, mode_id: str) -> ModeCfg:
    mode = cfg.mode(mode_id)
    if mode is None:
        raise ConfigError(f"no such mode: {mode_id}")
    users = [l.name for l in cfg.libraries if l.mode == mode_id]
    if users:
        raise ConfigError(
            f"{mode.name} is in use by {', '.join(users)} - point "
            f"{'them' if len(users) > 1 else 'it'} at another mode first")
    cfg.modes.remove(mode)
    return mode


def resolve(cfg: Config, lib: LibraryCfg,
            mode_id: str | None = None) -> Profile:
    """The profile to plan a file with.

    `mode_id` is the one-shot override an import hook passes; without one a
    file is treated with its library's own mode. The settings are deep
    copied, so nothing that happens to one file can leak into the live config
    or into another file being processed at the same time.
    """
    mode = cfg.mode(mode_id or lib.mode)
    if mode is None:
        known = ", ".join(m.id for m in cfg.modes) or "none"
        raise ConfigError(
            f"no such mode: {mode_id or lib.mode} (known modes: {known})")
    return Profile(
        id=lib.id, name=lib.name, mode=mode.id, mode_name=mode.name,
        video=copy.deepcopy(mode.video),
        audio=copy.deepcopy(mode.audio),
        subtitles=copy.deepcopy(mode.subtitles),
        output=copy.deepcopy(mode.output),
        notify=copy.deepcopy(mode.notify),
    )


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

    if not cfg.libraries:
        out.append("")
        out.append("# " + "-" * 70)
        out.append("# No libraries are configured, so nothing is scanned.")
        out.append("# Add one in the web panel, or by hand:")
        out.append("#")
        out.append("#   [[libraries]]")
        out.append("#   name = \"Media\"")
        out.append("#   paths = [\"/media\"]")
        out.append("# " + "-" * 70)
        out.append("")

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
        out.append("# A mode is everything that happens to a file. A library")
        out.append("# names the mode it is treated with, and a single")
        out.append("# /api/process call can name a different one for that")
        out.append("# file only - which is how an import hook does less work")
        out.append("# than a scheduled scan without a second copy of these")
        out.append("# settings existing anywhere.")
        out.append("# " + "-" * 70)
        for mode in cfg.modes:
            out.append("")
            for block in mode_schema(mode):
                if block["section"] == "":
                    out.append("[[modes]]")
                else:
                    out.append(f"[modes.{block['section']}]")
                for entry in block["fields"]:
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


def _migrate_library(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate settings that have been replaced, so old files still load.

    _fill rejects unknown keys, which is what catches typos - so a renamed
    setting has to be handled here or every existing config.toml would fail
    to load on upgrade.
    """
    out = dict(raw)
    output = dict(out.get("output") or {})
    if "only_replace_if_smaller" in output:
        # Became a size window. True was "it must shrink", false was "accept
        # whatever comes out"; neither said anything about a floor, so the
        # floor stays off for a config that predates it.
        must_shrink = bool(output.pop("only_replace_if_smaller"))
        output.setdefault("max_size_ratio", 1.0 if must_shrink else 10.0)
        output.setdefault("min_size_ratio", 0.0)
        out["output"] = output
    return out


PROFILE_SECTIONS = ("video", "audio", "subtitles", "output", "notify")


def _migrate_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate settings that were renamed, so old files still load.

    _fill rejects unknown keys, which is what catches typos - so a renamed
    setting has to be handled here or every existing config.toml would fail
    to load on upgrade.
    """
    out = dict(raw)
    output = dict(out.get("output") or {})
    if "only_replace_if_smaller" in output:
        # Became a size window. True was "it must shrink", false was "accept
        # whatever comes out"; neither said anything about a floor, so the
        # floor stays off for a config that predates it.
        must_shrink = bool(output.pop("only_replace_if_smaller"))
        output.setdefault("max_size_ratio", 1.0 if must_shrink else 10.0)
        output.setdefault("min_size_ratio", 0.0)
        out["output"] = output

    audio = dict(out.get("audio") or {})
    if audio:
        # Track selection used to be a scoring contest that picked one "best"
        # track to keep. It is now an explicit rule - widest surround track in
        # a wanted language, commentary excluded - so the score tables have
        # nothing left to weigh, and "keep only the best" has become "keep
        # only the stereo track", which is the same intent one step on.
        if "keep_best_only" in audio:
            audio.setdefault("keep_stereo_only", bool(audio.pop("keep_best_only")))
            audio.pop("keep_best_only", None)
        audio.pop("channel_score", None)
        audio.pop("codec_score", None)

        # A fold-down and a straight 2.0 transcode used to get their own
        # bitrates. One setting covers both now, so the pair has to collapse
        # to a single value: a downmix bitrate the user never touched was
        # only ever the old default, and the transcode value is the better
        # number, so that one survives - but an explicit choice is theirs and
        # is kept.
        convert = audio.pop("stereo_convert_bitrate", None)
        if convert is not None and audio.get("stereo_bitrate", "160k") == "160k":
            audio["stereo_bitrate"] = convert
        out["audio"] = audio
    return out


def _split_library(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate a pre-modes library into its routing and its settings.

    Libraries used to carry a full profile of their own. Those settings are
    now a mode, so they are lifted out here and the library is left pointing
    at one.
    """
    routing = {k: v for k, v in raw.items() if k not in PROFILE_SECTIONS}
    profile = {k: v for k, v in raw.items() if k in PROFILE_SECTIONS}
    return routing, _migrate_settings(profile)


def _mode_for(cfg: Config, settings: dict[str, Any], name: str,
              description: str = "") -> str:
    """Find or create a mode matching these settings, and return its id.

    Two libraries configured identically - the common case - end up sharing
    one mode rather than getting a copy each, which is the whole point of
    the change.
    """
    candidate = ModeCfg()
    _fill(candidate, settings, "")
    for existing in cfg.modes:
        same = all(getattr(existing, sec) == getattr(candidate, sec)
                   for sec in PROFILE_SECTIONS)
        if same:
            return existing.id
    candidate.id = slugify(name, {m.id for m in cfg.modes})
    candidate.name = name
    candidate.description = description
    cfg.modes.append(candidate)
    return candidate.id


def _from_dict(data: dict[str, Any]) -> Config:
    cfg = Config()
    data = dict(data)
    raw_libs = data.pop("libraries", None)
    raw_modes = data.pop("modes", None)

    _fill(cfg, data, "")

    if raw_modes is not None:
        cfg.modes = []
        for i, raw in enumerate(raw_modes):
            raw = dict(raw)
            # Modes used to be a set of dotted overrides on top of a library's
            # profile. They are the profile now, so an old one is read as the
            # default settings with its overrides applied.
            overrides = raw.pop("overrides", None)
            mode = ModeCfg()
            _fill(mode, _migrate_settings(raw), f"modes[{i}].")
            if overrides:
                _apply(mode, dict(overrides), MODE_META)
            if not raw.get("id"):
                mode.id = slugify(mode.name, {m.id for m in cfg.modes})
            cfg.modes.append(mode)

    if raw_libs is not None:
        cfg.libraries = []
        for i, raw in enumerate(raw_libs):
            routing, profile = _split_library(raw)
            lib = LibraryCfg()
            _fill(lib, routing, f"libraries[{i}].")
            if not routing.get("id"):
                lib.id = slugify(lib.name, {l.id for l in cfg.libraries})
            if profile:
                # Pre-modes library: its own settings become a mode, shared
                # with any other library configured identically.
                lib.mode = _mode_for(
                    cfg, profile, lib.name,
                    f"Migrated from the {lib.name} library's own settings.")
            elif "mode" not in routing and cfg.mode(lib.mode) is None:
                # No settings and no mode named: it ran on the defaults.
                lib.mode = _mode_for(cfg, {}, "Standard")
            cfg.libraries.append(lib)

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
