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
from app.ffmpeg import AAC_AUTO, build_args, encoder_for  # noqa: E402
from app.plan import plan_file            # noqa: E402
from app.probe import Probe               # noqa: E402


# A plan names the logical AAC encoder; which real one that is depends on
# the ffmpeg build and is only resolved when the arguments are built.
ENCODER = AAC_AUTO


def V(index, codec="h264", height=1080, attached=False):
    return {
        "index": index, "codec_type": "video", "codec_name": codec,
        "height": height, "width": int(height * 16 / 9),
        "disposition": {"default": 1, "attached_pic": int(attached)},
    }


def A(index, codec="eac3", channels=6, lang="eng", title=None, default=0,
      bitrate=None, comment=0, visual_impaired=0):
    tags = {"language": lang}
    if title:
        tags["title"] = title
    s = {
        "index": index, "codec_type": "audio", "codec_name": codec,
        "channels": channels, "tags": tags,
        "disposition": {"default": default, "comment": comment,
                        "visual_impaired": visual_impaired},
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

    def test_sd_encodes_at_its_own_crf(self):
        """SD is its own band: the same CRF is a different bitrate at 480p."""
        self.mode.video.crf_sd = 25
        p = self.plan([V(0, "h264", 480), A(1)])
        self.assertEqual(p.streams[0].codec, "libx265")
        self.assertIn("25", p.streams[0].extra)
        self.assertIn("encode video h264 -> x265 crf 25", p.reasons)

    def test_sd_that_is_already_hevc_is_still_copied(self):
        p = self.plan([V(0, "hevc", 480),
                       A(1, "aac", 2, "eng", title="Stereo", default=1)])
        self.assertEqual(p.streams[0].codec, "copy")
        self.assertFalse(p.needs_work)

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
        self.mode.audio.keep_stereo_only = True
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
        self.mode.audio.keep_stereo_only = True
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

    def test_every_original_track_is_kept_by_default(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre")])
        audio = self.kinds(p, "audio")
        self.assertEqual(len(audio), 3)          # both originals plus a downmix
        self.assertEqual(audio[0].codec, ENCODER)
        self.assertNotIn("drop extra audio", p.reasons)

    def test_keep_stereo_only_drops_the_surround_masters(self):
        self.mode.audio.keep_stereo_only = True
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre")])
        audio = self.kinds(p, "audio")
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0].codec, ENCODER)
        self.assertEqual(audio[0].src_index, 1)   # downmixed from the 5.1 eng
        self.assertEqual(audio[0].disposition, "default")
        self.assertIn("drop extra audio", p.reasons)

    def test_no_stereo_downmix_leaves_audio_exactly_as_it_came(self):
        self.mode.audio.add_stereo_downmix = False
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 2, "fre")])
        audio = self.kinds(p, "audio")
        self.assertEqual(len(audio), 2)
        self.assertTrue(all(s.codec == "copy" for s in audio))
        self.assertTrue(all(s.disposition is None for s in audio))
        self.assertFalse(p.needs_work)

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
    def test_the_downmix_leads_and_the_original_is_demoted(self):
        p = self.plan([V(0), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 6, "fre")])
        audio = self.kinds(p, "audio")
        self.assertEqual(audio[0].codec, ENCODER)
        self.assertEqual(audio[0].src_index, 1)
        self.assertEqual(audio[0].disposition, "default")
        self.assertEqual(audio[1].codec, "copy")
        self.assertEqual(audio[1].src_index, 1)
        self.assertEqual(audio[1].disposition, "0")

    def test_commentary_never_becomes_the_stereo_track(self):
        p = self.plan([V(0), A(1, "eac3", 6, "eng", default=1),
                       A(2, "aac", 2, "eng", title="Commentary")])
        audio = self.kinds(p, "audio")
        # The commentary is kept, but the downmix comes off the 5.1 master.
        self.assertEqual(audio[0].codec, ENCODER)
        self.assertEqual(audio[0].src_index, 1)
        self.assertEqual(audio[0].title, "Stereo")
        self.assertIn(2, [s.src_index for s in audio])

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
        stereo = next(s for s in self.kinds(p, "audio") if s.note == "stereo")
        self.assertEqual(stereo.src_index, 2)
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
        self.assertEqual(audio[0].codec, ENCODER)
        self.assertEqual(audio[0].src_index, 1)


