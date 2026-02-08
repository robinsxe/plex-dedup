"""
Plex API client for scanning libraries and finding duplicate movies and episodes.
"""

import logging
from dataclasses import dataclass, field
from plexapi.server import PlexServer
from plexapi.video import Movie, Show, Episode

logger = logging.getLogger(__name__)


@dataclass
class MediaFile:
    """Represents a single file/version of a media item in Plex."""
    title: str
    year: int | None
    plex_rating_key: str
    file_path: str
    file_size: int
    resolution: str
    video_codec: str
    audio_codec: str
    bitrate: int
    duration: int
    added_at: str
    container: str
    media_id: int

    # TV-specific fields
    show_title: str = ""
    season_number: int | None = None
    episode_number: int | None = None
    media_type: str = "movie"  # "movie" or "episode"

    @property
    def file_size_gb(self) -> float:
        return round(self.file_size / (1024 ** 3), 2)

    @property
    def quality_label(self) -> str:
        parts = []
        if self.resolution:
            parts.append(self.resolution)
        if self.video_codec:
            parts.append(self.video_codec)
        if self.audio_codec:
            parts.append(self.audio_codec)
        return " / ".join(parts) if parts else "Unknown"

    @property
    def display_title(self) -> str:
        if self.media_type == "episode" and self.show_title:
            return (
                f"{self.show_title} - S{self.season_number:02d}E{self.episode_number:02d}"
                f" - {self.title}"
            )
        return f"{self.title} ({self.year})" if self.year else self.title

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "year": self.year,
            "plex_rating_key": self.plex_rating_key,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "file_size_gb": self.file_size_gb,
            "resolution": self.resolution,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "bitrate": self.bitrate,
            "duration": self.duration,
            "added_at": self.added_at,
            "container": self.container,
            "quality_label": self.quality_label,
            "media_id": self.media_id,
            "show_title": self.show_title,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "media_type": self.media_type,
            "display_title": self.display_title,
        }


@dataclass
class DuplicateGroup:
    """A group of duplicate files for the same media item."""
    title: str
    year: int | None
    plex_rating_key: str
    files: list[MediaFile]
    tmdb_id: str | None = None
    imdb_id: str | None = None
    tvdb_id: str | None = None
    media_type: str = "movie"

    # TV-specific
    show_title: str = ""
    season_number: int | None = None
    episode_number: int | None = None

    @property
    def display_title(self) -> str:
        if self.media_type == "episode" and self.show_title:
            return (
                f"{self.show_title} - S{self.season_number:02d}E{self.episode_number:02d}"
                f" - {self.title}"
            )
        return f"{self.title} ({self.year})" if self.year else self.title

    @property
    def total_size(self) -> int:
        return sum(f.file_size for f in self.files)

    @property
    def wasted_space(self) -> int:
        if len(self.files) <= 1:
            return 0
        sorted_files = sorted(self.files, key=lambda f: f.file_size, reverse=True)
        return sum(f.file_size for f in sorted_files[1:])

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "display_title": self.display_title,
            "year": self.year,
            "plex_rating_key": self.plex_rating_key,
            "tmdb_id": self.tmdb_id,
            "imdb_id": self.imdb_id,
            "tvdb_id": self.tvdb_id,
            "media_type": self.media_type,
            "show_title": self.show_title,
            "season_number": self.season_number,
            "episode_number": self.episode_number,
            "file_count": len(self.files),
            "total_size": self.total_size,
            "wasted_space": self.wasted_space,
            "wasted_space_gb": round(self.wasted_space / (1024 ** 3), 2),
            "files": [f.to_dict() for f in self.files],
        }


