"""
Web dashboard for Plex Dedup.
Provides a visual interface for managing duplicates and subtitles.
"""

import logging
import subprocess
import threading
from flask import Flask, render_template, jsonify, request

from config import Config
from dedup_engine import DedupEngine, DeduplicationPlan
from subtitle_manager import SubtitleManager
from library_analyzer import LibraryAnalyzer, AnalysisResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# App version — resolved once at startup
def _get_version() -> str:
    # Docker build bakes the full SHA into /app/VERSION
    try:
        with open("VERSION") as f:
            v = f.read().strip()
            if v and v != "unknown":
                return v[:7]
    except FileNotFoundError:
        pass
    # Local dev — use git
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
current_plans: list[DeduplicationPlan] = []
current_analysis: list[AnalysisResult] = []

# Background task progress tracking
scan_lock = threading.Lock()
scan_progress = {
    "running": False,
    "phase": "",  # "analyzing", "searching", "done", "error"
    "current": 0,
    "total": 0,
    "current_title": "",
    "error": None,
}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Get connection status and config info."""
    errors = config.validate()
    if errors:
        return jsonify({"ok": False, "errors": errors})

    connections = engine.test_connections()

    # Test OpenSubtitles separately
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

    # Test Prowlarr separately
    prowlarr_ok = False
    if config.prowlarr_api_key:
        try:
            from prowlarr_client import ProwlarrClient
            pr_client = ProwlarrClient(config.prowlarr_url, config.prowlarr_api_key)
            prowlarr_ok = pr_client.test_connection()
        except Exception as e:
            logger.warning(f"Prowlarr connection test failed: {e}")

    return jsonify({
        "ok": connections.get("plex", False),
        "version": APP_VERSION,
        "plex_connected": connections.get("plex", False),
        "radarr_connected": connections.get("radarr", False),
        "sonarr_connected": connections.get("sonarr", False),
        "opensubtitles_connected": opensubs_ok,
        "prowlarr_connected": prowlarr_ok,
        "libraries": connections.get("libraries", []),
        "config": {
            "movie_library": config.plex_movie_library,
            "tv_library": config.plex_tv_library,
            "dry_run": config.dry_run,
            "keep_strategy": config.keep_strategy,
            "auto_unmonitor": config.auto_unmonitor,
            "subtitle_languages": config.subtitle_languages,
        },
    })


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


def _run_convert_scan(scan_type: str, limit: int):
    """Background worker for convert scan + Prowlarr search."""
    global current_analysis

    with scan_lock:
        scan_progress.update({
            "running": True, "phase": "analyzing", "current": 0,
            "total": 0, "current_title": "Starting...", "error": None,
        })

    def on_progress(current, total, title):
        with scan_lock:
            scan_progress.update({
                "current": current, "total": total, "current_title": title,
            })

    try:
        results = []
        if scan_type in ("movies", "all"):
            with scan_lock:
                scan_progress["current_title"] = f"Scanning {config.plex_movie_library}..."
            res = analyzer.analyze_library(
                config.plex_movie_library, "movie", limit=limit,
                progress_callback=on_progress,
            )
            results.extend(res)

        if scan_type in ("tv", "all"):
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
        needs = [r for r in results if r.status == "needs_replacement"]
        if needs:
            with scan_lock:
                scan_progress.update({
                    "phase": "searching", "current": 0,
                    "total": len(needs), "current_title": "Searching Prowlarr...",
                })
            analyzer.search_replacements(results)

        with scan_lock:
            scan_progress.update({
                "phase": "done", "running": False,
                "current_title": "Complete",
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
            return jsonify({"ok": False, "error": "Scan already in progress"}), 409
        scan_progress["running"] = True

    data = request.json or {}
    scan_type = data.get("scan_type", "movies")
    limit = data.get("limit", 0)

    thread = threading.Thread(
        target=_run_convert_scan, args=(scan_type, limit), daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "message": "Scan started"})


@app.route("/api/convert/progress")
def api_convert_progress():
    """Get current scan progress."""
    with scan_lock:
        result = dict(scan_progress)

    result["ok"] = True
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


@app.route("/api/config", methods=["GET", "PUT"])
def api_config():
    global config, engine, sub_manager, analyzer

    if request.method == "GET":
        return jsonify({
            "dry_run": config.dry_run,
            "keep_strategy": config.keep_strategy,
            "auto_unmonitor": config.auto_unmonitor,
            "delete_files": config.delete_files,
            "plex_movie_library": config.plex_movie_library,
            "plex_tv_library": config.plex_tv_library,
            "subtitle_languages": config.subtitle_languages,
        })

    data = request.json or {}
    if "dry_run" in data:
        config.dry_run = bool(data["dry_run"])
    if "keep_strategy" in data:
        config.keep_strategy = data["keep_strategy"]
    if "auto_unmonitor" in data:
        config.auto_unmonitor = bool(data["auto_unmonitor"])
    if "delete_files" in data:
        config.delete_files = bool(data["delete_files"])
    if "plex_movie_library" in data:
        config.plex_movie_library = data["plex_movie_library"]
    if "plex_tv_library" in data:
        config.plex_tv_library = data["plex_tv_library"]
    if "subtitle_languages" in data:
        config.subtitle_languages = data["subtitle_languages"]

    engine = DedupEngine(config)
    sub_manager = SubtitleManager(config)
    analyzer = LibraryAnalyzer(config)
    return jsonify({"ok": True})


def run():
    import os
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=config.web_host, port=config.web_port, debug=debug)


if __name__ == "__main__":
    run()
