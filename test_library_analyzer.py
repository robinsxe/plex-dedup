"""
Tests for LibraryAnalyzer.execute_replacement stale-cache recovery.

Run: python -m unittest test_library_analyzer
"""

import sys
import types
import unittest
from unittest.mock import MagicMock


def _install_stub(module_name: str, **attrs):
    """Register a minimal stub module so library_analyzer can import it
    without pulling in real deps (dotenv, requests, etc)."""
    if module_name in sys.modules:
        return
    mod = types.ModuleType(module_name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[module_name] = mod


# Stub heavy/external deps before importing library_analyzer.
_install_stub("config", Config=type("Config", (), {}))
_install_stub("plex_client", PlexClient=MagicMock)
_install_stub("opensubtitles_client", OpenSubtitlesClient=MagicMock)
_install_stub("prowlarr_client", ProwlarrClient=MagicMock)
_install_stub("radarr_client", RadarrClient=MagicMock)
_install_stub("sonarr_client", SonarrClient=MagicMock)

# arr_common is local, has no external deps — import the real one.
from arr_common import StaleReleaseError  # noqa: E402
from library_analyzer import (  # noqa: E402
    AnalysisResult,
    LibraryAnalyzer,
    _normalize_release_title,
)


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
        prowlarr_results=[],
        imdb_id="tt1234567",
        tmdb_id="98765",
        status="needs_replacement",
    )


def _make_release(**overrides) -> dict:
    base = {
        "title": "Test.Movie.2024.1080p.BluRay.x264-NORDIC",
        "guid": "guid-original",
        "indexerId": 7,
        "downloadUrl": "https://indexer.example/release.torrent",
        "protocol": "torrent",
        "publishDate": "2024-01-01T00:00:00Z",
        "indexer": "MyIndexer",
        "size": 5 * 1024**3,
    }
    base.update(overrides)
    return base


def _make_analyzer() -> LibraryAnalyzer:
    """Bypass __init__ so we don't need a real Config."""
    a = LibraryAnalyzer.__new__(LibraryAnalyzer)
    a.radarr = MagicMock()
    a.sonarr = MagicMock()
    a.grab_tracker = MagicMock()
    return a


class NormalizeTitleTests(unittest.TestCase):
    def test_lowercases_and_collapses_whitespace(self):
        self.assertEqual(
            _normalize_release_title("  Test.Movie  2024 \tBluRay  "),
            "test.movie 2024 bluray",
        )

    def test_preserves_group_suffix(self):
        self.assertNotEqual(
            _normalize_release_title("Foo-GROUPA"),
            _normalize_release_title("Foo-GROUPB"),
        )


class GrabWithRetryTests(unittest.TestCase):
    def test_happy_path_no_retry(self):
        a = _make_analyzer()
        result = _make_result()
        rel = _make_release()
        a.radarr.grab_release.return_value = True

        ok = a._grab_with_retry(result, rel)

        self.assertTrue(ok)
        a.radarr.grab_release.assert_called_once_with("guid-original", 7)
        a.radarr.search_releases.assert_not_called()
        a.radarr.push_release.assert_not_called()

    def test_stale_then_refresh_then_retry_succeeds(self):
        a = _make_analyzer()
        result = _make_result()
        original = _make_release()
        refreshed = _make_release(
            guid="guid-fresh", indexerId=9, title="test.movie.2024.1080p.bluray.x264-NORDIC"
        )
        a.radarr.grab_release.side_effect = [
            StaleReleaseError("cache evicted"),
            True,
        ]
        a.radarr.find_movie.return_value = {"id": 42}
        a.radarr.search_releases.return_value = [refreshed]

        ok = a._grab_with_retry(result, original)

        self.assertTrue(ok)
        self.assertEqual(a.radarr.grab_release.call_count, 2)
        a.radarr.grab_release.assert_any_call("guid-original", 7)
        a.radarr.grab_release.assert_any_call("guid-fresh", 9)
        a.radarr.push_release.assert_not_called()

    def test_stale_refresh_no_match_falls_back_to_push(self):
        a = _make_analyzer()
        result = _make_result()
        original = _make_release()
        a.radarr.grab_release.side_effect = StaleReleaseError("cache evicted")
        a.radarr.find_movie.return_value = {"id": 42}
        # Refresh returns a *different* release — must not be substituted.
        a.radarr.search_releases.return_value = [
            _make_release(title="Other.Movie.2024-GROUPX", guid="guid-other")
        ]
        a.radarr.push_release.return_value = True

        ok = a._grab_with_retry(result, original)

        self.assertTrue(ok)
        a.radarr.push_release.assert_called_once()
        kwargs = a.radarr.push_release.call_args.kwargs
        self.assertEqual(kwargs["download_url"], original["downloadUrl"])
        self.assertEqual(kwargs["protocol"], "torrent")
        self.assertEqual(kwargs["indexer"], "MyIndexer")

    def test_stale_refresh_skipped_when_no_ids(self):
        a = _make_analyzer()
        result = _make_result()
        result.tmdb_id = None
        result.imdb_id = None
        original = _make_release()
        a.radarr.grab_release.side_effect = StaleReleaseError("cache evicted")
        a.radarr.push_release.return_value = True

        ok = a._grab_with_retry(result, original)

        self.assertTrue(ok)
        a.radarr.find_movie.assert_not_called()
        a.radarr.search_releases.assert_not_called()
        a.radarr.push_release.assert_called_once()

    def test_retry_raises_non_stale_falls_back_to_push(self):
        a = _make_analyzer()
        result = _make_result()
        original = _make_release()
        a.radarr.grab_release.side_effect = [
            StaleReleaseError("cache evicted"),
            RuntimeError("network glitch on retry"),
        ]
        a.radarr.find_movie.return_value = {"id": 42}
        a.radarr.search_releases.return_value = [_make_release(guid="guid-fresh")]
        a.radarr.push_release.return_value = True

        ok = a._grab_with_retry(result, original)

        self.assertTrue(ok)
        a.radarr.push_release.assert_called_once()

    def test_push_fallback_skipped_when_fields_missing(self):
        a = _make_analyzer()
        result = _make_result()
        original = _make_release(downloadUrl=None, protocol=None)
        a.radarr.grab_release.side_effect = StaleReleaseError("cache evicted")
        a.radarr.find_movie.return_value = None  # forces straight to push

        ok = a._grab_with_retry(result, original)

        self.assertFalse(ok)
        a.radarr.push_release.assert_not_called()

    def test_tv_path_skips_refresh_goes_to_push(self):
        a = _make_analyzer()
        result = _make_result()
        result.media_type = "episode"
        original = _make_release()
        a.sonarr.grab_release.side_effect = StaleReleaseError("cache evicted")
        a.sonarr.push_release.return_value = True

        ok = a._grab_with_retry(result, original)

        self.assertTrue(ok)
        a.radarr.find_movie.assert_not_called()
        a.radarr.search_releases.assert_not_called()
        a.sonarr.push_release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