class TestStereoConversion(Base):
    """A 2.0 track that is not AAC is transcoded, not kept beside a copy."""

    def test_non_aac_stereo_is_reencoded_in_place(self):
        p = self.plan([V(0, "hevc"), A(1, "ac3", 2, "eng", default=1)])
        audio = self.kinds(p, "audio")
        self.assertEqual(len(audio), 1)                  # not both
        self.assertEqual(audio[0].codec, ENCODER)
        self.assertEqual(audio[0].src_index, 1)
        self.assertEqual(audio[0].extra, ["-b:{i}", "192k"])
        self.assertEqual(audio[0].disposition, "default")
        self.assertEqual(audio[0].title, "Stereo")
        self.assertIn("re-encode ac3 stereo to aac 192k", p.reasons)

    def test_it_is_preferred_over_folding_the_surround_mix_down(self):
        """Re-encoding a 2.0 mix beats a second lossy generation off the 5.1."""
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 2, "eng")])
        audio = self.kinds(p, "audio")
        self.assertEqual(audio[0].src_index, 2)
        self.assertEqual(audio[0].codec, ENCODER)
        self.assertFalse(any("downmix" in r for r in p.reasons), p.reasons)

    def test_an_existing_aac_stereo_track_wins_over_another_stereo_codec(self):
        p = self.plan([V(0, "hevc"), A(1, "ac3", 2, "eng"),
                       A(2, "aac", 2, "eng", title="Stereo", default=1)])
        audio = self.kinds(p, "audio")
        chosen = next(s for s in audio if s.disposition == "default")
        self.assertEqual(chosen.src_index, 2)
        self.assertEqual(chosen.codec, "copy")
        # The other one is still brought to AAC, just not made the default.
        other = next(s for s in audio if s.src_index == 1)
        self.assertEqual(other.codec, ENCODER)

    def test_a_foreign_stereo_track_is_converted_but_not_promoted(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "ac3", 2, "fre")])
        french = next(s for s in self.kinds(p, "audio") if s.src_index == 2)
        self.assertEqual(french.codec, ENCODER)
        self.assertEqual(french.disposition, "0")
        self.assertIsNone(french.title)


class TestStereoBitrateLadder(Base):
    """An existing 2.0 track is re-encoded at a rate its own bitrate earns."""

    def rate(self, bitrate, codec="ac3"):
        p = self.plan([V(0, "hevc"), A(1, codec, 2, "eng", default=1,
                                       bitrate=bitrate)])
        stream = self.kinds(p, "audio")[0]
        return stream.extra[stream.extra.index("-b:{i}") + 1]

    def test_a_thin_source_gets_the_low_band(self):
        self.assertEqual(self.rate(96_000), "96k")

    def test_the_low_threshold_is_inclusive(self):
        self.assertEqual(self.rate(112_000), "96k")

    def test_a_middling_source_gets_the_mid_band(self):
        self.assertEqual(self.rate(128_000), "128k")

    def test_the_mid_threshold_is_inclusive(self):
        self.assertEqual(self.rate(160_000), "128k")

    def test_a_fat_source_gets_the_configured_bitrate(self):
        self.assertEqual(self.rate(448_000), "192k")

    def test_an_unrecorded_bitrate_falls_back_rather_than_down(self):
        """Matroska often omits the rate, and unknown is not tiny."""
        self.assertEqual(self.rate(None), "192k")

    def test_the_reason_names_the_rate_actually_chosen(self):
        p = self.plan([V(0, "hevc"),
                       A(1, "mp3", 2, "eng", default=1, bitrate=128_000)])
        self.assertIn("re-encode mp3 stereo to aac 128k", p.reasons)

    def test_zeroed_thresholds_give_a_flat_bitrate(self):
        self.mode.audio.low_max_source_bitrate = "0"
        self.mode.audio.mid_max_source_bitrate = "0"
        self.assertEqual(self.rate(96_000), "192k")

    def test_a_fold_down_is_not_laddered(self):
        """A 5.1 source rate says nothing about what its downmix needs."""
        p = self.plan([V(0, "hevc"),
                       A(1, "eac3", 6, "eng", default=1, bitrate=96_000)])
        stream = self.kinds(p, "audio")[0]
        self.assertEqual(stream.extra[stream.extra.index("-b:{i}") + 1], "192k")

    def test_an_aac_track_is_still_copied_whatever_its_bitrate(self):
        p = self.plan([V(0, "hevc"), A(1, "aac", 2, "eng", title="Stereo",
                                       default=1, bitrate=64_000)])
        self.assertEqual(self.kinds(p, "audio")[0].codec, "copy")
        self.assertFalse(p.needs_work, p.reasons)


