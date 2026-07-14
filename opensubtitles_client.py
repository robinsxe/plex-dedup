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

# (connect, read) timeout for every HTTP call — without it a stalled call
# blocks the daily scheduler thread until container restart
REQUEST_TIMEOUT = (10, 60)


class DownloadLimitReached(Exception):
    """Daily download quota is spent (HTTP 406). Callers should stop
    downloading and keep remaining work queued for the next day."""


class SubtitleSearchError(Exception):
    """A subtitle search failed for transient reasons (rate limit, 5xx,
    network) — distinct from a search that genuinely found nothing."""


# download_subtitle error reasons that retrying cannot fix (bad credentials,
# malformed API response) — callers should fail fast instead of retrying
PERMANENT_DOWNLOAD_ERRORS = frozenset(
    {"not_logged_in", "reauthentication_failed", "no_download_link"}
)

# Per-language statuses emitted by process_media_item, single-sourced here so
# consumers (subtitle_queue, subtitle_manager) classify consistently
STATUS_TRANSIENT = frozenset({"search_error", "download_failed"})
STATUS_FAILURE = frozenset(
    {"download_failed", "no_file_id", "error", "search_error", "video_missing"}
)


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
        resp = self.session.get(
            f"{self.BASE_URL}/{endpoint}", params=params, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, data: dict = None) -> dict:
        self._rate_limit()
        resp = self.session.post(
            f"{self.BASE_URL}/{endpoint}", json=data, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        return resp.json()

    def login(self) -> bool:
        """Authenticate and get a session token (required for downloads)."""
        if not self.username or not self.password:
            logger.warning("OpenSubtitles credentials not set — downloads will fail")
            return False

        # Drop any stale token so a failed re-login can't leave an expired
        # Bearer header on the session
        self._token = None
        self.session.headers.pop("Authorization", None)

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
        parent_imdb_id: str = None,
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
            imdb_id: IMDB ID of the movie/episode itself (e.g., "tt1234567")
            tmdb_id: TMDB ID
            parent_imdb_id: IMDB ID of the parent TV show — the API expects
                the show id here (not in imdb_id) for episode searches
            file_hash: OpenSubtitles file hash
            query: Text search query
            season_number: For TV episodes
            episode_number: For TV episodes
            media_type: "movie" or "episode"

        Returns:
            List of subtitle results sorted by relevance.

        Raises:
            SubtitleSearchError: on HTTP/network failure, so callers can
                tell a failed search apart from an empty result.
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
            if parent_imdb_id:
                params["parent_imdb_id"] = parent_imdb_id.removeprefix("tt")
        else:
            params["type"] = "movie"

        # Prefer hash-based search (most accurate match)
        if file_hash:
            params["moviehash"] = file_hash

        # Then by external IDs
        if imdb_id:
            # Strip 'tt' prefix if present, API wants just the number
            params["imdb_id"] = imdb_id.removeprefix("tt")

        if tmdb_id:
            params["tmdb_id"] = tmdb_id

        # Fallback to text search
        if query and not (imdb_id or tmdb_id or parent_imdb_id or file_hash):
            params["query"] = query

        try:
            data = self._get("subtitles", params=params)
        except requests.HTTPError as e:
            logger.error(f"Subtitle search failed: {e}")
            raise SubtitleSearchError(str(e)) from e
        except Exception as e:
            logger.error(f"Subtitle search error: {e}")
            raise SubtitleSearchError(str(e)) from e

        results = data.get("data", [])
        logger.info(f"Found {len(results)} subtitle results")
        return results

    def download_subtitle(self, file_id: int, output_path: str) -> tuple[bool, str | None]:
        """
        Download a subtitle file by its file_id.

        Args:
            file_id: The OpenSubtitles file ID from search results.
            output_path: Where to save the .srt file.

        Returns:
            (success, error_reason) — error_reason is None on success.

        Raises:
            DownloadLimitReached: when the daily quota is spent (HTTP 406).
        """
        if not self._token:
            if not self.login():
                logger.error("Cannot download: not logged in")
                return False, "not_logged_in"

        # Single attempt loop with at most one 401-triggered re-login, so
        # every failure mode (including Timeout/OSError on the retry) maps
        # to the same (success, reason) contract from one place
        relogin_done = False
        while True:
            try:
                return self._download_once(file_id, output_path)
            except requests.HTTPError as e:
                # NOTE: e.response truthiness is False for 4xx/5xx — compare
                # against None explicitly
                status = e.response.status_code if e.response is not None else None
                if status == 406:
                    logger.error("Download limit reached for today")
                    raise DownloadLimitReached(
                        "daily download limit reached (HTTP 406)"
                    ) from e
                if status == 401 and not relogin_done:
                    # Token expired (~24h) — re-login once and retry
                    relogin_done = True
                    logger.info("OpenSubtitles token rejected (401), re-authenticating")
                    if not self.login():
                        return False, "reauthentication_failed"
                    continue
                logger.error(f"Subtitle download failed: {e}")
                return False, f"http_{status or 'error'}"
            except Exception as e:
                logger.error(f"Subtitle download error: {e}")
                return False, str(e)

    def _download_once(self, file_id: int, output_path: str) -> tuple[bool, str | None]:
        """Single download attempt. Raises requests.HTTPError on HTTP failure."""
        data = self._post("download", {"file_id": file_id})
        download_url = data.get("link")

        if not download_url:
            logger.error("No download link in response")
            return False, "no_download_link"

        # Download the actual subtitle file
        self._rate_limit()
        resp = self.session.get(download_url, timeout=REQUEST_TIMEOUT)
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
        return True, None

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
        parent_imdb_id: str = None,
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

        Raises:
            DownloadLimitReached: when the daily quota is spent mid-item.
        """
        results = {}

        # A missing video file means the media volume is not mounted at the
        # path Plex reports — downloading would consume quota and write the
        # .srt into the container filesystem where Plex never sees it.
        if not os.path.exists(file_path):
            logger.error(
                f"Video file not found: {file_path} — check that the media "
                f"volume is mounted at the same path as in Plex"
            )
            return {
                lang: {
                    "status": "video_missing",
                    "error": f"video file not found: {file_path}",
                }
                for lang in languages
            }

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
            try:
                search_results = self.search_subtitles(
                    languages=[lang],
                    imdb_id=imdb_id,
                    tmdb_id=tmdb_id,
                    parent_imdb_id=parent_imdb_id,
                    file_hash=file_hash,
                    query=title,
                    season_number=season_number,
                    episode_number=episode_number,
                    media_type=media_type,
                )
            except SubtitleSearchError as e:
                results[lang] = {"status": "search_error", "error": str(e)}
                continue

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
                try:
                    success, error = self.download_subtitle(file_id, sub_path)
                except DownloadLimitReached as e:
                    # Preserve earlier languages' outcomes (files may already
                    # be on disk) so callers can count/refresh them
                    e.partial_results = results
                    raise
                results[lang] = {
                    "status": "downloaded" if success else "download_failed",
                    "path": sub_path if success else None,
                    "subtitle": attrs.get("release", "Unknown"),
                }
                if error:
                    results[lang]["error"] = error

        return results
