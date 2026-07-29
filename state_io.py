"""
Crash-safe JSON persistence for the app's small state files (trackers, queue,
settings). Two guarantees the previous in-place `open(path, "w")` writes lacked:

  * atomic_write_json — a power loss or OOM-kill mid-write can no longer leave a
    truncated/half-written file: the new content is written to a temp file in the
    same directory and os.replace()'d into place (atomic on POSIX).
  * load_json — a file that is already corrupt is moved aside (quarantined)
    rather than silently reset to empty and then overwritten, so the bad state is
    recoverable and the failure is visible in the logs.
"""

import json
import logging
import os
import tempfile
import time

logger = logging.getLogger(__name__)


def atomic_write_json(path: str, data) -> None:
    """Write `data` as JSON to `path` atomically. Raises on failure."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: str, default=None):
    """Load JSON from `path`.

    Missing file -> `default`. A corrupt/unreadable file is renamed aside to
    `<path>.corrupt-<epoch>` and `default` is returned, so the next save starts
    clean without silently destroying evidence of the corruption.
    """
    if default is None:
        default = {}
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, ValueError, OSError) as e:
        quarantine = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, quarantine)
            logger.error(
                "Corrupt state file %s (%s) — moved to %s, starting fresh",
                path, e, quarantine,
            )
        except OSError as move_err:
            logger.error(
                "Corrupt state file %s (%s); could not quarantine it: %s",
                path, e, move_err,
            )
        return default
