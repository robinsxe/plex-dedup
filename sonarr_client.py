"""
Sonarr API client for managing TV show/episode monitoring status.
"""

import logging
import requests

logger = logging.getLogger(__name__)


class SonarrClient:
    """Client for interacting with Sonarr's API."""

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        })

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        resp = self.session.get(f"{self.url}/api/v3/{endpoint}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _put(self, endpoint: str, data: dict) -> dict:
        resp = self.session.put(f"{self.url}/api/v3/{endpoint}", json=data)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, endpoint: str, params: dict = None) -> bool:
        resp = self.session.delete(
            f"{self.url}/api/v3/{endpoint}", params=params
        )
        return resp.status_code in (200, 204)

    def test_connection(self) -> bool:
        """Test connection to Sonarr."""
        try:
            status = self._get("system/status")
            logger.info(f"Connected to Sonarr v{status.get('version', '?')}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Sonarr: {e}")
            return False

    def get_all_series(self) -> list[dict]:
        """Get all TV series from Sonarr."""
        return self._get("series")

    def find_series_by_tvdb(self, tvdb_id: str) -> dict | None:
        """Find a series in Sonarr by TVDB ID."""
        try:
            all_series = self.get_all_series()
            for series in all_series:
                if str(series.get("tvdbId", "")) == str(tvdb_id):
                    return series
        except Exception as e:
            logger.warning(f"Could not find series with TVDB ID {tvdb_id}: {e}")
        return None

    def find_series_by_imdb(self, imdb_id: str) -> dict | None:
        """Find a series in Sonarr by IMDB ID."""
        try:
            all_series = self.get_all_series()
            for series in all_series:
                if series.get("imdbId", "") == imdb_id:
                    return series
        except Exception as e:
            logger.warning(f"Could not find series with IMDB ID {imdb_id}: {e}")
        return None

    def find_series_by_title(self, title: str) -> dict | None:
        """Find a series in Sonarr by title."""
        try:
            all_series = self.get_all_series()
            title_lower = title.lower()
            for series in all_series:
                if series.get("title", "").lower() == title_lower:
                    return series
            # Fuzzy match
            for series in all_series:
                if title_lower in series.get("title", "").lower():
                    return series
        except Exception as e:
            logger.warning(f"Could not find series '{title}': {e}")
        return None

    def find_series(self, tvdb_id: str = None, imdb_id: str = None,
                    title: str = None) -> dict | None:
        """Find a series using the best available identifier."""
        if tvdb_id:
            series = self.find_series_by_tvdb(tvdb_id)
            if series:
                return series
        if imdb_id:
            series = self.find_series_by_imdb(imdb_id)
            if series:
                return series
        if title:
            series = self.find_series_by_title(title)
            if series:
                return series
        return None

    def get_episodes(self, series_id: int) -> list[dict]:
        """Get all episodes for a series."""
        try:
            return self._get("episode", params={"seriesId": series_id})
        except Exception as e:
            logger.warning(f"Could not get episodes for series {series_id}: {e}")
            return []

    def find_episode(self, series_id: int, season_number: int,
                     episode_number: int) -> dict | None:
        """Find a specific episode in Sonarr."""
        episodes = self.get_episodes(series_id)
        for ep in episodes:
            if (ep.get("seasonNumber") == season_number
                    and ep.get("episodeNumber") == episode_number):
                return ep
        return None

    def unmonitor_episode(self, episode: dict) -> bool:
        """Set a specific episode to unmonitored."""
        ep_id = episode["id"]
        title = (
            f"S{episode.get('seasonNumber', 0):02d}"
            f"E{episode.get('episodeNumber', 0):02d}"
            f" - {episode.get('title', 'Unknown')}"
        )

        if not episode.get("monitored", True):
            logger.info(f"Episode already unmonitored: {title}")
            return True

        try:
            episode["monitored"] = False
            self._put(f"episode/{ep_id}", episode)
            logger.info(f"Unmonitored episode in Sonarr: {title}")
            return True
        except Exception as e:
            logger.error(f"Failed to unmonitor episode '{title}': {e}")
            return False

    def unmonitor_series(self, series: dict) -> bool:
        """Set an entire series to unmonitored."""
        series_id = series["id"]
        title = series.get("title", "Unknown")

        if not series.get("monitored", True):
            logger.info(f"Series already unmonitored: {title}")
            return True

        try:
            series["monitored"] = False
            self._put(f"series/{series_id}", series)
            logger.info(f"Unmonitored series in Sonarr: {title}")
            return True
        except Exception as e:
            logger.error(f"Failed to unmonitor series '{title}': {e}")
            return False

    def get_episode_files(self, series_id: int) -> list[dict]:
        """Get all episode files for a series."""
        try:
            return self._get("episodefile", params={"seriesId": series_id})
        except Exception as e:
            logger.warning(f"Could not get episode files for series {series_id}: {e}")
            return []

    def delete_episode_file(self, episode_file_id: int) -> bool:
        """Delete a specific episode file."""
        try:
            return self._delete(f"episodefile/{episode_file_id}")
        except Exception as e:
            logger.error(f"Failed to delete episode file {episode_file_id}: {e}")
            return False
