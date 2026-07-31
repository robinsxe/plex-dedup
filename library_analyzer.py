"""
Library analyzer engine for finding movies and episodes that need replacement
to get Swedish subtitles. Scans Plex libraries, checks OpenSubtitles for
available Swedish subtitle releases, and coordinates replacement through the
Radarr/Sonarr interactive search (indexers synced from Prowlarr).

The engine is assembled here from two mixins — AnalysisOps (scanning,
OpenSubtitles lookups) and GrabOps (indexer search, grabbing) — plus the
persistent trackers in trackers.py and the shared data model in
analysis_models.py.
"""

import logging
import os

from config import Config
from plex_client import PlexClient
from opensubtitles_client import OpenSubtitlesClient
from prowlarr_client import ProwlarrClient
from radarr_client import RadarrClient
from sonarr_client import SonarrClient

from analysis_models import (  # noqa: F401 — re-exported public data model
    AnalysisResult,
    ReleaseMatch,
    _build_nordic_pattern,
    _normalize_release_title,
)
from trackers import (  # noqa: F401 — re-exported for callers and tests
    GrabTracker,
    SearchCooldownTracker,
    SkipTracker,
)
from library_analysis import AnalysisOps
from library_grabber import GrabOps

logger = logging.getLogger(__name__)


class LibraryAnalyzer(AnalysisOps, GrabOps):
    """
    Core engine for analyzing a Plex library and finding media items
    that need replacement to obtain Swedish subtitles.
    """

    def __init__(self, config: Config):
        self.config = config
        self.plex = PlexClient(config.plex_url, config.plex_token)
        self.opensubs = OpenSubtitlesClient(
            config.opensubtitles_api_key,
            config.opensubtitles_username,
            config.opensubtitles_password,
        )
        self.prowlarr = ProwlarrClient(config.prowlarr_url, config.prowlarr_api_key)
        self.radarr = RadarrClient(config.radarr_url, config.radarr_api_key)
        self.sonarr = SonarrClient(config.sonarr_url, config.sonarr_api_key)
        self._nordic_pattern = _build_nordic_pattern(config.subtitle_match_tags)
        self.grab_tracker = GrabTracker()
        self.skip_tracker = SkipTracker()
        self.search_cooldown = SearchCooldownTracker()

        # Max release size in GB (0 = no limit)
        self._max_size_gb = float(os.environ.get("CONVERT_MAX_SIZE_GB", "25"))
        # Rejected quality keywords (case-insensitive)
        self._rejected_qualities = {"remux", "2160p", "4k", "uhd"}
        # When true, refresh the *arr release cache right before each grab.
        # Eliminates the 404 round-trip for batches where search_replacements
        # ran long enough ago that the cache is guaranteed to be cold.
        self._grab_refresh_before = os.environ.get(
            "GRAB_REFRESH_BEFORE", "0"
        ).strip().lower() in ("1", "true", "yes", "on")
        # Per-batch grab counters; reset on each execute_all invocation.
        self._grab_stats = self._empty_grab_stats()

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_summary(results: list[AnalysisResult]) -> dict:
        """Return summary statistics for a set of analysis results."""
        total = len(results)
        has_subs = sum(1 for r in results if r.status == "has_subs")
        needs_replacement = sum(1 for r in results if r.status == "needs_replacement")
        no_subs = sum(1 for r in results if r.status == "no_subs_available")
        replaced = sum(1 for r in results if r.status == "replaced")
        errors = sum(1 for r in results if r.status == "error")
        nordic = sum(1 for r in results if r.has_nordic_release)
        with_results = sum(1 for r in results if r.indexer_results)

        return {
            "total_scanned": total,
            "has_subs": has_subs,
            "needs_replacement": needs_replacement,
            "no_subs_available": no_subs,
            "replaced": replaced,
            "errors": errors,
            "nordic_available": nordic,
            "indexer_results_found": with_results,
        }
