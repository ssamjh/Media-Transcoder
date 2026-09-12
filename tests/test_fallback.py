"""What happens to a run whose x265 pass came back bigger than the source.

The encode is the only part that failed to pay off. The stereo track and the
subtitle cleaning still have, so the run is rebuilt around the source video
stream rather than thrown away with it - and the result has to *settle*, or
the next scan would find the same work and be rejected the same way forever.

No ffmpeg is involved: encode and replace_original are stubbed, so what is
under test is the engine's decision, not the encoder's arithmetic.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import config as cfgmod          # noqa: E402
from app import engine as enginemod       # noqa: E402
from app import ffmpeg                    # noqa: E402
from app.config import Config             # noqa: E402
from app.db import Db                     # noqa: E402
from app.engine import Engine             # noqa: E402
from app.probe import Probe               # noqa: E402

from test_plan import A, S, V             # noqa: E402


class Base(unittest.TestCase):
    """An engine whose encoder is a pair of counters."""

    STREAMS = [V(0, "h264", 1080), A(1, "eac3", 6, "eng", default=1),
               S(2, "eng"), S(3, "jpn")]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        media = root / "media"
        media.mkdir()
        self.src = media / "Film.mkv"
        self.src.write_bytes(b"x" * 1024)

        self.cfg = Config()
        self.cfg.state_db = str(root / "state.db")
        self.cfg.output.temp_dir = str(root / "temp")
        self.cfg.schedule.enabled = False
        self.lib = cfgmod.add_library(self.cfg, "Media", [str(media)])
        self.lib.enabled = True
        self.mode = self.cfg.mode(self.lib.mode)

        self.db = Db(self.cfg.state_db)
        self.addCleanup(self.db.close)
        self.engine = Engine(self.cfg, self.db)

        # The plans the engine makes are real; only the two steps that touch
        # a file are replaced.
        self.plans: list = []
        self.ratios: list[float] = []
        self.accepted: list = []

        def fake_encode(plan, cfg, on_progress=None, cancel=None):
            self.plans.append(plan)
            ratio = self.ratios[min(len(self.plans) - 1, len(self.ratios) - 1)]
            out = Path(cfg.output.temp_dir) / f"out-{len(self.plans)}.mkv"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"y")
            return ffmpeg.EncodeResult(
                out_path=out, in_size=1_000_000,
                out_size=int(1_000_000 * ratio), elapsed=1.0,
                video_encoded=plan.encodes_video,
            )

        def fake_replace(src, result, cfg, profile, on_progress=None):
            if ffmpeg.size_verdict(result.in_size, result.out_size,
                                   profile.output, result.video_encoded):
                return False
            self.accepted.append(result)
            return True

        for holder, name, stub in (
            (enginemod.ffmpeg, "encode", fake_encode),
            (enginemod.ffmpeg, "replace_original", fake_replace),
            (enginemod, "probe_file", lambda p, *a, **kw: Probe(
                path=str(p), streams=self.STREAMS,
                fmt={"duration": "2700", "size": "1000000"})),
        ):
            self.addCleanup(setattr, holder, name, getattr(holder, name))
            setattr(holder, name, stub)

    def run_one(self, *ratios: float) -> str:
        """Process the file, with each pass coming back at the given ratio."""
        self.ratios = list(ratios)
        return self.engine._process_one(str(self.src))

    def row(self):
        return self.db.get(str(self.src))


class TestAcceptedEncode(Base):
    def test_an_encode_that_shrank_is_kept_and_nothing_is_rebuilt(self):
        self.assertEqual(self.run_one(0.6), "done")
        self.assertEqual(len(self.plans), 1)
        self.assertTrue(self.plans[0].encodes_video)
        self.assertEqual(self.row()["status"], "done")
        self.assertIsNone(self.row()["error"])


class TestRejectedForSize(Base):
    def test_a_bigger_output_is_rebuilt_around_the_source_video(self):
        self.assertEqual(self.run_one(1.4, 0.95), "done")
        self.assertEqual(len(self.plans), 2)
        first, second = self.plans
        self.assertTrue(first.encodes_video)
        self.assertFalse(second.encodes_video)
        self.assertEqual(len(self.accepted), 1)

    def test_the_rebuild_keeps_the_audio_and_subtitle_work(self):
        self.run_one(1.4, 0.95)
        first, second = self.plans
        self.assertEqual([s.src_index for s in second.streams],
                         [s.src_index for s in first.streams])
        self.assertTrue(any(s.kind == "audio" and s.is_encode
                            for s in second.streams))
        self.assertIn("drop unwanted subtitle", second.reasons)

    def test_a_rebuilt_file_settles_instead_of_queueing_forever(self):
        """Left pending, the same rejected encode would run on every scan."""
        self.run_one(1.4, 0.95)
        row = self.row()
        self.assertEqual(row["status"], "skip")
        self.assertIn("over the", row["error"])
        self.assertIn("kept the source video stream", row["error"])

    def test_the_rebuild_is_not_judged_on_size(self):
        """Copying the video back in can only make the file bigger again."""
        self.assertEqual(self.run_one(1.4, 1.35), "done")
        self.assertEqual(len(self.accepted), 1)
        self.assertEqual(self.row()["status"], "skip")

    def test_a_failed_rebuild_leaves_the_original_alone(self):
        def explode(plan, cfg, on_progress=None, cancel=None):
            if plan.encodes_video:
                self.plans.append(plan)
                out = Path(cfg.output.temp_dir) / "out.mkv"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"y")
                return ffmpeg.EncodeResult(out, 1_000_000, 1_400_000, 1.0, True)
            raise ffmpeg.EncodeError("muxer said no")

        enginemod.ffmpeg.encode = explode
        self.assertEqual(self.run_one(1.4), "skip")
        self.assertEqual(self.accepted, [])
        self.assertIn("original kept", self.row()["error"])

    def test_an_implausibly_small_output_is_not_rebuilt(self):
        """The floor means something was lost, which a rebuild cannot fix."""
        self.assertEqual(self.run_one(0.05), "skip")
        self.assertEqual(len(self.plans), 1)
        self.assertIn("floor", self.row()["error"])
        self.assertIn("original kept", self.row()["error"])


class TestVideoAlreadyCopied(Base):
    """A run that never encoded the video is never asked whether it shrank."""

    def test_a_cleanup_run_that_grew_is_still_accepted(self):
        self.lib.mode = "cleanup"
        self.assertEqual(self.run_one(1.4), "done")
        self.assertEqual(len(self.plans), 1)
        self.assertFalse(self.plans[0].encodes_video)
        self.assertEqual(len(self.accepted), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
