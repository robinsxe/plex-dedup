"""
Analysis half of LibraryAnalyzer: scanning a Plex library, querying
OpenSubtitles for Swedish-sub releases, and picking the recommended
replacement. Mixed into LibraryAnalyzer in library_analyzer.py, which
provides the clients, trackers, and _nordic_pattern this code reads
off ``self``.
"""

import logging
import os

from analysis_models import AnalysisResult, ReleaseMatch

logger = logging.getLogger(__name__)


class AnalysisOps:

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def parse_release_name(file_path: str) -> str:
        """
        Extract the release name from a file path by stripping directory
        and extension.

        Example:
            /data/movies/War.Machine.2026.1080p.WEB-DL.AAC5.1.AV1-LuCY.mkv
            -> War.Machine.2026.1080p.WEB-DL.AAC5.1.AV1-LuCY
        """
        basename = os.path.basename(file_path)
        name, _ = os.path.splitext(basename)
        return name

    def _is_nordic_release(self, release_name: str) -> bool:
        """
        Check if a release name contains any of the configured subtitle
        match tags as whole tokens (case-insensitive).
        """
        return bool(self._nordic_pattern.search(release_name))

    def _build_display_title(self, item: dict) -> str:
        """Build a human-readable display title from a media item dict."""
        if item.get("media_type") == "episode" and item.get("show_title"):
            season = item.get("season_number") or 0
            episode = item.get("episode_number") or 0
            return (
                f"{item['show_title']} - S{season:02d}E{episode:02d}"
                f" - {item['title']}"
            )
        year = item.get("year")
        if year:
            return f"{item['title']} ({year})"
        return item["title"]

    # ------------------------------------------------------------------ #
    # OpenSubtitles queries
    # ------------------------------------------------------------------ #

    def _find_swedish_releases(self, item: dict) -> list[ReleaseMatch]:
        """
        Query OpenSubtitles for a media item and find which releases have
        Swedish subtitles.

        Search priority: IMDB ID -> TMDB ID -> title query.
        Returns list of ReleaseMatch sorted by download_count descending.
        """
        search_kwargs: dict = {
            "languages": ["sv"],
            "media_type": item.get("media_type", "movie"),
        }

        if item.get("media_type") == "episode":
            search_kwargs["season_number"] = item.get("season_number")
            search_kwargs["episode_number"] = item.get("episode_number")
            # The API expects the show's id as parent_imdb_id for episode
            # searches — passing it as imdb_id returns zero matches
            if item.get("show_imdb_id"):
                search_kwargs["parent_imdb_id"] = item["show_imdb_id"]

        if item.get("imdb_id"):
            search_kwargs["imdb_id"] = item["imdb_id"]
        if item.get("tmdb_id"):
            search_kwargs["tmdb_id"] = item["tmdb_id"]
        if not (item.get("imdb_id") or item.get("tmdb_id") or item.get("show_imdb_id")):
            query = item.get("show_title") or item.get("title", "")
            search_kwargs["query"] = query

        # Failures propagate (transient ones as SubtitleSearchError) so
        # analyze() marks the item "error" instead of "no subs available" —
        # which would put it on a 30-day cooldown
        results = self.opensubs.search_subtitles(**search_kwargs)

        # Filter to Swedish results and build ReleaseMatch objects
        matches: list[ReleaseMatch] = []
        seen_releases: set[str] = set()

        for result in results:
            attrs = result.get("attributes", {})
            language = attrs.get("language", "")
            if language != "sv":
                continue

            release_name = attrs.get("release", "") or ""
            if not release_name or release_name in seen_releases:
                continue
            seen_releases.add(release_name)

            matches.append(ReleaseMatch(
                release_name=release_name,
                language="sv",
                download_count=attrs.get("download_count", 0),
                from_trusted=attrs.get("from_trusted", False),
                hearing_impaired=attrs.get("hearing_impaired", False),
            ))

        matches.sort(key=lambda m: m.download_count, reverse=True)
        return matches

    # ------------------------------------------------------------------ #
    # Replacement selection
    # ------------------------------------------------------------------ #

    def _find_best_replacement(
        self, current_release: str, matches: list[ReleaseMatch]
    ) -> str | None:
        """
        Pick the best release to replace the current one.

        Priority:
            1. NORDIC/SWE releases (likely have embedded Swedish subs)
            2. Most downloaded Swedish sub release
            3. Trusted uploader releases

        Returns None if the current release already has subs available
        (i.e., OpenSubtitles has Swedish subs for it).
        """
        if not matches:
            return None

        # If the current release already has Swedish subs available on
        # OpenSubtitles, no replacement needed — just download the sub.
        current_lower = current_release.lower()
        for match in matches:
            if match.release_name.lower() == current_lower:
                return None

        # 1. Prefer NORDIC/SWE releases
        nordic_matches = [m for m in matches if self._is_nordic_release(m.release_name)]
        if nordic_matches:
            nordic_matches.sort(key=lambda m: m.download_count, reverse=True)
            return nordic_matches[0].release_name

        # 2. Score remaining matches: download_count + trusted bonus
        def _score(m: ReleaseMatch) -> int:
            score = m.download_count
            if m.from_trusted:
                score += 500
            return score

        best = max(matches, key=_score)
        return best.release_name

    # ------------------------------------------------------------------ #
    # Main analysis
    # ------------------------------------------------------------------ #

    def analyze_library(
        self,
        library_name: str,
        library_type: str = "movie",
        limit: int = 0,
        progress_callback=None,
    ) -> list[AnalysisResult]:
        """
        Main analysis method. Scans a Plex library and checks each item
        for Swedish subtitle availability.

        Steps:
            1. Get all media files from Plex
            2. Filter to items missing Swedish subs
            3. For each, query OpenSubtitles for releases with Swedish subs
            4. Build AnalysisResult with status

        Args:
            library_name: Plex library name
            library_type: "movie" or "show"
            limit: Max items to analyze (0 = all)
            progress_callback: Optional callable(current, total, title)

        Returns:
            List of AnalysisResult objects.
        """
        logger.info(f"Starting library analysis: {library_name} ({library_type})")

        # Enforce expiry on trackers for long-running containers
        self.skip_tracker.purge_expired()
        self.search_cooldown.purge_expired()

        all_items = self.plex.get_all_media_files(library_name, library_type)
        logger.info(f"Found {len(all_items)} media files in library")

        missing_subs = [item for item in all_items if not item.get("has_swedish_sub")]

        # Filter out already-grabbed items
        before_filter = len(missing_subs)
        missing_subs = [
            item for item in missing_subs
            if not self.grab_tracker.is_grabbed(
                imdb_id=item.get("imdb_id"),
                tmdb_id=item.get("tmdb_id"),
                rating_key=item.get("rating_key"),
            )
        ]
        grabbed_skipped = before_filter - len(missing_subs)

        # Filter out items previously skipped (no indexer results)
        before_skip_filter = len(missing_subs)
        missing_subs = [
            item for item in missing_subs
            if not self.skip_tracker.is_skipped(
                imdb_id=item.get("imdb_id"),
                tmdb_id=item.get("tmdb_id"),
                rating_key=item.get("rating_key"),
            )
        ]
        skip_filtered = before_skip_filter - len(missing_subs)

        # Filter out items on search cooldown (recently searched)
        before_cooldown = len(missing_subs)
        missing_subs = [
            item for item in missing_subs
            if not self.search_cooldown.is_on_cooldown(
                imdb_id=item.get("imdb_id"),
                tmdb_id=item.get("tmdb_id"),
                rating_key=item.get("rating_key"),
            )
        ]
        cooldown_filtered = before_cooldown - len(missing_subs)

        logger.info(
            f"{len(missing_subs)} items missing Swedish subtitles "
            f"(out of {len(all_items)} total"
            f"{f', {grabbed_skipped} already grabbed' if grabbed_skipped else ''}"
            f"{f', {skip_filtered} skipped (no indexer results)' if skip_filtered else ''}"
            f"{f', {cooldown_filtered} on search cooldown' if cooldown_filtered else ''})"
        )

        if limit > 0:
            missing_subs = missing_subs[:limit]
            logger.info(f"Limited to {limit} items for analysis")

        results: list[AnalysisResult] = []
        total = len(missing_subs)

        for idx, item in enumerate(missing_subs, start=1):
            display_title = self._build_display_title(item)
            current_release = self.parse_release_name(item.get("file_path", ""))

            logger.info(f"[{idx}/{total}] Analyzing: {display_title}")

            if progress_callback:
                try:
                    progress_callback(idx, total, display_title)
                except Exception:
                    pass

            try:
                matches = self._find_swedish_releases(item)
            except Exception as e:
                logger.error(f"Error analyzing {display_title}: {e}")
                result = AnalysisResult(
                    title=item.get("title", ""),
                    display_title=display_title,
                    year=item.get("year"),
                    file_path=item.get("file_path", ""),
                    current_release=current_release,
                    media_type=item.get("media_type", "movie"),
                    has_swedish_sub=False,
                    swedish_sub_available=False,
                    matching_releases=[],
                    has_nordic_release=False,
                    recommended_release=None,
                    imdb_id=item.get("imdb_id"),
                    tmdb_id=item.get("tmdb_id"),
                    tvdb_id=item.get("tvdb_id"),
                    rating_key=item.get("rating_key", ""),
                    show_imdb_id=item.get("show_imdb_id"),
                    show_title=item.get("show_title", ""),
                    season_number=item.get("season_number"),
                    episode_number=item.get("episode_number"),
                    status="error",
                    error=str(e),
                )
                results.append(result)
                continue

            swedish_sub_available = len(matches) > 0
            has_nordic = any(self._is_nordic_release(m.release_name) for m in matches)
            recommended = self._find_best_replacement(current_release, matches)

            # Determine status
            if swedish_sub_available and recommended is None:
                # Current release already has subs on OpenSubtitles — just
                # download the subtitle, no file replacement needed.
                status = "has_subs"
            elif swedish_sub_available:
                status = "needs_replacement"
            else:
                status = "no_subs_available"

            result = AnalysisResult(
                title=item.get("title", ""),
                display_title=display_title,
                year=item.get("year"),
                file_path=item.get("file_path", ""),
                current_release=current_release,
                media_type=item.get("media_type", "movie"),
                has_swedish_sub=False,
                swedish_sub_available=swedish_sub_available,
                matching_releases=matches,
                has_nordic_release=has_nordic,
                recommended_release=recommended,
                imdb_id=item.get("imdb_id"),
                tmdb_id=item.get("tmdb_id"),
                tvdb_id=item.get("tvdb_id"),
                rating_key=item.get("rating_key", ""),
                show_imdb_id=item.get("show_imdb_id"),
                show_title=item.get("show_title", ""),
                season_number=item.get("season_number"),
                episode_number=item.get("episode_number"),
                status=status,
            )
            results.append(result)

            # Mark as recently analyzed so it's skipped on the next scan
            self.search_cooldown.mark_searched(result, defer_save=True)

            if swedish_sub_available:
                logger.info(
                    f"  Found {len(matches)} Swedish sub release(s)"
                    f"{'  —  NORDIC available' if has_nordic else ''}"
                    f"  —  status: {status}"
                )
            else:
                logger.info(f"  No Swedish subtitles found")

        has_subs_count = sum(1 for r in results if r.status == "has_subs")
        needs_count = sum(1 for r in results if r.status == "needs_replacement")
        none_count = sum(1 for r in results if r.status == "no_subs_available")
        error_count = sum(1 for r in results if r.status == "error")

        # Flush cooldown entries written during analysis
        self.search_cooldown.flush()

        logger.info(
            f"Analysis complete: {len(results)} items analyzed  —  "
            f"{has_subs_count} have subs available, "
            f"{needs_count} need replacement, "
            f"{none_count} no subs available, "
            f"{error_count} errors"
        )

        return results
