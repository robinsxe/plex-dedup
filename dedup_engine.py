"""
Dedup engine: coordinates Plex scanning, quality comparison,
Radarr/Sonarr unmonitoring, and file deletion.
"""

import os
import re
import shutil
import logging
from dataclasses import dataclass

from config import Config
from plex_client import PlexClient, DuplicateGroup, MediaFile
from radarr_client import RadarrClient
from sonarr_client import SonarrClient

logger = logging.getLogger(__name__)


@dataclass
class DeduplicationPlan:
    """A plan for what to keep and what to remove for a duplicate group."""
    group: DuplicateGroup
    keep: MediaFile
    remove: list[MediaFile]
    reason: str
    arr_entry: dict | None = None  # Radarr movie or Sonarr series
    arr_episode: dict | None = None  # Sonarr episode (for TV)
    executed: bool = False
    error: str | None = None

    @property
    def space_saved(self) -> int:
        return sum(f.file_size for f in self.remove)

    @property
    def space_saved_gb(self) -> float:
        return round(self.space_saved / (1024 ** 3), 2)

    def to_dict(self) -> dict:
        is_monitored = None
        if self.group.media_type == "movie" and self.arr_entry:
            is_monitored = self.arr_entry.get("monitored", False)
        elif self.group.media_type == "episode" and self.arr_episode:
            is_monitored = self.arr_episode.get("monitored", False)

        return {
            "title": self.group.title,
            "display_title": self.group.display_title,
            "year": self.group.year,
            "plex_rating_key": self.group.plex_rating_key,
            "tmdb_id": self.group.tmdb_id,
            "imdb_id": self.group.imdb_id,
            "tvdb_id": self.group.tvdb_id,
            "media_type": self.group.media_type,
            "show_title": self.group.show_title,
            "season_number": self.group.season_number,
            "episode_number": self.group.episode_number,
            "keep": self.keep.to_dict(),
            "remove": [f.to_dict() for f in self.remove],
            "reason": self.reason,
            "space_saved": self.space_saved,
            "space_saved_gb": self.space_saved_gb,
            "arr_found": self.arr_entry is not None,
            "arr_monitored": is_monitored,
            "executed": self.executed,
            "error": self.error,
            "file_count": len(self.group.files),
        }