class TestDownmixSelection(Base):
    """Which surround track gets folded down, and which never may."""

    def test_the_widest_track_wins(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "dts", 8, "eng")])
        self.assertEqual(self.kinds(p, "audio")[0].src_index, 2)

    def test_bitrate_breaks_a_channel_count_tie(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", bitrate=640_000),
                       A(2, "ac3", 6, "eng", bitrate=1_500_000)])
        self.assertEqual(self.kinds(p, "audio")[0].src_index, 2)

    def test_the_lowest_index_breaks_a_bitrate_tie(self):
        p = self.plan([V(0, "hevc"), A(3, "eac3", 6, "eng", bitrate=640_000),
                       A(1, "eac3", 6, "eng", bitrate=640_000)])
        self.assertEqual(self.kinds(p, "audio")[0].src_index, 1)

    def test_an_untagged_bitrate_does_not_sink_a_wider_track(self):
        p = self.plan([V(0, "hevc"), A(1, "ac3", 6, "eng", bitrate=640_000),
                       A(2, "truehd", 8, "eng")])
        self.assertEqual(self.kinds(p, "audio")[0].src_index, 2)

    def test_a_commentary_title_is_excluded(self):
        for title in ("Director's Commentary", "Isolated Score",
                      "Cast and crew", "Audio Description", "SIGN LANGUAGE"):
            with self.subTest(title=title):
                p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", title=title),
                               A(2, "eac3", 6, "eng")])
                self.assertEqual(self.kinds(p, "audio")[0].src_index, 2)

    def test_the_comment_disposition_is_excluded(self):
        p = self.plan([V(0, "hevc"), A(1, "dts", 8, "eng", comment=1),
                       A(2, "eac3", 6, "eng")])
        self.assertEqual(self.kinds(p, "audio")[0].src_index, 2)

    def test_the_visual_impaired_disposition_is_excluded(self):
        p = self.plan([V(0, "hevc"), A(1, "dts", 8, "eng", visual_impaired=1),
                       A(2, "eac3", 6, "eng")])
        self.assertEqual(self.kinds(p, "audio")[0].src_index, 2)

    def test_a_foreign_surround_track_is_not_a_candidate(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "fre", default=1)])
        audio = self.kinds(p, "audio")
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0].codec, "copy")
        self.assertFalse(p.needs_work)

    def test_an_odd_channel_count_is_not_a_candidate(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 7, "eng", default=1)])
        self.assertEqual(self.kinds(p, "audio")[0].codec, "copy")

    def test_nothing_left_after_exclusion_is_flagged_not_guessed(self):
        """Guessing here ships a film defaulting to a director talking."""
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", title="Commentary",
                                       default=1),
                       A(2, "dts", 8, "eng", visual_impaired=1)])
        audio = self.kinds(p, "audio")
        self.assertTrue(all(s.codec == "copy" for s in audio))
        self.assertIn("commentary or described audio", p.manual_review)
        # A flag, never a reason: needs_work would re-queue it forever.
        self.assertFalse(p.needs_work, p.reasons)

    def test_a_file_with_no_surround_track_is_not_flagged(self):
        p = self.plan([V(0, "hevc"), A(1, "aac", 1, "eng", default=1)])
        self.assertEqual(p.manual_review, "")


