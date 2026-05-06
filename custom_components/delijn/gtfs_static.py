"""GTFS static data manager — downloads, parses and caches schedule data."""

import csv
import gzip
import io
import json
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .api_client import DeLijnApiClient, DeLijnApiError
from .const import (
    CACHE_DIR_NAME,
    CACHE_LAST_MODIFIED_FILE,
    CACHE_ROUTES_FILE,
    CACHE_STOPS_FILE,
    CACHE_TRIPS_FILE,
)

_LOGGER = logging.getLogger(__name__)

# Only location_type 0 (stop) and 4 (boarding area) are physical boarding points
_VALID_STOP_LOCATION_TYPES = {"", "0", "4"}


class GtfsStaticManager:
    """Manages the De Lijn GTFS static schedule data.

    On first run, downloads the full ZIP (~200 MB) from the API, extracts
    stops.txt, routes.txt and trips.txt, then caches them locally as JSON.
    On subsequent HA restarts, loads from the local cache.
    Before each reload, compares the Last-Modified header to skip unnecessary downloads.
    """

    def __init__(self, hass: HomeAssistant, api_client: DeLijnApiClient) -> None:
        self._hass = hass
        self._api_client = api_client
        self._cache_dir = Path(hass.config.config_dir) / CACHE_DIR_NAME

        # In-memory lookup tables built from the GTFS files
        self._stops: dict[str, dict] = {}   # stop_id → {name, lat, lon}
        self._routes: dict[str, dict] = {}  # route_id → {short_name, long_name, type}
        self._trips: dict[str, dict] = {}   # trip_id → {route_short_name, headsign, direction_id, route_id}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Load data from cache, or download if cache is missing."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        if self._cache_is_complete():
            _LOGGER.debug("Loading GTFS static data from local cache")
            await self._hass.async_add_executor_job(self._load_from_cache)
        else:
            _LOGGER.info("GTFS static cache not found — downloading from API")
            await self._download_and_parse()

    async def refresh_if_updated(self) -> bool:
        """Check whether the remote GTFS file has changed; download if so.

        Uses the stored Last-Modified value to avoid unnecessary downloads.
        Returns True if the data was refreshed, False if still current.
        """
        stored = self._read_last_modified()
        remote = await self._api_client.get_static_last_modified()

        if remote and remote == stored:
            _LOGGER.debug("GTFS static data is up to date (Last-Modified unchanged)")
            return False

        _LOGGER.info("GTFS static data changed (Last-Modified: %s → %s) — refreshing", stored, remote)
        await self._download_and_parse()
        return True

    async def force_refresh(self) -> None:
        """Re-download and re-parse regardless of Last-Modified."""
        _LOGGER.info("Forcing GTFS static data refresh")
        await self._download_and_parse()

    def search_stops(self, query: str) -> list[dict]:
        """Return stops whose name contains the query string (case-insensitive)."""
        query_lower = query.lower()
        return [
            {"stop_id": stop_id, **info}
            for stop_id, info in self._stops.items()
            if query_lower in info["name"].lower()
        ]

    def get_stop_name(self, stop_id: str) -> str:
        """Return the human-readable name for a stop_id."""
        return self._stops.get(stop_id, {}).get("name", stop_id)

    def get_trip_info(self, trip_id: str) -> dict | None:
        """Return {route_short_name, headsign, direction_id, route_id} for a trip_id."""
        return self._trips.get(trip_id)

    def get_route_type(self, route_id: str) -> int:
        """Return the GTFS route_type (0=tram, 3=bus) for a route_id."""
        return self._routes.get(route_id, {}).get("type", 3)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _download_and_parse(self) -> None:
        """Download the GTFS ZIP, parse the needed files, write cache."""
        try:
            zip_bytes, last_modified = await self._api_client.fetch_static_gtfs()
        except DeLijnApiError as err:
            raise RuntimeError(f"GTFS download failed: {err}") from err

        _LOGGER.info("Parsing GTFS static data (%d MB)", len(zip_bytes) // 1_000_000)
        await self._hass.async_add_executor_job(self._parse_and_cache, zip_bytes, last_modified)
        _LOGGER.info("GTFS static data cached successfully")

    def _parse_and_cache(self, zip_bytes: bytes, last_modified: str | None) -> None:
        """Extract and parse stops, routes and trips from the ZIP (blocking, run in executor)."""
        import zipfile

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            self._stops = self._parse_stops(zf)
            self._routes = self._parse_routes(zf)
            self._trips = self._parse_trips(zf)

        self._write_cache()
        if last_modified:
            self._write_last_modified(last_modified)

    def _parse_stops(self, zf) -> dict[str, dict]:
        stops = {}
        with zf.open("stops.txt") as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            for row in reader:
                location_type = row.get("location_type", "0") or "0"
                if location_type not in _VALID_STOP_LOCATION_TYPES:
                    continue
                stop_id = row["stop_id"]
                stops[stop_id] = {
                    "name": row.get("stop_name", stop_id),
                    "lat": _parse_float(row.get("stop_lat")),
                    "lon": _parse_float(row.get("stop_lon")),
                }
        _LOGGER.debug("Parsed %d stops", len(stops))
        return stops

    def _parse_routes(self, zf) -> dict[str, dict]:
        routes = {}
        with zf.open("routes.txt") as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            for row in reader:
                route_id = row["route_id"]
                routes[route_id] = {
                    "short_name": row.get("route_short_name", ""),
                    "long_name": row.get("route_long_name", ""),
                    "type": int(row.get("route_type", 3) or 3),
                }
        _LOGGER.debug("Parsed %d routes", len(routes))
        return routes

    def _parse_trips(self, zf) -> dict[str, dict]:
        trips = {}
        with zf.open("trips.txt") as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
            for row in reader:
                trip_id = row["trip_id"]
                route_id = row.get("route_id", "")
                route_short_name = (
                    row.get("trip_short_name")
                    or self._routes.get(route_id, {}).get("short_name", "")
                )
                trips[trip_id] = {
                    "route_short_name": route_short_name,
                    "headsign": row.get("trip_headsign", ""),
                    "direction_id": int(row.get("direction_id", 0) or 0),
                    "route_id": route_id,
                }
        _LOGGER.debug("Parsed %d trips", len(trips))
        return trips

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------

    def _cache_is_complete(self) -> bool:
        return (
            (self._cache_dir / CACHE_STOPS_FILE).exists()
            and (self._cache_dir / CACHE_ROUTES_FILE).exists()
            and (self._cache_dir / CACHE_TRIPS_FILE).exists()
        )

    def _load_from_cache(self) -> None:
        """Load in-memory dicts from cached JSON files (blocking)."""
        with open(self._cache_dir / CACHE_STOPS_FILE, encoding="utf-8") as f:
            self._stops = json.load(f)

        with open(self._cache_dir / CACHE_ROUTES_FILE, encoding="utf-8") as f:
            self._routes = json.load(f)

        with gzip.open(self._cache_dir / CACHE_TRIPS_FILE, "rt", encoding="utf-8") as f:
            self._trips = json.load(f)

        _LOGGER.debug(
            "Loaded from cache: %d stops, %d routes, %d trips",
            len(self._stops), len(self._routes), len(self._trips),
        )

    def _write_cache(self) -> None:
        """Write in-memory dicts to local cache files (blocking)."""
        with open(self._cache_dir / CACHE_STOPS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._stops, f)

        with open(self._cache_dir / CACHE_ROUTES_FILE, "w", encoding="utf-8") as f:
            json.dump(self._routes, f)

        # trips.txt is large (~72 MB CSV → ~30 MB JSON) — store compressed
        with gzip.open(self._cache_dir / CACHE_TRIPS_FILE, "wt", encoding="utf-8") as f:
            json.dump(self._trips, f)

    def _read_last_modified(self) -> str | None:
        path = self._cache_dir / CACHE_LAST_MODIFIED_FILE
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return None

    def _write_last_modified(self, value: str) -> None:
        (self._cache_dir / CACHE_LAST_MODIFIED_FILE).write_text(value, encoding="utf-8")


def _parse_float(value: str | None) -> float:
    try:
        return float(value) if value else 0.0
    except ValueError:
        return 0.0