class DedupEngine:
    """Main engine for finding and resolving duplicate media."""

    def __init__(self, config: Config):
        self.config = config
        self.plex = PlexClient(config.plex_url, config.plex_token)
        self.radarr = RadarrClient(config.radarr_url, config.radarr_api_key)
        self.sonarr = SonarrClient(config.sonarr_url, config.sonarr_api_key)
        self._plans: list[DeduplicationPlan] = []

    def test_connections(self) -> dict:
        results = {
            "plex": self.plex.connect(),
            "radarr": self.radarr.test_connection(),
            "sonarr": self.sonarr.test_connection(),
        }
        if results["plex"]:
            results["libraries"] = self.plex.get_all_libraries()
        return results

    # ------------------------------------------------------------------ #
    # Quality scoring
    # ------------------------------------------------------------------ #

    def _parse_resolution_rank(self, resolution: str) -> int:
        res = resolution.lower().replace("p", "")
        mapping = {"4k": 2160, "2160": 2160, "1080": 1080, "720": 720,
                    "576": 576, "480": 480, "sd": 480}
        return mapping.get(res, 0)

    def _parse_source_from_path(self, file_path: str) -> str:
        path_lower = file_path.lower()
        patterns = {
            "remux": r"remux",
            "bluray": r"blu[\-\.]?ray|bdremux",
            "webdl": r"web[\-\.]?dl|webdl",
            "webrip": r"webrip|web[\-\.]?rip",
            "hdtv": r"hdtv",
            "dvd": r"dvd|dvdrip",
            "sdtv": r"sdtv|tvrip",
        }
        for source, pattern in patterns.items():
            if re.search(pattern, path_lower):
                return source
        return "unknown"

    def _score_file(self, media_file: MediaFile) -> float:
        score = 0.0

        res_rank = self._parse_resolution_rank(media_file.resolution)
        score += (res_rank / 2160) * 100

        source = self._parse_source_from_path(media_file.file_path)
        res_label = media_file.resolution.lower().replace("p", "")
        quality_key = f"{source}-{res_label}p" if res_label.isdigit() else source
        source_score = self.config.quality_ranks.get(quality_key, 0)
        score += source_score * 0.5

        if media_file.bitrate > 0:
            score += min(media_file.bitrate / 40000, 1.0) * 30

        codec = media_file.video_codec.upper()
        if codec in ("HEVC", "H265", "X265"):
            score += 10
        elif codec in ("AV1",):
            score += 12
        elif codec in ("H264", "X264", "AVC"):
            score += 5

        audio = media_file.audio_codec.upper()
        if "TRUEHD" in audio or "ATMOS" in audio:
            score += 10
        elif "DTS-HD" in audio or "DTS:X" in audio:
            score += 8
        elif "FLAC" in audio or "PCM" in audio:
            score += 7
        elif "DTS" in audio:
            score += 5
        elif "EAC3" in audio or "DD+" in audio:
            score += 4
        elif "AC3" in audio or "DD" in audio:
            score += 3
        elif "AAC" in audio:
            score += 2

        return round(score, 2)

    # ------------------------------------------------------------------ #
    # Keep strategies
    # ------------------------------------------------------------------ #

    def _pick_best_quality(self, files: list[MediaFile]) -> tuple[MediaFile, str]:
        scored = [(f, self._score_file(f)) for f in files]
        scored.sort(key=lambda x: x[1], reverse=True)
        best = scored[0]
        return best[0], f"Highest quality score: {best[1]}"

    def _pick_largest(self, files: list[MediaFile]) -> tuple[MediaFile, str]:
        largest = max(files, key=lambda f: f.file_size)
        return largest, f"Largest file: {largest.file_size_gb} GB"

    def _pick_newest(self, files: list[MediaFile]) -> tuple[MediaFile, str]:
        newest = max(files, key=lambda f: f.added_at)
        return newest, f"Most recently added: {newest.added_at}"

    def pick_keeper(self, files: list[MediaFile]) -> tuple[MediaFile, str]:
        strategy = self.config.keep_strategy
        if strategy == "best_quality":
            return self._pick_best_quality(files)
        elif strategy == "largest_file":
            return self._pick_largest(files)
        elif strategy == "newest":
            return self._pick_newest(files)
        return self._pick_best_quality(files)

    # ------------------------------------------------------------------ #
    # Scan movies
    # ------------------------------------------------------------------ #

    def _build_radarr_index(self) -> dict:
        """Build lookup index for Radarr movies."""
        index = {}
        try:
            for movie in self.radarr.get_all_movies():
                tmdb = str(movie.get("tmdbId", ""))
                imdb = movie.get("imdbId", "")
                if tmdb:
                    index[f"tmdb:{tmdb}"] = movie
                if imdb:
                    index[f"imdb:{imdb}"] = movie
                title_key = f"title:{movie.get('title', '').lower()}:{movie.get('year', '')}"
                index[title_key] = movie
        except Exception as e:
            logger.warning(f"Could not fetch Radarr movies: {e}")
        return index

    def _build_sonarr_index(self) -> dict:
        """Build lookup index for Sonarr series."""
        index = {}
        try:
            for series in self.sonarr.get_all_series():
                tvdb = str(series.get("tvdbId", ""))
                imdb = series.get("imdbId", "")
                if tvdb:
                    index[f"tvdb:{tvdb}"] = series
                if imdb:
                    index[f"imdb:{imdb}"] = series
                title_key = f"title:{series.get('title', '').lower()}"
                index[title_key] = series
        except Exception as e:
            logger.warning(f"Could not fetch Sonarr series: {e}")
        return index

    def _find_in_radarr(self, group: DuplicateGroup, index: dict) -> dict | None:
        if group.tmdb_id:
            entry = index.get(f"tmdb:{group.tmdb_id}")
            if entry:
                return entry
        if group.imdb_id:
            entry = index.get(f"imdb:{group.imdb_id}")
            if entry:
                return entry
        title_key = f"title:{group.title.lower()}:{group.year or ''}"
        return index.get(title_key)

    def _find_in_sonarr(self, group: DuplicateGroup, index: dict) -> tuple[dict | None, dict | None]:
        """Find series and episode in Sonarr for an episode duplicate group."""
        series = None
        episode = None

        if group.tvdb_id:
            series = index.get(f"tvdb:{group.tvdb_id}")
        if not series and group.imdb_id:
            series = index.get(f"imdb:{group.imdb_id}")
        if not series and group.show_title:
            series = index.get(f"title:{group.show_title.lower()}")

        if series and group.season_number is not None and group.episode_number is not None:
            episode = self.sonarr.find_episode(
                series["id"], group.season_number, group.episode_number
            )

        return series, episode

    # ------------------------------------------------------------------ #
    # Scan
    # ------------------------------------------------------------------ #

    def scan_movies(self, library_name: str = None) -> list[DeduplicationPlan]:
        lib = library_name or self.config.plex_movie_library
        logger.info(f"Scanning movie library: {lib}")

        duplicates = self.plex.find_movie_duplicates(lib)
        radarr_index = self._build_radarr_index()
        plans = []

        for group in duplicates:
            keeper, reason = self.pick_keeper(group.files)
            to_remove = [f for f in group.files if f is not keeper]
            radarr_movie = self._find_in_radarr(group, radarr_index)

            plan = DeduplicationPlan(
                group=group,
                keep=keeper,
                remove=to_remove,
                reason=reason,
                arr_entry=radarr_movie,
            )
            plans.append(plan)

        return plans

    def scan_episodes(self, library_name: str = None) -> list[DeduplicationPlan]:
        lib = library_name or self.config.plex_tv_library
        logger.info(f"Scanning TV library: {lib}")

        duplicates = self.plex.find_episode_duplicates(lib)
        sonarr_index = self._build_sonarr_index()
        plans = []

        for group in duplicates:
            keeper, reason = self.pick_keeper(group.files)
            to_remove = [f for f in group.files if f is not keeper]
            series, episode = self._find_in_sonarr(group, sonarr_index)

            plan = DeduplicationPlan(
                group=group,
                keep=keeper,
                remove=to_remove,
                reason=reason,
                arr_entry=series,
                arr_episode=episode,
            )
            plans.append(plan)

        return plans

    def scan(self, library_name: str = None, media_type: str = "movie") -> list[DeduplicationPlan]:
        if media_type == "show":
            plans = self.scan_episodes(library_name)
        else:
            plans = self.scan_movies(library_name)

        self._plans = plans
        total_saved = sum(p.space_saved for p in plans)
        total_saved_gb = round(total_saved / (1024 ** 3), 2)
        logger.info(f"Created {len(plans)} dedup plans. Potential savings: {total_saved_gb} GB")
        return plans

    def scan_all(self) -> list[DeduplicationPlan]:
        """Scan both movie and TV libraries."""
        all_plans = []
        try:
            all_plans.extend(self.scan_movies())
        except Exception as e:
            logger.error(f"Movie scan failed: {e}")
        try:
            all_plans.extend(self.scan_episodes())
        except Exception as e:
            logger.error(f"TV scan failed: {e}")
        self._plans = all_plans
        return all_plans

    # ------------------------------------------------------------------ #
    # Execute
    # ------------------------------------------------------------------ #

    def execute_plan(self, plan: DeduplicationPlan) -> bool:
        title = plan.group.display_title

        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would process: {title}")
            logger.info(f"  Keep: {plan.keep.file_path}")
            for f in plan.remove:
                logger.info(f"  Remove: {f.file_path}")
            plan.executed = True
            return True

        try:
            # Step 1: Unmonitor in Radarr/Sonarr
            if self.config.auto_unmonitor:
                if plan.group.media_type == "movie" and plan.arr_entry:
                    logger.info(f"Unmonitoring in Radarr: {title}")
                    self.radarr.unmonitor_movie(plan.arr_entry)
                elif plan.group.media_type == "episode" and plan.arr_episode:
                    logger.info(f"Unmonitoring episode in Sonarr: {title}")
                    self.sonarr.unmonitor_episode(plan.arr_episode)

            # Step 2: Delete duplicate files
            for remove_file in plan.remove:
                logger.info(f"Removing: {remove_file.file_path}")

                if self.config.delete_files:
                    deleted = self.plex.delete_media(
                        remove_file.plex_rating_key, remove_file.media_id,
                    )

                    if not deleted:
                        if self.config.recycle_bin:
                            dest = os.path.join(
                                self.config.recycle_bin,
                                os.path.basename(remove_file.file_path),
                            )
                            shutil.move(remove_file.file_path, dest)
                            logger.info(f"  Moved to recycle bin: {dest}")
                        elif os.path.exists(remove_file.file_path):
                            os.remove(remove_file.file_path)
                            logger.info(f"  Deleted from disk")

                            parent = os.path.dirname(remove_file.file_path)
                            if os.path.isdir(parent) and not os.listdir(parent):
                                os.rmdir(parent)
                                logger.info(f"  Removed empty directory: {parent}")

            plan.executed = True
            return True

        except Exception as e:
            plan.error = str(e)
            logger.error(f"Failed to execute plan for {title}: {e}")
            return False

    def execute_all(self, plans: list[DeduplicationPlan] = None):
        plans = plans or self._plans
        success = 0
        failed = 0

        for plan in plans:
            if self.execute_plan(plan):
                success += 1
            else:
                failed += 1

        if not self.config.dry_run and success > 0:
            logger.info("Refreshing Plex libraries...")
            try:
                self.plex.refresh_library(self.config.plex_movie_library)
            except Exception:
                pass
            try:
                self.plex.refresh_library(self.config.plex_tv_library)
            except Exception:
                pass

        logger.info(f"Execution complete: {success} succeeded, {failed} failed")
        return {"success": success, "failed": failed}

    def get_summary(self, plans: list[DeduplicationPlan] = None) -> dict:
        plans = plans or self._plans
        movie_plans = [p for p in plans if p.group.media_type == "movie"]
        tv_plans = [p for p in plans if p.group.media_type == "episode"]
        total_saved = sum(p.space_saved for p in plans)

        return {
            "total_duplicates": len(plans),
            "movie_duplicates": len(movie_plans),
            "episode_duplicates": len(tv_plans),
            "total_files_to_remove": sum(len(p.remove) for p in plans),
            "total_space_saved_bytes": total_saved,
            "total_space_saved_gb": round(total_saved / (1024 ** 3), 2),
            "arr_found": sum(1 for p in plans if p.arr_entry),
            "arr_not_found": sum(1 for p in plans if not p.arr_entry),
            "dry_run": self.config.dry_run,
            "keep_strategy": self.config.keep_strategy,
        }
