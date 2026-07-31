"""
Regression tests for DedupEngine.execute_all selection handling — the only
code path that deletes media. An empty plan list must run NOTHING; passing
None means "use the full scan result" — plus scoring/keeper-choice tests for
_score_file and pick_keeper (these decide which file gets deleted).

Run: python -m unittest test_dedup_engine
"""

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import test_stubs

test_stubs.install_common_stubs()

# dedup_engine imports these extra names from plex_client; the shared stub only
# provides PlexClient, so add the value classes it also pulls in.
_plex_stub = sys.modules["plex_client"]
_plex_stub.DuplicateGroup = type("DuplicateGroup", (), {})
_plex_stub.MediaFile = type("MediaFile", (), {})

from dedup_engine import DedupEngine  # noqa: E402
from library_analyzer import _build_nordic_pattern  # noqa: E402


def _make_engine(dry_run: bool = True) -> DedupEngine:
    """Build an engine without touching __init__ (which wires real clients)."""
    e = DedupEngine.__new__(DedupEngine)
    e.config = MagicMock()
    e.config.dry_run = dry_run
    e.plex = MagicMock()
    e._plans = []
    return e


class ExecuteAllSelectionTests(unittest.TestCase):
    def test_empty_list_runs_nothing(self):
        """An empty selection must NOT fall back to every plan."""
        e = _make_engine()
        e._plans = ["PLAN_A", "PLAN_B"]
        calls = []
        e.execute_plan = lambda plan: (calls.append(plan) or True)

        result = e.execute_all([])

        self.assertEqual(calls, [], "execute_all([]) must not execute any plan")
        self.assertEqual(result, {"success": 0, "failed": 0})

    def test_none_uses_scan_result(self):
        """None means 'no argument given' — use the stored scan result."""
        e = _make_engine()
        e._plans = ["PLAN_A", "PLAN_B"]
        calls = []
        e.execute_plan = lambda plan: (calls.append(plan) or True)

        result = e.execute_all(None)

        self.assertEqual(calls, ["PLAN_A", "PLAN_B"])
        self.assertEqual(result["success"], 2)

    def test_explicit_subset_runs_only_that_subset(self):
        e = _make_engine()
        e._plans = ["PLAN_A", "PLAN_B", "PLAN_C"]
        calls = []
        e.execute_plan = lambda plan: (calls.append(plan) or True)

        e.execute_all(["PLAN_B"])

        self.assertEqual(calls, ["PLAN_B"])


class RecycleBinDestTests(unittest.TestCase):
    def _engine(self, bin_dir):
        e = DedupEngine.__new__(DedupEngine)
        e.config = MagicMock()
        e.config.recycle_bin = bin_dir
        return e

    def test_no_collision_keeps_basename(self):
        d = tempfile.mkdtemp()
        e = self._engine(d)
        self.assertEqual(
            e._recycle_dest("/movies/A/Film.mkv"), os.path.join(d, "Film.mkv")
        )

    def test_same_basename_does_not_overwrite(self):
        d = tempfile.mkdtemp()
        e = self._engine(d)
        # First copy already sitting in the bin.
        open(os.path.join(d, "Film.mkv"), "w").close()
        dest2 = e._recycle_dest("/other/Film.mkv")
        self.assertEqual(dest2, os.path.join(d, "Film.1.mkv"))
        open(dest2, "w").close()
        # A third same-named file keeps climbing rather than clobbering.
        self.assertEqual(
            e._recycle_dest("/third/Film.mkv"), os.path.join(d, "Film.2.mkv")
        )


# Mirror of the Config.quality_ranks entries the scoring tests rely on; passed
# in explicitly so these tests pin _score_file's math, not config defaults.
_QUALITY_RANKS = {
    "remux-2160p": 100,
    "bluray-1080p": 75,
    "webdl-1080p": 70,
    "unknown": 0,
}


