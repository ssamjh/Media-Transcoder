"""Decide what, if anything, needs doing to a file.

Planning is pure: probe dict in, plan out, no side effects. That makes the
rules unit testable and - critically - makes them *idempotent*. Planning a
file this tool already produced must come back with needs_work=False, or the
library would be reprocessed on every scan forever.

Every rule is driven by the owning library's profile, and each stage has an
`enabled` switch. A disabled stage copies its streams through untouched, so a
library can clean audio while leaving subtitles alone, or clean everything
without re-encoding any video.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, Profile
from .probe import (
    Probe, channels_of, codec_of, is_attached_pic, is_default, lang_of, title_of,
)


@dataclass
class StreamPlan:
    """One output stream: which source stream it comes from and how."""

    src_index: int
    kind: str                       # video | audio | subtitle
    codec: str                      # "copy", "libx265", "aac", ...
    # Extra args; "{i}" is substituted with the *output* stream index.
    extra: list[str] = field(default_factory=list)
    disposition: str | None = None  # "default" | "0" | None (leave alone)
    title: str | None = None
    note: str = ""

    @property
    def is_encode(self) -> bool:
        return self.codec != "copy"


@dataclass
class FilePlan:
    path: str
    library: str = ""
    library_name: str = ""
    mode: str = ""
    mode_name: str = ""
    container: str = "mkv"
    streams: list[StreamPlan] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    skip_reason: str | None = None
    height: int = 0
    src_video_codec: str = ""
    duration: float = 0.0
    size: int = 0

    @property
    def needs_work(self) -> bool:
        return self.skip_reason is None and bool(self.reasons)

    @property
    def video_summary(self) -> str:
        if self.skip_reason:
            return f"skip ({self.skip_reason})"
        if any(s.kind == "video" and s.is_encode for s in self.streams):
            return f"{self.src_video_codec} -> x265"
        return f"{self.src_video_codec} copy"

    @property
    def audio_summary(self) -> str:
        a = [s for s in self.streams if s.kind == "audio"]
        return f"{len(a)} kept" if a else "none"

    @property
    def subs_summary(self) -> str:
        s = [x for x in self.streams if x.kind == "subtitle"]
        return f"{len(s)} kept" if s else "none"

    def to_dict(self) -> dict:
        """Serialisable form, used for the panel and the history record."""
        return {
            "path": self.path,
            "library": self.library,
            "library_name": self.library_name,
            "mode": self.mode,
            "mode_name": self.mode_name,
            "container": self.container,
            "needs_work": self.needs_work,
            "skip_reason": self.skip_reason,
            "height": self.height,
            "src_video_codec": self.src_video_codec,
            "duration": self.duration,
            "size": self.size,
            "reasons": list(self.reasons),
            "dropped": list(self.dropped),
            "summary": {
                "video": self.video_summary,
                "audio": self.audio_summary,
                "subs": self.subs_summary,
            },
            "streams": [
                {
                    "src_index": s.src_index,
                    "kind": s.kind,
                    "codec": s.codec,
                    "action": "encode" if s.is_encode else "copy",
                    "disposition": s.disposition,
                    "title": s.title,
                    "note": s.note,
                }
                for s in self.streams
            ],
        }


def _band(height: int, lib: Profile) -> str:
    v = lib.video
    if height > v.skip_above_height:
        return "over"
    if height <= v.sd_max_height:
        return "sd"
    if height <= v.h720_max_height:
        return "720p"
    if height <= v.h1080_max_height:
        return "1080p"
    return "over"


def _plan_video(probe: Probe, lib: Profile, cfg: Config,
                plan: FilePlan) -> bool:
    """Fills in the video stream. Returns False if the file should be skipped."""
    v, out = lib.video, lib.output
    videos = probe.of_type("video")
    if not videos:
        plan.skip_reason = "no video stream"
        return False

    # Cover art and thumbnails ride along as video streams.
    real = []
    for s in videos:
        cover = is_attached_pic(s) or codec_of(s) in out.image_codecs
        if cover and out.drop_cover_art:
            plan.dropped.append(f"cover art ({codec_of(s)})")
            plan.reasons.append("drop cover art")
        elif cover:
            plan.streams.append(
                StreamPlan(s["index"], "video", "copy", note="cover art")
            )
        else:
            real.append(s)

    if not real:
        plan.skip_reason = "no real video stream"
        return False

    main = real[0]
    for s in real[1:]:
        plan.dropped.append(f"extra video ({codec_of(s)})")
        plan.reasons.append("drop extra video")

    plan.height = int(main.get("height") or 0)
    plan.src_video_codec = codec_of(main)

    if not v.enabled:
        # Encoding is off for this library: leave the video exactly as it is.
        plan.streams.insert(
            0, StreamPlan(main["index"], "video", "copy", note="encoding off")
        )
        return True

    band = _band(plan.height, lib)

    if band == "over":
        plan.skip_reason = f"{plan.height}p above encode range"
        return False

    if band == "sd":
        # SD is cleaned but never re-encoded: x265 on an SD source costs more
        # in quality than it saves in bytes.
        plan.streams.insert(
            0, StreamPlan(main["index"], "video", "copy", note="SD, copy")
        )
        return True

    if plan.src_video_codec in v.already_encoded:
        plan.streams.insert(
            0, StreamPlan(main["index"], "video", "copy", note="already HEVC")
        )
        return True

    crf = v.crf_720p if band == "720p" else v.crf_1080p
    plan.streams.insert(0, StreamPlan(
        main["index"], "video", "libx265",
        extra=[
            "-preset:{i}", v.preset,
            "-crf:{i}", str(crf),
            "-x265-params:{i}", f"pools={cfg.workers.pools}",
        ],
        note=f"{band} crf {crf}",
    ))
    plan.reasons.append(f"encode video {plan.src_video_codec} -> x265 crf {crf}")
    return True


def _plan_audio(probe: Probe, lib: Profile, plan: FilePlan) -> None:
    a = lib.audio
    audio = probe.of_type("audio")
    if not audio:
        return

    if not a.enabled:
        for s in audio:
            plan.streams.append(
                StreamPlan(s["index"], "audio", "copy", note="audio untouched")
            )
        return

    commentary_re = re.compile(a.commentary_pattern, re.I)

    # "Most standard" = the track most likely to be the main soundtrack and to
    # play everywhere: preferred language, not commentary, a normal channel
    # layout, a widely supported codec. Bitrate only breaks ties.
    def score(s: dict[str, Any]) -> float:
        n = 0.0
        langs = a.preferred_languages
        lang = lang_of(s)
        n += (len(langs) - langs.index(lang)) * 100 if lang in langs else 0
        if commentary_re.search(title_of(s)):
            n -= 1000
        n += a.channel_score.get(str(channels_of(s)), 2)
        n += a.codec_score.get(codec_of(s), 0)
        try:
            n += min(int(s.get("bit_rate") or 0), 1_536_000) / 1_000_000
        except (TypeError, ValueError):
            pass
        return n

    ranked = sorted(audio, key=score, reverse=True)
    keep = ranked[0]

    def is_stereo_target(s: dict[str, Any]) -> bool:
        return codec_of(s) == a.stereo_codec and channels_of(s) == 2

    # Re-use a stereo downmix a previous run already created rather than
    # deleting it and encoding an identical one, which is what makes repeat
    # runs free. Commentary tracks are never adopted, or a re-run would
    # promote the commentary to the default audio track of the file.
    candidates = [
        s for s in ranked[1:]
        if is_stereo_target(s) and not commentary_re.search(title_of(s))
    ]
    existing = None
    if is_stereo_target(keep) and not commentary_re.search(title_of(keep)):
        existing = keep
    else:
        for pick in (
            lambda s: title_of(s) == a.stereo_title and lang_of(s) == lang_of(keep),
            lambda s: lang_of(s) == lang_of(keep),
            lambda s: title_of(s) == a.stereo_title,
        ):
            existing = next((s for s in candidates if pick(s)), None)
            if existing:
                break

    def stereo_name(s: dict[str, Any]) -> str | None:
        """Name an adopted stereo track, so it is obvious in a player.

        A downmix this tool encodes is titled on the way out; one it merely
        adopts keeps whatever name it arrived with, which is often none at
        all. Returns None when the title already says it - retitling a file
        that already says "Stereo" would make every scan find work forever.
        """
        if a.stereo_title.lower() in title_of(s).lower():
            return None
        plan.reasons.append("name the stereo track")
        return a.stereo_title

    def want(s: dict[str, Any], default: bool) -> str:
        if is_default(s) is not default:
            plan.reasons.append("fix audio disposition")
        return "default" if default else "0"

    if not a.keep_best_only:
        # Keep every track. A downmix is still added if one is wanted and
        # none of the existing tracks already is one.
        if a.add_stereo_downmix and existing is None:
            plan.streams.append(StreamPlan(
                keep["index"], "audio", a.stereo_encoder,
                extra=["-ac:{i}", "2", "-b:{i}", a.stereo_bitrate],
                disposition="default", title=a.stereo_title,
                note="new stereo downmix",
            ))
            plan.reasons.append(f"add {a.stereo_encoder} stereo downmix")
        for s in audio:
            default = a.add_stereo_downmix and s is existing
            plan.streams.append(StreamPlan(
                s["index"], "audio", "copy",
                disposition=want(s, default) if a.add_stereo_downmix else None,
                title=stereo_name(s) if default else None,
                note="kept",
            ))
        return

    kept_ids = {id(keep)} | ({id(existing)} if existing and a.add_stereo_downmix
                             else set())
    for s in audio:
        if id(s) not in kept_ids:
            plan.dropped.append(f"audio {codec_of(s)} {channels_of(s)}ch {lang_of(s)}")
            plan.reasons.append("drop extra audio")

    if not a.add_stereo_downmix:
        plan.streams.append(StreamPlan(
            keep["index"], "audio", "copy",
            disposition=want(keep, True), note="main track",
        ))
        return

    if existing is not None and existing is keep:
        plan.streams.append(StreamPlan(
            keep["index"], "audio", "copy",
            disposition=want(keep, True), title=stereo_name(keep),
            note="already AAC stereo",
        ))
        return

    if existing is not None:
        # Stereo downmix first so players that grab track 1 get the safe one.
        plan.streams.append(StreamPlan(
            existing["index"], "audio", "copy",
            disposition=want(existing, True), title=stereo_name(existing),
            note="existing stereo",
        ))
        plan.streams.append(StreamPlan(
            keep["index"], "audio", "copy",
            disposition=want(keep, False), note="main track",
        ))
        return

    # No usable stereo track: add one, downmixed from the track we keep.
    plan.streams.append(StreamPlan(
        keep["index"], "audio", a.stereo_encoder,
        extra=["-ac:{i}", "2", "-b:{i}", a.stereo_bitrate],
        disposition="default", title=a.stereo_title, note="new stereo downmix",
    ))
    plan.streams.append(StreamPlan(
        keep["index"], "audio", "copy",
        disposition=want(keep, False), note="main track",
    ))
    plan.reasons.append(f"add {a.stereo_encoder} stereo downmix")


# What each container can actually mux. ffmpeg refuses to write the header for
# anything else and the whole encode dies at the muxer, so a stream copy is not
# always free: MP4 carries mov_text, Matroska does not.
CONTAINER_SUBTITLES: dict[str, set[str]] = {
    "mkv": {
        "subrip", "srt", "ass", "ssa", "webvtt",
        "hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub",
        "dvb_subtitle", "xsub",
    },
}
SUBTITLE_FALLBACK = "srt"


def _subtitle_codec(s: dict[str, Any], container: str,
                    lib: Profile) -> str | None:
    """"copy", a codec to convert to, or None if the track cannot travel.

    Only containers with a known support list are judged; anything else is
    copied as before, which is what "keep" the source container means.
    """
    supported = CONTAINER_SUBTITLES.get(container)
    codec = codec_of(s)
    if supported is None or codec in supported:
        return "copy"
    if codec in lib.subtitles.image_codecs:
        return None                     # a picture cannot become text
    return SUBTITLE_FALLBACK


def _add_subtitle(s: dict[str, Any], lib: Profile, plan: FilePlan) -> None:
    """Copy, convert or drop one subtitle track for the target container."""
    codec = _subtitle_codec(s, plan.container, lib)
    if codec is None:
        plan.dropped.append(f"subtitle {codec_of(s)} {lang_of(s)}")
        plan.reasons.append(
            f"drop {codec_of(s)} subtitle, {plan.container} cannot carry it")
        return
    if codec != "copy":
        plan.reasons.append(
            f"convert {codec_of(s)} subtitle to {codec} for {plan.container}")
    plan.streams.append(StreamPlan(s["index"], "subtitle", codec,
                                   note=lang_of(s)))


def _plan_subtitles(probe: Probe, lib: Profile, plan: FilePlan) -> None:
    sub = lib.subtitles
    subs = probe.of_type("subtitle")
    if not subs:
        return

    if not sub.enabled:
        # "Untouched" still has to come out of the muxer intact: which tracks
        # are kept is policy, but whether the container can hold them is not.
        for s in subs:
            _add_subtitle(s, lib, plan)
        return

    keep = [s for s in subs if lang_of(s, "") in sub.keep_languages]

    if not keep:
        # No track matches a kept language. Only a *single* unlabelled track is
        # assumed to be one; a lone French track is a deliberate tag and gets
        # dropped like any other foreign track.
        lone_und = (
            sub.keep_lone_undefined
            and len(subs) == 1
            and lang_of(subs[0], "") in sub.undefined_languages
        )
        if lone_und:
            keep = list(subs)

    if sub.drop_image_subs:
        keep = [s for s in keep if codec_of(s) not in sub.image_codecs]

    kept_ids = {id(s) for s in keep}
    for s in subs:
        if id(s) not in kept_ids:
            plan.dropped.append(f"subtitle {codec_of(s)} {lang_of(s)}")
            plan.reasons.append("drop unwanted subtitle")

    for s in keep:
        _add_subtitle(s, lib, plan)


def plan_file(probe: Probe, lib: Profile, cfg: Config) -> FilePlan:
    source_ext = Path(probe.path).suffix.lower().lstrip(".")
    plan = FilePlan(
        path=probe.path, library=lib.id, library_name=lib.name,
        mode=lib.mode, mode_name=lib.mode_name,
        duration=probe.duration, size=probe.size,
        container=source_ext if lib.output.container == "keep"
        else lib.output.container,
    )

    if not _plan_video(probe, lib, cfg, plan):
        return plan

    _plan_audio(probe, lib, plan)
    _plan_subtitles(probe, lib, plan)

    if source_ext != plan.container:
        plan.reasons.append(f"remux to {plan.container}")

    # Collapse duplicate reasons while preserving order, purely for reporting.
    seen: set[str] = set()
    plan.reasons = [r for r in plan.reasons if not (r in seen or seen.add(r))]
    return plan
