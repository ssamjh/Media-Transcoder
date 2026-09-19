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

import copy as copymod
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, Profile, parse_bitrate
from .probe import (
    Probe, bitrate_of, channels_of, codec_of, is_attached_pic, is_comment,
    is_default, is_visual_impaired, lang_of, title_of,
)


@dataclass
class StreamPlan:
    """One output stream: which source stream it comes from and how."""

    src_index: int
    kind: str                       # video | audio | subtitle
    codec: str                      # "copy", "libx265", "aac", ...
    # Extra args; "{i}" is substituted with the *output* stream index.
    extra: list[str] = field(default_factory=list)
    # Decoder options, which ffmpeg only accepts *before* -i because they
    # configure the input side. "{s}" is substituted with the source index.
    input_extra: list[str] = field(default_factory=list)
    disposition: str | None = None  # "default" | "0" | None (leave alone)
    title: str | None = None
    language: str | None = None
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
    # Something a human has to decide, not work this tool can do. Deliberately
    # not a reason: it must never make needs_work true, or a file nobody ever
    # looks at would be re-planned and re-queued forever.
    manual_review: str = ""
    height: int = 0
    src_video_codec: str = ""
    duration: float = 0.0
    size: int = 0

    @property
    def needs_work(self) -> bool:
        return self.skip_reason is None and bool(self.reasons)

    @property
    def encodes_video(self) -> bool:
        return any(s.kind == "video" and s.is_encode for s in self.streams)

    def without_video_encode(self) -> "FilePlan":
        """The same run with the source video copied instead of re-encoded.

        Used when an x265 pass comes back bigger than the source it replaced.
        The encode is what failed to pay off, not the run: the stereo track
        and the subtitle cleaning are still worth having, and the original
        video stream is the best video this file is going to get.
        """
        fallback = copymod.deepcopy(self)
        for s in fallback.streams:
            if s.kind == "video" and s.is_encode:
                s.codec, s.extra, s.input_extra = "copy", [], []
                s.note = "x265 rejected, source video kept"
        fallback.reasons = [r for r in self.reasons
                            if not r.startswith("encode video ")]
        return fallback

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
            "manual_review": self.manual_review,
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
                    "language": s.language,
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

    if plan.src_video_codec in v.already_encoded:
        plan.streams.insert(
            0, StreamPlan(main["index"], "video", "copy", note="already HEVC")
        )
        return True

    # Each band gets its own CRF. SD has one of its own rather than borrowing
    # the 720p value, because the same quantiser is a very different bitrate
    # at 480 lines than at 720.
    crf = {"sd": v.crf_sd, "720p": v.crf_720p}.get(band, v.crf_1080p)
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


# The standard downmix matrix, normalised so the fold-down sums instead of
# clipping. Only reached for codecs that carry no coefficients of their own -
# a hand-written pan matrix would be second-guessing the mix engineer.
REMATRIX_FILTER = "aresample=rematrix_maxval=1.0"


