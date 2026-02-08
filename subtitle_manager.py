"""
Subtitle manager: scans Plex libraries for media missing subtitles
and downloads them from OpenSubtitles.
"""

import os
import logging
from dataclasses import dataclass, field

from config import Config
from plex_client import PlexClient
from opensubtitles_client import OpenSubtitlesClient

logger = logging.getLogger(__name__)


@dataclass
class SubtitleResult:
    """Result of processing a single media item for subtitles."""
    title: str
    display_title: str
    file_path: str
    media_type: str
    languages: dict = field(default_factory=dict)  # lang -> status dict

    @property
    def any_downloaded(self) -> bool:
        return any(
            r.get("status") == "downloaded" for r in self.languages.values()
        )

    @property
    def any_found(self) -> bool:
        return any(
            r.get("status") in ("found", "downloaded") for r in self.languages.values()
        )

    @property
    def all_exist(self) -> bool:
        return all(
            r.get("status") == "exists" for r in self.languages.values()
        )

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "display_title": self.display_title,
            "file_path": self.file_path,
            "media_type": self.media_type,
            "languages": self.languages,
            "any_downloaded": self.any_downloaded,
            "any_found": self.any_found,
            "all_exist": self.all_exist,
        }


class SubtitleManager:
    """Manages subtitle scanning and downloading for Plex libraries."""

    def __init__(self, config: Config):
        self.config = config
        self.plex = PlexClient(config.plex_url, config.plex_token)
        self.opensubs = OpenSubtitlesClient(
            api_key=config.opensubtitles_api_key,
            username=config.opensubtitles_username,
            password=config.opensubtitles_password,
        )
        self._results: list[SubtitleResult] = []

    def test_connections(self) -> dict:
        """Test connections to Plex and OpenSubtitles."""
        plex_ok = self.plex.connect()
        opensubs_ok = self.opensubs.test_connection()
        login_ok = False
        if opensubs_ok:
            login_ok = self.opensubs.login()
        return {
            "plex": plex_ok,
            "opensubtitles": opensubs_ok,
            "opensubtitles_login": login_ok,
        }

    def scan_missing_subtitles(
        self,
        library_name: str,
        library_type: str = "movie",
        languages: list[str] = None,
    ) -> list[dict]:
        """
        Scan a Plex library and find media files missing subtitles
        for the configured languages.
        """
        langs = languages or self.config.subtitle_languages
        logger.info(
            f"Scanning '{library_name}' for missing subtitles: "
            f"{', '.join(langs)}"
        )

        media_files = self.plex.get_all_media_files(library_name, library_type)
        missing = []

        for item in media_files:
            file_path = item["file_path"]
            if not file_path:
                continue

            missing_langs = []
            for lang in langs:
                sub_path = self.opensubs.get_subtitle_output_path(file_path, lang)
                if not os.path.exists(sub_path):
                    # Also check if Plex reports the subtitle
                    if lang == "sv" and item.get("has_swedish_sub"):
                        continue
                    missing_langs.append(lang)

            if missing_langs:
                item["missing_languages"] = missing_langs
                missing.append(item)

        logger.info(
            f"Found {len(missing)} items missing subtitles "
            f"out of {len(media_files)} total"
        )
        return missing

    def download_subtitles(
        self,
        library_name: str,
        library_type: str = "movie",
        languages: list[str] = None,
        dry_run: bool = None,
        limit: int = 0,
    ) -> list[SubtitleResult]:
        """
        Scan library and download missing subtitles.

        Args:
            library_name: Plex library to scan.
            library_type: "movie" or "show".
            languages: Language codes to download (default from config).
            dry_run: Override config dry_run setting.
            limit: Max number of items to process (0 = unlimited).

        Returns:
            List of SubtitleResult for processed items.
        """
        langs = languages or self.config.subtitle_languages
        is_dry_run = dry_run if dry_run is not None else self.config.dry_run

        missing_items = self.scan_missing_subtitles(
            library_name, library_type, langs
        )

        if limit > 0:
            missing_items = missing_items[:limit]

        results = []
        total = len(missing_items)

        for i, item in enumerate(missing_items, 1):
            if item["media_type"] == "episode":
                display = (
                    f"{item.get('show_title', '')} "
                    f"S{item.get('season_number', 0):02d}"
                    f"E{item.get('episode_number', 0):02d}"
                )
            else:
                display = f"{item['title']} ({item.get('year', '')})"

            logger.info(f"[{i}/{total}] Processing: {display}")

            sub_result = self.opensubs.process_media_item(
                file_path=item["file_path"],
                languages=item.get("missing_languages", langs),
                imdb_id=item.get("imdb_id"),
                tmdb_id=item.get("tmdb_id"),
                media_type=item["media_type"],
                season_number=item.get("season_number"),
                episode_number=item.get("episode_number"),
                title=item.get("show_title") or item.get("title"),
                dry_run=is_dry_run,
            )

            result = SubtitleResult(
                title=item.get("title", ""),
                display_title=display,
                file_path=item["file_path"],
                media_type=item["media_type"],
                languages=sub_result,
            )
            results.append(result)

        self._results = results

        # Summary
        downloaded = sum(1 for r in results if r.any_downloaded)
        found = sum(1 for r in results if r.any_found)
        not_found = sum(1 for r in results if not r.any_found and not r.all_exist)

        logger.info(
            f"Subtitle sync complete: "
            f"{downloaded} downloaded, {found} found, "
            f"{not_found} not available"
        )

        # Refresh Plex if we downloaded anything
        if not is_dry_run and downloaded > 0:
            logger.info("Refreshing Plex library to pick up new subtitles...")
            self.plex.refresh_library(library_name)

        return results

    def get_summary(self, results: list[SubtitleResult] = None) -> dict:
        """Get summary statistics."""
        results = results or self._results
        total = len(results)
        downloaded = 0
        found = 0
        not_found = 0
        already_exist = 0

        for r in results:
            for lang, info in r.languages.items():
                status = info.get("status", "")
                if status == "downloaded":
                    downloaded += 1
                elif status == "found":
                    found += 1
                elif status == "exists":
                    already_exist += 1
                elif status == "not_found":
                    not_found += 1

        return {
            "total_items_processed": total,
            "subtitles_downloaded": downloaded,
            "subtitles_found_dry_run": found,
            "subtitles_not_available": not_found,
            "subtitles_already_exist": already_exist,
            "languages": self.config.subtitle_languages,
        }
