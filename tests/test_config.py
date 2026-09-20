"""Config schema, validation, libraries and TOML round-tripping.

The web panel rewrites the config file, so a lossy round-trip would silently
corrupt settings. These tests pin that down, across multiple libraries with
independent profiles.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as cfgmod                    # noqa: E402
from app.config import Config, ConfigError          # noqa: E402


def one_library(path: str = "/media/A", name: str = "Media") -> Config:
    """A config with a single, enabled library on a mode of its own.

    Config() itself has no libraries, and add_library() creates them switched
    off and pointing at the shared Standard mode - so both steps are explicit
    here, as they are in the panel. Giving it a private mode keeps these
    tests from changing what every other library would do.
    """
    c = Config()
    lib = cfgmod.add_library(c, name, [path])
    lib.enabled = True
    lib.mode = cfgmod.add_mode(c, f"{name} mode", copy_from="standard").id
    return c


def mode_of(c: Config, lib_id: str = "media"):
    """The mode a library is treated with."""
    return c.mode(c.library(lib_id).mode)


def three_libraries() -> Config:
    c = one_library("/media/TV", "TV")
    for name, paths in [("Movies", ["/media/Movies"]),
                        ("Movies 4K", ["/media/Movies-4K", "/media/TV-4K"])]:
        cfgmod.add_library(c, name, paths).enabled = True
    return c


class TestRoundTrip(unittest.TestCase):
    def test_defaults_survive_a_round_trip(self):
        c = Config()
        self.assertEqual(c.libraries, [])      # a fresh install scans nothing
        self.assertEqual(cfgmod.loads(cfgmod.dump_toml(c)), c)

    def test_multiple_libraries_survive_a_round_trip(self):
        c = three_libraries()
        self.assertEqual(cfgmod.loads(cfgmod.dump_toml(c)), c)

    def test_divergent_modes_survive(self):
        c = three_libraries()
        for lib_id, updates in [
            ("tv", {"video.crf_1080p": 19, "subtitles.enabled": False,
                    "audio.keep_stereo_only": True}),
            ("movies", {"video.enabled": False, "output.container": "keep",
                        "audio.stereo_bitrate": "192k"}),
            ("movies-4k", {"audio.enabled": False,
                            "output.replace_original": False}),
        ]:
            lib = c.library(lib_id)
            # Each library gets a mode of its own to diverge in.
            lib.mode = cfgmod.add_mode(c, f"{lib.name} mode",
                                       copy_from="standard").id
            cfgmod.apply_mode_updates(c, c.mode(lib.mode), updates)
        cfgmod.apply_library_updates(c, c.library("movies-4k"),
                                     {"enabled": False})

        back = cfgmod.loads(cfgmod.dump_toml(c))
        self.assertEqual(back, c)
        self.assertFalse(mode_of(back, "movies").video.enabled)
        self.assertFalse(mode_of(back, "tv").subtitles.enabled)
        self.assertEqual(mode_of(back, "movies").output.container, "keep")
        self.assertFalse(back.library("movies-4k").enabled)

    def test_modified_global_values_survive(self):
        c = Config()
        cfgmod.apply_updates(c, {
            "workers.count": 8, "schedule.scan_interval_hours": 2.5,
            "output.min_duration_ratio": 0.95,
        })
        self.assertEqual(cfgmod.loads(cfgmod.dump_toml(c)), c)

    def test_awkward_strings_survive(self):
        c = one_library()
        mode = mode_of(c)
        cfgmod.apply_mode_updates(c, mode, {
            "audio.commentary_pattern": r'commentary|"quoted"|back\slash',
        })
        cfgmod.apply_library_updates(c, c.libraries[0],
                                     {"name": "Films & \"Shorts\""})
        back = cfgmod.loads(cfgmod.dump_toml(c))
        self.assertEqual(mode_of(back).audio.commentary_pattern,
                         mode.audio.commentary_pattern)
        self.assertEqual(back.libraries[0].name, c.libraries[0].name)

    def test_list_fields_survive(self):
        c = one_library()
        cfgmod.apply_mode_updates(
            c, mode_of(c), {"audio.downmix_channels": ["6", "8", "7"]})
        back = cfgmod.loads(cfgmod.dump_toml(c))
        self.assertEqual(mode_of(back).audio.downmix_channels, ["6", "8", "7"])

    def test_empty_string_entry_is_preserved(self):
        """"" means "no language tag" in undefined_languages."""
        c = one_library()
        self.assertIn("", mode_of(c).subtitles.undefined_languages)
        back = cfgmod.loads(cfgmod.dump_toml(c))
        self.assertIn("", mode_of(back).subtitles.undefined_languages)

    def test_save_and_load_from_disk(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            c = three_libraries()
            movies = c.library("movies")
            movies.mode = cfgmod.add_mode(c, "Movies mode",
                                          copy_from="standard").id
            cfgmod.apply_mode_updates(c, c.mode(movies.mode),
                                      {"video.crf_720p": 25})
            cfgmod.save(c, p)
            loaded = cfgmod.load(p)
            self.assertEqual(loaded, c)
            self.assertEqual(mode_of(loaded, "movies").video.crf_720p, 25)

    def test_generated_file_carries_its_documentation(self):
        text = cfgmod.dump_toml(one_library())
        self.assertIn("# Quality for 1080p sources", text)
        self.assertIn("(takes effect on restart)", text)
        self.assertIn("[[libraries]]", text)

    def test_missing_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cfgmod.load(Path(d) / "absent.toml"), Config())

    def test_named_integrations_and_secrets_survive_round_trip(self):
        c = Config()
        c.integrations.sonarr = [cfgmod.ArrInstanceCfg(
            id="tv", name="TV Sonarr", url="http://sonarr:8989",
            api_key="arr-key", path_from="/media", path_to="/library",
            request_timeout=12.0, command_timeout=45.0, poll_interval=1.5,
            max_retries=4, secret="webhook-secret")]
        c.integrations.autopulse = cfgmod.AutoPulseCfg(
            enabled=True, url="http://autopulse", username="u",
            password="p", trigger_endpoint="/triggers/manual",
            max_retries=5)
        back = cfgmod.loads(cfgmod.dump_toml(c))
        self.assertEqual(back, c)
        redacted = cfgmod.integration_schema(c)
        self.assertTrue(redacted["sonarr"][0]["api_key_configured"])
        self.assertNotIn("arr-key", str(redacted))
        self.assertTrue(redacted["autopulse"]["password_configured"])
        self.assertNotIn("p", redacted["autopulse"])


class TestLibraries(unittest.TestCase):
    def test_add_generates_a_unique_slug(self):
        c = one_library()
        a = cfgmod.add_library(c, "TV Shows", ["/media/TV"])
        b = cfgmod.add_library(c, "TV Shows", ["/media/TV2"])
        self.assertEqual(a.id, "tv-shows")
        self.assertEqual(b.id, "tv-shows-2")

    def test_a_new_library_starts_switched_off(self):
        """Adding paths must never be the thing that starts work."""
        c = one_library()
        lib = cfgmod.add_library(c, "New", ["/media/New"])
        self.assertFalse(lib.enabled)
        self.assertNotIn(lib, c.active_libraries)
        self.assertIsNone(c.library_for("/media/New/a.mkv"))

    def test_new_library_starts_on_the_standard_mode(self):
        """It carries no settings of its own: it names a mode instead."""
        c = one_library()
        lib = cfgmod.add_library(c, "New", ["/media/New"])
        self.assertEqual(lib.mode, "standard")
        profile = cfgmod.resolve(c, lib)
        self.assertTrue(profile.video.enabled)
        self.assertEqual(profile.video.crf_1080p, 22)
        self.assertEqual(profile.subtitles.keep_languages,
                         ["eng", "en", "english"])

    def test_rejects_overlapping_paths(self):
        c = one_library("/media/TV")
        for bad in (["/media/TV"], ["/media/TV/Sub"], ["/media"]):
            with self.assertRaises(ConfigError, msg=str(bad)):
                cfgmod.add_library(c, "Bad", bad)
        self.assertEqual(len(c.libraries), 1)   # nothing was left behind

    def test_rejects_duplicate_paths_within_one_library(self):
        c = one_library()
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(c, c.libraries[0],
                                         {"paths": ["/media/X", "/media/X"]})

    def test_rejects_nameless_or_pathless(self):
        c = one_library()
        with self.assertRaises(ConfigError):
            cfgmod.add_library(c, "   ", ["/media/B"])
        with self.assertRaises(ConfigError):
            cfgmod.add_library(c, "Fine", [])

    def test_remove(self):
        c = three_libraries()
        cfgmod.remove_library(c, "movies")
        self.assertIsNone(c.library("movies"))
        self.assertEqual(len(c.libraries), 2)

    def test_the_last_library_can_be_removed(self):
        """Nothing is scanned without a library, which is a valid state."""
        c = one_library()
        cfgmod.remove_library(c, c.libraries[0].id)
        self.assertEqual(c.libraries, [])
        self.assertEqual(cfgmod.loads(cfgmod.dump_toml(c)).libraries, [])

    def test_remove_unknown(self):
        with self.assertRaises(ConfigError):
            cfgmod.remove_library(three_libraries(), "nope")

    def test_id_is_read_only(self):
        c = one_library()
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(c, c.libraries[0], {"id": "other"})

    def test_active_libraries_excludes_disabled(self):
        c = three_libraries()
        c.library("movies").enabled = False
        self.assertEqual([l.id for l in c.active_libraries], ["tv", "movies-4k"])

    def test_library_for_handles_multiple_roots(self):
        c = three_libraries()
        self.assertEqual(c.library_for("/media/TV-4K/S01/E01.mkv").id, "movies-4k")
        self.assertEqual(c.library_for("/media/Movies-4K/Film/film.mkv").id, "movies-4k")


class TestMigration(unittest.TestCase):
    """Settings that were renamed still load from an existing config file."""

    HEAD = """