def _excluded(s: dict[str, Any], pattern: re.Pattern[str]) -> bool:
    """True for a track that is about the film rather than its soundtrack.

    Commentary, described audio, isolated scores. The dispositions come
    first because they are the muxer's own statement about the track; the
    title is checked as well because most releases only say it there.
    """
    return (is_comment(s) or is_visual_impaired(s)
            or bool(pattern.search(title_of(s))))


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

    pattern = re.compile(a.commentary_pattern, re.I)
    excluded = {id(s) for s in audio if _excluded(s, pattern)}

    def wanted(s: dict[str, Any]) -> bool:
        return lang_of(s) in a.preferred_languages

    def is_stereo(s: dict[str, Any]) -> bool:
        return channels_of(s) == 2

    def is_target_codec(s: dict[str, Any]) -> bool:
        return codec_of(s) == a.stereo_codec

    stereo: dict[str, Any] | None = None    # a 2.0 track already in the file
    downmix: dict[str, Any] | None = None   # a surround track to fold down

    if a.add_stereo_downmix:
        # A 2.0 track that is already here *is* the stereo track - re-encoded
        # to AAC below if it is not already, but never rebuilt out of the
        # surround mix, which would be a second lossy generation for nothing.
        # This is also what makes a repeat run free: the downmix the last run
        # wrote is found here and copied.
        ready = [s for s in audio
                 if is_stereo(s) and wanted(s) and id(s) not in excluded]
        if ready:
            stereo = next((s for s in ready if is_target_codec(s)), ready[0])
        else:
            pool = [s for s in audio if wanted(s)
                    and str(channels_of(s)) in a.downmix_channels]
            candidates = [s for s in pool if id(s) not in excluded]
            if candidates:
                # Widest mix, then highest bitrate, then the earliest track:
                # deterministic the whole way down, so the same file always
                # resolves to the same source and a re-plan never wavers.
                downmix = min(candidates, key=lambda s: (
                    -channels_of(s), -bitrate_of(s), s["index"]))
            elif pool:
                # Every surround track in the file is commentary or described
                # audio. Guessing here ships a film whose default track is a
                # director talking over it, so nothing is downmixed and the
                # file is flagged for a person to look at instead.
                plan.manual_review = (
                    "no stereo track made: every multichannel track is "
                    "commentary or described audio")

    have_stereo = stereo is not None or downmix is not None

    keep = list(audio)
    if a.keep_stereo_only and have_stereo:
        # The stereo mix stands in for the surround masters, but for nothing
        # that is commentary or description - there is no other copy of those
        # in the file - so they are kept whatever this switch says.
        keep = [s for s in audio if id(s) in excluded or s is stereo]

    kept = {id(s) for s in keep}
    for s in audio:
        if id(s) not in kept:
            plan.dropped.append(
                f"audio {codec_of(s)} {channels_of(s)}ch {lang_of(s)}")
            plan.reasons.append("drop extra audio")

    if stereo is not None:
        # Stereo first, so players that just grab track 1 get the safe one.
        keep = [stereo] + [s for s in keep if s is not stereo]

    def want(s: dict[str, Any], default: bool) -> str | None:
        """The disposition to write, or None to leave the file's own alone.

        Nothing is re-flagged when no stereo track came out of this run:
        clearing every default would leave a file with no default track.
        """
        if not have_stereo:
            return None
        if is_default(s) is not default:
            plan.reasons.append("fix audio disposition")
        return "default" if default else "0"

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

    def convert_bitrate(s: dict[str, Any]) -> str | None:
        """What to spend re-encoding an existing 2.0 track to AAC.

        Only this path is laddered. A fold-down is judged by the configured
        stereo_bitrate alone, because a 640k 5.1 source rate says nothing
        about what two channels folded out of it need - whereas a 2.0 track
        already carries the rate someone chose for exactly this mix, and
        re-encoding a 128k one at 192k buys size and no quality.

        Re-encoding at the same or a higher nominal bitrate cannot save
        space and cannot restore quality already lost by the source codec.
        If the selected rung would do that, use the highest configured rung
        below the source instead.  When no such rung exists, or the container
        does not record the source rate, return None so the original track is
        copied: unknown is not permission to make the file larger.
        """
        source = bitrate_of(s)
        if source <= 0:
            return None
        bands = ((a.low_max_source_bitrate, a.stereo_bitrate_low),
                 (a.mid_max_source_bitrate, a.stereo_bitrate_mid))
        target = a.stereo_bitrate
        for threshold, band_target in bands:
            limit = parse_bitrate(threshold)
            if limit and source <= limit:
                target = band_target
                break
        target_rate = parse_bitrate(target) or 0
        if 0 < target_rate < source:
            return target

        # The threshold-selected rung is equal to or above the source. Pick
        # the best configured AAC rate that is actually smaller instead.
        safer = {
            rate: parsed for rate in (
                a.stereo_bitrate_low,
                a.stereo_bitrate_mid,
                a.stereo_bitrate,
            )
            if (parsed := parse_bitrate(rate)) is not None
            and 0 < parsed < source
        }
        return max(safer, key=safer.get) if safer else None

    if downmix is not None:
        extra = ["-ac:{i}", "2", "-b:{i}", a.stereo_bitrate]
        input_extra: list[str] = []
        request = a.downmix_request.split()
        if request and codec_of(downmix) in a.downmix_metadata_codecs:
            # This stream carries the mix engineer's own Lo/Ro coefficients.
            # Asking the decoder for stereo applies them, which is a better
            # fold-down than any matrix applied afterwards - and it is a
            # decoder option, so it has to reach ffmpeg before -i.
            input_extra = [request[0] + ":{s}", *request[1:]]
        else:
            extra += ["-filter:{i}", REMATRIX_FILTER]
        plan.streams.append(StreamPlan(
            downmix["index"], "audio", a.stereo_encoder,
            extra=extra, input_extra=input_extra, disposition="default",
            title=a.stereo_title, language=lang_of(downmix),
            note=f"stereo downmix from {channels_of(downmix)}ch",
        ))
        plan.reasons.append(
            f"downmix {codec_of(downmix)} {channels_of(downmix)}ch to "
            f"{a.stereo_codec} stereo")

    for s in keep:
        default = s is stereo
        if a.add_stereo_downmix and is_stereo(s) and not is_target_codec(s):
            # Already 2.0, wrong codec: transcoded in place. Keeping the
            # source track beside its own AAC copy would leave the file
            # carrying the same mix twice. If no AAC rate can safely make it
            # smaller, preserve the source below instead.
            bitrate = convert_bitrate(s)
            if bitrate is not None:
                plan.streams.append(StreamPlan(
                    s["index"], "audio", a.stereo_encoder,
                    extra=["-b:{i}", bitrate],
                    disposition=want(s, default),
                    title=a.stereo_title if default else None,
                    language=lang_of(s),
                    note=f"{codec_of(s)} stereo re-encoded",
                ))
                plan.reasons.append(
                    f"re-encode {codec_of(s)} stereo to {a.stereo_codec} "
                    f"{bitrate}")
                continue
        plan.streams.append(StreamPlan(
            s["index"], "audio", "copy",
            disposition=want(s, default) if a.add_stereo_downmix else None,
            title=stereo_name(s) if default else None,
            note=(f"{codec_of(s)} stereo kept; AAC would not be smaller"
                  if is_stereo(s) and not is_target_codec(s)
                  else "stereo" if default else "kept"),
        ))


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
    # ffprobe can expose a Matroska subtitle stream while leaving codec_name
    # empty (for example, an unsupported codec ID).  It cannot be decoded to
    # our text fallback, and mapping it makes ffmpeg abort the whole output
    # with "no decoder found for: none".  There is no safe representation to
    # preserve, so omit it just as we do an incompatible picture subtitle.
    if not codec:
        return None
    if supported is None or codec in supported:
        return "copy"
    if codec in lib.subtitles.image_codecs:
        return None                     # a picture cannot become text
    return SUBTITLE_FALLBACK


def _add_subtitle(s: dict[str, Any], lib: Profile, plan: FilePlan) -> None:
    """Copy, convert or drop one subtitle track for the target container."""
    codec = _subtitle_codec(s, plan.container, lib)
    if codec is None:
        source_codec = codec_of(s)
        label = source_codec or "unknown"
        plan.dropped.append(f"subtitle {label} {lang_of(s)}")
        if source_codec:
            plan.reasons.append(
                f"drop {source_codec} subtitle, {plan.container} cannot carry it")
        else:
            plan.reasons.append(
                "drop unknown subtitle, no decoder or codec information")
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
