"""
Configuration management for Plex Dedup.
Loads settings from .env file or environment variables.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Plex settings
    plex_url: str = ""
    plex_token: str = ""
    plex_movie_library: str = "Movies"
    plex_tv_library: str = "TV Shows"

    # Radarr settings
    radarr_url: str = ""
    radarr_api_key: str = ""

    # Sonarr settings
    sonarr_url: str = ""
    sonarr_api_key: str = ""

    # OpenSubtitles settings
    opensubtitles_api_key: str = ""
    opensubtitles_username: str = ""
    opensubtitles_password: str = ""
    subtitle_languages: list = field(default_factory=lambda: ["sv", "en"])
    subtitle_auto_download: bool = True

    # Behavior settings
    dry_run: bool = True
    keep_strategy: str = "best_quality"
    auto_unmonitor: bool = True
    delete_files: bool = True
    recycle_bin: str = ""

    # Scheduling
    schedule_enabled: bool = False
    schedule_cron_hour: int = 3
    schedule_cron_minute: int = 0
    schedule_cron_day_of_week: str = "sun"

    # Quality ranking (higher = better)
    quality_ranks: dict = field(default_factory=lambda: {
        "remux-2160p": 100,
        "bluray-2160p": 95,
        "webdl-2160p": 90,
        "webrip-2160p": 85,
        "remux-1080p": 80,
        "bluray-1080p": 75,
        "webdl-1080p": 70,
        "webrip-1080p": 65,
        "bluray-720p": 50,
        "webdl-720p": 45,
        "webrip-720p": 40,
        "hdtv-1080p": 35,
        "hdtv-720p": 30,
        "dvd": 20,
        "sdtv": 10,
        "unknown": 0,
    })

    # Web UI
    web_host: str = "0.0.0.0"
    web_port: int = 8585

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        langs = os.getenv("SUBTITLE_LANGUAGES", "sv,en")
        lang_list = [l.strip() for l in langs.split(",") if l.strip()]

        return cls(
            plex_url=os.getenv("PLEX_URL", "http://localhost:32400"),
            plex_token=os.getenv("PLEX_TOKEN", ""),
            plex_movie_library=os.getenv("PLEX_MOVIE_LIBRARY", os.getenv("PLEX_LIBRARY_NAME", "Movies")),
            plex_tv_library=os.getenv("PLEX_TV_LIBRARY", "TV Shows"),
            radarr_url=os.getenv("RADARR_URL", "http://localhost:7878"),
            radarr_api_key=os.getenv("RADARR_API_KEY", ""),
            sonarr_url=os.getenv("SONARR_URL", "http://localhost:8989"),
            sonarr_api_key=os.getenv("SONARR_API_KEY", ""),
            opensubtitles_api_key=os.getenv("OPENSUBTITLES_API_KEY", ""),
            opensubtitles_username=os.getenv("OPENSUBTITLES_USERNAME", ""),
            opensubtitles_password=os.getenv("OPENSUBTITLES_PASSWORD", ""),
            subtitle_languages=lang_list,
            subtitle_auto_download=os.getenv("SUBTITLE_AUTO_DOWNLOAD", "true").lower() == "true",
            dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
            keep_strategy=os.getenv("KEEP_STRATEGY", "best_quality"),
            auto_unmonitor=os.getenv("AUTO_UNMONITOR", "true").lower() == "true",
            delete_files=os.getenv("DELETE_FILES", "true").lower() == "true",
            recycle_bin=os.getenv("RECYCLE_BIN", ""),
            schedule_enabled=os.getenv("SCHEDULE_ENABLED", "false").lower() == "true",
            schedule_cron_hour=int(os.getenv("SCHEDULE_CRON_HOUR", "3")),
            schedule_cron_minute=int(os.getenv("SCHEDULE_CRON_MINUTE", "0")),
            schedule_cron_day_of_week=os.getenv("SCHEDULE_CRON_DAY_OF_WEEK", "sun"),
            web_host=os.getenv("WEB_HOST", "0.0.0.0"),
            web_port=int(os.getenv("WEB_PORT", "8585")),
        )

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors."""
        errors = []
        if not self.plex_url:
            errors.append("PLEX_URL is required")
        if not self.plex_token:
            errors.append("PLEX_TOKEN is required")
        if self.keep_strategy not in ("best_quality", "largest_file", "newest"):
            errors.append(
                f"Invalid KEEP_STRATEGY: {self.keep_strategy}. "
                "Must be best_quality, largest_file, or newest"
            )
        return errors

    def validate_radarr(self) -> list[str]:
        errors = []
        if not self.radarr_url:
            errors.append("RADARR_URL is required for movie dedup")
        if not self.radarr_api_key:
            errors.append("RADARR_API_KEY is required for movie dedup")
        return errors

    def validate_sonarr(self) -> list[str]:
        errors = []
        if not self.sonarr_url:
            errors.append("SONARR_URL is required for TV dedup")
        if not self.sonarr_api_key:
            errors.append("SONARR_API_KEY is required for TV dedup")
        return errors

    def validate_opensubtitles(self) -> list[str]:
        errors = []
        if not self.opensubtitles_api_key:
            errors.append("OPENSUBTITLES_API_KEY is required for subtitles")
        if not self.opensubtitles_username:
            errors.append("OPENSUBTITLES_USERNAME is required for subtitle downloads")
        if not self.opensubtitles_password:
            errors.append("OPENSUBTITLES_PASSWORD is required for subtitle downloads")
        return errors
