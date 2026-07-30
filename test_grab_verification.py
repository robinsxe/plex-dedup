"""
Tests for post-grab verification: GrabTracker verification metadata and
LibraryAnalyzer.verify_grabs (confirm imports via Plex, release stalled
grabs for retry after cooldown).

Run: python -m unittest test_grab_verification
"""

import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import test_stubs

test_stubs.install_common_stubs()

from library_analyzer import LibraryAnalyzer  # noqa: E402
from trackers import GrabTracker  # noqa: E402

DAY = 86400


def _result(imdb="tt1", tmdb="100", rk="500", path="/movies/Old.Release.mkv"):
    return SimpleNamespace(
        display_title="Test Movie (2024)",
        recommended_release="Test.Movie.2024.1080p.BluRay.x264-NORDIC",
        file_path=path,
        imdb_id=imdb,
        tmdb_id=tmdb,
        rating_key=rk,
    )


class GrabTrackerVerificationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.tracker = GrabTracker(path=os.path.join(self.dir, "grabbed.json"))

    def _age(self, seconds):
        """Backdate every entry by the given number of seconds."""
        for entry in self.tracker._data.values():
            entry["grabbed_ts"] -= seconds

    def test_mark_grabbed_records_verification_metadata(self):
        self.tracker.mark_grabbed(_result())
        entry = self.tracker._data["imdb:tt1"]
        self.assertEqual(entry["file_path"], "/movies/Old.Release.mkv")
        self.assertEqual(entry["rating_key"], "500")
        self.assertFalse(entry["verified"])
        self.assertAlmostEqual(entry["grabbed_ts"], time.time(), delta=5)

    def test_pending_verification_dedupes_multi_key_entries(self):
        self.tracker.mark_grabbed(_result())
        self._age(3600)
        pending = self.tracker.pending_verification(60)
        self.assertEqual(len(pending), 1)  # one item despite 3 keys
        self.assertEqual(self.tracker.count, 3)

    def test_pending_verification_skips_young_and_legacy(self):
        # Legacy entry: pre-verification format without grabbed_ts/ids.
        self.tracker._data["imdb:legacy"] = {
            "title": "Old (2020)", "grabbed_at": "2026-01-01 00:00:00",
            "recommended_release": "Old.Release",
        }
        self.tracker.mark_grabbed(_result())  # just grabbed -> too young
        pending = self.tracker.pending_verification(3600)
        self.assertEqual(pending, [])

    def test_mark_verified_updates_all_keys(self):
        self.tracker.mark_grabbed(_result())
        self._age(3600)
        self.tracker.mark_verified(imdb_id="tt1", tmdb_id="100", rating_key="500")
        for key in ("imdb:tt1", "tmdb:100", "plex:500"):
            self.assertTrue(self.tracker._data[key]["verified"], key)
        self.assertEqual(self.tracker.pending_verification(0), [])
        # Still excluded from scans.
        self.assertTrue(self.tracker.is_grabbed(imdb_id="tt1"))

    def test_remove_deletes_all_keys_and_persists(self):
        self.tracker.mark_grabbed(_result())
        removed = self.tracker.remove(imdb_id="tt1", tmdb_id="100", rating_key="500")
        self.assertEqual(removed, 3)
        self.assertEqual(self.tracker.count, 0)
        self.assertFalse(self.tracker.is_grabbed(imdb_id="tt1"))
        # A fresh tracker must not resurrect the removed entry.
        fresh = GrabTracker(path=self.tracker._path)
        self.assertEqual(fresh.count, 0)


class VerifyGrabsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.a = LibraryAnalyzer.__new__(LibraryAnalyzer)
        self.a.grab_tracker = GrabTracker(path=os.path.join(self.dir, "grabbed.json"))
        self.a.search_cooldown = MagicMock()
        self.a.plex = MagicMock()
        self.a._verify_min_age_s = 0
        self.a._verify_deadline_s = 7 * DAY

    def _grab(self, age_seconds, **kwargs):
        self.a.grab_tracker.mark_grabbed(_result(**kwargs))
        for entry in self.a.grab_tracker._data.values():
            entry["grabbed_ts"] -= age_seconds

    def test_verified_when_swedish_sub_present(self):
        self._grab(age_seconds=3600)
        self.a.plex.get_item_media.return_value = {
            "file_paths": ["/movies/Old.Release.mkv"], "has_swedish_sub": True,
        }
        summary = self.a.verify_grabs()
        self.assertEqual(summary["verified"], 1)
        self.assertTrue(self.a.grab_tracker._data["imdb:tt1"]["verified"])
        self.a.search_cooldown.mark_searched.assert_not_called()

    def test_verified_when_old_file_replaced(self):
        self._grab(age_seconds=3600)
        self.a.plex.get_item_media.return_value = {
            "file_paths": ["/movies/New.NORDIC.Release.mkv"],
            "has_swedish_sub": False,
        }
        summary = self.a.verify_grabs()
        self.assertEqual(summary["verified"], 1)

    def test_pending_when_old_file_still_present_before_deadline(self):
        self._grab(age_seconds=1 * DAY)
        self.a.plex.get_item_media.return_value = {
            "file_paths": ["/movies/Old.Release.mkv"], "has_swedish_sub": False,
        }
        summary = self.a.verify_grabs()
        self.assertEqual(summary, {"checked": 1, "verified": 0,
                                   "released": 0, "pending": 1})
        self.assertTrue(self.a.grab_tracker.is_grabbed(imdb_id="tt1"))

    def test_released_to_cooldown_after_deadline(self):
        self._grab(age_seconds=8 * DAY)
        self.a.plex.get_item_media.return_value = {
            "file_paths": ["/movies/Old.Release.mkv"], "has_swedish_sub": False,
        }
        summary = self.a.verify_grabs()
        self.assertEqual(summary["released"], 1)
        # Entry gone -> next scan re-analyzes the item...
        self.assertFalse(self.a.grab_tracker.is_grabbed(imdb_id="tt1"))
        # ...but only after the search cooldown expires.
        self.a.search_cooldown.mark_searched.assert_called_once()
        shim = self.a.search_cooldown.mark_searched.call_args.args[0]
        self.assertEqual(shim.imdb_id, "tt1")
        self.assertEqual(shim.rating_key, "500")

    def test_plex_unreachable_leaves_entry_untouched(self):
        """A failed Plex lookup must never release a grab, even past deadline."""
        self._grab(age_seconds=30 * DAY)
        self.a.plex.get_item_media.return_value = None
        summary = self.a.verify_grabs()
        self.assertEqual(summary, {"checked": 1, "verified": 0,
                                   "released": 0, "pending": 1})
        self.assertTrue(self.a.grab_tracker.is_grabbed(imdb_id="tt1"))
        self.a.search_cooldown.mark_searched.assert_not_called()

    def test_missing_item_dropped_after_deadline_without_cooldown(self):
        """A title deleted from Plex must not stay excluded forever via its
        imdb/tmdb keys — but it must not land on cooldown either."""
        self._grab(age_seconds=8 * DAY)
        self.a.plex.get_item_media.return_value = {"missing": True}
        summary = self.a.verify_grabs()
        self.assertEqual(summary["released"], 1)
        self.assertFalse(self.a.grab_tracker.is_grabbed(imdb_id="tt1"))
        self.a.search_cooldown.mark_searched.assert_not_called()

    def test_missing_item_kept_before_deadline(self):
        self._grab(age_seconds=1 * DAY)
        self.a.plex.get_item_media.return_value = {"missing": True}
        summary = self.a.verify_grabs()
        self.assertEqual(summary, {"checked": 1, "verified": 0,
                                   "released": 0, "pending": 1})
        self.assertTrue(self.a.grab_tracker.is_grabbed(imdb_id="tt1"))

    def test_young_grabs_not_checked_at_all(self):
        self.a._verify_min_age_s = 3600
        self._grab(age_seconds=60)
        summary = self.a.verify_grabs()
        self.assertEqual(summary["checked"], 0)
        self.a.plex.get_item_media.assert_not_called()


if __name__ == "__main__":
    unittest.main()
