"""Processing modes: the settings themselves, and the guarantees around them.

A mode is everything that happens to a file. A library names the mode it is
treated with, and a single /api/process call - the one Sonarr and Radarr make
on import - can name a different one for that file only. Three properties
matter:

  - a mode is shared, not copied. Two libraries pointing at one mode are
    changed together, which is the reason modes exist at all.
  - resolving one must not leak. It returns a detached copy, so a "cleanup"
    request for one file cannot change what any other file, or any later
    scan, decides to do.
  - a mode must be idempotent *in its own terms*. Planning a cleanup-produced
    file under cleanup again must report no work, exactly as a library's own
    mode must for its own output, or an on-import hook that fires twice would
    encode twice.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config as cfgmod              # noqa: E402
from app.config import Config, ConfigError    # noqa: E402
from app.plan import plan_file                # noqa: E402

from test_plan import A, S, V, mk             # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.lib = cfgmod.add_library(self.cfg, "Media", ["/media"])
        self.lib.enabled = True

    def plan(self, streams, mode=None, **kw):
        profile = cfgmod.resolve(self.cfg, self.lib, mode)
        return plan_file(mk(streams, **kw), profile, self.cfg)


class TestDefaults(Base):
    def test_ships_with_standard_and_cleanup(self):
        self.assertEqual([m.id for m in self.cfg.modes], ["standard", "cleanup"])

    def test_a_new_library_is_treated_with_standard(self):
        self.assertEqual(self.lib.mode, "standard")
        self.assertEqual(cfgmod.resolve(self.cfg, self.lib).mode, "standard")

    def test_no_mode_named_means_the_librarys_own(self):
        self.lib.mode = "cleanup"
        for asked in (None, ""):
            with self.subTest(asked=asked):
                self.assertEqual(
                    cfgmod.resolve(self.cfg, self.lib, asked).mode, "cleanup")

    def test_a_resolved_profile_carries_both_identities(self):
        profile = cfgmod.resolve(self.cfg, self.lib, "cleanup")
        self.assertEqual((profile.id, profile.name), ("media", "Media"))
        self.assertEqual((profile.mode, profile.mode_name), ("cleanup", "Cleanup"))


class TestSharing(Base):
    """The point of the whole arrangement: one place for one behaviour."""

    def test_two_libraries_on_one_mode_change_together(self):
        other = cfgmod.add_library(self.cfg, "Movies", ["/movies"])
        self.assertEqual(other.mode, self.lib.mode)

        self.cfg.mode("standard").video.crf_1080p = 19
        for lib in (self.lib, other):
            self.assertEqual(cfgmod.resolve(self.cfg, lib).video.crf_1080p, 19)

    def test_a_library_can_be_pointed_at_another_mode(self):
        cfgmod.apply_library_updates(self.cfg, self.lib, {"mode": "cleanup"})
        self.assertFalse(cfgmod.resolve(self.cfg, self.lib).video.enabled)

    def test_a_library_cannot_name_a_mode_that_does_not_exist(self):
        with self.assertRaises(ConfigError) as ctx:
            cfgmod.apply_library_updates(self.cfg, self.lib, {"mode": "nope"})
        self.assertIn("no such processing mode", str(ctx.exception))
        self.assertEqual(self.lib.mode, "standard")      # nothing applied


class TestCleanup(Base):
    """The headline mode: everything except re-encoding video."""

    STREAMS = [V(0, "h264", 1080), A(1, "eac3", 6, "eng"),
               A(2, "ac3", 6, "fre"), S(3, "eng"), S(4, "jpn")]

    def test_video_is_copied_not_encoded(self):
        p = self.plan(self.STREAMS, mode="cleanup")
        self.assertEqual(p.streams[0].codec, "copy")
        self.assertFalse(any("x265" in r for r in p.reasons), p.reasons)

    def test_audio_and_subtitles_are_still_cleaned(self):
        p = self.plan(self.STREAMS, mode="cleanup")
        self.assertTrue(p.needs_work)
        self.assertIn("drop extra audio", p.reasons)
        self.assertIn("add aac stereo downmix", p.reasons)
        self.assertIn("drop unwanted subtitle", p.reasons)

    def test_the_same_file_under_standard_does_encode(self):
        p = self.plan(self.STREAMS, mode="standard")
        self.assertEqual(p.streams[0].codec, "libx265")

    def test_height_ceiling_does_not_apply(self):
        """Video off means 4K is cleaned rather than skipped outright."""
        p = self.plan([V(0, "h264", 2160), A(1, "eac3", 6, "eng")], mode="cleanup")
        self.assertIsNone(p.skip_reason)
        self.assertEqual(p.streams[0].codec, "copy")


class TestIsolation(Base):
    def test_resolving_does_not_hand_out_the_live_mode(self):
        profile = cfgmod.resolve(self.cfg, self.lib, "cleanup")
        self.assertIsNot(profile.video, self.cfg.mode("cleanup").video)

    def test_mutating_the_copy_does_not_reach_the_config(self):
        profile = cfgmod.resolve(self.cfg, self.lib)
        profile.audio.preferred_languages.append("fre")
        profile.video.crf_1080p = 5
        live = self.cfg.mode("standard")
        self.assertNotIn("fre", live.audio.preferred_languages)
        self.assertEqual(live.video.crf_1080p, 22)

    def test_repeated_resolution_is_stable(self):
        for _ in range(3):
            self.assertFalse(
                cfgmod.resolve(self.cfg, self.lib, "cleanup").video.enabled)
            self.assertTrue(cfgmod.resolve(self.cfg, self.lib).video.enabled)


class TestIdempotency(Base):
    """Each mode must be stable against the output it produces."""

    CLEANED = [
        V(0, "h264", 1080),                                   # never touched
        A(1, "aac", 2, "eng", title="Stereo", default=1),
        A(2, "eac3", 6, "eng", default=0),
        S(3, "eng"),
    ]

    def test_cleanup_output_needs_no_further_cleanup(self):
        p = self.plan(self.CLEANED, mode="cleanup")
        self.assertFalse(p.needs_work, f"unexpected work: {p.reasons}")

    def test_cleanup_output_still_owes_the_library_an_encode(self):
        """The point of a one-shot mode: the scan still sees work to do."""
        p = self.plan(self.CLEANED)
        self.assertTrue(p.needs_work)
        self.assertIn("encode video h264 -> x265 crf 22", p.reasons)

    def test_every_mode_is_stable_on_its_own_output(self):
        src = [V(0, "hevc", 1080),
               A(1, "aac", 2, "eng", title="Stereo", default=1),
               A(2, "eac3", 6, "eng", default=0), S(3, "eng")]
        for mode in self.cfg.modes:
            with self.subTest(mode=mode.id):
                for _ in range(3):
                    p = self.plan(src, mode=mode.id)
                    self.assertFalse(p.needs_work, f"{mode.id}: {p.reasons}")


class TestEditing(Base):
    def test_a_new_mode_starts_as_a_copy(self):
        self.cfg.mode("standard").video.crf_1080p = 19
        copied = cfgmod.add_mode(self.cfg, "Archive", copy_from="standard")
        self.assertEqual(copied.video.crf_1080p, 19)
        self.assertEqual(copied.name, "Archive")
        self.assertEqual(copied.id, "archive")
        self.assertEqual(copied.description, "")     # the source's is not copied

    def test_a_copy_is_detached_from_its_source(self):
        copied = cfgmod.add_mode(self.cfg, "Archive", copy_from="standard")
        copied.video.crf_1080p = 16
        self.assertEqual(self.cfg.mode("standard").video.crf_1080p, 22)

    def test_a_mode_can_start_from_the_built_in_defaults(self):
        self.cfg.mode("standard").video.crf_1080p = 19
        fresh = cfgmod.add_mode(self.cfg, "Fresh")
        self.assertEqual(fresh.video.crf_1080p, 22)

    def test_copying_an_unknown_mode_is_rejected(self):
        with self.assertRaises(ConfigError):
            cfgmod.add_mode(self.cfg, "Archive", copy_from="nope")

    def test_ids_are_unique(self):
        self.assertNotEqual(cfgmod.add_mode(self.cfg, "Cleanup").id, "cleanup")

    def test_settings_are_coerced_from_form_strings(self):
        mode = cfgmod.add_mode(self.cfg, "Strings")
        cfgmod.apply_mode_updates(self.cfg, mode,
                                  {"video.enabled": "false",
                                   "video.crf_1080p": "20"})
        self.assertIs(mode.video.enabled, False)
        self.assertEqual(mode.video.crf_1080p, 20)


class TestValidation(Base):
    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            cfgmod.resolve(self.cfg, self.lib, "nope")
        self.assertIn("no such mode", str(ctx.exception))
        self.assertIn("cleanup", str(ctx.exception))

    def test_unknown_setting_is_rejected_when_saved(self):
        mode = self.cfg.mode("cleanup")
        with self.assertRaises(ConfigError):
            cfgmod.apply_mode_updates(self.cfg, mode, {"video.nonsense": 1})

    def test_out_of_range_setting_is_rejected_when_saved(self):
        mode = self.cfg.mode("cleanup")
        with self.assertRaises(ConfigError):
            cfgmod.apply_mode_updates(self.cfg, mode, {"video.crf_1080p": 99})

    def test_a_rejected_update_changes_nothing(self):
        mode = self.cfg.mode("standard")
        with self.assertRaises(ConfigError):
            cfgmod.apply_mode_updates(
                self.cfg, mode, {"video.crf_1080p": 19, "video.preset": "turbo"})
        self.assertEqual(mode.video.crf_1080p, 22)

    def test_incoherent_height_bands_are_rejected(self):
        mode = self.cfg.mode("standard")
        with self.assertRaises(ConfigError):
            cfgmod.apply_mode_updates(self.cfg, mode, {"video.sd_max_height": 4000})

    def test_id_is_read_only(self):
        with self.assertRaises(ConfigError):
            cfgmod.apply_mode_updates(self.cfg, self.cfg.mode("cleanup"),
                                      {"id": "other"})

    def test_a_mode_in_use_cannot_be_removed(self):
        with self.assertRaises(ConfigError) as ctx:
            cfgmod.remove_mode(self.cfg, self.lib.mode)
        self.assertIn("Media", str(ctx.exception))
        self.assertIsNotNone(self.cfg.mode("standard"))

    def test_an_unused_mode_can_be_removed(self):
        cfgmod.remove_mode(self.cfg, "cleanup")
        self.assertIsNone(self.cfg.mode("cleanup"))

    def test_remove_unknown(self):
        with self.assertRaises(ConfigError):
            cfgmod.remove_mode(self.cfg, "nope")


class TestPersistence(Base):
    def test_modes_survive_a_round_trip(self):
        mode = cfgmod.add_mode(self.cfg, "Audio only", copy_from="cleanup")
        cfgmod.apply_mode_updates(self.cfg, mode, {"subtitles.enabled": False})
        back = cfgmod.loads(cfgmod.dump_toml(self.cfg))
        self.assertEqual(back, self.cfg)
        self.assertEqual([m.id for m in back.modes],
                         ["standard", "cleanup", "audio-only"])
        self.assertFalse(back.mode("audio-only").video.enabled)
        self.assertFalse(back.mode("audio-only").subtitles.enabled)

    def test_which_mode_a_library_uses_survives(self):
        cfgmod.apply_library_updates(self.cfg, self.lib, {"mode": "cleanup"})
        back = cfgmod.loads(cfgmod.dump_toml(self.cfg))
        self.assertEqual(back.libraries[0].mode, "cleanup")

    def test_generated_file_documents_the_modes(self):
        text = cfgmod.dump_toml(Config())
        self.assertIn("[[modes]]", text)
        self.assertIn("[modes.video]", text)

    def test_a_config_without_modes_gets_the_defaults(self):
        back = cfgmod.loads('state_db = "/tmp/x.db"')
        self.assertEqual([m.id for m in back.modes], ["standard", "cleanup"])

    def test_an_empty_mode_list_is_honoured(self):
        """An explicit empty list is a choice, not a missing section."""
        self.assertEqual(cfgmod.loads("modes = []").modes, [])


if __name__ == "__main__":
    unittest.main()