class TestDownmixMethod(Base):
    """How the fold-down is done: the mix engineer's coefficients if there are
    any, and a normalised matrix if there are not."""

    def stereo(self, streams):
        return self.kinds(self.plan(streams), "audio")[0]

    def test_a_codec_with_downmix_metadata_asks_the_decoder(self):
        for codec in ("ac3", "eac3", "dts", "truehd"):
            with self.subTest(codec=codec):
                s = self.stereo([V(0, "hevc"), A(1, codec, 6, "eng", default=1)])
                self.assertEqual(s.input_extra, ["-downmix:{s}", "stereo"])
                self.assertNotIn("-filter:{i}", s.extra)

    def test_anything_else_uses_the_normalised_matrix(self):
        s = self.stereo([V(0, "hevc"), A(1, "flac", 6, "eng", default=1)])
        self.assertEqual(s.input_extra, [])
        self.assertIn("-filter:{i}", s.extra)
        self.assertIn("aresample=rematrix_maxval=1.0", s.extra)

    def test_the_downmix_is_constant_bitrate_and_tagged(self):
        s = self.stereo([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1)])
        self.assertIn("-b:{i}", s.extra)
        self.assertNotIn("-q:{i}", s.extra)
        self.assertEqual(s.extra[s.extra.index("-b:{i}") + 1], "192k")
        self.assertEqual(s.language, "eng")
        self.assertEqual(s.title, "Stereo")
        self.assertEqual(s.disposition, "default")
        self.assertIn("-ac:{i}", s.extra)

    def test_it_always_comes_off_the_source_track(self):
        """Never off a stereo track an earlier step in the same run made."""
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1)])
        downmix = self.kinds(p, "audio")[0]
        self.assertEqual(downmix.src_index, 1)


class TestKeepStereoOnly(Base):
    def setUp(self):
        super().setUp()
        self.mode.audio.keep_stereo_only = True

    def test_commentary_and_described_audio_survive(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "eac3", 6, "eng", title="Commentary"),
                       A(3, "aac", 2, "eng", visual_impaired=1),
                       A(4, "ac3", 6, "fre")])
        kept = {s.src_index for s in self.kinds(p, "audio")}
        self.assertEqual(kept, {1, 2, 3})     # 1 is the downmix source
        self.assertEqual(self.kinds(p, "audio")[0].codec, ENCODER)
        self.assertIn("audio ac3 6ch fre", p.dropped)

    def test_nothing_is_dropped_when_no_stereo_could_be_made(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", title="Commentary",
                                       default=1),
                       A(2, "ac3", 6, "fre")])
        self.assertEqual(len(self.kinds(p, "audio")), 2)
        self.assertEqual(p.dropped, [])

    def test_an_existing_stereo_track_is_the_one_kept(self):
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1),
                       A(2, "aac", 2, "eng", title="Stereo")])
        audio = self.kinds(p, "audio")
        self.assertEqual([s.src_index for s in audio], [2])
        self.assertEqual(audio[0].codec, "copy")
        self.assertEqual(audio[0].disposition, "default")


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

    def test_idempotent_when_only_the_stereo_track_is_kept(self):
        self.mode.audio.keep_stereo_only = True
        after = [V(0, "hevc", 1080),
                 A(1, "aac", 2, "eng", title="Stereo", default=1),
                 A(2, "eac3", 6, "eng", title="Commentary", default=0),
                 S(3, "eng")]
        for _ in range(3):
            p = self.plan(after)
            self.assertFalse(p.needs_work, f"unexpected work: {p.reasons}")

    def test_a_reencoded_stereo_track_is_not_reencoded_again(self):
        before = self.plan([V(0, "hevc"), A(1, "ac3", 2, "eng", default=1)])
        self.assertTrue(before.needs_work)
        after = self.plan([V(0, "hevc"),
                           A(1, "aac", 2, "eng", title="Stereo", default=1)])
        self.assertFalse(after.needs_work, f"unexpected work: {after.reasons}")

    def test_a_file_flagged_for_review_is_never_re_queued(self):
        streams = [V(0, "hevc", 1080),
                   A(1, "eac3", 6, "eng", title="Commentary", default=1),
                   S(2, "eng")]
        for _ in range(3):
            p = self.plan(streams)
            self.assertTrue(p.manual_review)
            self.assertFalse(p.needs_work, f"unexpected work: {p.reasons}")

    def test_sd_output_needs_no_further_work(self):
        p = self.plan([V(0, "hevc", 480),
                       A(1, "aac", 2, "eng", title="Stereo", default=1),
                       A(2, "eac3", 6, "eng", default=0)])
        self.assertFalse(p.needs_work, f"unexpected work: {p.reasons}")


