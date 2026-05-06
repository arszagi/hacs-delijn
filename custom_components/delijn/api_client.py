"""HTTP client for the De Lijn V1 Core API."""

import logging
import aiohttp

from .const import API_BASE_URL, API_AUTH_HEADER, API_ENTITIES

_LOGGER = logging.getLogger(__name__)


class DeLijnApiError(Exception):
    """Raised when an API call fails."""


class DeLijnApiClient:
    """Handles all HTTP communication with the De Lijn V1 Core API.

    Base URL: https://api.delijn.be/DLKernOpenData/api/v1
    Auth: Ocp-Apim-Subscription-Key header
    """

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._headers = {API_AUTH_HEADER: api_key}

    # ------------------------------------------------------------------
    # Stop data
    # ------------------------------------------------------------------

    async def fetch_stops_for_entity(self, entity_id: int) -> list[dict]:
        """Fetch all stops managed by a regional entity."""
        data = await self._get(f"/entiteiten/{entity_id}/haltes")
        return data.get("haltes", [])

    async def fetch_all_stops(self) -> list[dict]:
        """Fetch stops from all 5 De Lijn regional entities."""
        all_stops = []
        for entity_id in API_ENTITIES:
            try:
                stops = await self.fetch_stops_for_entity(entity_id)
                all_stops.extend(stops)
                _LOGGER.debug("Entity %d: %d stops", entity_id, len(stops))
            except DeLijnApiError as err:
                _LOGGER.warning("Could not fetch stops for entity %d: %s", entity_id, err)
        return all_stops

    # ------------------------------------------------------------------
    # Real-time data per stop
    # ------------------------------------------------------------------

    async def fetch_realtime(
        self, entiteitnummer: str, haltenummer: str, max_departures: int = 10
    ) -> dict:
        """Fetch real-time departures for a single stop."""
        return await self._get(
            f"/haltes/{entiteitnummer}/{haltenummer}/real-time",
            params={"maxAantalDoorkomsten": max_departures},
        )

    async def fetch_realtime_batch(self, haltesleutels: str, max_departures: int = 10) -> dict:
        """Fetch real-time departures for multiple stops in one call.

        haltesleutels format: "{entity}_{halte}_{entity}_{halte}_..."
        e.g. "3_354661_3_304660_3_304661"
        """
        return await self._get(
            f"/haltes/lijst/{haltesleutels}/real-time",
            params={"maxAantalDoorkomsten": max_departures},
        )

    async def fetch_disruptions(self, entiteitnummer: str, haltenummer: str) -> dict:
        """Fetch active disruptions and diversions for a single stop."""
        return await self._get(f"/haltes/{entiteitnummer}/{haltenummer}/storingen")

    async def fetch_disruptions_batch(self, haltesleutels: str) -> dict:
        """Fetch storingen for multiple stops in one call."""
        return await self._get(f"/haltes/lijst/{haltesleutels}/storingen")

    async def fetch_omleidingen_batch(self, haltesleutels: str) -> dict:
        """Fetch omleidingen (route diversions) for multiple stops in one call."""
        return await self._get(f"/haltes/lijst/{haltesleutels}/omleidingen")

    async def fetch_timetable(
        self, entiteitnummer: str, haltenummer: str, date: str | None = None
    ) -> dict:
        """Fetch the full scheduled timetable for a stop (fallback when RT unavailable).

        date format: YYYY-MM-DD. Defaults to today.
        """
        params = {"datum": date} if date else {}
        return await self._get(
            f"/haltes/{entiteitnummer}/{haltenummer}/dienstregelingen", params=params
        )

    async def fetch_lines_for_stop(self, entiteitnummer: str, haltenummer: str) -> list[dict]:
        """Fetch the line directions serving a stop."""
        data = await self._get(f"/haltes/{entiteitnummer}/{haltenummer}/lijnrichtingen")
        return data.get("lijnrichtingen", [])

    async def fetch_line_disruptions(
        self, entiteitnummer: str, lijnnummer: str, richting: str
    ) -> dict:
        """Fetch diversions/disruptions for a specific line direction."""
        return await self._get(
            f"/lijnen/{entiteitnummer}/{lijnnummer}/lijnrichtingen/{richting}/omleidingen"
        )

    async def fetch_line_colors_batch(self, lijnsleutels: str) -> dict:
        """Fetch badge colors for multiple lines in one call.

        lijnsleutels format: "{entity}_{lijn}_{entity}_{lijn}_..."
        e.g. "3_170_3_171"
        """
        return await self._get(f"/lijnen/lijst/{lijnsleutels}/lijnkleuren")

    async def fetch_line_colors(self, entiteitnummer: str, lijnnummer: str) -> dict | None:
        """Return the 4 color codes for a line badge (voorgrond, achtergrond, etc.)."""
        try:
            return await self._get(f"/lijnen/{entiteitnummer}/{lijnnummer}/lijnkleuren")
        except DeLijnApiError:
            return None

    async def fetch_color(self, code: str) -> str | None:
        """Return the hex color value for a De Lijn color code (e.g. 'LG' → '#BBDD00')."""
        try:
            data = await self._get(f"/kleuren/{code}")
            hex_val = data.get("hex", "")
            return f"#{hex_val}" if hex_val else None
        except DeLijnApiError:
            return None

    async def fetch_public_line_number(
        self, entiteitnummer: str, lijnnummer: str
    ) -> str | None:
        """Return the public-facing line number (e.g. 'R70') for an internal lijnnummer."""
        try:
            data = await self._get(f"/lijnen/{entiteitnummer}/{lijnnummer}")
            return data.get("lijnnummerPubliek") or None
        except DeLijnApiError:
            return None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def validate_api_key(self) -> bool:
        """Test the API key by fetching the entities list."""
        try:
            data = await self._get("/entiteiten")
            return isinstance(data, dict) and "entiteiten" in data
        except DeLijnApiError:
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API_BASE_URL}{path}"
        try:
            async with self._session.get(url, headers=self._headers, params=params) as response:
                if response.status == 404:
                    raise DeLijnApiError(f"Not found: {path}")
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientResponseError as err:
            raise DeLijnApiError(f"HTTP {err.status}: {path}") from err
        except aiohttp.ClientError as err:
            raise DeLijnApiError(f"Connection error: {err}") from err
