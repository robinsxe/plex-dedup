"""
Shared sys.modules stubs so test modules can import production modules
without real deps (requests, plexapi, dotenv), plus mirror copies of the
opensubtitles_client constants. test_opensubtitles_client imports the real
module and pins these mirrors against it, so drift fails a test.
"""

import sys
import types
from unittest.mock import MagicMock

PERMANENT_DOWNLOAD_ERRORS = frozenset(
    {"not_logged_in", "reauthentication_failed", "no_download_link"}
)
STATUS_TRANSIENT = frozenset({"search_error", "download_failed"})
STATUS_FAILURE = frozenset(
    {"download_failed", "no_file_id", "error", "search_error", "video_missing"}
)


def _install(module_name: str, **attrs):
    """Register a stub module unless something (a stub or the real module)
    already occupies the name — in a combined run, first loader wins."""
    if module_name in sys.modules:
        return
    mod = types.ModuleType(module_name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[module_name] = mod


def install_common_stubs():
    _install("config", Config=type("Config", (), {}))
    # Factory (not a bare MagicMock class) so PlexClient(url, token) yields
    # a fully mockable instance instead of a str-spec'd mock
    _install("plex_client", PlexClient=lambda *a, **k: MagicMock())
    _install(
        "opensubtitles_client",
        OpenSubtitlesClient=MagicMock,
        DownloadLimitReached=type("DownloadLimitReached", (Exception,), {}),
        SubtitleSearchError=type("SubtitleSearchError", (Exception,), {}),
        PERMANENT_DOWNLOAD_ERRORS=PERMANENT_DOWNLOAD_ERRORS,
        STATUS_TRANSIENT=STATUS_TRANSIENT,
        STATUS_FAILURE=STATUS_FAILURE,
    )
    _install("prowlarr_client", ProwlarrClient=MagicMock)
    _install("radarr_client", RadarrClient=MagicMock)
    _install("sonarr_client", SonarrClient=MagicMock)
