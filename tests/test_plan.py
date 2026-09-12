"""Planning rules, including the idempotency guarantee.

The critical property under test: planning a file this tool already produced
must report needs_work=False. If that ever breaks, every scan re-processes
the whole library forever.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as cfgmod          # noqa: E402
from app.config import Config             # noqa: E402
from app.ffmpeg import build_args         # noqa: E402
from app.plan import plan_file            # noqa: E402
from app.probe import Probe               # noqa: E402


def V(index, codec="h264", height=1080, attached=False):
    return {
        "index": index, "codec_type": "video", "codec_name": codec,
        "height": height, "width": int(height * 16 / 9),
        "disposition": {"default": 1, "attached_pic": int(attached)},
    }


def A(index, codec="eac3", channels=6, lang="eng", title=None, default=0, bitrate=None):
    tags = {"language": lang}
    if title:
        tags["title"] = title
    s = {
        "index": index, "codec_type": "audio", "codec_name": codec,
        "channels": channels, "tags": tags, "disposition": {"default": default},
    }
    if bitrate:
        s["bit_rate"] = str(bitrate)
    return s


def S(index, lang="eng", codec="subrip"):
    return {
        "index": index, "codec_type": "subtitle", "codec_name": codec,
        "tags": ({"language": lang} if lang is not None else {}),
        "disposition": {"default": 0},
    }


def mk(streams, path="/media/TV/Show/S01E01.mkv", duration=2700.0, size=4 * 2**30):
    return Probe(path=path, streams=streams,
                 fmt={"duration": str(duration), "size": str(size)})


class Base(unittest.TestCase):
    def setUp(self):
        """A library, and the mode it is treated with.

        The library says where files are; the mode says what happens to
        them, so the tests below change `self.mode`.
        """
        self.cfg = Config()
        self.lib = cfgmod.add_library(self.cfg, "Media", ["/media"])
        self.lib.enabled = True
        self.mode = self.cfg.mode(self.lib.mode)

    def plan(self, streams, **kw):
        profile = cfgmod.resolve(self.cfg, self.lib)
        return plan_file(mk(streams, **kw), profile, self.cfg)

    def kinds(self, p, kind):
        return [s for s in p.streams if s.kind == kind]


class TestVideo(Base):
    def test_1080p_h264_encodes_at_crf22(self):
        p = self.plan([V(0, "h264", 1080), A(1)])
        self.assertTrue(p.needs_work)
        v = p.streams[0]
        self.assertEqual(v.codec, "libx265")
        self.assertIn("22", v.extra)
        self.assertIn("-crf:{i}", v.extra)

    def test_720p_uses_crf23(self):
        p = self.plan([V(0, "h264", 720), A(1)])
        self.assertIn("23", p.streams[0].extra)

    def test_already_hevc_is_copied_not_reencoded(self):
        p = self.plan([V(0, "hevc", 1080), A(1, "aac", 2, title="Stereo", default=1)])
        self.assertEqual(p.streams[0].codec, "copy")
        self.assertFalse(p.needs_work)

    def test_hevc_with_undefaulted_audio_still_needs_a_disposition_fix(self):
        p = self.plan([V(0, "hevc", 1080),
                       A(1, "aac", 2, title="Stereo", default=0)])
        self.assertEqual(p.streams[0].codec, "copy")
        self.assertTrue(p.needs_work)
        self.assertEqual(p.reasons, ["fix audio disposition"])

    def test_sd_is_cleaned_but_never_encoded(self):
        p = self.plan([V(0, "h264", 480), A(1), A(2, lang="fre")])
        self.assertEqual(p.streams[0].codec, "copy")
        self.assertTrue(p.needs_work)
        self.assertIn("drop extra audio", p.reasons)

    def test_1440p_and_above_skipped_entirely(self):
        for h in (1440, 2160):
            p = self.plan([V(0, "h264", h), A(1), S(2, "fre")])
            self.assertFalse(p.needs_work, f"{h}p should be skipped")
            self.assertIsNotNone(p.skip_reason)

    def test_cover_art_dropped(self):
        p = self.plan([V(0, "h264", 1080), V(1, "mjpeg", 500, attached=True), A(2)])
        self.assertEqual(len(self.kinds(p, "video")), 1)
        self.assertIn("drop cover art", p.reasons)

    def test_cover_art_kept_when_the_library_says_so(self):
        self.mode.output.drop_cover_art = False
        p = self.plan([V(0, "hevc", 1080), V(1, "mjpeg", 500, attached=True),
                       A(2, "aac", 2, title="Stereo", default=1)])
        self.assertEqual(len(self.kinds(p, "video")), 2)
        self.assertFalse(p.needs_work)


class TestStageSwitches(Base):
    """A library can enable only some of the work."""

    def test_video_disabled_copies_video_but_still_cleans(self):
        self.mode.video.enabled = False
        p = self.plan([V(0, "h264", 1080), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre"), S(3, "eng"), S(4, "jpn")])
        self.assertEqual(p.streams[0].codec, "copy")
        self.assertFalse(any(s.is_encode for s in self.kinds(p, "video")))
        self.assertTrue(p.needs_work)
        self.assertIn("drop extra audio", p.reasons)
        self.assertIn("drop unwanted subtitle", p.reasons)
        self.assertNotIn("encode video h264 -> x265 crf 22", p.reasons)

    def test_video_disabled_ignores_the_height_ceiling(self):
        """4K is only skipped because of encoding; cleaning still applies."""
        self.mode.video.enabled = False
        p = self.plan([V(0, "h264", 2160), A(1, "eac3", 6, "eng", default=1),
                       S(2, "eng"), S(3, "fre")])
        self.assertIsNone(p.skip_reason)
        self.assertTrue(p.needs_work)
        self.assertIn("drop unwanted subtitle", p.reasons)

    def test_audio_disabled_copies_every_track(self):
        self.mode.audio.enabled = False
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre"), A(3, "dts", 8, "jpn")])
        audio = self.kinds(p, "audio")
        self.assertEqual(len(audio), 3)
        self.assertTrue(all(s.codec == "copy" for s in audio))
        self.assertTrue(all(s.disposition is None for s in audio))
        self.assertFalse(p.needs_work)

    def test_subtitles_disabled_copies_every_track(self):
        self.mode.subtitles.enabled = False
        p = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", title="Stereo", default=1),
                       S(2, "eng"), S(3, "fre"), S(4, "jpn")])
        self.assertEqual(len(self.kinds(p, "subtitle")), 3)
        self.assertFalse(p.needs_work)

    def test_clean_audio_but_not_subtitles(self):
        """The example from the brief."""
        self.mode.subtitles.enabled = False
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre"), S(3, "fre"), S(4, "jpn")])
        self.assertEqual(len(self.kinds(p, "subtitle")), 2)
        self.assertIn("drop extra audio", p.reasons)
        self.assertTrue(any("stereo" in r for r in p.reasons))

    def test_everything_off_means_nothing_to_do(self):
        self.mode.video.enabled = False
        self.mode.audio.enabled = False
        self.mode.subtitles.enabled = False
        self.mode.output.drop_cover_art = False
        p = self.plan([V(0, "h264", 1080), A(1, "ac3", 6, "fre"), S(2, "jpn")])
        self.assertFalse(p.needs_work)
        self.assertEqual(len(p.streams), 3)

    def test_keep_best_only_off_keeps_every_track(self):
        self.mode.audio.keep_best_only = False
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre")])
        audio = self.kinds(p, "audio")
        self.assertEqual(len(audio), 3)          # both originals plus a downmix
        self.assertEqual(audio[0].codec, "aac")
        self.assertNotIn("drop extra audio", p.reasons)

    def test_no_stereo_downmix_keeps_just_the_best_track(self):
        self.mode.audio.add_stereo_downmix = False
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre")])
        audio = self.kinds(p, "audio")
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0].src_index, 1)
        self.assertEqual(audio[0].disposition, "default")
        self.assertNotIn("add aac stereo downmix", p.reasons)

    def test_container_keep_avoids_a_pointless_remux(self):
        self.mode.output.container = "keep"
        p = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", title="Stereo", default=1)],
                      path="/media/Movies/Film.mp4")
        self.assertEqual(p.container, "mp4")
        self.assertFalse(p.needs_work)


class TestSubtitles(Base):
    def test_keeps_only_english(self):
        subs = [S(3, "eng"), S(4, "fre"), S(5, "ger"), S(6, "spa"),
                S(7, "ita"), S(8, "pol"), S(9, "por"), S(10, "jpn")]
        p = self.plan([V(0), A(1), A(2, "aac", 2, title="Stereo")] + subs)
        kept = self.kinds(p, "subtitle")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].src_index, 3)

    def test_keeps_multiple_english_tracks(self):
        p = self.plan([V(0), A(1), S(2, "eng"), S(3, "eng"), S(4, "fre")])
        self.assertEqual(len(self.kinds(p, "subtitle")), 2)

    def test_lone_undefined_track_is_kept(self):
        for lang in (None, "und", ""):
            p = self.plan([V(0), A(1), S(2, lang)])
            self.assertEqual(len(self.kinds(p, "subtitle")), 1, f"lang={lang!r}")

    def test_lone_tagged_foreign_track_is_dropped(self):
        p = self.plan([V(0), A(1), S(2, "fre")])
        self.assertEqual(self.kinds(p, "subtitle"), [])

    def test_multiple_foreign_none_english_all_dropped(self):
        p = self.plan([V(0), A(1), S(2, "fre"), S(3, "spa")])
        self.assertEqual(self.kinds(p, "subtitle"), [])

    def test_keep_languages_is_configurable(self):
        self.mode.subtitles.keep_languages = ["fre", "eng"]
        p = self.plan([V(0), A(1), S(2, "fre"), S(3, "jpn"), S(4, "eng")])
        self.assertEqual({s.src_index for s in self.kinds(p, "subtitle")}, {2, 4})


class TestAudio(Base):
    def test_keeps_best_track_and_adds_stereo(self):
        p = self.plan([V(0), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre")])
        audio = self.kinds(p, "audio")
        self.assertEqual(len(audio), 2)
        self.assertEqual(audio[0].codec, "aac")
        self.assertEqual(audio[0].disposition, "default")
        self.assertEqual(audio[1].codec, "copy")
        self.assertEqual(audio[1].src_index, 1)
        self.assertEqual(audio[1].disposition, "0")

    def test_commentary_never_becomes_the_stereo_track(self):
        p = self.plan([V(0), A(1, "eac3", 6, "eng", default=1),
                       A(2, "aac", 2, "eng", title="Commentary")])
        audio = self.kinds(p, "audio")
        self.assertNotIn(2, [s.src_index for s in audio])
        self.assertEqual(audio[0].codec, "aac")
        self.assertEqual(audio[0].src_index, 1)

    def test_reuses_existing_stereo_instead_of_rebuilding(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng"),
                       A(2, "aac", 2, "eng", title="Stereo", default=1)])
        audio = self.kinds(p, "audio")
        self.assertTrue(all(s.codec == "copy" for s in audio))
        self.assertFalse(p.needs_work)

    def test_single_aac_stereo_gets_no_duplicate(self):
        p = self.plan([V(0, "hevc"),
                       A(1, "aac", 2, "eng", title="Stereo", default=1)])
        self.assertEqual(len(self.kinds(p, "audio")), 1)
        self.assertFalse(p.needs_work)

    def test_an_adopted_stereo_track_is_named(self):
        """A downmix we encode is titled; one we adopt has to be too."""
        p = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", default=1)])
        track = self.kinds(p, "audio")[0]
        self.assertEqual(track.codec, "copy")
        self.assertEqual(track.title, "Stereo")
        self.assertIn("name the stereo track", p.reasons)

    def test_naming_the_stereo_track_is_idempotent(self):
        """Or every scan would find the same work on the same file forever."""
        p = self.plan([V(0, "hevc"),
                       A(1, "aac", 2, "eng", title="Stereo", default=1)])
        self.assertIsNone(self.kinds(p, "audio")[0].title)
        self.assertNotIn("name the stereo track", p.reasons)

    def test_a_title_that_already_says_stereo_is_left_alone(self):
        p = self.plan([V(0, "hevc"),
                       A(1, "aac", 2, "eng", title="AAC stereo 2.0", default=1)])
        self.assertIsNone(self.kinds(p, "audio")[0].title)
        self.assertNotIn("name the stereo track", p.reasons)

    def test_an_adopted_second_stereo_track_is_named_too(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "aac", 2, "eng")])
        stereo = next(s for s in self.kinds(p, "audio") if s.note == "existing stereo")
        self.assertEqual(stereo.title, "Stereo")

    def test_wrong_disposition_triggers_a_fix(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "aac", 2, "eng", title="Stereo", default=0)])
        self.assertTrue(p.needs_work)
        self.assertIn("fix audio disposition", p.reasons)

    def test_foreign_stereo_not_adopted_over_english_main(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "aac", 2, "jpn")])
        audio = self.kinds(p, "audio")
        self.assertEqual(audio[0].codec, "aac")
        self.assertEqual(audio[0].src_index, 1)


class TestSubtitleContainers(Base):
    """Not every subtitle codec survives a copy into every container.

    MP4 carries mov_text and Matroska does not, so copying one into an .mkv
    fails at the muxer - taking the whole encode with it.
    """

    def test_mov_text_is_converted_for_mkv(self):
        p = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", title="Stereo", default=1),
                       S(2, "eng", codec="mov_text")],
                      path="/media/TV/a.mp4")
        track = self.kinds(p, "subtitle")[0]
        self.assertEqual(track.codec, "srt")
        self.assertIn("convert mov_text subtitle to srt for mkv", p.reasons)

    def test_a_codec_mkv_supports_is_still_copied(self):
        for codec in ("subrip", "ass", "webvtt", "hdmv_pgs_subtitle"):
            p = self.plan([V(0, "hevc"),
                           A(1, "aac", 2, "eng", title="Stereo", default=1),
                           S(2, "eng", codec=codec)])
            self.assertEqual(self.kinds(p, "subtitle")[0].codec, "copy", codec)

    def test_an_image_track_mkv_cannot_hold_is_dropped_not_converted(self):
        """A picture cannot become text, so the only options are drop or fail."""
        self.mode.subtitles.image_codecs = ["mov_text_pictures"]
        p = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", title="Stereo", default=1),
                       S(2, "eng", codec="mov_text_pictures")])
        self.assertEqual(self.kinds(p, "subtitle"), [])
        self.assertIn("drop mov_text_pictures subtitle, mkv cannot carry it",
                      p.reasons)

    def test_keeping_the_source_container_copies_as_before(self):
        self.mode.output.container = "keep"
        p = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", title="Stereo", default=1),
                       S(2, "eng", codec="mov_text")],
                      path="/media/TV/a.mp4")
        self.assertEqual(self.kinds(p, "subtitle")[0].codec, "copy")

    def test_disabled_subtitles_still_respect_the_container(self):
        """Which tracks are kept is policy; whether they can be muxed is not."""
        self.mode.subtitles.enabled = False
        p = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", title="Stereo", default=1),
                       S(2, "fre", codec="mov_text"), S(3, "jpn", codec="mov_text")],
                      path="/media/TV/a.mp4")
        subs = self.kinds(p, "subtitle")
        self.assertEqual(len(subs), 2)                 # none dropped
        self.assertTrue(all(t.codec == "srt" for t in subs))


class TestIdempotency(Base):
    """A file this tool produced must never be picked up again."""

    def test_output_of_a_full_run_needs_no_further_work(self):
        p = self.plan([
            V(0, "hevc", 1080),
            A(1, "aac", 2, "eng", title="Stereo", default=1),
            A(2, "eac3", 6, "eng", default=0),
            S(3, "eng"),
        ])
        self.assertFalse(p.needs_work, f"unexpected work: {p.reasons}")

    def test_a_converted_subtitle_is_not_converted_again(self):
        """The srt this run writes must read back as nothing left to do."""
        before = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", title="Stereo", default=1),
                            S(2, "eng", codec="mov_text")], path="/media/TV/a.mp4")
        self.assertTrue(before.needs_work)
        after = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", title="Stereo", default=1),
                           S(2, "eng", codec="subrip")], path="/media/TV/a.mkv")
        self.assertFalse(after.needs_work, f"unexpected work: {after.reasons}")

    def test_stable_across_repeated_planning(self):
        streams = [
            V(0, "hevc", 720),
            A(1, "aac", 2, "eng", title="Stereo", default=1),
            A(2, "dts", 8, "eng", default=0),
            S(3, "eng"), S(4, "eng"),
        ]
        for _ in range(5):
            self.assertFalse(self.plan(streams).needs_work)

    def test_non_mkv_source_needs_a_remux_even_when_clean(self):
        p = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", default=1)],
                      path="/media/Movies/Film.mp4")
        self.assertTrue(p.needs_work)
        self.assertIn("remux to mkv", p.reasons)

    def test_idempotent_with_every_stage_disabled(self):
        self.mode.video.enabled = False
        self.mode.audio.enabled = False
        self.mode.subtitles.enabled = False
        streams = [V(0, "h264", 1080), A(1, "ac3", 6, "fre"), S(2, "jpn")]
        for _ in range(3):
            self.assertFalse(self.plan(streams).needs_work)

    def test_idempotent_when_keeping_all_audio(self):
        self.mode.audio.keep_best_only = False
        after = [
            V(0, "hevc", 1080),
            A(1, "aac", 2, "eng", title="Stereo", default=1),
            A(2, "eac3", 6, "eng", default=0),
            A(3, "ac3", 6, "fre", default=0),
            S(4, "eng"),
        ]
        p = self.plan(after)
        self.assertFalse(p.needs_work, f"unexpected work: {p.reasons}")

    def test_idempotent_without_a_stereo_downmix(self):
        self.mode.audio.add_stereo_downmix = False
        after = [V(0, "hevc", 1080), A(1, "eac3", 6, "eng", default=1), S(2, "eng")]
        for _ in range(3):
            self.assertFalse(self.plan(after).needs_work)


class TestArgs(Base):
    def test_every_output_stream_gets_an_explicit_codec(self):
        """No -c: means ffmpeg picks the container default (libx264 for mkv)."""
        p = self.plan([V(0, "h264", 1080), A(1, "eac3", 6, "eng", default=1),
                       S(2, "eng"), S(3, "fre")])
        args = build_args(p, "/tmp/out.mkv")
        for i in range(len(p.streams)):
            self.assertIn(f"-c:{i}", args, f"stream {i} has no explicit codec")

    def test_video_args_are_complete(self):
        p = self.plan([V(0, "h264", 1080), A(1)])
        joined = " ".join(build_args(p, "/tmp/out.mkv"))
        self.assertIn("-c:0 libx265", joined)
        self.assertIn("-preset:0 medium", joined)
        self.assertIn("-crf:0 22", joined)
        self.assertIn(f"-x265-params:0 pools={self.cfg.workers.pools}", joined)

    def test_dropped_streams_are_never_mapped(self):
        p = self.plan([V(0, "h264"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre"), S(3, "eng"), S(4, "jpn")])
        args = build_args(p, "/tmp/out.mkv")
        mapped = {args[i + 1] for i, a in enumerate(args) if a == "-map"}
        self.assertNotIn("0:2", mapped)
        self.assertNotIn("0:4", mapped)

    def test_stereo_downmix_args(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1)])
        joined = " ".join(build_args(p, "/tmp/out.mkv"))
        self.assertIn("-c:0 copy", joined)
        self.assertIn("-c:1 aac", joined)
        self.assertIn("-ac:1 2", joined)
        self.assertIn("-b:1 160k", joined)
        self.assertIn("title=Stereo", joined)

    def test_video_is_always_the_first_output_stream(self):
        p = self.plan([V(0, "h264", 1080), A(1, "eac3", 6, "eng", default=1),
                       S(2, "eng")])
        self.assertEqual(p.streams[0].kind, "video")


class TestLibraryRouting(unittest.TestCase):
    def test_each_library_is_planned_under_its_own_mode(self):
        cfg = Config()
        cfgmod.add_library(cfg, "TV", ["/media/TV"]).enabled = True
        cfgmod.add_library(cfg, "Movies", ["/media/Movies"]).enabled = True
        tv, movies = cfg.library("tv"), cfg.library("movies")

        # Movies gets a mode of its own; TV stays on the shared default.
        light = cfgmod.add_mode(cfg, "Light", copy_from=tv.mode)
        light.video.enabled = False
        light.subtitles.enabled = False
        movies.mode = light.id

        streams = [V(0, "h264", 1080), A(1, "eac3", 6, "eng", default=1),
                   S(2, "fre")]
        tv_plan = plan_file(mk(streams, path="/media/TV/a.mkv"),
                            cfgmod.resolve(cfg, tv), cfg)
        mv_plan = plan_file(mk(streams, path="/media/Movies/b.mkv"),
                            cfgmod.resolve(cfg, movies), cfg)

        vid = lambda pl: [s for s in pl.streams if s.kind == "video"]
        self.assertTrue(any(s.is_encode for s in vid(tv_plan)))
        self.assertIn("drop unwanted subtitle", tv_plan.reasons)

        # The Light mode has video encoding off, but audio cleaning still on,
        # so it adds a stereo downmix while leaving the video stream alone.
        self.assertFalse(any(s.is_encode for s in vid(mv_plan)))
        self.assertNotIn("drop unwanted subtitle", mv_plan.reasons)
        self.assertTrue(any(s.is_encode for s in mv_plan.streams
                            if s.kind == "audio"))
        # The plan belongs to the library, whichever mode produced it.
        self.assertEqual(mv_plan.library, "movies")
        self.assertEqual(mv_plan.library_name, "Movies")
        self.assertEqual(mv_plan.mode, "light")
        self.assertEqual(tv_plan.mode, "standard")

    def test_routing_picks_the_longest_matching_root(self):
        cfg = Config()
        cfgmod.add_library(cfg, "TV", ["/media/TV"]).enabled = True
        cfgmod.add_library(cfg, "Anime", ["/media/TV-Anime"]).enabled = True
        self.assertEqual(cfg.library_for("/media/TV/Show/a.mkv").id, "tv")
        self.assertEqual(cfg.library_for("/media/TV-Anime/b.mkv").id, "anime")
        self.assertIsNone(cfg.library_for("/media/Other/c.mkv"))

    def test_disabled_libraries_route_nothing(self):
        cfg = Config()
        cfgmod.add_library(cfg, "TV", ["/media/TV"]).enabled = False
        self.assertIsNone(cfg.library_for("/media/TV/a.mkv"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
