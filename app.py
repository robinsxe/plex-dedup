"""
Web dashboard for Plex Dedup.
Provides a visual interface for managing duplicates and subtitles.
"""

import logging
import re
import subprocess
import threading
import time
from collections import deque
from flask import Flask, render_template, jsonify, request

from config import Config, SETTINGS_FILE
from dedup_engine import DedupEngine, DeduplicationPlan
from subtitle_manager import SubtitleManager
from library_analyzer import LibraryAnalyzer, AnalysisResult
from subtitle_queue import SubtitleQueue

_SENSITIVE_PATTERN = re.compile(
    r'(api[_-]?key|token|password|authorization|bearer|secret)[=:\s]+\S+',
    re.IGNORECASE,
)


def _redact(message: str) -> str:
    """Redact sensitive values from log messages."""
    return _SENSITIVE_PATTERN.sub(
        lambda m: m.group().split("=")[0] + "=***REDACTED***"
        if "=" in m.group()
        else m.group().split(":")[0] + ": ***REDACTED***",
        message,
    )


class MemoryLogHandler(logging.Handler):
    """Ring buffer that keeps the last N log records in memory for the UI."""

    def __init__(self, capacity=500):
        super().__init__()
        self._buffer = deque(maxlen=capacity)

    def emit(self, record):
        try:
            self._buffer.append({
                "timestamp": time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(record.created)
                ),
                "level": record.levelname,
                "logger": record.name,
                "message": _redact(record.getMessage()),
            })
        except Exception:
            self.handleError(record)

    def get_logs(self, limit=200, level=None):
        self.acquire()
        try:
            logs = list(self._buffer)
        finally:
            self.release()
        if level:
            logs = [entry for entry in logs if entry["level"] == level.upper()]
        limit = min(limit, 500)
        return logs[-limit:]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log_buffer = MemoryLogHandler(capacity=500)
logging.getLogger().addHandler(log_buffer)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# App version — resolved once at startup
def _get_version() -> str:
    # Docker build bakes version into /app/VERSION (tag like v1.2.0 or SHA)
    try:
        with open("VERSION") as f:
            v = f.read().strip()
            if v and v != "unknown":
                # If it's a tag (e.g. v1.2.0), return as-is; otherwise shorten SHA
                return v if v.startswith("v") else v[:7]
    except FileNotFoundError:
        pass
    # Local dev — prefer tag if HEAD is tagged, otherwise short SHA
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        if tag:
            return tag
    except Exception:
        pass
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"

APP_VERSION = _get_version()

# Global state
config = Config.from_env()
engine = DedupEngine(config)
sub_manager = SubtitleManager(config)
analyzer = LibraryAnalyzer(config)
sub_queue = SubtitleQueue(config)
current_plans: list[DeduplicationPlan] = []
current_analysis: list[AnalysisResult] = []

# Background task progress tracking
SCAN_TIMEOUT_SECONDS = 1800  # 30 minutes — auto-expire stale locks

scan_lock = threading.Lock()
scan_cancel = threading.Event()
scan_progress = {
    "running": False,
    "phase": "",  # "analyzing", "searching", "done", "error", "cancelled"
    "current": 0,
    "total": 0,
    "current_title": "",
    "error": None,
    "started_at": None,
}


@app.route("/")
def index():
    return render_template("index.html")


STATUS_CACHE_TTL = 60  # seconds — avoid hammering all services on every poll
_status_cache = {"data": None, "expires": 0}


