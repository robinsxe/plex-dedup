"""
Tests for OpenSubtitlesClient: parent_imdb_id in episode searches, typed
search errors, 406 -> DownloadLimitReached, 401 -> re-login-and-retry,
and the missing-video-file guard in process_media_item.

Run: python -m unittest test_opensubtitles_client
"""

import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


def _install_requests_stub():
    """Minimal requests stand-in so opensubtitles_client imports without
    the real package (not installed in the dev environment)."""
    if "requests" in sys.modules:
        return
    mod = types.ModuleType("requests")

    class HTTPError(Exception):
        def __init__(self, *args, response=None):
            super().__init__(*args)
            self.response = response

    mod.HTTPError = HTTPError
    mod.Session = MagicMock
    sys.modules["requests"] = mod


_install_requests_stub()
# Another test module may have stubbed opensubtitles_client — this file
# tests the real one
sys.modules.pop("opensubtitles_client", None)

import requests  # noqa: E402
from opensubtitles_client import (  # noqa: E402
    PERMANENT_DOWNLOAD_ERRORS,
    STATUS_FAILURE,
    STATUS_TRANSIENT,
    DownloadLimitReached,
    OpenSubtitlesClient,
    SubtitleSearchError,
)


def _resp(status=200, json_data=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data if json_data is not None else {}
    if status >= 400:
        err = requests.HTTPError(f"HTTP {status}")
        err.response = r
        r.raise_for_status.side_effect = err
    else:
        r.raise_for_status.return_value = None
    return r


def _client(**kwargs):
    c = OpenSubtitlesClient("api-key", "user", "pass", **kwargs)
    c.session = MagicMock()
    c.session.headers = {}
    # Skip rate-limit sleeps in tests
    c._rate_limit = lambda: None
    return c


class SearchTests(unittest.TestCase):
    def test_episode_search_uses_parent_imdb_id(self):
        c = _client()
        c.session.get.return_value = _resp(200, {"data": []})

        c.search_subtitles(
            languages=["sv"],
            parent_imdb_id="tt0000100",
            media_type="episode",
            season_number=1,
            episode_number=2,
        )

        params = c.session.get.call_args.kwargs["params"]
        self.assertEqual(params["parent_imdb_id"], "0000100")
        self.assertNotIn("imdb_id", params)
        self.assertEqual(params["type"], "episode")
        self.assertEqual(params["season_number"], 1)
        self.assertEqual(params["episode_number"], 2)

    def test_search_http_failure_raises_typed_error(self):
        c = _client()
        c.session.get.return_value = _resp(500)

        with self.assertRaises(SubtitleSearchError):
            c.search_subtitles(languages=["sv"], imdb_id="tt1")

    def test_query_fallback_not_used_when_parent_imdb_present(self):
        c = _client()
        c.session.get.return_value = _resp(200, {"data": []})

        c.search_subtitles(
            languages=["sv"],
            parent_imdb_id="tt0000100",
            query="Some Show",
            media_type="episode",
        )

        params = c.session.get.call_args.kwargs["params"]
        self.assertNotIn("query", params)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out_path = os.path.join(tmp.name, "sub", "movie.sv.srt")

    def test_406_raises_download_limit_reached(self):
        c = _client()
        c._token = "token"
        c.session.post.return_value = _resp(406)

        with self.assertRaises(DownloadLimitReached):
            c.download_subtitle(1, self.out_path)

    def test_401_relogins_and_retries_successfully(self):
        c = _client()
        c._token = "expired"
        c.session.post.side_effect = [
            _resp(401),                                   # download w/ stale token
            _resp(200, {"token": "fresh"}),               # re-login
            _resp(200, {"link": "http://cdn/x.srt"}),     # retried download
        ]
        cdn = _resp(200)
        cdn.content = b"subtitle data"
        c.session.get.return_value = cdn

        success, error = c.download_subtitle(1, self.out_path)

        self.assertTrue(success)
        self.assertIsNone(error)
        self.assertEqual(c.session.post.call_count, 3)
        with open(self.out_path, "rb") as f:
            self.assertEqual(f.read(), b"subtitle data")

    def test_406_after_relogin_still_raises_limit(self):
        c = _client()
        c._token = "expired"
        c.session.post.side_effect = [
            _resp(401),                        # download w/ stale token
            _resp(200, {"token": "fresh"}),    # re-login
            _resp(406),                        # retried download hits quota
        ]

        with self.assertRaises(DownloadLimitReached):
            c.download_subtitle(1, self.out_path)

    def test_401_with_failed_relogin_reports_reason(self):
        c = _client()
        c._token = "expired"
        c.session.post.side_effect = [
            _resp(401),   # download w/ stale token
            _resp(401),   # re-login also rejected
        ]

        success, error = c.download_subtitle(1, self.out_path)

        self.assertFalse(success)
        self.assertEqual(error, "reauthentication_failed")
        self.assertFalse(os.path.exists(self.out_path))

    def test_non_http_error_during_relogin_retry_returns_failure(self):
        # A Timeout/OSError on the retried download must map to the
        # (success, reason) contract, not escape the method
        c = _client()
        c._token = "expired"
        c.session.post.side_effect = [
            _resp(401),                        # download w/ stale token
            _resp(200, {"token": "fresh"}),    # re-login
            OSError("network unreachable"),    # retried download blows up
        ]

        success, error = c.download_subtitle(1, self.out_path)

        self.assertFalse(success)
        self.assertIn("network unreachable", error)


class ProcessMediaItemTests(unittest.TestCase):
    def test_missing_video_file_is_reported_without_api_calls(self):
        c = _client()

        results = c.process_media_item(
            file_path="/not/mounted/movie.mkv",
            languages=["sv", "en"],
            imdb_id="tt1",
            dry_run=False,
        )

        self.assertEqual(
            {r["status"] for r in results.values()}, {"video_missing"}
        )
        c.session.get.assert_not_called()
        c.session.post.assert_not_called()

    def test_search_error_marks_language_and_continues(self):
        c = _client()
        with tempfile.NamedTemporaryFile(suffix=".mkv") as video:
            with patch.object(
                c, "search_subtitles",
                side_effect=SubtitleSearchError("HTTP 429"),
            ):
                results = c.process_media_item(
                    file_path=video.name,
                    languages=["sv"],
                    imdb_id="tt1",
                    dry_run=False,
                )

        self.assertEqual(results["sv"]["status"], "search_error")
        self.assertIn("429", results["sv"]["error"])

    def test_limit_mid_item_attaches_partial_results(self):
        c = _client()
        with tempfile.NamedTemporaryFile(suffix=".mkv") as video:
            fake_hit = {"attributes": {
                "language": "sv", "release": "X",
                "files": [{"file_id": 7}],
            }}
            with patch.object(c, "search_subtitles", return_value=[fake_hit]), \
                 patch.object(c, "find_best_subtitle", return_value=fake_hit), \
                 patch.object(c, "download_subtitle", side_effect=[
                     (True, None),
                     DownloadLimitReached("HTTP 406"),
                 ]):
                with self.assertRaises(DownloadLimitReached) as ctx:
                    c.process_media_item(
                        file_path=video.name,
                        languages=["sv", "en"],
                        imdb_id="tt1",
                        dry_run=False,
                    )

        partial = ctx.exception.partial_results
        self.assertEqual(partial["sv"]["status"], "downloaded")


class StubSyncTests(unittest.TestCase):
    def test_shared_constants_match_test_stubs(self):
        """test_stubs mirrors these for modules tested against stubs —
        drift here means queue/manager tests exercise stale values."""
        import test_stubs
        self.assertEqual(
            PERMANENT_DOWNLOAD_ERRORS, test_stubs.PERMANENT_DOWNLOAD_ERRORS
        )
        self.assertEqual(STATUS_TRANSIENT, test_stubs.STATUS_TRANSIENT)
        self.assertEqual(STATUS_FAILURE, test_stubs.STATUS_FAILURE)


if __name__ == "__main__":
    unittest.main()
