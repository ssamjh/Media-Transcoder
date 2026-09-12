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
    """A config with a single library: Config() itself now has none."""
    c = Config()
    cfgmod.add_library(c, name, [path])
    return c


def three_libraries() -> Config:
    c = one_library("/media/TV", "TV")
    cfgmod.add_library(c, "Movies", ["/media/Movies"])
    cfgmod.add_library(c, "Home Video", ["/media/Home", "/media/Camera"])
    return c


class TestRoundTrip(unittest.TestCase):
    def test_defaults_survive_a_round_trip(self):
        c = Config()
        self.assertEqual(c.libraries, [])      # a fresh install scans nothing
        self.assertEqual(cfgmod.loads(cfgmod.dump_toml(c)), c)

    def test_multiple_libraries_survive_a_round_trip(self):
        c = three_libraries()
        self.assertEqual(cfgmod.loads(cfgmod.dump_toml(c)), c)

    def test_divergent_library_profiles_survive(self):
        c = three_libraries()
        cfgmod.apply_library_updates(c, c.library("tv"), {
            "video.crf_1080p": 19, "subtitles.enabled": False,
            "audio.keep_best_only": False,
        })
        cfgmod.apply_library_updates(c, c.library("movies"), {
            "video.enabled": False, "output.container": "keep",
            "audio.stereo_bitrate": "192k",
        })
        cfgmod.apply_library_updates(c, c.library("home-video"), {
            "enabled": False, "audio.enabled": False,
            "output.replace_original": False,
        })
        back = cfgmod.loads(cfgmod.dump_toml(c))
        self.assertEqual(back, c)
        self.assertFalse(back.library("movies").video.enabled)
        self.assertFalse(back.library("tv").subtitles.enabled)
        self.assertEqual(back.library("movies").output.container, "keep")

    def test_modified_global_values_survive(self):
        c = Config()
        cfgmod.apply_updates(c, {
            "workers.count": 8, "schedule.scan_interval_hours": 2.5,
            "output.min_duration_ratio": 0.95,
        })
        self.assertEqual(cfgmod.loads(cfgmod.dump_toml(c)), c)

    def test_awkward_strings_survive(self):
        c = one_library()
        lib = c.libraries[0]
        cfgmod.apply_library_updates(c, lib, {
            "audio.commentary_pattern": r'commentary|"quoted"|back\slash',
            "audio.stereo_title": 'He said "hi"',
            "name": "Films & \"Shorts\"",
        })
        back = cfgmod.loads(cfgmod.dump_toml(c))
        self.assertEqual(back.libraries[0].audio.commentary_pattern,
                         lib.audio.commentary_pattern)
        self.assertEqual(back.libraries[0].audio.stereo_title,
                         lib.audio.stereo_title)
        self.assertEqual(back.libraries[0].name, lib.name)

    def test_dict_fields_survive(self):
        c = one_library()
        cfgmod.apply_library_updates(c, c.libraries[0],
                                     {"audio.channel_score": {"6": 40, "2": 10}})
        back = cfgmod.loads(cfgmod.dump_toml(c))
        self.assertEqual(back.libraries[0].audio.channel_score, {"6": 40, "2": 10})

    def test_empty_string_entry_is_preserved(self):
        """"" means "no language tag" in undefined_languages."""
        c = one_library()
        self.assertIn("", c.libraries[0].subtitles.undefined_languages)
        back = cfgmod.loads(cfgmod.dump_toml(c))
        self.assertIn("", back.libraries[0].subtitles.undefined_languages)

    def test_save_and_load_from_disk(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            c = three_libraries()
            cfgmod.apply_library_updates(c, c.library("movies"),
                                         {"video.crf_720p": 25})
            cfgmod.save(c, p)
            loaded = cfgmod.load(p)
            self.assertEqual(loaded, c)
            self.assertEqual(loaded.library("movies").video.crf_720p, 25)

    def test_generated_file_carries_its_documentation(self):
        text = cfgmod.dump_toml(one_library())
        self.assertIn("# Quality for 1080p sources", text)
        self.assertIn("(takes effect on restart)", text)
        self.assertIn("[[libraries]]", text)

    def test_missing_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(cfgmod.load(Path(d) / "absent.toml"), Config())


class TestLibraries(unittest.TestCase):
    def test_add_generates_a_unique_slug(self):
        c = one_library()
        a = cfgmod.add_library(c, "TV Shows", ["/media/TV"])
        b = cfgmod.add_library(c, "TV Shows", ["/media/TV2"])
        self.assertEqual(a.id, "tv-shows")
        self.assertEqual(b.id, "tv-shows-2")

    def test_new_library_starts_from_defaults(self):
        c = one_library()
        lib = cfgmod.add_library(c, "New", ["/media/New"])
        self.assertTrue(lib.video.enabled)
        self.assertEqual(lib.video.crf_1080p, 22)
        self.assertEqual(lib.subtitles.keep_languages, ["eng", "en", "english"])

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
        self.assertEqual([l.id for l in c.active_libraries], ["tv", "home-video"])

    def test_library_for_handles_multiple_roots(self):
        c = three_libraries()
        self.assertEqual(c.library_for("/media/Camera/clip.mp4").id, "home-video")
        self.assertEqual(c.library_for("/media/Home/x/y.mkv").id, "home-video")


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.c = one_library()
        self.lib = self.c.libraries[0]

    def test_rejects_unknown_key(self):
        with self.assertRaises(ConfigError):
            cfgmod.apply_updates(self.c, {"workers.nope": 1})
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(self.c, self.lib, {"video.nope": 1})

    def test_rejects_out_of_range(self):
        for key, value in [("web.port", 0), ("workers.count", 0),
                           ("schedule.scan_interval_hours", 0)]:
            with self.assertRaises(ConfigError, msg=key):
                cfgmod.apply_updates(Config(), {key: value})
        for key, value in [("video.crf_1080p", 99), ("video.crf_1080p", -1),
                           ("min_size_mb", -5)]:
            c = one_library()
            with self.assertRaises(ConfigError, msg=key):
                cfgmod.apply_library_updates(c, c.libraries[0], {key: value})

    def test_rejects_bad_choice(self):
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(self.c, self.lib, {"video.preset": "turbo"})
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(self.c, self.lib,
                                         {"output.container": "avi"})

    def test_rejects_non_numeric(self):
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(self.c, self.lib,
                                         {"video.crf_1080p": "high"})

    def test_rejects_inconsistent_height_bands(self):
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(self.c, self.lib,
                                         {"video.sd_max_height": 2000})

    def test_rejects_empty_library_paths(self):
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(self.c, self.lib, {"paths": []})

    def test_nothing_is_applied_when_one_field_fails(self):
        before = self.lib.video.crf_720p
        with self.assertRaises(ConfigError):
            cfgmod.apply_library_updates(self.c, self.lib, {
                "video.crf_720p": 18, "video.crf_1080p": 999,
            })
        self.assertEqual(self.lib.video.crf_720p, before)

    def test_coerces_strings_from_form_fields(self):
        changed = cfgmod.apply_library_updates(self.c, self.lib, {
            "video.crf_1080p": "20",
            "subtitles.drop_image_subs": "true",
            "video.enabled": "false",
            "paths": "/a, /b",
        })
        self.assertEqual(len(changed), 4)
        self.assertEqual(self.lib.video.crf_1080p, 20)
        self.assertIs(self.lib.subtitles.drop_image_subs, True)
        self.assertIs(self.lib.video.enabled, False)
        self.assertEqual(self.lib.paths, ["/a", "/b"])

    def test_unchanged_values_are_not_reported_as_changed(self):
        self.assertEqual(
            cfgmod.apply_library_updates(self.c, self.lib,
                                         {"video.preset": "medium"}), [])


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

        lib = cfgmod.LibraryCfg()
        l = {f["key"]: f["type"] for block in cfgmod.library_schema(lib)
             for f in block["fields"]}
        self.assertEqual(l["paths"], "list")
        self.assertEqual(l["audio.channel_score"], "map")
        self.assertEqual(l["video.preset"], "str")
        self.assertEqual(l["video.enabled"], "bool")

    def test_library_id_is_marked_read_only(self):
        lib = cfgmod.LibraryCfg()
        entry = next(f for block in cfgmod.library_schema(lib)
                     for f in block["fields"] if f["key"] == "id")
        self.assertTrue(entry["readonly"])

    def test_each_stage_exposes_an_enabled_switch(self):
        lib = cfgmod.LibraryCfg()
        keys = {f["key"] for block in cfgmod.library_schema(lib)
                for f in block["fields"]}
        for key in ("video.enabled", "audio.enabled", "subtitles.enabled"):
            self.assertIn(key, keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
