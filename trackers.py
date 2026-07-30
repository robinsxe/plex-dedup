"""
Persistent keyed JSON state stores for the convert pipeline: grabbed items,
skip-listed items, and search cooldowns. All three share one thread-safe
KeyedJsonStore base — gunicorn serves requests from multiple threads while a
background scan thread mutates the same store instances, so every public
method takes the store lock. On-disk formats are unchanged from the previous
per-class implementations.
"""

import logging
import os
import threading
import time

from state_io import atomic_write_json, load_json

logger = logging.getLogger(__name__)

GRABBED_FILE = os.environ.get("GRABBED_DB", "/data/grabbed.json")
SKIPPED_FILE = os.environ.get("SKIPPED_DB", "/data/skipped.json")
COOLDOWN_FILE = os.environ.get("COOLDOWN_DB", "/data/cooldown.json")


def _parse_int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        return max(1, min(value, 365))
    except (ValueError, TypeError):
        logger.warning(f"Invalid value for {name}, using default {default}")
        return default


SKIP_EXPIRY_DAYS = _parse_int_env("SKIP_EXPIRY_DAYS", 30)
SEARCH_COOLDOWN_DAYS = _parse_int_env("SEARCH_COOLDOWN_DAYS", 30)


class KeyedJsonStore:
    """
    Thread-safe JSON-file store keyed on media IDs (imdb/tmdb/plex).

    Subclasses that want expiring entries set ``ts_field`` (the epoch field
    inside each entry) and ``expiry_days``; expired entries are purged on
    load and via purge_expired().
    """

    label = "state"  # used in log messages
    ts_field: str | None = None
    expiry_days: int = 0

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self._load()

    @staticmethod
    def _keys(imdb_id: str = None, tmdb_id: str = None,
              rating_key: str = None) -> list[str]:
        """All lookup keys with a non-empty id."""
        keys = []
        if imdb_id:
            keys.append(f"imdb:{imdb_id}")
        if tmdb_id:
            keys.append(f"tmdb:{tmdb_id}")
        if rating_key:
            keys.append(f"plex:{rating_key}")
        return keys

    def _load(self):
        with self._lock:
            self._data = load_json(self._path)
            self._purge_expired_locked()
            logger.info(
                f"Loaded {len(self._data)} {self.label} items from {self._path}")

    def _save_locked(self) -> bool:
        try:
            atomic_write_json(self._path, dict(self._data))
            return True
        except Exception as e:
            logger.error(f"Could not save {self.label} DB: {e}")
            return False

    def purge_expired(self) -> None:
        """Drop entries older than expiry_days (no-op for non-expiring stores)."""
        with self._lock:
            self._purge_expired_locked()

    def _purge_expired_locked(self) -> None:
        if not self.ts_field or self.expiry_days <= 0:
            return
        cutoff = time.time() - (self.expiry_days * 86400)
        before = len(self._data)
        self._data = {
            k: v for k, v in self._data.items()
            if v.get(self.ts_field, 0) > cutoff
        }
        removed = before - len(self._data)
        if removed:
            logger.info(
                f"Purged {removed} expired {self.label} entries "
                f"(>{self.expiry_days} days)")
            self._save_locked()

    def _has(self, imdb_id: str = None, tmdb_id: str = None,
             rating_key: str = None) -> bool:
        keys = self._keys(imdb_id, tmdb_id, rating_key)
        with self._lock:
            return any(k in self._data for k in keys)

    def _set(self, result, entry: dict, defer_save: bool) -> bool:
        """Store entry under every id present on result. Returns save success
        (True when the write is deferred)."""
        with self._lock:
            for key in self._keys(result.imdb_id, result.tmdb_id,
                                  result.rating_key):
                self._data[key] = dict(entry)
            if defer_save:
                return True
            return self._save_locked()

    def flush(self) -> bool:
        """Write pending (deferred) changes to disk."""
        with self._lock:
            return self._save_locked()

    def clear(self) -> int:
        """Clear all entries. Returns count cleared."""
        with self._lock:
            count = len(self._data)
            self._data = {}
            self._save_locked()
            return count

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._data)


class GrabTracker(KeyedJsonStore):
    """Tracks which items have been grabbed to avoid re-downloading.
    Entries never expire."""

    label = "grabbed"

    def __init__(self, path: str = GRABBED_FILE):
        super().__init__(path)

    def is_grabbed(self, imdb_id: str = None, tmdb_id: str = None,
                   rating_key: str = None) -> bool:
        """Check if an item was already grabbed."""
        return self._has(imdb_id, tmdb_id, rating_key)

    def mark_grabbed(self, result) -> bool:
        """Mark an AnalysisResult as grabbed. Returns False if persistence
        failed — the caller must surface that, since a lost grab record causes
        the item to be re-grabbed (re-downloaded) on the next scan."""
        entry = {
            "title": result.display_title,
            "grabbed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "recommended_release": result.recommended_release,
        }
        return self._set(result, entry, defer_save=False)


class SkipTracker(KeyedJsonStore):
    """Tracks items where no indexer results were found, so they can be
    skipped on subsequent scans. Entries expire after SKIP_EXPIRY_DAYS."""

    label = "skipped"
    ts_field = "skipped_ts"
    expiry_days = SKIP_EXPIRY_DAYS

    def __init__(self, path: str = SKIPPED_FILE):
        super().__init__(path)

    def is_skipped(self, imdb_id: str = None, tmdb_id: str = None,
                   rating_key: str = None) -> bool:
        """Check if an item was previously skipped."""
        return self._has(imdb_id, tmdb_id, rating_key)

    def mark_skipped(self, result, reason: str = "no_indexer_results",
                     defer_save: bool = False) -> None:
        """Mark an AnalysisResult as skipped (no indexer results found).

        Args:
            result: The AnalysisResult to mark.
            reason: Why it was skipped ("no_indexer_results" or "all_filtered").
            defer_save: If True, don't write to disk — call flush() later.
        """
        entry = {
            "title": result.display_title,
            "skipped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "skipped_ts": time.time(),
            "reason": reason,
        }
        self._set(result, entry, defer_save)


class SearchCooldownTracker(KeyedJsonStore):
    """Tracks items that have been searched on indexers recently, so they
    are not re-searched on every scan. Entries expire after
    SEARCH_COOLDOWN_DAYS regardless of whether results were found."""

    label = "cooldown"
    ts_field = "ts"
    expiry_days = SEARCH_COOLDOWN_DAYS

    def __init__(self, path: str = COOLDOWN_FILE):
        super().__init__(path)

    def is_on_cooldown(self, imdb_id: str = None, tmdb_id: str = None,
                       rating_key: str = None) -> bool:
        """Check if an item was searched recently."""
        return self._has(imdb_id, tmdb_id, rating_key)

    def mark_searched(self, result, defer_save: bool = False) -> None:
        """Mark an AnalysisResult as recently searched on indexers."""
        entry = {
            "title": result.display_title,
            "searched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.time(),
        }
        self._set(result, entry, defer_save)
