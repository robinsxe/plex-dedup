"""
Tests for SubtitleManager: DownloadLimitReached stops the batch while
keeping partial results, and get_summary counts failure statuses.

Run: python -m unittest test_subtitle_manager
"""

import unittest
from unittest.mock import MagicMock

import test_stubs

test_stubs.install_common_stubs()

from opensubtitles_client import DownloadLimitReached  # noqa: E402
from subtitle_manager import SubtitleManager, SubtitleResult  # noqa: E402


class FakeConfig:
    subtitle_languages = ["sv", "en"]
    dry_run = False
    opensubtitles_api_key = "key"
    opensubtitles_username = "user"
    opensubtitles_password = "pass"
    plex_url = "http://plex"
    plex_token = "token"
    plex_movie_library = "Movies"
    plex_tv_library = "TV Shows"


def _manager() -> SubtitleManager:
    mgr = SubtitleManager(FakeConfig())
    mgr.opensubs = MagicMock()
    mgr.plex = MagicMock()
    return mgr


class DownloadForItemsTests(unittest.TestCase):
    def test_limit_reached_stops_batch_and_keeps_partial_results(self):
        mgr = _manager()
        mgr.opensubs.process_media_item.side_effect = [
            {"sv": {"status": "downloaded", "path": "/a.sv.srt"}},
            DownloadLimitReached("daily download limit reached (HTTP 406)"),
        ]
        items = [
            {"file_path": "/a.mkv", "media_type": "movie", "title": "A"},
            {"file_path": "/b.mkv", "media_type": "movie", "title": "B"},
            {"file_path": "/c.mkv", "media_type": "movie", "title": "C"},
        ]

        results = mgr.download_for_items(items, dry_run=False)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].any_downloaded)
        self.assertEqual(mgr.opensubs.process_media_item.call_count, 2)
        mgr.plex.refresh_library.assert_called_once_with("Movies")

    def test_limit_with_partial_results_keeps_downloaded_language(self):
        mgr = _manager()
        err = DownloadLimitReached("daily download limit reached (HTTP 406)")
        err.partial_results = {"sv": {"status": "downloaded", "path": "/b.sv.srt"}}
        mgr.opensubs.process_media_item.side_effect = [err]

        results = mgr.download_for_items(
            [{"file_path": "/b.mkv", "media_type": "movie", "title": "B"}],
            dry_run=False,
        )

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].any_downloaded)
        mgr.plex.refresh_library.assert_called_once_with("Movies")


class GetSummaryTests(unittest.TestCase):
    def test_failure_statuses_are_counted(self):
        mgr = _manager()
        results = [
            SubtitleResult(
                title="A", display_title="A (2024)", file_path="/a.mkv",
                media_type="movie",
                languages={
                    "sv": {"status": "download_failed", "error": "http_500"},
                    "en": {"status": "not_found"},
                },
            ),
            SubtitleResult(
                title="B", display_title="B (2024)", file_path="/b.mkv",
                media_type="movie",
                languages={
                    "sv": {"status": "video_missing", "error": "gone"},
                    "en": {"status": "exists", "path": "/b.en.srt"},
                },
            ),
        ]

        summary = mgr.get_summary(results)

        self.assertEqual(summary["subtitles_failed"], 2)
        self.assertEqual(summary["subtitles_not_available"], 1)
        self.assertEqual(summary["subtitles_already_exist"], 1)
        self.assertEqual(summary["subtitles_downloaded"], 0)


if __name__ == "__main__":
    unittest.main()
