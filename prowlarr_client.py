"""
Prowlarr API client for searching and grabbing releases via indexers.
"""

import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

# Category IDs used by Prowlarr/Newznab
MOVIE_CATEGORIES = [2000]
TV_CATEGORIES = [5000]


class ProwlarrClient:
    """Client for interacting with Prowlarr's API."""

    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        })

    def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """Send a GET request to the Prowlarr API."""
        resp = self.session.get(f"{self.url}/api/v1/{endpoint}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, data: dict) -> dict | list:
        """Send a POST request to the Prowlarr API."""
        resp = self.session.post(f"{self.url}/api/v1/{endpoint}", json=data)
        resp.raise_for_status()
        return resp.json()

    def test_connection(self) -> bool:
        """Test connection to Prowlarr."""
        try:
            status = self._get("system/status")
            logger.info(f"Connected to Prowlarr v{status.get('version', '?')}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Prowlarr: {e}")
            return False

    def get_indexers(self) -> list[dict]:
        """Get all configured indexers from Prowlarr."""
        try:
            indexers = self._get("indexer")
            logger.info(f"Found {len(indexers)} configured indexers")
            return indexers
        except Exception as e:
            logger.error(f"Failed to get indexers: {e}")
            return []

    def search(
        self,
        query: str,
        categories: list[int] = None,
        indexer_ids: list[int] = None,
    ) -> list[dict]:
        """
        Search for releases across indexers.

        Args:
            query: Search query string.
            categories: List of category IDs to filter by (e.g. [2000] for movies).
            indexer_ids: List of specific indexer IDs to search. Searches all if None.

        Returns:
            List of release result dicts from Prowlarr.
        """
        params = {"query": query, "type": "search"}

        if categories:
            params["categories"] = categories
        if indexer_ids:
            params["indexerIds"] = indexer_ids

        try:
            results = self._get("search", params=params)
            logger.info(
                f"Search for '{query}' returned {len(results)} results"
            )
            return results
        except Exception as e:
            logger.error(f"Search failed for '{query}': {e}")
            return []

    def grab(self, guid: str, indexer_id: int) -> bool:
        """
        Grab a release and push it to the download client.

        Args:
            guid: The GUID of the release to grab.
            indexer_id: The indexer ID the release came from.

        Returns:
            True if the grab was successful, False otherwise.
        """
        try:
            self._post("search", data={"guid": guid, "indexerId": indexer_id})
            logger.info(f"Grabbed release {guid} from indexer {indexer_id}")
            return True
        except Exception as e:
            logger.error(
                f"Failed to grab release {guid} from indexer {indexer_id}: {e}"
            )
            return False

    def search_release(
        self, release_name: str, media_type: str = "movie"
    ) -> list[dict]:
        """
        Convenience method to search for a release with appropriate categories
        and return results sorted by seeders (descending) then age (ascending).

        Args:
            release_name: Name of the release to search for.
            media_type: Either "movie" or "tv" to select category filter.

        Returns:
            Sorted list of release results.
        """
        if media_type == "movie":
            categories = MOVIE_CATEGORIES
        elif media_type == "tv":
            categories = TV_CATEGORIES
        else:
            logger.warning(
                f"Unknown media type '{media_type}', searching without category filter"
            )
            categories = None

        results = self.search(release_name, categories=categories)

        def sort_key(release: dict):
            seeders = release.get("seeders", 0) or 0
            publish_date = release.get("publishDate", "")
            try:
                age = (
                    datetime.now()
                    - datetime.fromisoformat(
                        publish_date.replace("Z", "+00:00")
                    ).replace(tzinfo=None)
                ).total_seconds()
            except (ValueError, TypeError):
                age = float("inf")
            return (-seeders, age)

        results.sort(key=sort_key)
        logger.info(
            f"search_release for '{release_name}' ({media_type}) "
            f"returned {len(results)} sorted results"
        )
        return results