[[libraries]]
name = "TV"
paths = ["/media/TV"]
[libraries.output]
"""

    def load(self, body: str):
        """The mode that the migrated library ends up being treated with."""
        cfg = cfgmod.loads(self.HEAD + body)
        return cfg.mode(cfg.libraries[0].mode)

    def test_only_replace_if_smaller_true_becomes_a_ceiling_of_one(self):
        out = self.load("only_replace_if_smaller = true").output
        self.assertEqual(out.max_size_ratio, 1.0)
        self.assertEqual(out.min_size_ratio, 0.0)   # it never had a floor

    def test_only_replace_if_smaller_false_lifts_the_ceiling(self):
        out = self.load("only_replace_if_smaller = false").output
        self.assertEqual(out.max_size_ratio, 10.0)

    def test_an_explicit_new_setting_wins_over_the_old_one(self):
        out = self.load("""
only_replace_if_smaller = true
max_size_ratio = 1.5
""").output
        self.assertEqual(out.max_size_ratio, 1.5)

    def test_a_genuinely_unknown_key_is_still_an_error(self):
        with self.assertRaises(ConfigError):
            self.load("only_replace_if_bigger = true")

    AUDIO_HEAD = """
[[libraries]]
name = "TV"
paths = ["/media/TV"]
[libraries.audio]
"""

    def load_audio(self, body: str):
        cfg = cfgmod.loads(self.AUDIO_HEAD + body)
        return cfg.mode(cfg.libraries[0].mode).audio

    def test_the_two_stereo_bitrates_collapse_to_the_transcode_value(self):
        audio = self.load_audio("""
