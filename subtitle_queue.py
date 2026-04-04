"""
Subtitle download queue with daily limit handling.
Queues subtitle downloads and processes them respecting the OpenSubtitles
daily download limit (20/day for free accounts).
"""

import json
import logging
import os
import threading
import time

from config import Config
from opensubtitles_client import OpenSubtitlesClient
from plex_client import PlexClient

logger = logging.getLogger(__name__)

QUEUE_FILE = os.environ.get("QUEUE_DB", "/data/sub_queue.json")


class SubtitleQueue:
    """Persistent queue for subtitle downloads with daily limit handling."""

    def __init__(self, config: Config):
        self.config = config
        self._path = QUEUE_FILE
        self._lock = threading.Lock()
        self._data: list[dict] = []
        self._last_run: str | None = None
        self._last_run_result: dict | None = None
        self._load()

    def _load(self):
        try:
            with open(self._path) as f:
                raw = json.load(f)
            self._data = raw.get("queue", [])
            # Crash recovery: reset any "processing" items back to pending
            recovered = 0
            for item in self._data:
                if item.get("status") == "processing":
                    item["status"] = "pending"
                    recovered += 1
            if recovered:
                logger.info(f"Recovered {recovered} items stuck in 'processing' state")
                self._save()
            self._last_run = raw.get("last_run")
            self._last_run_result = raw.get("last_run_result")
            logger.info(
                f"Loaded subtitle queue: {len(self._data)} items "
                f"({self.pending_count} pending)"
            )
        except FileNotFoundError:
            self._data = []
        except Exception as e:
            logger.warning(f"Could not load subtitle queue: {e}")
            self._data = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w") as f:
                json.dump({
                    "queue": self._data,
                    "last_run": self._last_run,
                    "last_run_result": self._last_run_result,
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save subtitle queue: {e}")

    def _dedup_key(self, item: dict) -> str:
        """Build a dedup key from the best available ID."""
        if item.get("imdb_id"):
            return f"imdb:{item['imdb_id']}"
        if item.get("tmdb_id"):
            return f"tmdb:{item['tmdb_id']}"
        return f"path:{item.get('file_path', '')}"

    def add(self, items: list[dict]) -> dict:
        """
        Add items to the download queue. Deduplicates against existing
        pending items.

        Args:
            items: List of dicts with file_path, media_type, title, etc.

        Returns:
            Summary dict with added/skipped counts.
        """
        with self._lock:
            return self._add_unlocked(items)

    def _add_unlocked(self, items: list[dict]) -> dict:
        existing_keys = {self._dedup_key(i) for i in self._data if i.get("status") == "pending"}
        added = 0
        skipped = 0

        for item in items:
            key = self._dedup_key(item)
            if key in existing_keys:
                skipped += 1
                continue

            self._data.append({
                "file_path": item.get("file_path", ""),
                "media_type": item.get("media_type", "movie"),
                "title": item.get("title", ""),
                "year": item.get("year"),
                "imdb_id": item.get("imdb_id"),
                "tmdb_id": item.get("tmdb_id"),
                "season_number": item.get("season_number"),
                "episode_number": item.get("episode_number"),
                "show_title": item.get("show_title", ""),
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "pending",
            })
            existing_keys.add(key)
            added += 1

        self._save()
        logger.info(f"Queue: added {added}, skipped {skipped} duplicates")
        return {"added": added, "skipped": skipped, "total_pending": self.pending_count}

    def process(self, limit: int = 0, dry_run: bool = False) -> dict:
        """
        Process pending items in the queue up to the daily limit.
        Stops early if OpenSubtitles returns 406 (limit reached).

        Args:
            limit: Override daily limit (0 = use config default).
            dry_run: If True, don't actually download.

        Returns:
            Summary dict with downloaded/failed/remaining counts.
        """
        max_downloads = limit if limit > 0 else self.config.subtitle_daily_limit

        # Claim items under lock to prevent double-processing
        with self._lock:
            pending = [i for i in self._data if i.get("status") == "pending"]
            if not pending:
                logger.info("Queue: no pending items to process")
                return {"downloaded": 0, "failed": 0, "limit_reached": False,
                        "remaining": 0, "dry_run": dry_run}
            to_process = pending[:max_downloads]
            for item in to_process:
                item["status"] = "processing"
            self._save()

        # Process outside lock (HTTP calls)
        result = self._process_items(to_process, dry_run)

        # Finalize under lock
        with self._lock:
            self._cleanup_history()
            self._last_run = time.strftime("%Y-%m-%d %H:%M:%S")
            self._last_run_result = result
            self._save()

        # Refresh Plex outside lock
        if result["downloaded"] > 0 and not dry_run:
            self._refresh_plex(to_process)

        return result

    def _process_items(self, to_process: list[dict], dry_run: bool) -> dict:
        """Process a batch of claimed items. Called without lock held."""
        opensubs = OpenSubtitlesClient(
            self.config.opensubtitles_api_key,
            self.config.opensubtitles_username,
            self.config.opensubtitles_password,
        )

        downloaded = 0
        failed = 0
        limit_reached = False
        langs = self.config.subtitle_languages

        logger.info(
            f"Queue: processing {len(to_process)} items "
            f"(daily limit: {self.config.subtitle_daily_limit}, dry_run: {dry_run})"
        )

        for i, item in enumerate(to_process, 1):
            display = self._display_title(item)
            logger.info(f"Queue [{i}/{len(to_process)}]: {display}")

            if dry_run:
                item["status"] = "downloaded"
                downloaded += 1
                continue

            try:
                sub_result = opensubs.process_media_item(
                    file_path=item.get("file_path", ""),
                    languages=langs,
                    imdb_id=item.get("imdb_id"),
                    tmdb_id=item.get("tmdb_id"),
                    media_type=item.get("media_type", "movie"),
                    season_number=item.get("season_number"),
                    episode_number=item.get("episode_number"),
                    title=item.get("show_title") or item.get("title"),
                    dry_run=False,
                )

                any_downloaded = any(
                    v.get("status") == "downloaded" for v in sub_result.values()
                )
                any_limit = any(
                    "limit" in str(v.get("error", "")).lower()
                    or v.get("status") == "limit_reached"
                    for v in sub_result.values()
                )

                if any_limit:
                    logger.warning("Queue: daily download limit reached, stopping")
                    item["status"] = "pending"
                    # Revert remaining items to pending
                    for remaining in to_process[i:]:
                        if remaining["status"] == "processing":
                            remaining["status"] = "pending"
                    limit_reached = True
                    break
                elif any_downloaded:
                    item["status"] = "downloaded"
                    item["downloaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    downloaded += 1
                else:
                    item["status"] = "failed"
                    failed += 1

            except Exception as e:
                error_msg = str(e).lower()
                if "406" in error_msg or "limit" in error_msg:
                    logger.warning(f"Queue: download limit reached: {e}")
                    item["status"] = "pending"
                    for remaining in to_process[i:]:
                        if remaining["status"] == "processing":
                            remaining["status"] = "pending"
                    limit_reached = True
                    break
                else:
                    logger.error(f"Queue: failed to download for {display}: {e}")
                    item["status"] = "failed"
                    failed += 1

        logger.info(
            f"Queue processing complete: {downloaded} downloaded, "
            f"{failed} failed, {self.pending_count} remaining"
            f"{', limit reached' if limit_reached else ''}"
        )

        return {"downloaded": downloaded, "failed": failed,
                "limit_reached": limit_reached,
                "remaining": self.pending_count, "dry_run": dry_run}

    def _cleanup_history(self):
        """Remove old completed items, keep last 100."""
        active = [i for i in self._data if i.get("status") in ("pending", "processing")]
        completed = [i for i in self._data if i.get("status") in ("downloaded", "failed")]
        if len(completed) > 100:
            completed = completed[-100:]
        self._data = active + completed

    def _refresh_plex(self, processed: list[dict]):
        """Refresh relevant Plex libraries after downloads."""
        plex = PlexClient(self.config.plex_url, self.config.plex_token)
        libraries = set()
        for item in processed:
            if item.get("status") == "downloaded":
                if item.get("media_type") == "episode":
                    libraries.add(self.config.plex_tv_library)
                else:
                    libraries.add(self.config.plex_movie_library)
        for lib in libraries:
            try:
                plex.refresh_library(lib)
                logger.info(f"Refreshed Plex library '{lib}'")
            except Exception as e:
                logger.warning(f"Failed to refresh library '{lib}': {e}")

    def get_status(self) -> dict:
        """Get queue status for the API."""
        with self._lock:
            return {
                "pending": self.pending_count,
                "total": len(self._data),
                "downloaded": sum(1 for i in self._data if i.get("status") == "downloaded"),
                "failed": sum(1 for i in self._data if i.get("status") == "failed"),
                "last_run": self._last_run,
                "last_run_result": self._last_run_result,
                "daily_limit": self.config.subtitle_daily_limit,
                "queue_hour": self.config.subtitle_queue_hour,
            }

    def get_pending(self) -> list[dict]:
        """Get all pending items."""
        with self._lock:
            return [i for i in self._data if i.get("status") == "pending"]

    def clear(self, status: str = None) -> int:
        """Clear queue items. If status is given, only clear that status."""
        with self._lock:
            if status:
                before = len(self._data)
                self._data = [i for i in self._data if i.get("status") != status]
                count = before - len(self._data)
            else:
                count = len(self._data)
                self._data = []
            self._save()
            return count

    @property
    def pending_count(self) -> int:
        return sum(1 for i in self._data if i.get("status") == "pending")

    @property
    def count(self) -> int:
        return len(self._data)

    @staticmethod
    def _display_title(item: dict) -> str:
        if item.get("media_type") == "episode" and item.get("show_title"):
            s = item.get("season_number") or 0
            e = item.get("episode_number") or 0
            return f"{item['show_title']} S{s:02d}E{e:02d}"
        year = item.get("year")
        title = item.get("title", "Unknown")
        return f"{title} ({year})" if year else title
