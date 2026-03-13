"""
Library analyzer engine for finding movies and episodes that need replacement
to get Swedish subtitles. Scans Plex libraries, checks OpenSubtitles for
available Swedish subtitle releases, and coordinates replacement via Prowlarr.
"""

import os
import re
import logging
from dataclasses import dataclass, field

from config import Config
from plex_client import PlexClient
from opensubtitles_client import OpenSubtitlesClient
from prowlarr_client import ProwlarrClient

logger = logging.getLogger(__name__)


def _build_nordic_pattern(tags: list[str]) -> re.Pattern:
    """Build a regex that matches any of the given tags as whole tokens."""
    escaped = [re.escape(t) for t in tags]
    alternatives = "|".join(escaped)
    return re.compile(
        rf"(?<![A-Za-z])(?:{alternatives})(?![A-Za-z])",
        re.IGNORECASE,
    )


@dataclass
class ReleaseMatch:
    """A release on OpenSubtitles that has Swedish subtitles."""

    release_name: str
    language: str  # "sv"
    download_count: int
    from_trusted: bool
    hearing_impaired: bool

    def to_dict(self) -> dict:
        return {
            "release_name": self.release_name,
            "language": self.language,
            "download_count": self.download_count,
            "from_trusted": self.from_trusted,
            "hearing_impaired": self.hearing_impaired,
        }


@dataclass
class AnalysisResult:
    """Analysis result for a single media item."""

    title: str
    display_title: str
    year: int | None
    file_path: str
    current_release: str  # parsed from filename
    media_type: str  # "movie" or "episode"
    has_swedish_sub: bool

    # What we found
    swedish_sub_available: bool  # Any release has Swedish subs on OpenSubtitles
    matching_releases: list[ReleaseMatch]  # Releases WITH Swedish subs
    has_nordic_release: bool  # Any release has NORDIC/SWE/SWESUB/SWEDISH in name

    # For replacement
    recommended_release: str | None  # Best release to grab
    prowlarr_results: list[dict] = field(default_factory=list)

    # IDs for arr integration
    imdb_id: str | None = None
    tmdb_id: str | None = None
    rating_key: str = ""

    # TV-specific
    show_title: str = ""
    season_number: int | None = None
    episode_number: int | None = None

    # Status
    status: str = "pending"  # pending, needs_replacement, has_subs, no_subs_available, replaced, error
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "display_title": self.display_title,
            "year": self.year,
            "file_path": self.file_path,
            "current_release": self.current_release,
            "media_type": self.media_type,
            "has_swedish_sub": self.has_swedish_sub,
            "swedish_sub_available": self.swedish_sub_available,
            "matching_releases": [m.to_dict() for m in self.matching_releases],
            "has_nordic_release": self.has_nordic_release,
            "recommended_release": self.recommended_release,
            "prowlarr_results": self.prowlarr_results,
            "imdb_id": self.imdb_id,
            "tmdb_id": self.tmdb_id,
            "rating_key": self.rating_key,
            "show_title": self.show_title,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "status": self.status,
            "error": self.error,
        }


