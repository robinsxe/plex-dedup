"""
Tests for tracker persistence robustness and the grab-save-failure surfacing:
 - GrabTracker writes atomically and reports save success/failure
 - a corrupt tracker file is quarantined on load, not silently emptied
 - execute_replacement flags result.error when a grab succeeds but its record
   cannot be persisted (which would otherwise cause silent re-grabs)

Run: python -m unittest test_tracker_persistence
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

import test_stubs

test_stubs.install_common_stubs()

import trackers  # noqa: E402
from library_analyzer import AnalysisResult, LibraryAnalyzer  # noqa: E402
from trackers import GrabTracker, SkipTracker  # noqa: E402


def _make_result() -> AnalysisResult:
    return AnalysisResult(
        title="Test Movie",
        display_title="Test Movie (2024)",
        year=2024,
        file_path="/fake/path.mkv",
        current_release="Test.Movie.2024.OLD",
        media_type="movie",
        has_swedish_sub=False,
        swedish_sub_available=True,
        matching_releases=[],
        has_nordic_release=True,
        recommended_release="Test.Movie.2024.1080p.BluRay.x264-NORDIC",
        indexer_results=[{
            "title": "Test.Movie.2024.1080p.BluRay.x264-NORDIC",
            "guid": "guid-1",
            "indexerId": 7,
        }],
        imdb_id="tt1234567",
        tmdb_id="98765",
        tvdb_id=None,
        show_title="",
        season_number=None,
        episode_number=None,
        episode_id=None,
        status="needs_replacement",
    )


class GrabTrackerPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "grabbed.json")

    def test_mark_grabbed_persists_and_reports_success(self):
        tracker = GrabTracker(path=self.path)
        ok = tracker.mark_grabbed(_make_result())
        self.assertTrue(ok)
        self.assertTrue(tracker.is_grabbed(imdb_id="tt1234567"))
        # A fresh tracker reading the same file must see the record.
        self.assertTrue(GrabTracker(path=self.path).is_grabbed(tmdb_id="98765"))

    def test_mark_grabbed_reports_failure_when_save_fails(self):
        tracker = GrabTracker(path=self.path)
        original = trackers.atomic_write_json
        trackers.atomic_write_json = MagicMock(side_effect=OSError("disk full"))
        try:
            self.assertFalse(tracker.mark_grabbed(_make_result()))
        finally:
            trackers.atomic_write_json = original

    def test_corrupt_file_is_quarantined_on_load(self):
        with open(self.path, "w") as f:
            f.write("{ broken json")
        tracker = GrabTracker(path=self.path)  # must not raise
        self.assertEqual(tracker.count, 0)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(
            len([n for n in os.listdir(self.dir)
                 if n.startswith("grabbed.json.corrupt-")]),
            1,
        )


class SkipTrackerPersistenceTests(unittest.TestCase):
    def test_corrupt_skip_file_quarantined_not_wiped_silently(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "skipped.json")
        with open(path, "w") as f:
            f.write("not json at all")
        tracker = SkipTracker(path=path)
        self.assertEqual(tracker.count, 0)
        self.assertEqual(
            len([n for n in os.listdir(d) if n.startswith("skipped.json.corrupt-")]),
            1,
        )


class GrabSaveSurfacingTests(unittest.TestCase):
    def _analyzer(self) -> LibraryAnalyzer:
        a = LibraryAnalyzer.__new__(LibraryAnalyzer)
        a._grab_refresh_before = False
        a._grab_with_retry = MagicMock(return_value=True)
        a.grab_tracker = MagicMock()
        return a

    def test_error_set_when_grab_record_cannot_be_saved(self):
        a = self._analyzer()
        a.grab_tracker.mark_grabbed.return_value = False
        result = _make_result()
        ok = a.execute_replacement(result, dry_run=False)
        self.assertTrue(ok)  # the grab itself succeeded
        self.assertEqual(result.status, "replaced")
        self.assertIsNotNone(result.error)
        self.assertIn("re-grabbed", result.error)

    def test_no_error_on_successful_save(self):
        a = self._analyzer()
        a.grab_tracker.mark_grabbed.return_value = True
        result = _make_result()
        ok = a.execute_replacement(result, dry_run=False)
        self.assertTrue(ok)
        self.assertEqual(result.status, "replaced")
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()
