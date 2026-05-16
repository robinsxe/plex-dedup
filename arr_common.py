"""
Shared exceptions and helpers for the Radarr / Sonarr clients.
"""


class StaleReleaseError(Exception):
    """
    Raised when a grab fails because the *arr release cache no longer holds
    the guid (HTTP 404 on POST /api/v3/release). Caller should re-search and retry.
    """