class LibraryAnalyzer:
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
        self._nordic_pattern = _build_nordic_pattern(config.subtitle_match_tags)

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

        if item.get("imdb_id"):
            search_kwargs["imdb_id"] = item["imdb_id"]
        if item.get("tmdb_id"):
            search_kwargs["tmdb_id"] = item["tmdb_id"]
        if not item.get("imdb_id") and not item.get("tmdb_id"):
            query = item.get("show_title") or item.get("title", "")
            search_kwargs["query"] = query

        try:
            results = self.opensubs.search_subtitles(**search_kwargs)
        except Exception as e:
            logger.error(f"OpenSubtitles search failed for {item.get('title')}: {e}")
            return []

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

        all_items = self.plex.get_all_media_files(library_name, library_type)
        logger.info(f"Found {len(all_items)} media files in library")

        missing_subs = [item for item in all_items if not item.get("has_swedish_sub")]
        logger.info(
            f"{len(missing_subs)} items missing Swedish subtitles "
            f"(out of {len(all_items)} total)"
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
                    rating_key=item.get("rating_key", ""),
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
                rating_key=item.get("rating_key", ""),
                show_title=item.get("show_title", ""),
                season_number=item.get("season_number"),
                episode_number=item.get("episode_number"),
                status=status,
            )
            results.append(result)

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

        logger.info(
            f"Analysis complete: {len(results)} items analyzed  —  "
            f"{has_subs_count} have subs available, "
            f"{needs_count} need replacement, "
            f"{none_count} no subs available, "
            f"{error_count} errors"
        )

        return results

    # ------------------------------------------------------------------ #
    # Prowlarr search
    # ------------------------------------------------------------------ #

    def search_replacements(
        self, results: list[AnalysisResult], limit: int = 0
    ) -> list[AnalysisResult]:
        """
        For items that need replacement, search Prowlarr for the
        recommended release. Updates prowlarr_results on each
        AnalysisResult.

        Args:
            results: List of AnalysisResult from analyze_library
            limit: Max items to search (0 = all)

        Returns:
            The same list with prowlarr_results populated.
        """
        needs_replacement = [r for r in results if r.status == "needs_replacement"]

        if limit > 0:
            needs_replacement = needs_replacement[:limit]

        logger.info(
            f"Searching Prowlarr for {len(needs_replacement)} replacement releases"
        )

        for idx, result in enumerate(needs_replacement, start=1):
            if not result.recommended_release:
                continue

            logger.info(
                f"[{idx}/{len(needs_replacement)}] "
                f"Searching: {result.recommended_release}"
            )

            try:
                prowlarr_results = self.prowlarr.search_release(
                    result.recommended_release,
                    result.media_type,
                )
                result.prowlarr_results = prowlarr_results or []

                if prowlarr_results:
                    logger.info(
                        f"  Found {len(prowlarr_results)} result(s) on Prowlarr"
                    )
                else:
                    logger.info(f"  No results found on Prowlarr")

            except Exception as e:
                logger.error(
                    f"Prowlarr search failed for {result.recommended_release}: {e}"
                )
                result.prowlarr_results = []

        return results

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def execute_replacement(self, result: AnalysisResult, dry_run: bool = True) -> bool:
        """
        Execute a single replacement by grabbing the release via Prowlarr.

        In dry_run mode, just logs what would happen.
        """
        if result.status != "needs_replacement":
            logger.info(
                f"Skipping {result.display_title}  —  status is {result.status}"
            )
            return False

        if not result.prowlarr_results:
            logger.warning(
                f"No Prowlarr results for {result.display_title}  —  "
                f"run search_replacements first"
            )
            return False

        best_result = result.prowlarr_results[0]
        guid = best_result.get("guid", "")
        indexer_id = best_result.get("indexerId") or best_result.get("indexer_id")
        release_title = best_result.get("title", result.recommended_release)

        if not guid or indexer_id is None:
            logger.warning(
                f"Missing guid or indexer_id for {result.display_title} — skipping"
            )
            return False

        if dry_run:
            logger.info(
                f"[DRY RUN] Would grab: {release_title} "
                f"for {result.display_title}"
            )
            return True

        try:
            logger.info(f"Grabbing: {release_title} for {result.display_title}")
            success = self.prowlarr.grab(guid, indexer_id)
            if not success:
                result.status = "error"
                result.error = "Grab returned failure"
                logger.error(f"Grab failed for {release_title}")
                return False
            result.status = "replaced"
            logger.info(f"Successfully pushed to download client: {release_title}")
            return True
        except Exception as e:
            result.status = "error"
            result.error = f"Grab failed: {e}"
            logger.error(f"Failed to grab {release_title}: {e}")
            return False

    def execute_all(
        self, results: list[AnalysisResult], dry_run: bool = True
    ) -> dict:
        """Execute all replacements. Returns summary dict."""
        needs_replacement = [r for r in results if r.status == "needs_replacement"]

        logger.info(
            f"Executing {'DRY RUN ' if dry_run else ''}"
            f"replacements for {len(needs_replacement)} items"
        )

        success = 0
        failed = 0
        skipped = 0

        for result in needs_replacement:
            if not result.prowlarr_results:
                skipped += 1
                continue

            if self.execute_replacement(result, dry_run=dry_run):
                success += 1
            else:
                failed += 1

        summary = {
            "total": len(needs_replacement),
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "dry_run": dry_run,
        }

        logger.info(
            f"Execution complete: {success} succeeded, "
            f"{failed} failed, {skipped} skipped"
        )
        return summary

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
        with_prowlarr = sum(1 for r in results if r.prowlarr_results)

        return {
            "total_scanned": total,
            "has_subs": has_subs,
            "needs_replacement": needs_replacement,
            "no_subs_available": no_subs,
            "replaced": replaced,
            "errors": errors,
            "nordic_available": nordic,
            "prowlarr_results_found": with_prowlarr,
        }