stereo_bitrate = "160k"
stereo_convert_bitrate = "192k"
""")
        self.assertEqual(audio.stereo_bitrate, "192k")

    def test_an_explicit_downmix_bitrate_survives_the_collapse(self):
        audio = self.load_audio("""
stereo_bitrate = "128k"
stereo_convert_bitrate = "192k"
""")
        self.assertEqual(audio.stereo_bitrate, "128k")

    def test_a_lone_convert_bitrate_becomes_the_stereo_bitrate(self):
        self.assertEqual(
            self.load_audio('stereo_convert_bitrate = "256k"').stereo_bitrate,
            "256k")

    def test_the_retired_stereo_title_is_ignored(self):
        audio = self.load_audio('stereo_title = "Stereo"')
        self.assertFalse(hasattr(audio, "stereo_title"))


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.c = one_library()
        self.lib = self.c.libraries[0]
        self.mode = mode_of(self.c)

    def set_mode(self, updates):
        return cfgmod.apply_mode_updates(self.c, self.mode, updates)

    def test_rejects_unknown_key(self):
        with self.assertRaises(ConfigError):
            cfgmod.apply_updates(self.c, {"workers.nope": 1})
        with self.assertRaises(ConfigError):
            self.set_mode({"video.nope": 1})

    def test_rejects_out_of_range(self):
        for key, value in [("web.port", 0), ("workers.count", 0),
                           ("schedule.scan_interval_hours", 0)]:
            with self.assertRaises(ConfigError, msg=key):
                cfgmod.apply_updates(Config(), {key: value})
        for key, value in [("video.crf_1080p", 99), ("video.crf_1080p", -1)]:
            c = one_library()
            with self.assertRaises(ConfigError, msg=key):
                cfgmod.apply_mode_updates(c, mode_of(c), {key: value})
        c = one_library()
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(c, c.libraries[0], {"min_size_mb": -5})

    def test_rejects_bad_choice(self):
        with self.assertRaises(ConfigError):
            self.set_mode({"video.preset": "turbo"})
        with self.assertRaises(ConfigError):
            self.set_mode({"output.container": "avi"})

    def test_rejects_non_numeric(self):
        with self.assertRaises(ConfigError):
            self.set_mode({"video.crf_1080p": "high"})

    def test_rejects_a_size_window_that_accepts_nothing(self):
        with self.assertRaises(ConfigError):
            self.set_mode({"output.min_size_ratio": 0.9,
                           "output.max_size_ratio": 0.5})

    def test_rejects_inconsistent_height_bands(self):
        with self.assertRaises(ConfigError):
            self.set_mode({"video.sd_max_height": 2000})

    def test_rejects_a_bitrate_that_is_not_one(self):
        with self.assertRaises(ConfigError):
            self.set_mode({"audio.stereo_bitrate": "loud"})

    def test_rejects_inconsistent_bitrate_bands(self):
        with self.assertRaises(ConfigError):
            self.set_mode({"audio.stereo_bitrate_low": "320k"})

    def test_rejects_a_low_threshold_above_the_mid_one(self):
        with self.assertRaises(ConfigError):
            self.set_mode({"audio.low_max_source_bitrate": "256k"})

    def test_accepts_a_consistent_ladder(self):
        self.set_mode({"audio.stereo_bitrate": "256k",
                       "audio.stereo_bitrate_mid": "192k",
                       "audio.stereo_bitrate_low": "128k"})
        self.assertEqual(self.mode.audio.stereo_bitrate_low, "128k")

    def test_rejects_empty_library_paths(self):
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(self.c, self.lib, {"paths": []})

    def test_rejects_invalid_integration_timeouts_and_retries(self):
        self.c.integrations.sonarr = [cfgmod.ArrInstanceCfg(
            id="tv", request_timeout=0.0)]
        with self.assertRaises(ConfigError):
            cfgmod._validate_global(self.c)
        self.c.integrations.sonarr[0].request_timeout = 15.0
        self.c.integrations.sonarr[0].max_retries = 11
        with self.assertRaises(ConfigError):
            cfgmod._validate_global(self.c)

    def test_rejects_incomplete_paths_and_enabled_autopulse_without_url(self):
        self.c.integrations.sonarr = [cfgmod.ArrInstanceCfg(
            id="tv", path_from="/arr/media")]
        with self.assertRaises(ConfigError):
            cfgmod._validate_global(self.c)
        self.c.integrations.sonarr = []
        self.c.integrations.autopulse.enabled = True
        with self.assertRaises(ConfigError):
            cfgmod._validate_global(self.c)

    def test_nothing_is_applied_when_one_field_fails(self):
        before = self.mode.video.crf_720p
        with self.assertRaises(ConfigError):
            self.set_mode({"video.crf_720p": 18, "video.crf_1080p": 999})
        self.assertEqual(self.mode.video.crf_720p, before)

    def test_coerces_strings_from_form_fields(self):
        changed = self.set_mode({
            "video.crf_1080p": "20",
            "subtitles.drop_image_subs": "true",
            "video.enabled": "false",
        })
        changed += cfgmod.apply_library_updates(self.c, self.lib,
                                                {"paths": "/a, /b"})
        self.assertEqual(len(changed), 4)
        self.assertEqual(self.mode.video.crf_1080p, 20)
        self.assertIs(self.mode.subtitles.drop_image_subs, True)
        self.assertIs(self.mode.video.enabled, False)
        self.assertEqual(self.lib.paths, ["/a", "/b"])

    def test_unchanged_values_are_not_reported_as_changed(self):
        self.assertEqual(
            cfgmod.apply_library_updates(self.c, self.lib,
                                         {"name": self.lib.name}), [])


class TestSchema(unittest.TestCase):
    def test_every_global_field_is_described(self):
        missing = [f["key"] for block in cfgmod.schema(Config())
                   for f in block["fields"] if not f["desc"]]
        self.assertEqual(missing, [], f"undocumented settings: {missing}")

    def test_every_library_field_is_described(self):
        lib = cfgmod.LibraryCfg()
        missing = [f["key"] for block in cfgmod.library_schema(lib)
                   for f in block["fields"] if not f["desc"]]
        self.assertEqual(missing, [], f"undocumented settings: {missing}")

    def test_global_schema_excludes_libraries(self):
        keys = {f["key"] for block in cfgmod.schema(Config())
                for f in block["fields"]}
        self.assertNotIn("libraries", keys)

    def test_types_are_recognised(self):
        g = {f["key"]: f["type"] for block in cfgmod.schema(Config())
             for f in block["fields"]}
        self.assertEqual(g["schedule.enabled"], "bool")
        self.assertEqual(g["workers.count"], "int")
        self.assertEqual(g["output.min_duration_ratio"], "float")

        l = {f["key"]: f["type"] for block in
             cfgmod.library_schema(cfgmod.LibraryCfg())
             for f in block["fields"]}
        self.assertEqual(l["paths"], "list")
        self.assertEqual(l["min_size_mb"], "int")

        m = {f["key"]: f["type"] for block in
             cfgmod.mode_schema(cfgmod.ModeCfg())
             for f in block["fields"]}
        self.assertEqual(m["audio.downmix_channels"], "list")
        self.assertEqual(m["video.preset"], "str")
        self.assertEqual(m["video.enabled"], "bool")

    def test_library_id_is_marked_read_only(self):
        lib = cfgmod.LibraryCfg()
        entry = next(f for block in cfgmod.library_schema(lib)
                     for f in block["fields"] if f["key"] == "id")
        self.assertTrue(entry["readonly"])

    def test_each_stage_exposes_an_enabled_switch(self):
        keys = {f["key"] for block in cfgmod.mode_schema(cfgmod.ModeCfg())
                for f in block["fields"]}
        for key in ("video.enabled", "audio.enabled", "subtitles.enabled"):
            self.assertIn(key, keys)

    def test_every_mode_field_is_described(self):
        missing = [f["key"] for block in cfgmod.mode_schema(cfgmod.ModeCfg())
                   for f in block["fields"] if not f["desc"]]
        self.assertEqual(missing, [], f"undocumented settings: {missing}")

    def test_the_mode_field_offers_the_modes_that_exist(self):
        cfg = one_library()
        entry = next(f for block in cfgmod.library_schema(cfg.libraries[0], cfg)
                     for f in block["fields"] if f["key"] == "mode")
        self.assertEqual(entry["choices"], [m.id for m in cfg.modes])


if __name__ == "__main__":
    unittest.main(verbosity=2)
