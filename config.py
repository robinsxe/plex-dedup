"""
Configuration management for Plex Dedup.
Loads settings from .env file, environment variables, or persisted settings file.
"""

import json
import logging
import os
import stat
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SETTINGS_FILE = os.environ.get("SETTINGS_FILE", "/data/settings.json")


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

    # Prowlarr settings
    prowlarr_url: str = ""
    prowlarr_api_key: str = ""

    # OpenSubtitles settings
    opensubtitles_api_key: str = ""
    opensubtitles_username: str = ""
    opensubtitles_password: str = ""
    subtitle_languages: list = field(default_factory=lambda: ["sv", "en"])
    subtitle_auto_download: bool = True
    subtitle_match_tags: list = field(default_factory=lambda: ["NORDIC", "SWE", "SWESUB", "SWEDISH"])

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

    # Subtitle queue
    subtitle_daily_limit: int = 20
    subtitle_queue_hour: int = 4  # Hour of day (0-23) to process queue

    # Web UI
    web_host: str = "0.0.0.0"
    web_port: int = 8585

    def save_to_file(self, path: str = SETTINGS_FILE) -> bool:
        """Persist current config to a JSON file."""
        data = {
            "plex_url": self.plex_url,
            "plex_token": self.plex_token,
            "plex_movie_library": self.plex_movie_library,
            "plex_tv_library": self.plex_tv_library,
            "radarr_url": self.radarr_url,
            "radarr_api_key": self.radarr_api_key,
            "sonarr_url": self.sonarr_url,
            "sonarr_api_key": self.sonarr_api_key,
            "prowlarr_url": self.prowlarr_url,
            "prowlarr_api_key": self.prowlarr_api_key,
            "opensubtitles_api_key": self.opensubtitles_api_key,
            "opensubtitles_username": self.opensubtitles_username,
            "opensubtitles_password": self.opensubtitles_password,
            "subtitle_languages": self.subtitle_languages,
            "subtitle_auto_download": self.subtitle_auto_download,
            "subtitle_match_tags": self.subtitle_match_tags,
            "subtitle_daily_limit": self.subtitle_daily_limit,
            "subtitle_queue_hour": self.subtitle_queue_hour,
            "dry_run": self.dry_run,
            "keep_strategy": self.keep_strategy,
            "auto_unmonitor": self.auto_unmonitor,
            "delete_files": self.delete_files,
        }
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner only
            logger.info(f"Settings saved to {path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save settings to {path}: {e}")
            return False

    @classmethod
    def _load_settings_file(cls, path: str = SETTINGS_FILE) -> dict:
        """Load saved settings from JSON file. Returns empty dict on failure."""
        try:
            with open(path) as f:
                data = json.load(f)
            logger.info(f"Loaded settings from {path}")
            return data
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"Could not load settings from {path}: {e}")
            return {}

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration. Settings file takes priority, env vars as fallback."""
        saved = cls._load_settings_file()

        def _get(key: str, env_key: str, default: str = "") -> str:
            """Get value: saved settings > env var > default."""
            if key in saved and saved[key] not in (None, ""):
                return str(saved[key])
            return os.getenv(env_key, default)

        langs_raw = saved.get("subtitle_languages") if "subtitle_languages" in saved else None
        if langs_raw and isinstance(langs_raw, list):
            lang_list = langs_raw
        else:
            langs = os.getenv("SUBTITLE_LANGUAGES", "sv,en")
            lang_list = [l.strip() for l in langs.split(",") if l.strip()]

        match_tags_raw = saved.get("subtitle_match_tags") if "subtitle_match_tags" in saved else None
        if match_tags_raw and isinstance(match_tags_raw, list):
            match_tags = match_tags_raw
        else:
            match_tags = [t.strip() for t in os.getenv("SUBTITLE_MATCH_TAGS", "NORDIC,SWE,SWESUB,SWEDISH").split(",") if t.strip()]

        return cls(
            plex_url=_get("plex_url", "PLEX_URL", "http://localhost:32400"),
            plex_token=_get("plex_token", "PLEX_TOKEN", ""),
            plex_movie_library=_get("plex_movie_library", "PLEX_MOVIE_LIBRARY",
                                    os.getenv("PLEX_LIBRARY_NAME", "Movies")),
            plex_tv_library=_get("plex_tv_library", "PLEX_TV_LIBRARY", "TV Shows"),
            radarr_url=_get("radarr_url", "RADARR_URL", "http://localhost:7878"),
            radarr_api_key=_get("radarr_api_key", "RADARR_API_KEY", ""),
            sonarr_url=_get("sonarr_url", "SONARR_URL", "http://localhost:8989"),
            sonarr_api_key=_get("sonarr_api_key", "SONARR_API_KEY", ""),
            prowlarr_url=_get("prowlarr_url", "PROWLARR_URL", "http://localhost:9696"),
            prowlarr_api_key=_get("prowlarr_api_key", "PROWLARR_API_KEY", ""),
            opensubtitles_api_key=_get("opensubtitles_api_key", "OPENSUBTITLES_API_KEY", ""),
            opensubtitles_username=_get("opensubtitles_username", "OPENSUBTITLES_USERNAME", ""),
            opensubtitles_password=_get("opensubtitles_password", "OPENSUBTITLES_PASSWORD", ""),
            subtitle_languages=lang_list,
            subtitle_auto_download=_get("subtitle_auto_download", "SUBTITLE_AUTO_DOWNLOAD", "true").lower() == "true",
            subtitle_match_tags=match_tags,
            subtitle_daily_limit=int(_get("subtitle_daily_limit", "SUBTITLE_DAILY_LIMIT", "20")),
            subtitle_queue_hour=int(_get("subtitle_queue_hour", "SUBTITLE_QUEUE_HOUR", "4")),
            dry_run=_get("dry_run", "DRY_RUN", "true").lower() == "true",
            keep_strategy=_get("keep_strategy", "KEEP_STRATEGY", "best_quality"),
            auto_unmonitor=_get("auto_unmonitor", "AUTO_UNMONITOR", "true").lower() == "true",
            delete_files=_get("delete_files", "DELETE_FILES", "true").lower() == "true",
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

    def validate_prowlarr(self) -> list[str]:
        errors = []
        if not self.prowlarr_url:
            errors.append("PROWLARR_URL is required for library conversion")
        if not self.prowlarr_api_key:
            errors.append("PROWLARR_API_KEY is required for library conversion")
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