class PlexClient:
    """Client for interacting with Plex Media Server."""

    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self._server = None

    def connect(self) -> bool:
        try:
            self._server = PlexServer(self.url, self.token)
            logger.info(f"Connected to Plex: {self._server.friendlyName}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Plex: {e}")
            return False

    @property
    def server(self) -> PlexServer:
        if self._server is None:
            self.connect()
        return self._server

    def get_library(self, library_name: str):
        try:
            return self.server.library.section(library_name)
        except Exception as e:
            logger.error(f"Library '{library_name}' not found: {e}")
            available = [s.title for s in self.server.library.sections()]
            logger.info(f"Available libraries: {available}")
            raise

    def get_all_libraries(self) -> list[dict]:
        """Get all library names with their types."""
        return [
            {"name": s.title, "type": s.type}
            for s in self.server.library.sections()
            if s.type in ("movie", "show")
        ]

    def get_movie_libraries(self) -> list[str]:
        return [s.title for s in self.server.library.sections() if s.type == "movie"]

    def get_tv_libraries(self) -> list[str]:
        return [s.title for s in self.server.library.sections() if s.type == "show"]

    def _extract_media_file(self, item, media, part, media_type="movie",
                            show_title="", season_num=None, episode_num=None) -> MediaFile:
        """Extract a MediaFile from a Plex media part."""
        video_codec = ""
        audio_codec = ""
        resolution = ""

        for stream in part.streams:
            if stream.streamType == 1:
                video_codec = getattr(stream, "codec", "") or ""
                resolution = getattr(media, "videoResolution", "") or ""
                if resolution:
                    resolution = f"{resolution}p" if resolution.isdigit() else resolution
            elif stream.streamType == 2:
                if not audio_codec:
                    audio_codec = getattr(stream, "codec", "") or ""

        return MediaFile(
            title=item.title,
            year=getattr(item, "year", None),
            plex_rating_key=str(item.ratingKey),
            file_path=part.file or "",
            file_size=part.size or 0,
            resolution=resolution,
            video_codec=video_codec.upper(),
            audio_codec=audio_codec.upper(),
            bitrate=media.bitrate or 0,
            duration=item.duration or 0,
            added_at=str(item.addedAt) if item.addedAt else "",
            container=media.container or "",
            media_id=media.id,
            show_title=show_title,
            season_number=season_num,
            episode_number=episode_num,
            media_type=media_type,
        )

    def _get_guids(self, item) -> dict:
        """Extract external IDs from a Plex item."""
        ids = {"tmdb": None, "imdb": None, "tvdb": None}
        try:
            for guid in item.guids:
                if "tmdb://" in guid.id:
                    ids["tmdb"] = guid.id.replace("tmdb://", "")
                elif "imdb://" in guid.id:
                    ids["imdb"] = guid.id.replace("imdb://", "")
                elif "tvdb://" in guid.id:
                    ids["tvdb"] = guid.id.replace("tvdb://", "")
        except Exception:
            pass
        return ids

    def find_movie_duplicates(self, library_name: str) -> list[DuplicateGroup]:
        """Scan a Plex movie library and find all movies with multiple files."""
        library = self.get_library(library_name)
        logger.info(f"Scanning movie library: {library_name}")

        duplicates = []
        all_movies = library.all()
        logger.info(f"Found {len(all_movies)} movies in library")

        for movie in all_movies:
            files = []
            for media in movie.media:
                for part in media.parts:
                    try:
                        mf = self._extract_media_file(movie, media, part)
                        files.append(mf)
                    except Exception as e:
                        logger.warning(f"Error processing {movie.title}: {e}")

            if len(files) > 1:
                ids = self._get_guids(movie)
                group = DuplicateGroup(
                    title=movie.title,
                    year=movie.year,
                    plex_rating_key=str(movie.ratingKey),
                    files=files,
                    tmdb_id=ids["tmdb"],
                    imdb_id=ids["imdb"],
                    media_type="movie",
                )
                duplicates.append(group)
                logger.info(f"  Duplicate movie: {movie.title} ({movie.year}) - {len(files)} versions")

        logger.info(f"Found {len(duplicates)} movies with duplicates")
        return duplicates

    def find_episode_duplicates(self, library_name: str) -> list[DuplicateGroup]:
        """Scan a Plex TV library and find all episodes with multiple files."""
        library = self.get_library(library_name)
        logger.info(f"Scanning TV library: {library_name}")

        duplicates = []
        all_shows = library.all()
        logger.info(f"Found {len(all_shows)} shows in library")

        for show in all_shows:
            show_ids = self._get_guids(show)

            try:
                episodes = show.episodes()
            except Exception as e:
                logger.warning(f"Error fetching episodes for {show.title}: {e}")
                continue

            for episode in episodes:
                files = []
                for media in episode.media:
                    for part in media.parts:
                        try:
                            mf = self._extract_media_file(
                                episode, media, part,
                                media_type="episode",
                                show_title=show.title,
                                season_num=episode.parentIndex,
                                episode_num=episode.index,
                            )
                            files.append(mf)
                        except Exception as e:
                            logger.warning(
                                f"Error processing {show.title} "
                                f"S{episode.parentIndex}E{episode.index}: {e}"
                            )

                if len(files) > 1:
                    ep_ids = self._get_guids(episode)
                    group = DuplicateGroup(
                        title=episode.title,
                        year=getattr(episode, "year", None),
                        plex_rating_key=str(episode.ratingKey),
                        files=files,
                        tvdb_id=show_ids.get("tvdb") or ep_ids.get("tvdb"),
                        imdb_id=ep_ids.get("imdb"),
                        media_type="episode",
                        show_title=show.title,
                        season_number=episode.parentIndex,
                        episode_number=episode.index,
                    )
                    duplicates.append(group)
                    logger.info(
                        f"  Duplicate episode: {show.title} "
                        f"S{episode.parentIndex:02d}E{episode.index:02d} "
                        f"- {len(files)} versions"
                    )

        logger.info(f"Found {len(duplicates)} episodes with duplicates")
        return duplicates

    def find_duplicates(self, library_name: str, library_type: str = "movie") -> list[DuplicateGroup]:
        """Find duplicates in a library (auto-detects type)."""
        if library_type == "show":
            return self.find_episode_duplicates(library_name)
        return self.find_movie_duplicates(library_name)

    def get_all_media_files(self, library_name: str, library_type: str = "movie") -> list[dict]:
        """Get all media files with their paths for subtitle scanning."""
        library = self.get_library(library_name)
        results = []

        if library_type == "movie":
            for movie in library.all():
                ids = self._get_guids(movie)
                for media in movie.media:
                    for part in media.parts:
                        results.append({
                            "title": movie.title,
                            "year": movie.year,
                            "file_path": part.file or "",
                            "rating_key": str(movie.ratingKey),
                            "imdb_id": ids.get("imdb"),
                            "tmdb_id": ids.get("tmdb"),
                            "media_type": "movie",
                            "duration": movie.duration,
                            "has_swedish_sub": self._has_subtitle(part, "sv"),
                        })
        else:
            for show in library.all():
                show_ids = self._get_guids(show)
                try:
                    for episode in show.episodes():
                        ep_ids = self._get_guids(episode)
                        for media in episode.media:
                            for part in media.parts:
                                results.append({
                                    "title": episode.title,
                                    "show_title": show.title,
                                    "season_number": episode.parentIndex,
                                    "episode_number": episode.index,
                                    "year": getattr(episode, "year", None),
                                    "file_path": part.file or "",
                                    "rating_key": str(episode.ratingKey),
                                    "imdb_id": show_ids.get("imdb") or ep_ids.get("imdb"),
                                    "tvdb_id": show_ids.get("tvdb"),
                                    "media_type": "episode",
                                    "duration": episode.duration,
                                    "has_swedish_sub": self._has_subtitle(part, "sv"),
                                })
                except Exception as e:
                    logger.warning(f"Error processing show {show.title}: {e}")

        return results

    def _has_subtitle(self, part, language_code: str) -> bool:
        """Check if a media part already has a subtitle in the given language."""
        try:
            for stream in part.streams:
                if stream.streamType == 3:  # Subtitle stream
                    lang = getattr(stream, "languageCode", "") or ""
                    if lang.lower().startswith(language_code.lower()):
                        return True
        except Exception:
            pass
        return False

    def delete_media(self, rating_key: str, media_id: int) -> bool:
        try:
            url = f"{self.url}/library/metadata/{rating_key}/media/{media_id}"
            response = self.server._session.delete(
                url, headers={"X-Plex-Token": self.token},
            )
            if response.status_code in (200, 204):
                logger.info(f"Deleted media {media_id} from {rating_key}")
                return True
            else:
                logger.error(f"Failed to delete media {media_id}: HTTP {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Error deleting media {media_id}: {e}")
            return False

    def refresh_library(self, library_name: str):
        try:
            library = self.get_library(library_name)
            library.update()
            logger.info(f"Triggered library refresh for: {library_name}")
        except Exception as e:
            logger.error(f"Failed to refresh library: {e}")
