"""
Radarr API client for managing movie monitoring status.
"""

import logging
import requests

logger = logging.getLogger(__name__)


class RadarrClient:
    """Client for interacting with Radarr's API."""

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
        """Test connection to Radarr."""
        try:
            status = self._get("system/status")
            logger.info(f"Connected to Radarr v{status.get('version', '?')}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Radarr: {e}")
            return False

    def get_all_movies(self) -> list[dict]:
        """Get all movies from Radarr."""
        return self._get("movie")

    def find_movie_by_tmdb(self, tmdb_id: str) -> dict | None:
        """Find a movie in Radarr by TMDB ID."""
        try:
            movies = self._get("movie", params={"tmdbId": tmdb_id})
            if isinstance(movies, list) and movies:
                return movies[0]
            elif isinstance(movies, dict):
                return movies
        except Exception as e:
            logger.warning(f"Could not find movie with TMDB ID {tmdb_id}: {e}")
        return None

    def find_movie_by_imdb(self, imdb_id: str) -> dict | None:
        """Find a movie in Radarr by IMDB ID."""
        try:
            movies = self._get("movie", params={"imdbId": imdb_id})
            if isinstance(movies, list) and movies:
                return movies[0]
            elif isinstance(movies, dict):
                return movies
        except Exception as e:
            logger.warning(f"Could not find movie with IMDB ID {imdb_id}: {e}")
        return None

    def find_movie_by_title(self, title: str, year: int = None) -> dict | None:
        """Find a movie in Radarr by title (and optionally year)."""
        try:
            all_movies = self.get_all_movies()
            for movie in all_movies:
                if movie.get("title", "").lower() == title.lower():
                    if year is None or movie.get("year") == year:
                        return movie
            # Fuzzy match - check if title is contained
            for movie in all_movies:
                if title.lower() in movie.get("title", "").lower():
                    if year is None or movie.get("year") == year:
                        return movie
        except Exception as e:
            logger.warning(f"Could not find movie '{title}': {e}")
        return None

    def find_movie(
        self,
        tmdb_id: str = None,
        imdb_id: str = None,
        title: str = None,
        year: int = None,
    ) -> dict | None:
        """
        Find a movie in Radarr using the best available identifier.
        Tries TMDB ID first, then IMDB ID, then title.
        """
        if tmdb_id:
            movie = self.find_movie_by_tmdb(tmdb_id)
            if movie:
                return movie

        if imdb_id:
            movie = self.find_movie_by_imdb(imdb_id)
            if movie:
                return movie

        if title:
            movie = self.find_movie_by_title(title, year)
            if movie:
                return movie

        return None

    def unmonitor_movie(self, radarr_movie: dict) -> bool:
        """
        Set a movie to unmonitored in Radarr.
        This prevents Radarr from downloading it again.
        """
        movie_id = radarr_movie["id"]
        title = radarr_movie.get("title", "Unknown")

        if not radarr_movie.get("monitored", True):
            logger.info(f"Movie already unmonitored: {title}")
            return True

        try:
            radarr_movie["monitored"] = False
            self._put(f"movie/{movie_id}", radarr_movie)
            logger.info(f"Unmonitored movie in Radarr: {title}")
            return True
        except Exception as e:
            logger.error(f"Failed to unmonitor '{title}': {e}")
            return False

    def delete_movie_file(self, movie_file_id: int) -> bool:
        """Delete a specific movie file from Radarr."""
        try:
            return self._delete(f"moviefile/{movie_file_id}")
        except Exception as e:
            logger.error(f"Failed to delete movie file {movie_file_id}: {e}")
            return False

    def get_movie_files(self, movie_id: int) -> list[dict]:
        """Get all files for a specific movie in Radarr."""
        try:
            return self._get("moviefile", params={"movieId": movie_id})
        except Exception as e:
            logger.warning(f"Could not get files for movie {movie_id}: {e}")
            return []

    def get_quality_profile(self, profile_id: int) -> dict | None:
        """Get a quality profile by ID."""
        try:
            return self._get(f"qualityprofile/{profile_id}")
        except Exception:
            return None