@app.route("/api/status")
def api_status():
    """Get connection status and config info (connection results cached for 60s)."""
    errors = config.validate()
    if errors:
        return jsonify({"ok": False, "errors": errors})

    now = time.time()
    if _status_cache["data"] and now < _status_cache["expires"]:
        cached = dict(_status_cache["data"])
    else:
        connections = engine.test_connections()

        opensubs_ok = False
        if config.opensubtitles_api_key:
            try:
                from opensubtitles_client import OpenSubtitlesClient
                os_client = OpenSubtitlesClient(
                    config.opensubtitles_api_key,
                    config.opensubtitles_username,
                    config.opensubtitles_password,
                )
                opensubs_ok = os_client.test_connection()
            except Exception as e:
                logger.warning(f"OpenSubtitles connection test failed: {e}")

        prowlarr_ok = False
        if config.prowlarr_api_key:
            try:
                from prowlarr_client import ProwlarrClient
                pr_client = ProwlarrClient(config.prowlarr_url, config.prowlarr_api_key)
                prowlarr_ok = pr_client.test_connection()
            except Exception as e:
                logger.warning(f"Prowlarr connection test failed: {e}")

        cached = {
            "ok": connections.get("plex", False),
            "version": APP_VERSION,
            "plex_connected": connections.get("plex", False),
            "radarr_connected": connections.get("radarr", False),
            "sonarr_connected": connections.get("sonarr", False),
            "opensubtitles_connected": opensubs_ok,
            "prowlarr_connected": prowlarr_ok,
            "libraries": connections.get("libraries", []),
        }
        _status_cache["data"] = cached
        _status_cache["expires"] = now + STATUS_CACHE_TTL

    # Always return fresh config (not cached — user can change it)
    cached["config"] = {
        "movie_library": config.plex_movie_library,
        "tv_library": config.plex_tv_library,
        "dry_run": config.dry_run,
        "keep_strategy": config.keep_strategy,
        "auto_unmonitor": config.auto_unmonitor,
        "subtitle_languages": config.subtitle_languages,
    }
    return jsonify(cached)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Scan for duplicates in movies, TV, or both."""
    global current_plans

    data = request.json or {}
    scan_type = data.get("scan_type", "all")  # "movies", "tv", "all"

    try:
        if scan_type == "all":
            current_plans = engine.scan_all()
        elif scan_type == "tv":
            library = data.get("library", config.plex_tv_library)
            current_plans = engine.scan(library, "show")
        else:
            library = data.get("library", config.plex_movie_library)
            current_plans = engine.scan(library, "movie")

        summary = engine.get_summary(current_plans)
        plans_data = [p.to_dict() for p in current_plans]
        return jsonify({"ok": True, "summary": summary, "plans": plans_data})
    except Exception as e:
        logger.error(f"Scan failed: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/execute", methods=["POST"])
def api_execute():
    """Execute dedup plans."""
    global current_plans
    data = request.json or {}
    selected_keys = data.get("selected")

    if not current_plans:
        return jsonify({"ok": False, "error": "No scan results. Run a scan first."})

    plans_to_run = current_plans
    if selected_keys:
        plans_to_run = [
            p for p in current_plans
            if p.group.plex_rating_key in selected_keys
        ]

    result = engine.execute_all(plans_to_run)
    return jsonify({
        "ok": True,
        "result": result,
        "plans": [p.to_dict() for p in plans_to_run],
    })


@app.route("/api/execute/<rating_key>", methods=["POST"])
def api_execute_single(rating_key):
    plan = next(
        (p for p in current_plans if p.group.plex_rating_key == rating_key),
        None,
    )
    if not plan:
        return jsonify({"ok": False, "error": "Plan not found"}), 404

    success = engine.execute_plan(plan)
    return jsonify({"ok": success, "plan": plan.to_dict()})


@app.route("/api/subtitles/scan", methods=["POST"])
def api_subtitle_scan():
    """Scan for missing subtitles."""
    data = request.json or {}
    scan_type = data.get("scan_type", "movies")  # "movies", "tv", "all"
    languages = data.get("languages", config.subtitle_languages)

    results = []
    try:
        if scan_type in ("movies", "all"):
            missing = sub_manager.scan_missing_subtitles(
                config.plex_movie_library, "movie", languages
            )
            results.extend(missing)

        if scan_type in ("tv", "all"):
            missing = sub_manager.scan_missing_subtitles(
                config.plex_tv_library, "show", languages
            )
            results.extend(missing)

        return jsonify({
            "ok": True,
            "total_missing": len(results),
            "items": results[:200],  # Limit response size
        })
    except Exception as e:
        logger.error(f"Subtitle scan failed: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/subtitles/download", methods=["POST"])
def api_subtitle_download():
    """Download missing subtitles."""
    data = request.json or {}
    scan_type = data.get("scan_type", "movies")
    languages = data.get("languages", config.subtitle_languages)
    limit = data.get("limit", 50)

    results = []
    try:
        if scan_type in ("movies", "all"):
            res = sub_manager.download_subtitles(
                config.plex_movie_library, "movie", languages,
                dry_run=config.dry_run, limit=limit,
            )
            results.extend(res)

        if scan_type in ("tv", "all"):
            res = sub_manager.download_subtitles(
                config.plex_tv_library, "show", languages,
                dry_run=config.dry_run, limit=limit,
            )
            results.extend(res)

        summary = sub_manager.get_summary(results)
        return jsonify({
            "ok": True,
            "summary": summary,
            "results": [r.to_dict() for r in results[:200]],
        })
    except Exception as e:
        logger.error(f"Subtitle download failed: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


class _ScanCancelled(Exception):
    """Raised when the user cancels a running scan."""


def _run_convert_scan(scan_type: str, limit: int, search_limit: int = 50):
    """Background worker for convert scan + Prowlarr search."""
    global current_analysis

    with scan_lock:
        scan_progress.update({
            "running": True, "phase": "analyzing", "current": 0,
            "total": 0, "current_title": "Starting...", "error": None,
        })

    def _check_cancel():
        if scan_cancel.is_set():
            raise _ScanCancelled()

    def on_progress(current, total, title):
        _check_cancel()
        with scan_lock:
            scan_progress.update({
                "current": current, "total": total, "current_title": title,
            })

    try:
        results = []
        if scan_type in ("movies", "all"):
            _check_cancel()
            with scan_lock:
                scan_progress["current_title"] = f"Scanning {config.plex_movie_library}..."
            res = analyzer.analyze_library(
                config.plex_movie_library, "movie", limit=limit,
                progress_callback=on_progress,
            )
            results.extend(res)

        if scan_type in ("tv", "all"):
            _check_cancel()
            with scan_lock:
                scan_progress["current_title"] = f"Scanning {config.plex_tv_library}..."
                scan_progress["current"] = 0
            res = analyzer.analyze_library(
                config.plex_tv_library, "show", limit=limit,
                progress_callback=on_progress,
            )
            results.extend(res)

        current_analysis = results

        # Auto-search Prowlarr for items needing replacement
        # search_limit: 0 = all, -1 = skip, N = first N items
        needs = [r for r in results if r.status == "needs_replacement"]
        if needs and config.prowlarr_api_key and search_limit != -1:
            _check_cancel()
            search_count = len(needs) if search_limit == 0 else min(search_limit, len(needs))

            def on_search_progress(current, total, title):
                _check_cancel()
                with scan_lock:
                    scan_progress.update({
                        "current": current, "total": total,
                        "current_title": f"[{current}/{total}] {title}",
                    })

            with scan_lock:
                scan_progress.update({
                    "phase": "searching", "current": 0,
                    "total": search_count,
                    "current_title": "Searching Prowlarr...",
                })
            analyzer.search_replacements(
                results, limit=search_limit,
                progress_callback=on_search_progress,
            )

        with scan_lock:
            scan_progress.update({
                "phase": "done", "running": False,
                "current_title": "Complete",
            })

    except _ScanCancelled:
        logger.info("Scan cancelled by user")
        with scan_lock:
            scan_progress.update({
                "phase": "cancelled", "running": False,
                "error": "Scan was cancelled by user",
                "started_at": None,
            })

    except Exception as e:
        logger.error(f"Convert scan failed: {e}", exc_info=True)
        with scan_lock:
            scan_progress.update({
                "phase": "error", "running": False, "error": str(e),
            })


@app.route("/api/convert/scan", methods=["POST"])
def api_convert_scan():
    """Start library analysis in background."""
    with scan_lock:
        if scan_progress["running"]:
            started = scan_progress.get("started_at")
            if started and (time.time() - started) > SCAN_TIMEOUT_SECONDS:
                logger.warning("Scan lock expired after timeout — resetting")
                scan_progress.update({
                    "running": False, "phase": "error",
                    "error": "Scan timed out and was reset",
                    "started_at": None,
                })
            else:
                return jsonify({"ok": False, "error": "Scan already in progress"}), 409
        scan_progress["running"] = True
        scan_progress["started_at"] = time.time()

    scan_cancel.clear()
    data = request.json or {}
    scan_type = data.get("scan_type", "movies")
    limit = data.get("limit", 0)
    search_limit = data.get("search_limit", 50)

    thread = threading.Thread(
        target=_run_convert_scan, args=(scan_type, limit, search_limit), daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "message": "Scan started"})


@app.route("/api/convert/progress")
def api_convert_progress():
    """Get current scan progress."""
    with scan_lock:
        result = dict(scan_progress)

    result["ok"] = True
    result["grabbed_count"] = analyzer.grab_tracker.count
    result["skipped_count"] = analyzer.skip_tracker.count
    result["cooldown_count"] = analyzer.search_cooldown.count
    if result["phase"] == "done" and current_analysis:
        summary = LibraryAnalyzer.get_summary(current_analysis)
        result["summary"] = summary
        result["results"] = [r.to_dict() for r in current_analysis[:200]]

    return jsonify(result)


@app.route("/api/convert/search", methods=["POST"])
def api_convert_search():
    """Search Prowlarr for replacement releases."""
    global current_analysis

    if not current_analysis:
        return jsonify({"ok": False, "error": "No analysis results. Run convert/scan first."})

    data = request.json or {}
    limit = data.get("limit", 0)

    try:
        analyzer.search_replacements(current_analysis, limit=limit)
        summary = LibraryAnalyzer.get_summary(current_analysis)
        needs = [r for r in current_analysis if r.status == "needs_replacement"]
        return jsonify({
            "ok": True,
            "summary": summary,
            "needs_replacement": [r.to_dict() for r in needs[:200]],
        })
    except Exception as e:
        logger.error(f"Convert search failed: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/convert/execute", methods=["POST"])
def api_convert_execute():
    """Execute replacement downloads via Prowlarr."""
    global current_analysis

    if not current_analysis:
        return jsonify({"ok": False, "error": "No analysis results. Run convert/scan first."})

    try:
        result = analyzer.execute_all(current_analysis, dry_run=config.dry_run)
        return jsonify({
            "ok": True,
            "result": result,
            "results": [
                r.to_dict() for r in current_analysis
                if r.status in ("replaced", "needs_replacement")
            ][:200],
        })
    except Exception as e:
        logger.error(f"Convert execute failed: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/convert/grabbed", methods=["GET", "DELETE"])
def api_convert_grabbed():
    """View or clear the grabbed items tracker."""
    if request.method == "DELETE":
        count = analyzer.grab_tracker.clear()
        return jsonify({"ok": True, "cleared": count})
    return jsonify({
        "ok": True,
        "count": analyzer.grab_tracker.count,
    })


@app.route("/api/convert/skipped", methods=["GET", "DELETE"])
def api_convert_skipped():
    """View or clear the skipped items tracker."""
    if request.method == "DELETE":
        count = analyzer.skip_tracker.clear()
        return jsonify({"ok": True, "cleared": count})
    return jsonify({
        "ok": True,
        "count": analyzer.skip_tracker.count,
    })


@app.route("/api/convert/cooldown", methods=["GET", "DELETE"])
def api_convert_cooldown():
    """View or clear the search cooldown tracker."""
    if request.method == "DELETE":
        count = analyzer.search_cooldown.clear()
        return jsonify({"ok": True, "cleared": count})
    return jsonify({
        "ok": True,
        "count": analyzer.search_cooldown.count,
    })


@app.route("/api/convert/download-subs", methods=["POST"])
def api_convert_download_subs():
    """Download .srt files for 'has_subs' items from the current analysis."""
    global current_analysis

    if not current_analysis:
        return jsonify({"ok": False, "error": "No analysis results. Run a scan first."}), 400

    data = request.json or {}
    try:
        limit = max(0, int(data.get("limit", 0)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit must be a non-negative integer"}), 400

    has_subs_items = [
        {
            "file_path": r.file_path,
            "media_type": r.media_type,
            "title": r.title,
            "year": r.year,
            "imdb_id": r.imdb_id,
            "tmdb_id": r.tmdb_id,
            "season_number": r.season_number,
            "episode_number": r.episode_number,
            "show_title": r.show_title,
        }
        for r in current_analysis if r.status == "has_subs"
    ]

    if not has_subs_items:
        return jsonify({"ok": True, "summary": {"total_items_processed": 0}, "results": []})

    try:
        results = sub_manager.download_for_items(
            has_subs_items,
            languages=config.subtitle_languages,
            dry_run=config.dry_run,
            limit=limit,
        )
        summary = sub_manager.get_summary(results)
        return jsonify({
            "ok": True,
            "summary": summary,
            "results": [r.to_dict() for r in results[:200]],
        })
    except Exception as e:
        logger.error(f"Subtitle download for analysis items failed: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/convert/cancel", methods=["POST"])
def api_convert_cancel():
    """Cancel a running scan or force-reset a stuck lock."""
    scan_cancel.set()
    with scan_lock:
        was_running = scan_progress["running"]
        # If the thread is alive it will detect the event and clean up.
        # If it's already dead (stale lock), force-reset here.
        if was_running:
            scan_progress.update({
                "running": False,
                "phase": "cancelled",
                "error": "Scan was cancelled by user",
                "started_at": None,
            })
    logger.info(f"Scan cancel requested (was_running={was_running})")
    return jsonify({"ok": True, "was_running": was_running})


@app.route("/api/logs")
def api_logs():
    """Get recent log entries."""
    limit = request.args.get("limit", 200, type=int)
    level = request.args.get("level", None)
    logs = log_buffer.get_logs(limit=limit, level=level)
    return jsonify({"ok": True, "logs": logs, "total": len(logs)})


_SECRET_MASK = "__MASKED__"


def _mask(value: str) -> str:
    """Mask a secret value for API display."""
    if not value:
        return ""
    return _SECRET_MASK


@app.route("/api/config", methods=["GET", "PUT"])
def api_config():
    global config, engine, sub_manager, analyzer, sub_queue

    if request.method == "GET":
        return jsonify({
            # Plex
            "plex_url": config.plex_url,
            "plex_token": _mask(config.plex_token),
            "plex_movie_library": config.plex_movie_library,
            "plex_tv_library": config.plex_tv_library,
            # Radarr
            "radarr_url": config.radarr_url,
            "radarr_api_key": _mask(config.radarr_api_key),
            # Sonarr
            "sonarr_url": config.sonarr_url,
            "sonarr_api_key": _mask(config.sonarr_api_key),
            # Prowlarr
            "prowlarr_url": config.prowlarr_url,
            "prowlarr_api_key": _mask(config.prowlarr_api_key),
            # OpenSubtitles
            "opensubtitles_api_key": _mask(config.opensubtitles_api_key),
            "opensubtitles_username": config.opensubtitles_username,
            "opensubtitles_password": _mask(config.opensubtitles_password),
            # Behavior
            "dry_run": config.dry_run,
            "keep_strategy": config.keep_strategy,
            "auto_unmonitor": config.auto_unmonitor,
            "delete_files": config.delete_files,
            "subtitle_languages": config.subtitle_languages,
            # Queue
            "subtitle_daily_limit": config.subtitle_daily_limit,
            "subtitle_queue_hour": config.subtitle_queue_hour,
        })

    data = request.json or {}

    # String fields — only update if not masked placeholder
    str_fields = {
        "plex_url": "plex_url",
        "plex_token": "plex_token",
        "plex_movie_library": "plex_movie_library",
        "plex_tv_library": "plex_tv_library",
        "radarr_url": "radarr_url",
        "radarr_api_key": "radarr_api_key",
        "sonarr_url": "sonarr_url",
        "sonarr_api_key": "sonarr_api_key",
        "prowlarr_url": "prowlarr_url",
        "prowlarr_api_key": "prowlarr_api_key",
        "opensubtitles_api_key": "opensubtitles_api_key",
        "opensubtitles_username": "opensubtitles_username",
        "opensubtitles_password": "opensubtitles_password",
        "keep_strategy": "keep_strategy",
    }
    # Validate keep_strategy if provided
    if "keep_strategy" in data:
        if data["keep_strategy"] not in ("best_quality", "largest_file", "newest"):
            return jsonify({"ok": False, "error": "Invalid keep_strategy"}), 400

    # Validate URL fields
    url_fields = {"plex_url", "radarr_url", "sonarr_url", "prowlarr_url"}
    for url_key in url_fields:
        if url_key in data and data[url_key]:
            from urllib.parse import urlparse
            parsed = urlparse(data[url_key])
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return jsonify({"ok": False, "error": f"Invalid URL for {url_key}"}), 400

    for json_key, attr in str_fields.items():
        if json_key in data:
            value = data[json_key]
            # Skip masked/empty values — don't overwrite real secrets
            if not value or value == _SECRET_MASK:
                continue
            if not isinstance(value, str):
                continue
            setattr(config, attr, value)

    # Boolean fields
    bool_fields = ["dry_run", "auto_unmonitor", "delete_files"]
    for key in bool_fields:
        if key in data:
            setattr(config, key, bool(data[key]))

    # List fields
    if "subtitle_languages" in data:
        val = data["subtitle_languages"]
        if isinstance(val, str):
            config.subtitle_languages = [l.strip() for l in val.split(",") if l.strip()]
        elif isinstance(val, list):
            config.subtitle_languages = val

    # Integer fields
    if "subtitle_daily_limit" in data:
        try:
            config.subtitle_daily_limit = max(1, int(data["subtitle_daily_limit"]))
        except (TypeError, ValueError):
            pass
    if "subtitle_queue_hour" in data:
        try:
            config.subtitle_queue_hour = max(0, min(23, int(data["subtitle_queue_hour"])))
        except (TypeError, ValueError):
            pass

    # Persist and rebuild services
    config.save_to_file()
    _status_cache["data"] = None  # Invalidate connection cache
    engine = DedupEngine(config)
    sub_manager = SubtitleManager(config)
    analyzer = LibraryAnalyzer(config)
    sub_queue = SubtitleQueue(config)
    return jsonify({"ok": True})


# ---- Subtitle Queue ----

@app.route("/api/subtitles/queue", methods=["GET", "POST", "DELETE"])
def api_subtitle_queue():
    """Manage the subtitle download queue."""
    global sub_queue

    if request.method == "GET":
        status = sub_queue.get_status()
        status["ok"] = True
        return jsonify(status)

    if request.method == "DELETE":
        data = request.json or {}
        status_filter = data.get("status")
        count = sub_queue.clear(status=status_filter)
        return jsonify({"ok": True, "cleared": count})

    # POST — add has_subs items from current analysis to queue
    if not current_analysis:
        return jsonify({"ok": False, "error": "No analysis results. Run a scan first."}), 400

    has_subs_items = [
        {
            "file_path": r.file_path,
            "media_type": r.media_type,
            "title": r.title,
            "year": r.year,
            "imdb_id": r.imdb_id,
            "tmdb_id": r.tmdb_id,
            "season_number": r.season_number,
            "episode_number": r.episode_number,
            "show_title": r.show_title,
        }
        for r in current_analysis if r.status == "has_subs"
    ]

    result = sub_queue.add(has_subs_items)
    result["ok"] = True
    return jsonify(result)


@app.route("/api/subtitles/queue/process", methods=["POST"])
def api_subtitle_queue_process():
    """Manually trigger queue processing (respects daily limit)."""
    data = request.json or {}
    dry_run = data.get("dry_run", config.dry_run)

    try:
        result = sub_queue.process(dry_run=dry_run)
        result["ok"] = True
        return jsonify(result)
    except Exception as e:
        logger.error(f"Queue processing failed: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


# ---- Background Scheduler ----

def _queue_scheduler():
    """Background thread that processes the subtitle queue daily."""
    logger.info(
        f"Subtitle queue scheduler started "
        f"(runs daily at {config.subtitle_queue_hour:02d}:00)"
    )
    while True:
        try:
            now = time.localtime()
            # Calculate seconds until next run
            target_hour = config.subtitle_queue_hour
            if now.tm_hour < target_hour:
                wait_hours = target_hour - now.tm_hour
            elif now.tm_hour == target_hour and now.tm_min == 0:
                wait_hours = 0
            else:
                wait_hours = 24 - now.tm_hour + target_hour

            wait_seconds = (wait_hours * 3600) - (now.tm_min * 60) - now.tm_sec
            if wait_seconds <= 0:
                wait_seconds = 86400  # Wait a full day

            next_run = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(time.time() + wait_seconds),
            )
            logger.info(f"Queue scheduler: next run at {next_run}")

            # Sleep in 60s chunks so we can pick up config changes
            slept = 0
            while slept < wait_seconds:
                time.sleep(min(60, wait_seconds - slept))
                slept += 60

            if sub_queue.pending_count > 0:
                logger.info(
                    f"Queue scheduler: processing {sub_queue.pending_count} "
                    f"pending items"
                )
                sub_queue.process(dry_run=False)
            else:
                logger.info("Queue scheduler: no pending items, skipping")

        except Exception as e:
            logger.error(f"Queue scheduler error: {e}", exc_info=True)
            time.sleep(3600)  # Wait an hour on error


_scheduler_thread = threading.Thread(target=_queue_scheduler, daemon=True)
_scheduler_thread.start()


def run():
    import os
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=config.web_host, port=config.web_port, debug=debug)


if __name__ == "__main__":
    run()
