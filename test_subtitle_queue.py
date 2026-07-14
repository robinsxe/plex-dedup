"""
Tests for SubtitleQueue: dry-run purity, daily-limit revert, transient
retry with attempt cap, exists-as-success, permanent failure reasons,
per-episode dedup keys, and crash recovery.

Run: python -m unittest test_subtitle_queue
"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import test_stubs

test_stubs.install_common_stubs()

import subtitle_queue  # noqa: E402
from opensubtitles_client import DownloadLimitReached  # noqa: E402
from subtitle_queue import MAX_ATTEMPTS, SubtitleQueue, scheduler_due  # noqa: E402


class FakeConfig:
    subtitle_daily_limit = 20
    subtitle_languages = ["sv", "en"]
    subtitle_queue_hour = 4
    opensubtitles_api_key = "key"
    opensubtitles_username = "user"
    opensubtitles_password = "pass"
    plex_url = "http://plex"
    plex_token = "token"
    plex_movie_library = "Movies"
    plex_tv_library = "TV Shows"


def _movie(title="Movie A", imdb="tt0000001", path=None):
    return {
        "file_path": path or f"/media/{title}.mkv",
        "media_type": "movie",
        "title": title,
        "year": 2024,
        "imdb_id": imdb,
        "tmdb_id": None,
    }


def _episode(show_imdb="tt0000100", season=1, episode=1):
    return {
        "file_path": f"/media/show/s{season:02d}e{episode:02d}.mkv",
        "media_type": "episode",
        "title": f"Ep {episode}",
        "imdb_id": None,
        "tmdb_id": None,
        "show_imdb_id": show_imdb,
        "season_number": season,
        "episode_number": episode,
        "show_title": "Test Show",
    }


class SubtitleQueueTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.queue_path = os.path.join(tmp.name, "sub_queue.json")
        orig = subtitle_queue.QUEUE_FILE
        subtitle_queue.QUEUE_FILE = self.queue_path
        self.addCleanup(setattr, subtitle_queue, "QUEUE_FILE", orig)
        self.queue = SubtitleQueue(FakeConfig())

    def _patch_client(self, process_side_effect):
        """Replace OpenSubtitlesClient so process() hits no network."""
        client = MagicMock()
        client.process_media_item.side_effect = process_side_effect
        patcher = patch.object(
            subtitle_queue, "OpenSubtitlesClient", return_value=client
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def _statuses(self):
        return [i["status"] for i in self.queue._data]


class DryRunTests(SubtitleQueueTestCase):
    def test_dry_run_reports_without_mutating(self):
        self.queue.add([_movie("A", "tt1"), _movie("B", "tt2")])
        client = self._patch_client(AssertionError("must not be called"))

        result = self.queue.process(dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["would_process"], 2)
        self.assertEqual(result["downloaded"], 0)
        self.assertEqual(self._statuses(), ["pending", "pending"])
        client.process_media_item.assert_not_called()
        # Persisted file must still hold pending items
        with open(self.queue_path) as f:
            saved = json.load(f)
        self.assertEqual(
            [i["status"] for i in saved["queue"]], ["pending", "pending"]
        )

    def test_dry_run_count_respects_limit(self):
        self.queue.add([_movie(f"M{i}", f"tt{i}") for i in range(5)])
        self._patch_client(AssertionError("must not be called"))

        result = self.queue.process(limit=3, dry_run=True)

        self.assertEqual(result["would_process"], 3)
        self.assertEqual(result["remaining"], 5)
        self.assertEqual(self._statuses(), ["pending"] * 5)


class LimitReachedTests(SubtitleQueueTestCase):
    def test_limit_mid_batch_reverts_remaining_to_pending(self):
        self.queue.add(
            [_movie("A", "tt1"), _movie("B", "tt2"), _movie("C", "tt3")]
        )
        self._patch_client([
            {"sv": {"status": "downloaded", "path": "/a.sv.srt"}},
            DownloadLimitReached("daily download limit reached (HTTP 406)"),
        ])

        result = self.queue.process()

        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertTrue(result["limit_reached"])
        # _cleanup_history reorders (active first) — compare by title
        by_title = {i["title"]: i["status"] for i in self.queue._data}
        self.assertEqual(
            by_title, {"A": "downloaded", "B": "pending", "C": "pending"}
        )


class TransientFailureTests(SubtitleQueueTestCase):
    def test_download_failure_retries_until_attempt_cap(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(
            lambda **kw: {"sv": {"status": "download_failed", "error": "http_500"}}
        )

        for attempt in range(1, MAX_ATTEMPTS):
            result = self.queue.process()
            self.assertEqual(result["will_retry"], 1, f"attempt {attempt}")
            self.assertEqual(self._statuses(), ["pending"])
            self.assertEqual(self.queue._data[0]["attempts"], attempt)

        result = self.queue.process()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self._statuses(), ["failed"])
        self.assertEqual(self.queue._data[0]["attempts"], MAX_ATTEMPTS)

        # Failed items must not be picked up again
        result = self.queue.process()
        self.assertEqual(result["downloaded"], 0)
        self.assertEqual(result["remaining"], 0)

    def test_search_error_is_transient(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(
            lambda **kw: {"sv": {"status": "search_error", "error": "HTTP 429"}}
        )

        result = self.queue.process()

        self.assertEqual(result["will_retry"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self._statuses(), ["pending"])
        self.assertIn("429", self.queue._data[0]["error"])

    def test_processing_exception_is_transient(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(OSError("unreadable file"))

        result = self.queue.process()

        self.assertEqual(result["will_retry"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self._statuses(), ["pending"])
        self.assertEqual(self.queue._data[0]["attempts"], 1)

    def test_permanent_download_error_fails_without_retry(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(lambda **kw: {
            "sv": {"status": "download_failed", "error": "reauthentication_failed"}
        })

        result = self.queue.process()

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["will_retry"], 0)
        self.assertEqual(self._statuses(), ["failed"])
        self.assertIn("reauthentication_failed", self.queue._data[0]["error"])


class OutcomeClassificationTests(SubtitleQueueTestCase):
    def test_already_existing_subtitles_count_as_success(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(lambda **kw: {
            "sv": {"status": "exists", "path": "/a.sv.srt"},
            "en": {"status": "exists", "path": "/a.en.srt"},
        })
        plex = MagicMock()
        with patch.object(subtitle_queue, "PlexClient", return_value=plex):
            result = self.queue.process()

        self.assertEqual(result["already_had"], 1)
        self.assertEqual(result["downloaded"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self._statuses(), ["downloaded"])
        # Nothing was written — no Plex refresh needed
        plex.refresh_library.assert_not_called()

    def test_mixed_download_and_transient_failure_retries(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(lambda **kw: {
            "sv": {"status": "downloaded", "path": "/a.sv.srt"},
            "en": {"status": "download_failed", "error": "http_500"},
        })
        plex = MagicMock()
        with patch.object(subtitle_queue, "PlexClient", return_value=plex):
            result = self.queue.process()

        # The failed language must be retried, but the sv file that WAS
        # written still triggers a Plex refresh
        self.assertEqual(result["will_retry"], 1)
        self.assertEqual(result["downloaded"], 0)
        self.assertEqual(result["files_downloaded"], 1)
        self.assertEqual(self._statuses(), ["pending"])
        plex.refresh_library.assert_called_once_with("Movies")

    def test_not_found_is_permanent_failure_with_reason(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(lambda **kw: {"sv": {"status": "not_found"}})

        result = self.queue.process()

        self.assertEqual(result["failed"], 1)
        item = self.queue._data[0]
        self.assertEqual(item["status"], "failed")
        self.assertTrue(item["error"])

    def test_video_missing_is_permanent_failure(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(lambda **kw: {
            "sv": {"status": "video_missing", "error": "video file not found: /x"},
            "en": {"status": "video_missing", "error": "video file not found: /x"},
        })

        result = self.queue.process()

        self.assertEqual(result["failed"], 1)
        self.assertIn("video file not found", self.queue._data[0]["error"])

    def test_episode_processing_passes_parent_imdb_id(self):
        self.queue.add([_episode(show_imdb="tt0000100", season=1, episode=2)])
        client = self._patch_client(
            lambda **kw: {"sv": {"status": "downloaded", "path": "/e.sv.srt"}}
        )

        self.queue.process()

        kwargs = client.process_media_item.call_args.kwargs
        self.assertEqual(kwargs["parent_imdb_id"], "tt0000100")
        self.assertIsNone(kwargs["imdb_id"])


class DedupTests(SubtitleQueueTestCase):
    def test_episodes_of_same_show_get_separate_entries(self):
        result = self.queue.add([
            _episode(season=1, episode=1),
            _episode(season=1, episode=2),
            _episode(season=2, episode=1),
        ])
        self.assertEqual(result["added"], 3)
        self.assertEqual(result["skipped"], 0)

    def test_duplicate_episode_is_skipped(self):
        self.queue.add([_episode(season=1, episode=1)])
        result = self.queue.add([_episode(season=1, episode=1)])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_duplicate_movie_is_skipped(self):
        self.queue.add([_movie("A", "tt1")])
        result = self.queue.add([_movie("A", "tt1", path="/other/copy.mkv")])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped"], 1)

    def test_failed_item_not_readded_until_cleared(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(lambda **kw: {"sv": {"status": "not_found"}})
        self.queue.process()
        self.assertEqual(self._statuses(), ["failed"])

        result = self.queue.add([_movie("A", "tt1")])
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skipped"], 1)

        # Clearing failed items allows a deliberate re-add
        self.queue.clear(status="failed")
        result = self.queue.add([_movie("A", "tt1")])
        self.assertEqual(result["added"], 1)


class StatusTests(SubtitleQueueTestCase):
    def test_failed_items_exposed_with_reasons(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(lambda **kw: {"sv": {"status": "not_found"}})
        self.queue.process()

        status = self.queue.get_status()

        self.assertEqual(status["failed"], 1)
        self.assertEqual(len(status["failed_items"]), 1)
        self.assertTrue(status["failed_items"][0]["error"])


class PlexRefreshTests(SubtitleQueueTestCase):
    def test_successful_batch_refreshes_movie_library(self):
        self.queue.add([_movie("A", "tt1")])
        self._patch_client(
            lambda **kw: {"sv": {"status": "downloaded", "path": "/a.sv.srt"}}
        )
        plex = MagicMock()
        with patch.object(subtitle_queue, "PlexClient", return_value=plex):
            self.queue.process()
        plex.refresh_library.assert_called_once_with("Movies")


class SchedulerDueTests(unittest.TestCase):
    def _at(self, stamp: str) -> time.struct_time:
        return time.strptime(stamp, "%Y-%m-%d %H:%M")

    def test_due_inside_target_hour(self):
        self.assertTrue(scheduler_due(self._at("2026-07-14 04:23"), 4, None))

    def test_due_at_exact_top_of_hour(self):
        # The old sleep-math scheduler skipped a whole day in this case
        self.assertTrue(scheduler_due(self._at("2026-07-14 04:00"), 4, None))

    def test_not_due_outside_target_hour(self):
        self.assertFalse(scheduler_due(self._at("2026-07-14 05:00"), 4, None))

    def test_not_due_twice_same_day(self):
        self.assertFalse(
            scheduler_due(self._at("2026-07-14 04:45"), 4, "2026-07-14")
        )

    def test_due_again_next_day(self):
        self.assertTrue(
            scheduler_due(self._at("2026-07-15 04:01"), 4, "2026-07-14")
        )


class CrashRecoveryTests(SubtitleQueueTestCase):
    def test_legacy_episode_entry_migrated_on_load(self):
        legacy = {
            "file_path": "/media/show/s01e01.mkv", "media_type": "episode",
            "title": "Ep 1", "imdb_id": "tt0000100", "season_number": 1,
            "episode_number": 1, "show_title": "Show", "status": "pending",
        }
        with open(self.queue_path, "w") as f:
            json.dump({"queue": [legacy]}, f)

        migrated = SubtitleQueue(FakeConfig())

        item = migrated._data[0]
        self.assertIsNone(item["imdb_id"])
        self.assertEqual(item["show_imdb_id"], "tt0000100")

    def test_processing_items_reset_to_pending_on_load(self):
        item = _movie("A", "tt1")
        item["status"] = "processing"
        with open(self.queue_path, "w") as f:
            json.dump({"queue": [item]}, f)

        recovered = SubtitleQueue(FakeConfig())

        self.assertEqual(recovered._data[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
