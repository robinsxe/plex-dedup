"""
OpenSubtitles.com REST API client for searching and downloading subtitles.
Uses the new REST API (not the legacy XML-RPC API).

API docs: https://opensubtitles.stoplight.io/docs/opensubtitles-api
"""

import os
import hashlib
import struct
import logging
import time
import requests

logger = logging.getLogger(__name__)

# Rate limiting: OpenSubtitles allows ~5 requests/second for logged-in users
RATE_LIMIT_DELAY = 0.25  # seconds between requests


class OpenSubtitlesClient:
    """Client for OpenSubtitles.com REST API."""

    BASE_URL = "https://api.opensubtitles.com/api/v1"

    def __init__(self, api_key: str, username: str = "", password: str = ""):
        self.api_key = api_key
        self.username = username
        self.password = password
        self._token = None
        self._last_request = 0
        self.session = requests.Session()
        self.session.headers.update({
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "PlexDedup v1.0",
        })

    def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        elapsed = time.time() - self._last_request
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self._last_request = time.time()

    def _get(self, endpoint: str, params: dict = None) -> dict:
        self._rate_limit()
        resp = self.session.get(f"{self.BASE_URL}/{endpoint}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, data: dict = None) -> dict:
        self._rate_limit()
        resp = self.session.post(f"{self.BASE_URL}/{endpoint}", json=data)
        resp.raise_for_status()
        return resp.json()

    def login(self) -> bool:
        """Authenticate and get a session token (required for downloads)."""
        if not self.username or not self.password:
            logger.warning("OpenSubtitles credentials not set — downloads will fail")
            return False

        try:
            data = self._post("login", {
                "username": self.username,
                "password": self.password,
            })
            self._token = data.get("token")
            if self._token:
                self.session.headers["Authorization"] = f"Bearer {self._token}"
                user = data.get("user", {})
                remaining = user.get("allowed_downloads", "?")
                logger.info(
                    f"OpenSubtitles login OK — "
                    f"downloads remaining today: {remaining}"
                )
                return True
            else:
                logger.error("Login succeeded but no token returned")
                return False
        except requests.HTTPError as e:
            logger.error(f"OpenSubtitles login failed: {e}")
            return False
        except Exception as e:
            logger.error(f"OpenSubtitles login error: {e}")
            return False

    def test_connection(self) -> bool:
        """Test that the API key works."""
        try:
            # Use infos/languages as a lightweight test endpoint
            self._get("infos/languages")
            logger.info("OpenSubtitles API connection OK")
            return True
        except Exception as e:
            logger.error(f"OpenSubtitles connection test failed: {e}")
            return False

    @staticmethod
    def compute_hash(file_path: str) -> str | None:
        """
        Compute the OpenSubtitles hash for a file.
        This is a 64-bit hash based on file size + first/last 64KB.
        """
        try:
            file_size = os.path.getsize(file_path)
            if file_size < 65536:
                return None

            hash_val = file_size
            with open(file_path, "rb") as f:
                # Read first 64KB
                for _ in range(65536 // 8):
                    buf = f.read(8)
                    (val,) = struct.unpack("<q", buf)
                    hash_val += val
                    hash_val &= 0xFFFFFFFFFFFFFFFF

                # Read last 64KB
                f.seek(-65536, 2)
                for _ in range(65536 // 8):
                    buf = f.read(8)
                    (val,) = struct.unpack("<q", buf)
                    hash_val += val
                    hash_val &= 0xFFFFFFFFFFFFFFFF

            return f"{hash_val:016x}"
        except Exception as e:
            logger.warning(f"Could not compute hash for {file_path}: {e}")
            return None

    def search_subtitles(
        self,
        languages: list[str],
        imdb_id: str = None,
        tmdb_id: str = None,
        file_hash: str = None,
        query: str = None,
        season_number: int = None,
        episode_number: int = None,
        media_type: str = "movie",
    ) -> list[dict]:
        """
        Search for subtitles on OpenSubtitles.

        Args:
            languages: List of ISO 639-1 codes (e.g., ["sv", "en"])
            imdb_id: IMDB ID (e.g., "tt1234567")
            tmdb_id: TMDB ID
            file_hash: OpenSubtitles file hash
            query: Text search query
            season_number: For TV episodes
            episode_number: For TV episodes
            media_type: "movie" or "episode"

        Returns:
            List of subtitle results sorted by relevance.
        """
        params = {
            "languages": ",".join(languages),
        }

        if media_type == "episode":
            params["type"] = "episode"
            if season_number is not None:
                params["season_number"] = season_number
            if episode_number is not None:
                params["episode_number"] = episode_number
        else:
            params["type"] = "movie"

        # Prefer hash-based search (most accurate match)
        if file_hash:
            params["moviehash"] = file_hash

        # Then by external IDs
        if imdb_id:
            # Strip 'tt' prefix if present, API wants just the number
            imdb_num = imdb_id.replace("tt", "")
            params["imdb_id"] = imdb_num

        if tmdb_id:
            params["tmdb_id"] = tmdb_id

        # Fallback to text search
        if query and not (imdb_id or tmdb_id or file_hash):
            params["query"] = query

        try:
            data = self._get("subtitles", params=params)
            results = data.get("data", [])
            logger.info(f"Found {len(results)} subtitle results")
            return results
        except requests.HTTPError as e:
            logger.error(f"Subtitle search failed: {e}")
            return []
        except Exception as e:
            logger.error(f"Subtitle search error: {e}")
            return []

    def download_subtitle(self, file_id: int, output_path: str) -> bool:
        """
        Download a subtitle file by its file_id.

        Args:
            file_id: The OpenSubtitles file ID from search results.
            output_path: Where to save the .srt file.

        Returns:
            True if download succeeded.
        """
        if not self._token:
            if not self.login():
                logger.error("Cannot download: not logged in")
                return False

        try:
            data = self._post("download", {"file_id": file_id})
            download_url = data.get("link")

            if not download_url:
                logger.error("No download link in response")
                return False

            # Download the actual subtitle file
            self._rate_limit()
            resp = self.session.get(download_url)
            resp.raise_for_status()

            # Ensure output directory exists
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            with open(output_path, "wb") as f:
                f.write(resp.content)

            remaining = data.get("remaining", "?")
            logger.info(
                f"Downloaded subtitle to {output_path} "
                f"(downloads remaining: {remaining})"
            )
            return True

        except requests.HTTPError as e:
            if e.response and e.response.status_code == 406:
                logger.error("Download limit reached for today")
            else:
                logger.error(f"Subtitle download failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Subtitle download error: {e}")
            return False

    def find_best_subtitle(self, results: list[dict], language: str) -> dict | None:
        """
        Pick the best subtitle from search results for a given language.
        Prefers: hearing_impaired=False, highest download_count, trusted uploader.
        """
        lang_results = []
        for r in results:
            attrs = r.get("attributes", {})
            sub_lang = attrs.get("language", "")
            if sub_lang == language:
                lang_results.append(r)

        if not lang_results:
            return None

        def score(r):
            attrs = r.get("attributes", {})
            s = 0
            # Prefer non-hearing-impaired
            if not attrs.get("hearing_impaired", False):
                s += 1000
            # Prefer higher download count
            s += attrs.get("download_count", 0)
            # Prefer trusted uploaders
            if attrs.get("from_trusted", False):
                s += 500
            # Prefer machine-translated = False
            if not attrs.get("machine_translated", False):
                s += 2000
            # Prefer AI-translated = False
            if not attrs.get("ai_translated", False):
                s += 2000
            return s

        lang_results.sort(key=score, reverse=True)
        return lang_results[0]

    def get_subtitle_output_path(self, video_path: str, language: str) -> str:
        """
        Generate the subtitle output path following Plex naming conventions.
        e.g., Movie.2024.1080p.mkv -> Movie.2024.1080p.sv.srt
        """
        base, _ = os.path.splitext(video_path)
        return f"{base}.{language}.srt"

    def process_media_item(
        self,
        file_path: str,
        languages: list[str],
        imdb_id: str = None,
        tmdb_id: str = None,
        media_type: str = "movie",
        season_number: int = None,
        episode_number: int = None,
        title: str = None,
        dry_run: bool = True,
    ) -> dict:
        """
        Search for and optionally download subtitles for a single media file.

        Returns:
            Dict with results per language.
        """
        results = {}

        # Check which languages already exist
        for lang in languages:
            sub_path = self.get_subtitle_output_path(file_path, lang)
            if os.path.exists(sub_path):
                results[lang] = {
                    "status": "exists",
                    "path": sub_path,
                }
                logger.info(f"Subtitle already exists: {sub_path}")
                continue

            # Compute file hash for better matching
            file_hash = self.compute_hash(file_path)

            # Search
            search_results = self.search_subtitles(
                languages=[lang],
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
                file_hash=file_hash,
                query=title,
                season_number=season_number,
                episode_number=episode_number,
                media_type=media_type,
            )

            best = self.find_best_subtitle(search_results, lang)

            if not best:
                results[lang] = {"status": "not_found"}
                logger.info(f"No {lang} subtitle found for {os.path.basename(file_path)}")
                continue

            attrs = best.get("attributes", {})
            file_info = (attrs.get("files") or [{}])[0] if attrs.get("files") else {}
            file_id = file_info.get("file_id")

            if not file_id:
                results[lang] = {"status": "no_file_id"}
                continue

            if dry_run:
                results[lang] = {
                    "status": "found",
                    "subtitle": attrs.get("release", "Unknown"),
                    "downloads": attrs.get("download_count", 0),
                    "would_save_to": sub_path,
                }
                logger.info(
                    f"[DRY RUN] Would download {lang} subtitle: "
                    f"{attrs.get('release', '?')}"
                )
            else:
                success = self.download_subtitle(file_id, sub_path)
                results[lang] = {
                    "status": "downloaded" if success else "download_failed",
                    "path": sub_path if success else None,
                    "subtitle": attrs.get("release", "Unknown"),
                }

        return results
