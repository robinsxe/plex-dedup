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
from opensubtitles_client import (
    PERMANENT_DOWNLOAD_ERRORS,
    STATUS_TRANSIENT,
    DownloadLimitReached,
    OpenSubtitlesClient,
)
from plex_client import PlexClient

logger = logging.getLogger(__name__)

QUEUE_FILE = os.environ.get("QUEUE_DB", "/data/sub_queue.json")

# Transient failures (search errors, download errors) revert to pending and
# are retried on later runs, up to this many attempts
MAX_ATTEMPTS = 3


def scheduler_due(now: time.struct_time, target_hour: int,
                  last_run_date: str | None) -> bool:
    """Decide whether the daily queue run is due on a scheduler tick:
    inside the target hour and not yet run today."""
    return (
        now.tm_hour == target_hour
        and time.strftime("%Y-%m-%d", now) != last_run_date
    )


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
            # Migration: entries written before the episode/show id split
            # carry the show-level IMDB id in imdb_id — searching with it
            # as the episode id returns nothing
            migrated = 0
            for item in self._data:
                if (item.get("media_type") == "episode"
                        and item.get("imdb_id")
                        and not item.get("show_imdb_id")):
                    item["show_imdb_id"] = item["imdb_id"]
                    item["imdb_id"] = None
                    migrated += 1
            if recovered:
                logger.info(f"Recovered {recovered} items stuck in 'processing' state")
            if migrated:
                logger.info(f"Migrated {migrated} legacy episode queue entries")
            if recovered or migrated:
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
        """Build a dedup key from the best available ID. Episodes include
        season/episode so a show's episodes don't collapse to one entry."""
        if item.get("media_type") == "episode":
            show_id = item.get("show_imdb_id") or item.get("imdb_id")
            if show_id:
                return (
                    f"show:{show_id}"
                    f":s{item.get('season_number')}e{item.get('episode_number')}"
                )
            return f"path:{item.get('file_path', '')}"
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
        # Dedup against failed items too — otherwise a permanently failed
        # item is re-added with attempts=0 on every analysis run and burns
        # quota forever. Clear failed items (DELETE status=failed) to re-add.
        existing_keys = {
            self._dedup_key(i) for i in self._data
            if i.get("status") in ("pending", "failed")
        }
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
                "show_imdb_id": item.get("show_imdb_id"),
                "rating_key": item.get("rating_key", ""),
                "season_number": item.get("season_number"),
                "episode_number": item.get("episode_number"),
                "show_title": item.get("show_title", ""),
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "pending",
                "attempts": 0,
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
            dry_run: If True, only report what would be processed —
                queue items are not modified.

        Returns:
            Summary dict with downloaded/failed/retried/remaining counts.
        """
        max_downloads = limit if limit > 0 else self.config.subtitle_daily_limit

        # Dry run: report only — never mutate or persist item status
        if dry_run:
            with self._lock:
                pending_count = self.pending_count
            would_process = min(pending_count, max_downloads)
            logger.info(
                f"Queue dry run: {would_process} of {pending_count} "
                f"pending items would be processed"
            )
            return self._result(dry_run=True, would_process=would_process,
                                remaining=pending_count)

        # Claim items under lock to prevent double-processing
        with self._lock:
            pending = [i for i in self._data if i.get("status") == "pending"]
            if not pending:
                logger.info("Queue: no pending items to process")
                return self._result()
            to_process = pending[:max_downloads]
            for item in to_process:
                item["status"] = "processing"
            self._save()

        # Process outside lock (HTTP calls)
        result, refresh_types = self._process_items(to_process)

        # Finalize under lock
        with self._lock:
            self._cleanup_history()
            self._last_run = time.strftime("%Y-%m-%d %H:%M:%S")
            self._last_run_result = result
            self._save()

        # Refresh Plex outside lock, based on files actually written this
        # run (an item reverted to pending for retry may still have new files)
        if refresh_types:
            self._refresh_plex(refresh_types)

        return result

    @staticmethod
    def _result(downloaded: int = 0, already_had: int = 0, failed: int = 0,
                will_retry: int = 0, files_downloaded: int = 0,
                limit_reached: bool = False, remaining: int = 0,
                dry_run: bool = False, would_process: int = 0) -> dict:
        """Single construction point for the process() summary shape."""
        return {
            "downloaded": downloaded,
            "already_had": already_had,
            "failed": failed,
            "will_retry": will_retry,
            "files_downloaded": files_downloaded,
            "limit_reached": limit_reached,
            "remaining": remaining,
            "dry_run": dry_run,
            "would_process": would_process,
        }

    def _process_items(self, to_process: list[dict]) -> tuple[dict, set]:
        """Process a batch of claimed items. Called without lock held.

        Returns (summary dict, media types with files written this run —
        the Plex libraries that need a refresh).
        """
        opensubs = OpenSubtitlesClient(
            self.config.opensubtitles_api_key,
            self.config.opensubtitles_username,
            self.config.opensubtitles_password,
        )

        downloaded = 0
        already_had = 0
        failed = 0
        will_retry = 0
        files_downloaded = 0
        limit_reached = False
        refresh_types: set[str] = set()
        langs = self.config.subtitle_languages

        logger.info(
            f"Queue: processing {len(to_process)} items "
            f"(daily limit: {self.config.subtitle_daily_limit})"
        )

        for i, item in enumerate(to_process, 1):
            display = self._display_title(item)
            logger.info(f"Queue [{i}/{len(to_process)}]: {display}")

            try:
                sub_result = opensubs.process_media_item(
                    file_path=item.get("file_path", ""),
                    languages=langs,
                    imdb_id=item.get("imdb_id"),
                    tmdb_id=item.get("tmdb_id"),
                    parent_imdb_id=item.get("show_imdb_id"),
                    media_type=item.get("media_type", "movie"),
                    season_number=item.get("season_number"),
                    episode_number=item.get("episode_number"),
                    title=item.get("show_title") or item.get("title"),
                    dry_run=False,
                )
            except DownloadLimitReached as e:
                logger.warning("Queue: daily download limit reached, stopping")
                # Languages downloaded before the limit hit still need a
                # Plex refresh even though the item goes back to pending
                partial = getattr(e, "partial_results", None) or {}
                got = sum(1 for r in partial.values()
                          if r.get("status") == "downloaded")
                if got:
                    files_downloaded += got
                    refresh_types.add(item.get("media_type", "movie"))
                item["status"] = "pending"
                # Revert remaining items to pending
                for remaining in to_process[i:]:
                    if remaining["status"] == "processing":
                        remaining["status"] = "pending"
                limit_reached = True
                break
            except Exception as e:
                if self._record_failure_attempt(item, display, str(e)):
                    failed += 1
                else:
                    will_retry += 1
                continue

            statuses = {lang: r.get("status") for lang, r in sub_result.items()}
            errors = "; ".join(
                f"{lang}: {r['error']}"
                for lang, r in sub_result.items() if r.get("error")
            )
            got = sum(1 for s in statuses.values() if s == "downloaded")
            if got:
                files_downloaded += got
                refresh_types.add(item.get("media_type", "movie"))

            permanent = any(
                r.get("status") == "download_failed"
                and r.get("error") in PERMANENT_DOWNLOAD_ERRORS
                for r in sub_result.values()
            )
            transient = any(
                r.get("status") in STATUS_TRANSIENT
                and r.get("error") not in PERMANENT_DOWNLOAD_ERRORS
                for r in sub_result.values()
            )

            if permanent:
                # Unrecoverable without a config change (bad credentials,
                # malformed response) — retrying would just burn attempts
                item["status"] = "failed"
                item["error"] = errors or "download failed permanently"
                failed += 1
            elif transient:
                # Retry later — even when another language downloaded this
                # run (it shows as "exists" on the retry and is skipped)
                if self._record_failure_attempt(
                        item, display, errors or "download failed"):
                    failed += 1
                else:
                    will_retry += 1
            elif got:
                item["status"] = "downloaded"
                item["downloaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                item.pop("error", None)
                downloaded += 1
            elif statuses and all(s == "exists" for s in statuses.values()):
                # All subtitles already on disk — done, nothing to download
                item["status"] = "downloaded"
                item["note"] = "subtitles already existed on disk"
                already_had += 1
            else:
                # not_found / no_file_id / video_missing — retrying won't help
                item["status"] = "failed"
                item["error"] = errors or f"no subtitle found ({statuses})"
                failed += 1

        logger.info(
            f"Queue processing complete: {downloaded} downloaded, "
            f"{already_had} already on disk, {failed} failed, "
            f"{will_retry} scheduled for retry, "
            f"{self.pending_count} remaining"
            f"{', limit reached' if limit_reached else ''}"
        )

        result = self._result(
            downloaded=downloaded, already_had=already_had, failed=failed,
            will_retry=will_retry, files_downloaded=files_downloaded,
            limit_reached=limit_reached, remaining=self.pending_count,
        )
        return result, refresh_types

    @staticmethod
    def _record_failure_attempt(item: dict, display: str, reason: str) -> bool:
        """Record a transient failure; the item goes back to pending until
        MAX_ATTEMPTS is reached. Returns True if now permanently failed."""
        attempts = item.get("attempts", 0) + 1
        item["attempts"] = attempts
        item["error"] = reason
        if attempts < MAX_ATTEMPTS:
            item["status"] = "pending"
            logger.warning(
                f"Queue: {display} failed (attempt {attempts}/{MAX_ATTEMPTS}), "
                f"will retry: {reason}"
            )
            return False
        item["status"] = "failed"
        logger.error(
            f"Queue: {display} permanently failed after {MAX_ATTEMPTS} "
            f"attempts: {reason}"
        )
        return True

    def _cleanup_history(self):
        """Remove old completed items, keep last 100."""
        active = [i for i in self._data if i.get("status") in ("pending", "processing")]
        completed = [i for i in self._data if i.get("status") in ("downloaded", "failed")]
        if len(completed) > 100:
            completed = completed[-100:]
        self._data = active + completed

    def _refresh_plex(self, media_types: set):
        """Refresh the Plex libraries for media types that got new files."""
        plex = PlexClient(self.config.plex_url, self.config.plex_token)
        libraries = {
            self.config.plex_tv_library if t == "episode"
            else self.config.plex_movie_library
            for t in media_types
        }
        for lib in libraries:
            try:
                plex.refresh_library(lib)
                logger.info(f"Refreshed Plex library '{lib}'")
            except Exception as e:
                logger.warning(f"Failed to refresh library '{lib}': {e}")

    def get_status(self) -> dict:
        """Get queue status for the API."""
        with self._lock:
            failed_items = [
                {
                    "title": self._display_title(i),
                    "error": i.get("error", ""),
                    "attempts": i.get("attempts", 0),
                }
                for i in self._data if i.get("status") == "failed"
            ]
            return {
                "pending": self.pending_count,
                "total": len(self._data),
                "downloaded": sum(1 for i in self._data if i.get("status") == "downloaded"),
                "failed": len(failed_items),
                "failed_items": failed_items[-20:],
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
