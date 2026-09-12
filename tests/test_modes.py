"""Processing modes: overrides, validation, and the idempotency they must keep.

A mode is a one-shot override of a library's profile, used by the
/api/process call Sonarr and Radarr make on import. Two properties matter:

  - a mode must not leak. Resolving one returns a detached copy, so a
    "cleanup" request for one file cannot change what any other file, or any
    later scan, decides to do.
  - a mode must be idempotent *in its own terms*. Planning a cleanup-produced
    file under cleanup again must report no work, exactly as the library
    profile must for its own output, or an on-import hook that fires twice
    would encode twice.
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
        self.lib = self.cfg.libraries[0]

    def plan(self, streams, mode=None, **kw):
        lib = cfgmod.resolve_library(self.cfg, self.lib, mode)
        return plan_file(mk(streams, **kw), lib, self.cfg)


class TestDefaults(Base):
    def test_ships_with_all_and_cleanup(self):
        self.assertEqual([m.id for m in self.cfg.modes], ["all", "cleanup"])

    def test_all_is_the_library_profile_untouched(self):
        self.assertEqual(self.cfg.mode("all").overrides, {})
        self.assertIs(cfgmod.resolve_library(self.cfg, self.lib, "all"), self.lib)

    def test_no_mode_is_the_library_profile(self):
        self.assertIs(cfgmod.resolve_library(self.cfg, self.lib, None), self.lib)
        self.assertIs(cfgmod.resolve_library(self.cfg, self.lib, ""), self.lib)


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

    def test_the_same_file_under_all_does_encode(self):
        p = self.plan(self.STREAMS, mode="all")
        self.assertEqual(p.streams[0].codec, "libx265")

    def test_height_ceiling_does_not_apply(self):
        """Video off means 4K is cleaned rather than skipped outright."""
        p = self.plan([V(0, "h264", 2160), A(1, "eac3", 6, "eng")], mode="cleanup")
        self.assertIsNone(p.skip_reason)
        self.assertEqual(p.streams[0].codec, "copy")


class TestIsolation(Base):
    def test_resolving_does_not_mutate_the_library(self):
        derived = cfgmod.resolve_library(self.cfg, self.lib, "cleanup")
        self.assertFalse(derived.video.enabled)
        self.assertTrue(self.lib.video.enabled)
        self.assertIsNot(derived, self.lib)

    def test_mutating_the_copy_does_not_reach_the_library(self):
        derived = cfgmod.resolve_library(self.cfg, self.lib, "cleanup")
        derived.audio.preferred_languages.append("fre")
        self.assertNotIn("fre", self.lib.audio.preferred_languages)

    def test_repeated_resolution_is_stable(self):
        for _ in range(3):
            self.assertFalse(
                cfgmod.resolve_library(self.cfg, self.lib, "cleanup").video.enabled)
            self.assertTrue(self.lib.video.enabled)


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


class TestValidation(Base):
    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            cfgmod.resolve_library(self.cfg, self.lib, "nope")
        self.assertIn("no such mode", str(ctx.exception))
        self.assertIn("cleanup", str(ctx.exception))

    def test_identity_and_routing_cannot_be_overridden(self):
        for key in ("id", "name", "enabled", "paths"):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError):
                    cfgmod.add_mode(self.cfg, "Bad", {key: "x"})

    def test_unknown_override_key_is_rejected_when_saved(self):
        with self.assertRaises(ConfigError):
            cfgmod.add_mode(self.cfg, "Bad", {"video.nonsense": 1})

    def test_out_of_range_override_is_rejected_when_saved(self):
        with self.assertRaises(ConfigError):
            cfgmod.add_mode(self.cfg, "Bad", {"video.crf_1080p": 99})

    def test_a_rejected_mode_is_not_left_behind(self):
        before = list(self.cfg.modes)
        with self.assertRaises(ConfigError):
            cfgmod.add_mode(self.cfg, "Bad", {"video.nonsense": 1})
        self.assertEqual(self.cfg.modes, before)

    def test_incoherent_height_bands_are_rejected(self):
        with self.assertRaises(ConfigError):
            cfgmod.add_mode(self.cfg, "Bad", {"video.sd_max_height": 4000})

    def test_overrides_are_coerced_to_the_library_types(self):
        mode = cfgmod.add_mode(self.cfg, "Strings",
                               {"video.enabled": "false", "video.crf_1080p": "20"})
        self.assertIs(mode.overrides["video.enabled"], False)
        self.assertEqual(mode.overrides["video.crf_1080p"], 20)
        derived = cfgmod.resolve_library(self.cfg, self.lib, mode.id)
        self.assertFalse(derived.video.enabled)
        self.assertEqual(derived.video.crf_1080p, 20)

    def test_ids_are_unique(self):
        self.assertNotEqual(cfgmod.add_mode(self.cfg, "Cleanup").id, "cleanup")

    def test_the_last_mode_cannot_be_removed(self):
        for m in list(self.cfg.modes)[:-1]:
            cfgmod.remove_mode(self.cfg, m.id)
        with self.assertRaises(ConfigError):
            cfgmod.remove_mode(self.cfg, self.cfg.modes[0].id)

    def test_remove_unknown(self):
        with self.assertRaises(ConfigError):
            cfgmod.remove_mode(self.cfg, "nope")

    def test_update_replaces_overrides_wholesale(self):
        mode = self.cfg.mode("cleanup")
        cfgmod.apply_mode_updates(self.cfg, mode,
                                  {"overrides": {"subtitles.enabled": False}})
        self.assertEqual(mode.overrides, {"subtitles.enabled": False})

    def test_a_rejected_update_changes_nothing(self):
        mode = self.cfg.mode("cleanup")
        with self.assertRaises(ConfigError):
            cfgmod.apply_mode_updates(self.cfg, mode,
                                      {"overrides": {"video.nonsense": 1}})
        self.assertEqual(mode.overrides, {"video.enabled": False})

    def test_id_is_read_only(self):
        with self.assertRaises(ConfigError):
            cfgmod.apply_mode_updates(self.cfg, self.cfg.mode("cleanup"),
                                      {"id": "other"})


class TestRoundTrip(unittest.TestCase):
    def test_modes_survive_a_round_trip(self):
        cfg = Config()
        cfgmod.add_mode(cfg, "Audio only",
                        {"video.enabled": False, "subtitles.enabled": False})
        back = cfgmod.loads(cfgmod.dump_toml(cfg))
        self.assertEqual([m.id for m in back.modes],
                         ["all", "cleanup", "audio-only"])
        self.assertEqual(back.mode("audio-only").overrides,
                         {"video.enabled": False, "subtitles.enabled": False})

    def test_generated_file_documents_the_modes(self):
        text = cfgmod.dump_toml(Config())
        self.assertIn("[[modes]]", text)
        self.assertIn("one-shot override", text)

    def test_a_config_without_modes_gets_the_defaults(self):
        back = cfgmod.loads('state_db = "/tmp/x.db"')
        self.assertEqual([m.id for m in back.modes], ["all", "cleanup"])

    def test_an_empty_mode_list_is_honoured(self):
        """An explicit empty list is a choice, not a missing section."""
        self.assertEqual(cfgmod.loads("modes = []").modes, [])


if __name__ == "__main__":
    unittest.main()
