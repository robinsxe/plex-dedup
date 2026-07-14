"""
Tests for LibraryAnalyzer.execute_replacement stale-cache recovery,
GRAB_REFRESH_BEFORE proactive refresh, batch telemetry counters,
skip-tracker integration on unrecoverable failure, and Sonarr TV refresh.

Run: python -m unittest test_library_analyzer
"""

import unittest
from unittest.mock import MagicMock

import test_stubs

test_stubs.install_common_stubs()

from arr_common import StaleReleaseError  # noqa: E402
from library_analyzer import (  # noqa: E402
    AnalysisResult,
    LibraryAnalyzer,
    _normalize_release_title,
)


def _make_result(media_type: str = "movie") -> AnalysisResult:
    return AnalysisResult(
        title="Test Movie" if media_type == "movie" else "Test Show",
        display_title=(
            "Test Movie (2024)" if media_type == "movie"
            else "Test Show - S01E02"
        ),
        year=2024,
        file_path="/fake/path.mkv",
        current_release="Test.Movie.2024.OLD",
        media_type=media_type,
        has_swedish_sub=False,
        swedish_sub_available=True,
        matching_releases=[],
        has_nordic_release=True,
        recommended_release="Test.Movie.2024.1080p.BluRay.x264-NORDIC",
        prowlarr_results=[],
        imdb_id="tt1234567",
        tmdb_id="98765",
        tvdb_id="55555",
        show_title="Test Show",
        season_number=1,
        episode_number=2,
        episode_id=314 if media_type == "episode" else None,
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
    a = LibraryAnalyzer.__new__(LibraryAnalyzer)
    a.radarr = MagicMock()
    a.sonarr = MagicMock()
    a.grab_tracker = MagicMock()
    a.skip_tracker = MagicMock()
    a._grab_stats = LibraryAnalyzer._empty_grab_stats()
    a._grab_refresh_before = False
    return a


class FindSwedishReleasesTests(unittest.TestCase):
    def _analyzer(self) -> LibraryAnalyzer:
        a = LibraryAnalyzer.__new__(LibraryAnalyzer)
        a.opensubs = MagicMock()
        return a

    def test_search_error_propagates_instead_of_no_subs(self):
        from opensubtitles_client import SubtitleSearchError
        a = self._analyzer()
        a.opensubs.search_subtitles.side_effect = SubtitleSearchError("HTTP 429")

        with self.assertRaises(SubtitleSearchError):
            a._find_swedish_releases(
                {"media_type": "movie", "imdb_id": "tt1", "title": "X"}
            )

    def test_generic_error_propagates_to_analyze_handler(self):
        a = self._analyzer()
        a.opensubs.search_subtitles.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            a._find_swedish_releases(
                {"media_type": "movie", "imdb_id": "tt1", "title": "X"}
            )

    def test_episode_search_uses_parent_imdb_id_not_query(self):
        a = self._analyzer()
        a.opensubs.search_subtitles.return_value = []

        a._find_swedish_releases({
            "media_type": "episode",
            "show_imdb_id": "tt0000100",
            "season_number": 1,
            "episode_number": 2,
            "title": "Ep",
            "show_title": "Test Show",
        })

        kwargs = a.opensubs.search_subtitles.call_args.kwargs
        self.assertEqual(kwargs["parent_imdb_id"], "tt0000100")
        self.assertNotIn("imdb_id", kwargs)
        self.assertNotIn("query", kwargs)
        self.assertEqual(kwargs["season_number"], 1)
        self.assertEqual(kwargs["episode_number"], 2)


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
    def test_happy_path_no_retry_increments_direct(self):
        a = _make_analyzer()
        result = _make_result()
        rel = _make_release()
        a.radarr.grab_release.return_value = True

        ok = a._grab_with_retry(result, rel)

        self.assertTrue(ok)
        a.radarr.grab_release.assert_called_once_with("guid-original", 7)
        a.radarr.search_releases.assert_not_called()
        a.radarr.push_release.assert_not_called()
        self.assertEqual(a._grab_stats["grabbed_direct"], 1)
        self.assertEqual(a._grab_stats["stale_recovered"], 0)

    def test_stale_then_refresh_then_retry_increments_recovered(self):
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
        self.assertEqual(a._grab_stats["stale_recovered"], 1)
        self.assertEqual(a._grab_stats["grabbed_direct"], 0)
        a.radarr.push_release.assert_not_called()

    def test_stale_refresh_no_match_falls_back_to_push(self):
        a = _make_analyzer()
        result = _make_result()
        original = _make_release()
        a.radarr.grab_release.side_effect = StaleReleaseError("cache evicted")
        a.radarr.find_movie.return_value = {"id": 42}
        a.radarr.search_releases.return_value = [
            _make_release(title="Other.Movie.2024-GROUPX", guid="guid-other")
        ]
        a.radarr.push_release.return_value = True

        ok = a._grab_with_retry(result, original)

        self.assertTrue(ok)
        self.assertEqual(a._grab_stats["push_fallback_used"], 1)
        a.skip_tracker.mark_skipped.assert_not_called()
        a.radarr.push_release.assert_called_once()

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
        self.assertEqual(a._grab_stats["push_fallback_used"], 1)

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
        self.assertEqual(a._grab_stats["push_fallback_used"], 1)

    def test_unrecoverable_marks_skip_and_counter(self):
        a = _make_analyzer()
        result = _make_result()
        original = _make_release(downloadUrl=None, protocol=None)
        a.radarr.grab_release.side_effect = StaleReleaseError("cache evicted")
        a.radarr.find_movie.return_value = None  # refresh fails

        ok = a._grab_with_retry(result, original)

        self.assertFalse(ok)
        a.radarr.push_release.assert_not_called()
        a.skip_tracker.mark_skipped.assert_called_once()
        kwargs = a.skip_tracker.mark_skipped.call_args.kwargs
        self.assertEqual(kwargs.get("reason"), "stale_grab_unrecoverable")
        # Skip writes must be deferred so we don't disk-write per failure.
        self.assertTrue(kwargs.get("defer_save"))
        self.assertEqual(a._grab_stats["failed_unrecoverable"], 1)

    def test_tv_stale_recovers_via_sonarr_refresh(self):
        a = _make_analyzer()
        result = _make_result(media_type="episode")
        original = _make_release(guid="sonarr-original", indexerId=11)
        refreshed = _make_release(guid="sonarr-fresh", indexerId=12)
        a.sonarr.grab_release.side_effect = [
            StaleReleaseError("cache evicted"),
            True,
        ]
        a.sonarr.search_releases.return_value = [refreshed]

        ok = a._grab_with_retry(result, original)

        self.assertTrue(ok)
        a.sonarr.search_releases.assert_called_once_with(314)
        self.assertEqual(a._grab_stats["stale_recovered"], 1)

    def test_tv_without_episode_id_goes_to_push(self):
        a = _make_analyzer()
        result = _make_result(media_type="episode")
        result.episode_id = None
        original = _make_release()
        a.sonarr.grab_release.side_effect = StaleReleaseError("cache evicted")
        a.sonarr.push_release.return_value = True

        ok = a._grab_with_retry(result, original)

        self.assertTrue(ok)
        a.sonarr.search_releases.assert_not_called()
        self.assertEqual(a._grab_stats["push_fallback_used"], 1)


class RefreshedBestResultTests(unittest.TestCase):
    def test_returns_new_dict_with_fresh_guid_on_match(self):
        a = _make_analyzer()
        result = _make_result()
        best = _make_release()
        fresh = _make_release(guid="guid-fresh", indexerId=99)
        a.radarr.find_movie.return_value = {"id": 42}
        a.radarr.search_releases.return_value = [fresh]

        out = a._refreshed_best_result(result, best)

        self.assertEqual(out["guid"], "guid-fresh")
        self.assertEqual(out["indexerId"], 99)
        # input dict must not be mutated
        self.assertEqual(best["guid"], "guid-original")
        self.assertEqual(best["indexerId"], 7)
        self.assertIsNot(out, best)

    def test_returns_same_dict_when_no_match(self):
        a = _make_analyzer()
        result = _make_result()
        best = _make_release()
        a.radarr.find_movie.return_value = {"id": 42}
        a.radarr.search_releases.return_value = [
            _make_release(title="Different.Release", guid="other")
        ]

        out = a._refreshed_best_result(result, best)

        self.assertIs(out, best)
        self.assertEqual(best["guid"], "guid-original")


class ResetGrabStatsTests(unittest.TestCase):
    def test_reset_zeroes_all_counters(self):
        a = _make_analyzer()
        a._grab_stats["grabbed_direct"] = 12
        a._grab_stats["stale_recovered"] = 5

        a.reset_grab_stats()

        self.assertEqual(
            a._grab_stats,
            {
                "grabbed_direct": 0,
                "stale_recovered": 0,
                "push_fallback_used": 0,
                "failed_unrecoverable": 0,
            },
        )


class ResolveSonarrEpisodeIdTests(unittest.TestCase):
    def test_resolves_via_tvdb(self):
        a = _make_analyzer()
        result = _make_result(media_type="episode")
        result.episode_id = None
        a.sonarr.find_series.return_value = {"id": 77}
        a.sonarr.find_episode.return_value = {"id": 4242}

        ep_id = a._resolve_sonarr_episode_id(result)

        self.assertEqual(ep_id, 4242)
        a.sonarr.find_series.assert_called_once()
        a.sonarr.find_episode.assert_called_once_with(77, 1, 2)

    def test_returns_none_without_season(self):
        a = _make_analyzer()
        result = _make_result(media_type="episode")
        result.season_number = None

        ep_id = a._resolve_sonarr_episode_id(result)

        self.assertIsNone(ep_id)
        a.sonarr.find_series.assert_not_called()

    def test_returns_none_when_series_missing(self):
        a = _make_analyzer()
        result = _make_result(media_type="episode")
        a.sonarr.find_series.return_value = None

        ep_id = a._resolve_sonarr_episode_id(result)

        self.assertIsNone(ep_id)


if __name__ == "__main__":
    unittest.main()