class TestVideoCopyFallback(Base):
    """What is left of a run whose x265 pass came back bigger than the source."""

    def plan_1080p(self):
        return self.plan([V(0, "h264", 1080), A(1, "eac3", 6, "eng", default=1),
                          S(2, "eng"), S(3, "jpn")])

    def test_the_video_stream_reverts_to_a_copy(self):
        fallback = self.plan_1080p().without_video_encode()
        video = self.kinds(fallback, "video")
        self.assertEqual([s.codec for s in video], ["copy"])
        self.assertEqual(video[0].extra, [])
        self.assertFalse(fallback.encodes_video)

    def test_the_audio_and_subtitle_work_is_kept(self):
        original = self.plan_1080p()
        fallback = original.without_video_encode()
        self.assertEqual([s.src_index for s in fallback.streams],
                         [s.src_index for s in original.streams])
        self.assertEqual([(s.kind, s.codec) for s in fallback.streams][1:],
                         [(s.kind, s.codec) for s in original.streams][1:])
        self.assertIn("drop unwanted subtitle", fallback.reasons)
        self.assertTrue(any("downmix" in r for r in fallback.reasons))

    def test_the_encode_reason_goes_with_the_encode(self):
        fallback = self.plan_1080p().without_video_encode()
        self.assertFalse(any(r.startswith("encode video") for r in fallback.reasons))

    def test_the_original_plan_is_untouched(self):
        original = self.plan_1080p()
        original.without_video_encode()
        self.assertTrue(original.encodes_video)
        self.assertIn("encode video h264 -> x265 crf 22", original.reasons)


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
        self.mode.audio.keep_stereo_only = True
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
        self.assertIn(f"-c:1 {encoder_for(ENCODER)}", joined)
        self.assertIn("-ac:1 2", joined)
        self.assertIn("-b:1 192k", joined)
        self.assertIn("title=Stereo", joined)
        self.assertIn("language=eng", joined)

    def test_a_decoder_option_lands_before_the_input(self):
        """-downmix configures the decoder, so ffmpeg only takes it ahead of -i."""
        p = self.plan([V(0, "hevc"), A(1, "eac3", 6, "eng", default=1)])
        args = build_args(p, "/tmp/out.mkv")
        self.assertIn("-downmix:1", args)
        self.assertLess(args.index("-downmix:1"), args.index("-i"))
        self.assertEqual(args[args.index("-downmix:1") + 1], "stereo")

    def test_the_matrix_filter_is_an_output_option(self):
        p = self.plan([V(0, "hevc"), A(1, "flac", 6, "eng", default=1)])
        args = build_args(p, "/tmp/out.mkv")
        self.assertIn("-filter:1", args)
        self.assertGreater(args.index("-filter:1"), args.index("-i"))

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