def _mf(**overrides):
    """Minimal MediaFile stand-in with only the attributes scoring reads."""
    defaults = dict(
        has_swedish_sub=False,
        file_path="/movies/Film (2020)/Film.2020.mkv",
        resolution="",
        video_codec="",
        audio_codec="",
        bitrate=0,
        file_size=0,
        file_size_gb=0.0,
        added_at="2024-01-01 00:00:00",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _scoring_engine(strategy: str = "best_quality") -> DedupEngine:
    e = DedupEngine.__new__(DedupEngine)
    e.config = MagicMock()
    e.config.quality_ranks = dict(_QUALITY_RANKS)
    e.config.keep_strategy = strategy
    e._nordic_pattern = _build_nordic_pattern(["NORDIC", "SWE", "SWESUB", "SWEDISH"])
    return e


class ScoreFileTests(unittest.TestCase):
    def setUp(self):
        self.e = _scoring_engine()

    def test_confirmed_swedish_sub_bonus(self):
        self.assertEqual(self.e._score_file(_mf(has_swedish_sub=True)), 10000.0)

    def test_nordic_tag_in_filename_bonus(self):
        f = _mf(file_path="/movies/Film (2020)/Film.2020.NORDIC.mkv")
        self.assertEqual(self.e._score_file(f), 5000.0)

    def test_nordic_tag_only_counts_in_basename(self):
        """A tag in the directory name must not award the filename bonus."""
        f = _mf(file_path="/NORDIC/Film.2020.mkv")
        self.assertEqual(self.e._score_file(f), 0.0)

    def test_nordic_tag_must_be_whole_token(self):
        f = _mf(file_path="/movies/Film.NORDICUS.mkv")
        self.assertEqual(self.e._score_file(f), 0.0)

    def test_confirmed_sub_outranks_tag(self):
        plain = self.e._score_file(_mf(has_swedish_sub=True))
        tagged = self.e._score_file(
            _mf(file_path="/m/Film.2160p.NORDIC.Remux.mkv", resolution="2160",
                bitrate=999999, video_codec="HEVC", audio_codec="TrueHD Atmos")
        )
        self.assertGreater(plain, tagged)

    def test_resolution_scores(self):
        self.assertEqual(self.e._score_file(_mf(resolution="2160")), 100.0)
        self.assertEqual(self.e._score_file(_mf(resolution="1080p")), 50.0)
        self.assertEqual(self.e._score_file(_mf(resolution="gibberish")), 0.0)

    def test_quality_rank_from_source_and_resolution(self):
        f = _mf(file_path="/movies/Film.1080p.BluRay.mkv", resolution="1080p")
        # +50 resolution, +75*0.5 for bluray-1080p
        self.assertEqual(self.e._score_file(f), 87.5)

    def test_bitrate_capped_at_30(self):
        self.assertEqual(self.e._score_file(_mf(bitrate=20000)), 15.0)
        self.assertEqual(self.e._score_file(_mf(bitrate=40000)), 30.0)
        self.assertEqual(self.e._score_file(_mf(bitrate=999999)), 30.0)

    def test_video_codec_scores(self):
        self.assertEqual(self.e._score_file(_mf(video_codec="HEVC")), 10.0)
        self.assertEqual(self.e._score_file(_mf(video_codec="AV1")), 12.0)
        self.assertEqual(self.e._score_file(_mf(video_codec="x264")), 5.0)
        self.assertEqual(self.e._score_file(_mf(video_codec="MPEG2")), 0.0)

    def test_audio_chain_checks_dts_hd_before_dts(self):
        self.assertEqual(self.e._score_file(_mf(audio_codec="DTS-HD MA")), 8.0)
        self.assertEqual(self.e._score_file(_mf(audio_codec="DTS")), 5.0)
        self.assertEqual(self.e._score_file(_mf(audio_codec="TrueHD Atmos")), 10.0)
        self.assertEqual(self.e._score_file(_mf(audio_codec="EAC3")), 4.0)


class PickKeeperTests(unittest.TestCase):
    def test_best_quality_keeps_highest_score(self):
        e = _scoring_engine("best_quality")
        plain = _mf()
        tagged = _mf(file_path="/movies/Film.NORDIC.mkv")
        keep, reason = e.pick_keeper([plain, tagged])
        self.assertIs(keep, tagged)
        self.assertTrue(reason.startswith("Highest quality score"))

    def test_best_quality_tie_keeps_first(self):
        e = _scoring_engine("best_quality")
        a, b = _mf(), _mf()
        keep, _ = e.pick_keeper([a, b])
        self.assertIs(keep, a)

    def test_largest_file_strategy(self):
        e = _scoring_engine("largest_file")
        small = _mf(file_size=5)
        big = _mf(file_size=10)
        keep, _ = e.pick_keeper([small, big])
        self.assertIs(keep, big)

    def test_largest_file_tie_keeps_first(self):
        e = _scoring_engine("largest_file")
        a, b = _mf(file_size=7), _mf(file_size=7)
        keep, _ = e.pick_keeper([a, b])
        self.assertIs(keep, a)

    def test_newest_strategy(self):
        e = _scoring_engine("newest")
        old = _mf(added_at="2024-01-01 00:00:00")
        new = _mf(added_at="2025-06-15 12:00:00")
        keep, _ = e.pick_keeper([old, new])
        self.assertIs(keep, new)

    def test_unknown_strategy_falls_back_to_best_quality(self):
        e = _scoring_engine("bogus_strategy")
        plain = _mf()
        tagged = _mf(file_path="/movies/Film.SWESUB.mkv")
        keep, _ = e.pick_keeper([plain, tagged])
        self.assertIs(keep, tagged)


if __name__ == "__main__":
    unittest.main()
