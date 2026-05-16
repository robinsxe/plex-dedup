"""
Library analyzer engine for finding movies and episodes that need replacement
to get Swedish subtitles. Scans Plex libraries, checks OpenSubtitles for
available Swedish subtitle releases, and coordinates replacement via Prowlarr.
"""

import os
import re
import json
import logging
import time
from dataclasses import dataclass, field

from config import Config
from plex_client import PlexClient
from opensubtitles_client import OpenSubtitlesClient
from prowlarr_client import ProwlarrClient
from arr_common import StaleReleaseError
from radarr_client import RadarrClient
from sonarr_client import SonarrClient

logger = logging.getLogger(__name__)

GRABBED_FILE = os.environ.get("GRABBED_DB", "/data/grabbed.json")
SKIPPED_FILE = os.environ.get("SKIPPED_DB", "/data/skipped.json")
COOLDOWN_FILE = os.environ.get("COOLDOWN_DB", "/data/cooldown.json")

def _parse_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        return max(1, min(value, 365))
    except (ValueError, TypeError):
        logger.warning(f"Invalid value for {name}, using default {default}")
        return default

SKIP_EXPIRY_DAYS = _parse_int_env("SKIP_EXPIRY_DAYS", 30)
SEARCH_COOLDOWN_DAYS = _parse_int_env("SEARCH_COOLDOWN_DAYS", 30)


class GrabTracker:
    """Tracks which items have been grabbed to avoid re-downloading."""

    def __init__(self, path: str = GRABBED_FILE):
        self._path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            with open(self._path) as f:
                self._data = json.load(f)
            logger.info(f"Loaded {len(self._data)} grabbed items from {self._path}")
        except FileNotFoundError:
            self._data = {}
        except Exception as e:
            logger.warning(f"Could not load grabbed DB: {e}")
            self._data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save grabbed DB: {e}")

    def is_grabbed(self, imdb_id: str = None, tmdb_id: str = None,
                   rating_key: str = None) -> bool:
        """Check if an item was already grabbed."""
        for key in [f"imdb:{imdb_id}", f"tmdb:{tmdb_id}", f"plex:{rating_key}"]:
            if key and key.split(":", 1)[1] and key in self._data:
                return True
        return False

    def mark_grabbed(self, result) -> None:
        """Mark an AnalysisResult as grabbed."""
        entry = {
            "title": result.display_title,
            "grabbed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "recommended_release": result.recommended_release,
        }
        if result.imdb_id:
            self._data[f"imdb:{result.imdb_id}"] = entry
        if result.tmdb_id:
            self._data[f"tmdb:{result.tmdb_id}"] = entry
        if result.rating_key:
            self._data[f"plex:{result.rating_key}"] = entry
        self._save()

    def clear(self) -> int:
        """Clear all grabbed items. Returns count cleared."""
        count = len(self._data)
        self._data = {}
        self._save()
        return count

    @property
    def count(self) -> int:
        return len(self._data)


class SkipTracker:
    """Tracks items where no indexer results were found, so they can be
    skipped on subsequent scans. Entries expire after SKIP_EXPIRY_DAYS."""

    def __init__(self, path: str = SKIPPED_FILE):
        self._path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            with open(self._path) as f:
                self._data = json.load(f)
            self._purge_expired()
            logger.info(f"Loaded {len(self._data)} skipped items from {self._path}")
        except FileNotFoundError:
            self._data = {}
        except Exception as e:
            logger.warning(f"Could not load skipped DB: {e}")
            self._data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save skipped DB: {e}")

    def _purge_expired(self):
        """Remove entries older than SKIP_EXPIRY_DAYS."""
        now = time.time()
        cutoff = now - (SKIP_EXPIRY_DAYS * 86400)
        before = len(self._data)
        self._data = {
            k: v for k, v in self._data.items()
            if v.get("skipped_ts", 0) > cutoff
        }
        removed = before - len(self._data)
        if removed:
            logger.info(f"Purged {removed} expired skip entries (>{SKIP_EXPIRY_DAYS} days)")
            self._save()

    def is_skipped(self, imdb_id: str = None, tmdb_id: str = None,
                   rating_key: str = None) -> bool:
        """Check if an item was previously skipped."""
        for key in [f"imdb:{imdb_id}", f"tmdb:{tmdb_id}", f"plex:{rating_key}"]:
            if key and key.split(":", 1)[1] and key in self._data:
                return True
        return False

    def mark_skipped(self, result, reason: str = "no_indexer_results",
                     defer_save: bool = False) -> None:
        """Mark an AnalysisResult as skipped (no indexer results found).

        Args:
            result: The AnalysisResult to mark.
            reason: Why it was skipped ("no_indexer_results" or "all_filtered").
            defer_save: If True, don't write to disk — call flush() later.
        """
        entry = {
            "title": result.display_title,
            "skipped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "skipped_ts": time.time(),
            "reason": reason,
        }
        if result.imdb_id:
            self._data[f"imdb:{result.imdb_id}"] = entry
        if result.tmdb_id:
            self._data[f"tmdb:{result.tmdb_id}"] = entry
        if result.rating_key:
            self._data[f"plex:{result.rating_key}"] = entry
        if not defer_save:
            self._save()

    def flush(self) -> None:
        """Write pending changes to disk."""
        self._save()

    def clear(self) -> int:
        """Clear all skipped items. Returns count cleared."""
        count = len(self._data)
        self._data = {}
        self._save()
        return count

    @property
    def count(self) -> int:
        return len(self._data)


