"""HTTP client for the De Lijn GTFS API."""

import logging
import aiohttp

from .const import (
    API_BASE_URL,
    API_STATIC_PATH,
    API_TRIP_UPDATES_PATH,
    API_ALERTS_PATH,
    API_AUTH_HEADER,
)

_LOGGER = logging.getLogger(__name__)


class DeLijnApiError(Exception):
    """Raised when an API call fails."""


class DeLijnApiClient:
    """Handles all HTTP communication with the De Lijn GTFS API."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._headers = {
            API_AUTH_HEADER: api_key,
            "Cache-Control": "no-cache",
        }

    async def fetch_trip_updates(self) -> dict:
        """Fetch the GTFS-RT trip updates feed (JSON format)."""
        url = f"{API_BASE_URL}{API_TRIP_UPDATES_PATH}?format=json"
        return await self._get_json(url)

    async def fetch_alerts(self) -> dict:
        """Fetch the GTFS-RT service alerts feed (JSON format)."""
        url = f"{API_BASE_URL}{API_ALERTS_PATH}?format=json"
        return await self._get_json(url)

    async def fetch_static_gtfs(self) -> tuple[bytes, str | None]:
        """Download the full static GTFS ZIP.

        Returns a tuple of (zip_bytes, last_modified_header).
        """
        url = f"{API_BASE_URL}{API_STATIC_PATH}"
        try:
            async with self._session.get(url, headers=self._headers) as response:
                response.raise_for_status()
                last_modified = response.headers.get("Last-Modified")
                content = await response.read()
                return content, last_modified
        except aiohttp.ClientError as err:
            raise DeLijnApiError(f"Failed to download static GTFS: {err}") from err

    async def get_static_last_modified(self) -> str | None:
        """Retrieve the Last-Modified header of the static GTFS without reading the body.

        Opens a streaming connection, reads only the response headers, then closes.
        This avoids downloading the full 200 MB file when checking for updates.
        Note: still counts as one API quota call.
        """
        url = f"{API_BASE_URL}{API_STATIC_PATH}"
        try:
            async with self._session.get(
                url, headers=self._headers, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                response.raise_for_status()
                last_modified = response.headers.get("Last-Modified")
                # Close without reading body
                return last_modified
        except aiohttp.ClientError as err:
            _LOGGER.warning("Could not check static GTFS Last-Modified: %s", err)
            return None

    async def validate_api_key(self) -> bool:
        """Test the API key by fetching the RT feed and checking the response shape."""
        try:
            data = await self.fetch_trip_updates()
            return isinstance(data, dict) and "entity" in data
        except DeLijnApiError:
            return False

    async def _get_json(self, url: str) -> dict:
        """Perform a GET request and return the parsed JSON response."""
        try:
            async with self._session.get(url, headers=self._headers) as response:
                response.raise_for_status()
                return await response.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            raise DeLijnApiError(f"API returned HTTP {err.status}: {url}") from err
        except aiohttp.ClientError as err:
            raise DeLijnApiError(f"Connection error: {err}") from err
