"""
Contract tests for the Radarr/Sonarr release search: it must return a list on
success and None on failure, so a transient indexer/API outage is not mistaken
for "no releases" and skip-listed for weeks.

Uses a minimal `requests` stub so the real client modules import without the
dependency installed.

Run: python -m unittest test_arr_clients
"""

import sys
import types
import unittest

# Minimal requests stub — installed before importing the real clients.
if "requests" not in sys.modules or not hasattr(sys.modules["requests"], "HTTPError"):
    _req = types.ModuleType("requests")

    class _HTTPError(Exception):
        def __init__(self, *a, response=None):
            super().__init__(*a)
            self.response = response

    class _Session:
        def __init__(self):
            self.headers = {}

        def get(self, *a, **k):
            raise AssertionError("test must override client.session")

    _req.HTTPError = _HTTPError
    _req.Session = _Session
    sys.modules["requests"] = _req

from radarr_client import RadarrClient  # noqa: E402
from sonarr_client import SonarrClient  # noqa: E402


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeSession:
    """Session whose GET either returns a fixed payload or raises."""
    def __init__(self, payload=None, error=None):
        self.headers = {}
        self._payload = payload
        self._error = error

    def get(self, url, params=None, timeout=None):
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._payload)


class SearchReleasesContractTests(unittest.TestCase):
    def test_radarr_returns_list_on_success(self):
        client = RadarrClient("http://radarr", "key")
        client.session = _FakeSession(payload=[{"guid": "a"}, {"guid": "b"}])
        self.assertEqual(client.search_releases(1), [{"guid": "a"}, {"guid": "b"}])

    def test_radarr_returns_none_on_failure(self):
        client = RadarrClient("http://radarr", "key")
        client.session = _FakeSession(error=ConnectionError("radarr down"))
        self.assertIsNone(client.search_releases(1))

    def test_sonarr_returns_list_on_success(self):
        client = SonarrClient("http://sonarr", "key")
        client.session = _FakeSession(payload=[{"guid": "x"}])
        self.assertEqual(client.search_releases(9), [{"guid": "x"}])

    def test_sonarr_returns_none_on_failure(self):
        client = SonarrClient("http://sonarr", "key")
        client.session = _FakeSession(error=ConnectionError("sonarr down"))
        self.assertIsNone(client.search_releases(9))


if __name__ == "__main__":
    unittest.main()