class SearchCooldownTracker:
    """Tracks items that have been searched on indexers recently, so they
    are not re-searched on every scan. Entries expire after
    SEARCH_COOLDOWN_DAYS regardless of whether results were found."""

    def __init__(self, path: str = COOLDOWN_FILE):
        self._path = path
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self):
        try:
            with open(self._path) as f:
                self._data = json.load(f)
            self._purge_expired()
            logger.info(
                f"Loaded {len(self._data)} cooldown items from {self._path}")
        except FileNotFoundError:
            self._data = {}
        except Exception as e:
            logger.warning(f"Could not load cooldown DB: {e}")
            self._data = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save cooldown DB: {e}")

    def _purge_expired(self):
        now = time.time()
        cutoff = now - (SEARCH_COOLDOWN_DAYS * 86400)
        before = len(self._data)
        self._data = {
            k: v for k, v in self._data.items()
            if v.get("ts", 0) > cutoff
        }
        removed = before - len(self._data)
        if removed:
            logger.info(
                f"Purged {removed} expired cooldown entries "
                f"(>{SEARCH_COOLDOWN_DAYS} days)")
            self._save()

    def _key(self, imdb_id=None, tmdb_id=None, rating_key=None):
        """Return the best available lookup key."""
        if imdb_id:
            return f"imdb:{imdb_id}"
        if tmdb_id:
            return f"tmdb:{tmdb_id}"
        if rating_key:
            return f"plex:{rating_key}"
        return None

    def is_on_cooldown(self, imdb_id: str = None, tmdb_id: str = None,
                       rating_key: str = None) -> bool:
        """Check if an item was searched recently."""
        for key in [f"imdb:{imdb_id}", f"tmdb:{tmdb_id}", f"plex:{rating_key}"]:
            if key and key.split(":", 1)[1] and key in self._data:
                return True
        return False

    def mark_searched(self, result, defer_save: bool = False) -> None:
        """Mark an AnalysisResult as recently searched on indexers."""
        entry = {
            "title": result.display_title,
            "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.time(),
        }
        if result.imdb_id:
            self._data[f"imdb:{result.imdb_id}"] = entry
        if result.tmdb_id:
            self._data[f"tmdb:{result.tmdb_id}"] = entry
        if result.rating_key:
            self._data[f"plex:{result.rating_key}"] = entry
        if not defer_save:
            self._save()

    def flush(self) -> None:
        self._save()

    def clear(self) -> int:
        count = len(self._data)
        self._data = {}
        self._save()
        return count

    @property
    def count(self) -> int:
        return len(self._data)


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_release_title(title: str) -> str:
    """
    Loose normalization for comparing release titles across two consecutive
    indexer searches. Lowercases and collapses whitespace; keeps everything
    else so we still distinguish e.g. -GROUPA from -GROUPB.
    """
    return _WHITESPACE_RE.sub(" ", title.strip().lower())


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
    tvdb_id: str | None = None
    rating_key: str = ""

    # TV-specific
    show_title: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    # Sonarr internal episode ID, resolved during search_replacements.
    episode_id: int | None = None

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
            "tvdb_id": self.tvdb_id,
            "rating_key": self.rating_key,
            "show_title": self.show_title,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "episode_id": self.episode_id,
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

    @staticmethod
    def _empty_grab_stats() -> dict:
        return {
            "grabbed_direct": 0,
            "stale_recovered": 0,
            "push_fallback_used": 0,
            "failed_unrecoverable": 0,
        }

    def reset_grab_stats(self) -> None:
        """
        Reset per-batch grab telemetry counters. ``execute_all`` calls this
        automatically; direct callers of ``execute_replacement`` (e.g. the
        web UI or single-item CLI flows) should invoke this themselves
        before a batch if they want clean counter values.
        """
        self._grab_stats = self._empty_grab_stats()

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

        # Enforce expiry on trackers for long-running containers
        self.skip_tracker._purge_expired()
        self.search_cooldown._purge_expired()

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

    # ------------------------------------------------------------------ #
    # Prowlarr search
    # ------------------------------------------------------------------ #

    def _build_radarr_index(self) -> dict:
        """Build lookup index for Radarr movies by TMDB/IMDB ID."""
        index = {}
        try:
            for movie in self.radarr.get_all_movies():
                tmdb = str(movie.get("tmdbId", ""))
                imdb = movie.get("imdbId", "")
                if tmdb:
                    index[f"tmdb:{tmdb}"] = movie
                if imdb:
                    index[f"imdb:{imdb}"] = movie
        except Exception as e:
            logger.warning(f"Could not fetch Radarr movies: {e}")
        return index

    def _find_in_radarr(self, result: AnalysisResult, index: dict) -> dict | None:
        """Find a movie in Radarr index by TMDB/IMDB ID."""
        if result.tmdb_id:
            entry = index.get(f"tmdb:{result.tmdb_id}")
            if entry:
                return entry
        if result.imdb_id:
            entry = index.get(f"imdb:{result.imdb_id}")
            if entry:
                return entry
        return None

    def _resolve_sonarr_episode_id(self, result: AnalysisResult) -> int | None:
        """
        Resolve a Sonarr internal episode_id for a TV AnalysisResult by
        looking up the series (tvdb/imdb/title) then the episode
        (season/episode number). Returns None if anything is missing.
        """
        if result.season_number is None or result.episode_number is None:
            return None
        series = self.sonarr.find_series(
            tvdb_id=result.tvdb_id,
            imdb_id=result.imdb_id,
            title=result.show_title or result.title,
        )
        if not series or "id" not in series:
            return None
        episode = self.sonarr.find_episode(
            series["id"], result.season_number, result.episode_number
        )
        if not episode or "id" not in episode:
            return None
        return episode["id"]

    def search_replacements(
        self, results: list[AnalysisResult], limit: int = 0,
        progress_callback=None,
    ) -> list[AnalysisResult]:
        """
        For items that need replacement, search Radarr/Sonarr indexers
        for available releases. Uses the *arr interactive search API
        which queries all configured indexers (synced from Prowlarr).

        Args:
            results: List of AnalysisResult from analyze_library
            limit: Max items to search (0 = all)
            progress_callback: Optional callable(current, total, title)

        Returns:
            The same list with prowlarr_results populated.
        """
        needs_replacement = [r for r in results if r.status == "needs_replacement"]

        if limit > 0:
            needs_replacement = needs_replacement[:limit]

        total = len(needs_replacement)
        logger.info(f"Searching indexers for {total} replacement releases")

        # Build Radarr index for movie lookups
        logger.info("Building Radarr movie index...")
        radarr_index = self._build_radarr_index()
        logger.info(f"Radarr index: {len(radarr_index)} entries")

        for idx, result in enumerate(needs_replacement, start=1):
            if not result.recommended_release:
                continue

            if progress_callback:
                try:
                    progress_callback(idx, total, result.display_title)
                except Exception:
                    pass

            logger.info(
                f"[{idx}/{total}] "
                f"Searching indexers for: {result.display_title} "
                f"(want: {result.recommended_release})"
            )

            try:
                all_results = []

                if result.media_type == "movie":
                    # Find the movie in Radarr to get its internal ID
                    radarr_movie = self._find_in_radarr(result, radarr_index)
                    if not radarr_movie:
                        logger.info(f"  Not found in Radarr — skipping")
                        result.prowlarr_results = []
                        continue
                    movie_id = radarr_movie["id"]
                    all_results = self.radarr.search_releases(movie_id)
                else:
                    episode_id = self._resolve_sonarr_episode_id(result)
                    if episode_id is None:
                        logger.info(f"  Not found in Sonarr — skipping")
                        result.prowlarr_results = []
                        continue
                    result.episode_id = episode_id
                    all_results = self.sonarr.search_releases(episode_id)

                if not all_results:
                    logger.info(f"  No releases found on indexers — adding to skip list")
                    result.prowlarr_results = []
                    self.skip_tracker.mark_skipped(
                        result, reason="no_indexer_results", defer_save=True)
                    continue

                # Filter out remux, 4K, and oversized releases
                filtered = []
                for r in all_results:
                    title_lower = (r.get("title") or "").lower()
                    size_gb = (r.get("size") or 0) / (1024 ** 3)

                    # Reject by quality keywords
                    if any(kw in title_lower for kw in self._rejected_qualities):
                        continue
                    # Reject by size
                    if self._max_size_gb > 0 and size_gb > self._max_size_gb:
                        continue
                    filtered.append(r)

                if not filtered and all_results:
                    logger.info(
                        f"  {len(all_results)} releases found but all filtered "
                        f"(remux/4K/>{self._max_size_gb}GB) — adding to skip list"
                    )
                    self.skip_tracker.mark_skipped(
                        result, reason="all_filtered", defer_save=True)
                    result.prowlarr_results = []
                    continue

                all_results = filtered

                # Score results: prefer NORDIC/SWE releases
                recommended_lower = result.recommended_release.lower()
                nordic_results = []
                matching_results = []
                other_results = []

                for r in all_results:
                    title = (r.get("title") or "").lower()
                    if recommended_lower and recommended_lower in title:
                        matching_results.append(r)
                    elif self._is_nordic_release(r.get("title", "")):
                        nordic_results.append(r)
                    else:
                        other_results.append(r)

                # Sort each group by quality preference
                def _quality_score(r):
                    t = (r.get("title") or "").lower()
                    score = 0
                    # Resolution preference
                    if "1080p" in t:
                        score += 100
                    elif "720p" in t:
                        score += 50
                    # Source preference
                    if "bluray" in t or "blu-ray" in t:
                        score += 30
                    elif "web-dl" in t or "webdl" in t:
                        score += 20
                    elif "webrip" in t:
                        score += 15
                    elif "hdtv" in t:
                        score += 10
                    return -score  # negative for ascending sort

                matching_results.sort(key=_quality_score)
                nordic_results.sort(key=_quality_score)
                other_results.sort(key=_quality_score)

                # Priority: exact match > nordic release > everything else
                ranked = matching_results + nordic_results + other_results
                result.prowlarr_results = ranked

                logger.info(
                    f"  Found {len(ranked)} release(s) "
                    f"({len(matching_results)} matching, "
                    f"{len(nordic_results)} NORDIC)"
                )

            except Exception as e:
                logger.error(
                    f"Search failed for {result.display_title}: {e}"
                )
                result.prowlarr_results = []

        # Flush deferred skip entries to disk in one write
        self.skip_tracker.flush()

        return results

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #

    def execute_replacement(self, result: AnalysisResult, dry_run: bool = True) -> bool:
        """
        Execute a single replacement by grabbing the release via Radarr/Sonarr.
        Uses the same grab mechanism as clicking "download" in the Radarr/Sonarr UI.

        In dry_run mode, just logs what would happen.
        """
        if result.status != "needs_replacement":
            logger.info(
                f"Skipping {result.display_title}  —  status is {result.status}"
            )
            return False

        if not result.prowlarr_results:
            logger.warning(
                f"No indexer results for {result.display_title}  —  "
                f"run search_replacements first"
            )
            return False

        best_result = result.prowlarr_results[0]
        release_title = best_result.get("title", result.recommended_release)
        guid = best_result.get("guid", "")
        indexer_id = best_result.get("indexerId") or best_result.get("indexer_id")

        if not guid or indexer_id is None:
            logger.warning(
                f"Missing guid or indexer_id for {result.display_title} — skipping"
            )
            return False

        if dry_run:
            logger.info(
                f"[DRY RUN] Would grab via {'Radarr' if result.media_type == 'movie' else 'Sonarr'}: "
                f"{release_title} for {result.display_title}"
            )
            return True

        if self._grab_refresh_before:
            best_result = self._refreshed_best_result(result, best_result)

        try:
            success = self._grab_with_retry(result, best_result)

            if not success:
                result.status = "error"
                result.error = "Grab returned failure"
                logger.error(f"Grab failed for {release_title}")
                return False

            result.status = "replaced"
            self.grab_tracker.mark_grabbed(result)
            logger.info(
                f"Successfully grabbed via {'Radarr' if result.media_type == 'movie' else 'Sonarr'}: "
                f"{release_title}"
            )
            return True
        except Exception as e:
            result.status = "error"
            result.error = f"Grab failed: {e}"
            logger.error(f"Failed to grab {release_title}: {e}")
            return False

    def _grab_with_retry(
        self,
        result: "AnalysisResult",
        best_result: dict,
    ) -> bool:
        """
        Grab with stale-cache recovery.

        Radarr/Sonarr cache search results in memory for a short window. If the
        guid is evicted (HTTP 404 on POST /api/v3/release), re-search to refresh
        the cache, find the same release by title, and retry. As a last resort,
        push by downloadUrl which bypasses the guid cache entirely.

        Unrecoverable failures are added to the skip tracker so subsequent
        scans don't immediately re-attempt the same broken release.
        """
        is_movie = result.media_type == "movie"
        client: "RadarrClient | SonarrClient" = self.radarr if is_movie else self.sonarr
        label = "Radarr" if is_movie else "Sonarr"
        release_title = best_result.get("title") or result.recommended_release or ""
        guid = best_result.get("guid", "")
        indexer_id = best_result.get("indexerId") or best_result.get("indexer_id")

        logger.info(f"Grabbing via {label}: {release_title}")
        try:
            if client.grab_release(guid, indexer_id):
                self._grab_stats["grabbed_direct"] += 1
                return True
        except StaleReleaseError as e:
            logger.warning(f"{label} cache stale for {release_title} ({e}) — refreshing")

        # Refresh cache and retry (symmetric for movies + TV).
        refreshed = (
            self._refresh_radarr_release(result, release_title) if is_movie
            else self._refresh_sonarr_release(result, release_title)
        )
        if refreshed:
            new_guid, new_indexer = refreshed
            logger.info(
                f"Retrying grab via {label} with refreshed guid for {release_title}"
            )
            try:
                if client.grab_release(new_guid, new_indexer):
                    self._grab_stats["stale_recovered"] += 1
                    return True
                logger.warning(
                    f"{label} retry returned failure for {release_title} — falling back to push"
                )
            except StaleReleaseError:
                logger.warning(
                    f"{label} still 404 after refresh for {release_title} — falling back to push"
                )
            except Exception as e:
                logger.warning(
                    f"{label} retry raised {type(e).__name__} for {release_title} ({e}) "
                    f"— falling back to push"
                )

        # Final fallback: push by URL (bypasses guid cache)
        if self._push_fallback(client, label, release_title, best_result):
            self._grab_stats["push_fallback_used"] += 1
            return True

        self._grab_stats["failed_unrecoverable"] += 1
        try:
            self.skip_tracker.mark_skipped(
                result, reason="stale_grab_unrecoverable", defer_save=True
            )
        except Exception as e:
            logger.warning(f"Could not mark {result.display_title} as skipped: {e}")
        return False

    def _refreshed_best_result(
        self, result: "AnalysisResult", best_result: dict
    ) -> dict:
        """
        Proactively refresh the *arr release cache and return a best_result
        with a fresh guid/indexerId. Returns the original dict untouched if
        refresh found no match. Never mutates the input. Triggered by the
        GRAB_REFRESH_BEFORE env flag.
        """
        is_movie = result.media_type == "movie"
        label = "Radarr" if is_movie else "Sonarr"
        release_title = best_result.get("title") or result.recommended_release or ""

        refreshed = (
            self._refresh_radarr_release(result, release_title) if is_movie
            else self._refresh_sonarr_release(result, release_title)
        )
        if not refreshed:
            return best_result

        new_guid, new_indexer = refreshed
        old_indexer = best_result.get("indexerId") or best_result.get("indexer_id")
        if new_guid == best_result.get("guid") and new_indexer == old_indexer:
            return best_result

        logger.info(f"Pre-grab refresh updated guid for {release_title} via {label}")
        updated = dict(best_result)
        updated["guid"] = new_guid
        updated["indexerId"] = new_indexer
        return updated

    def _refresh_sonarr_release(
        self, result: "AnalysisResult", release_title: str
    ) -> tuple[str, int] | None:
        """
        Re-run Sonarr's interactive search and locate the same release by
        normalized title. Returns (guid, indexerId) on match, None otherwise.

        Requires ``result.episode_id`` (populated by search_replacements).
        """
        if not result.episode_id:
            logger.warning(
                f"Skipping Sonarr refresh for {result.display_title}: "
                f"no episode_id"
            )
            return None

        fresh = self.sonarr.search_releases(result.episode_id)
        if not fresh:
            logger.warning(
                f"Sonarr refresh returned no releases for {release_title} "
                f"(indexer transient failure or release no longer listed)"
            )
            return None

        target = _normalize_release_title(release_title)
        for r in fresh:
            if _normalize_release_title(r.get("title") or "") == target:
                guid = r.get("guid")
                indexer_id = r.get("indexerId") or r.get("indexer_id")
                if guid and indexer_id is not None:
                    return guid, indexer_id

        logger.warning(
            f"Original release {release_title!r} not in refreshed Sonarr results — "
            f"refusing to substitute a different release"
        )
        return None

    def _refresh_radarr_release(
        self, result: "AnalysisResult", release_title: str
    ) -> tuple[str, int] | None:
        """
        Re-run Radarr's interactive search and locate the same release by
        normalized title. Returns (guid, indexerId) on match, None otherwise.

        Skips when neither tmdb_id nor imdb_id is known, since the title-only
        fallback in find_movie() pulls the full Radarr catalog per item.
        """
        if not result.tmdb_id and not result.imdb_id:
            logger.warning(
                f"Skipping Radarr refresh for {result.display_title}: "
                f"no tmdb_id or imdb_id — title-only lookup is too expensive"
            )
            return None

        movie = self.radarr.find_movie(
            tmdb_id=result.tmdb_id,
            imdb_id=result.imdb_id,
            title=result.title,
            year=result.year,
        )
        if not movie or "id" not in movie:
            logger.warning(f"Could not re-locate movie in Radarr: {result.display_title}")
            return None

        fresh = self.radarr.search_releases(movie["id"])
        if not fresh:
            logger.warning(
                f"Radarr refresh returned no releases for {release_title} "
                f"(indexer transient failure or release no longer listed)"
            )
            return None

        target = _normalize_release_title(release_title)
        for r in fresh:
            if _normalize_release_title(r.get("title") or "") == target:
                guid = r.get("guid")
                indexer_id = r.get("indexerId") or r.get("indexer_id")
                if guid and indexer_id is not None:
                    return guid, indexer_id

        logger.warning(
            f"Original release {release_title!r} not in refreshed results — "
            f"refusing to substitute a different release"
        )
        return None

    def _push_fallback(
        self,
        client: "RadarrClient | SonarrClient",
        label: str,
        release_title: str,
        best_result: dict,
    ) -> bool:
        """
        Push the release by downloadUrl, bypassing the guid cache. Best-effort:
        skips cleanly if the original result dict lacks required fields.
        """
        download_url = best_result.get("downloadUrl") or best_result.get("download_url")
        protocol = best_result.get("protocol")
        publish_date = best_result.get("publishDate") or best_result.get("publish_date") or ""
        indexer_name = best_result.get("indexer") or ""

        if not download_url or not protocol:
            logger.error(
                f"Push fallback for {release_title} not possible "
                f"(missing downloadUrl or protocol)"
            )
            return False

        logger.info(f"Push fallback via {label}: {release_title}")
        return client.push_release(
            title=release_title,
            download_url=download_url,
            protocol=protocol,
            publish_date=publish_date,
            indexer=indexer_name,
        )

    def execute_all(
        self, results: list[AnalysisResult], dry_run: bool = True
    ) -> dict:
        """Execute all replacements. Returns summary dict."""
        needs_replacement = [r for r in results if r.status == "needs_replacement"]

        logger.info(
            f"Executing {'DRY RUN ' if dry_run else ''}"
            f"replacements for {len(needs_replacement)} items"
        )

        # Reset per-batch grab telemetry.
        self.reset_grab_stats()

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

        # Flush deferred skip-tracker writes from unrecoverable grabs.
        try:
            self.skip_tracker.flush()
        except Exception as e:
            logger.warning(f"Could not flush skip tracker: {e}")

        stats = dict(self._grab_stats)
        summary = {
            "total": len(needs_replacement),
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "dry_run": dry_run,
            "grab_stats": stats,
        }

        logger.info(
            f"Execution complete: {success} succeeded, "
            f"{failed} failed, {skipped} skipped"
        )
        if not dry_run and any(stats.values()):
            logger.info(
                f"Grab telemetry: direct={stats['grabbed_direct']}, "
                f"stale_recovered={stats['stale_recovered']}, "
                f"push_fallback={stats['push_fallback_used']}, "
                f"unrecoverable={stats['failed_unrecoverable']}"
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
