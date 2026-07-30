"""
Shared data model and release-name utilities for the convert pipeline.
Lives in its own module so the analysis and grabber mixins, the assembled
LibraryAnalyzer, and dedup_engine can all import it without cycles.
"""

import re
from dataclasses import dataclass, field

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

    # For replacement — release candidates from the Radarr/Sonarr interactive
    # search (indexers synced from Prowlarr)
    recommended_release: str | None  # Best release to grab
    indexer_results: list[dict] = field(default_factory=list)

    # IDs for arr integration
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tvdb_id: str | None = None
    rating_key: str = ""
    # Show-level IMDB id for episodes (imdb_id is the episode's own, if any)
    show_imdb_id: str | None = None

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
            "indexer_results": self.indexer_results,
            "imdb_id": self.imdb_id,
            "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id,
            "rating_key": self.rating_key,
            "show_imdb_id": self.show_imdb_id,
            "show_title": self.show_title,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "episode_id": self.episode_id,
            "status": self.status,
            "error": self.error,
        }
