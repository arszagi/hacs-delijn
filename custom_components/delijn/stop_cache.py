"""Stop data cache — downloads and stores all De Lijn stops from the V1 Core API."""

import gzip
import json
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .api_client import DeLijnApiClient, DeLijnApiError
from .const import CACHE_DIR_NAME, CACHE_STOPS_FILE

_LOGGER = logging.getLogger(__name__)

_MAX_SEARCH_RESULTS = 20


class StopCache:
    """Manages a local cache of all De Lijn stops.

    On first use, downloads all stops from the 5 regional entities (~30K stops, ~17 MB).
    Subsequent HA restarts load from the local gzip-compressed JSON cache.
    The cache can be force-refreshed from the options flow.
    """

    def __init__(self, hass: HomeAssistant, api_client: DeLijnApiClient) -> None:
        self._hass = hass
        self._api_client = api_client
        self._cache_dir = Path(hass.config.config_dir) / CACHE_DIR_NAME

        # stop_key ("3_354661") → stop dict
        self._stops: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Load stops from cache or download if missing."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self._cache_dir / CACHE_STOPS_FILE

        if cache_path.exists():
            _LOGGER.debug("Loading stop cache from disk")
            await self._hass.async_add_executor_job(self._load_from_disk)
        else:
            _LOGGER.info("Stop cache not found — downloading from API")
            await self._download_and_cache()

    async def force_refresh(self) -> None:
        """Re-download all stops regardless of cache state."""
        _LOGGER.info("Forcing stop cache refresh")
        await self._download_and_cache()

    def search(self, query: str) -> list[dict]:
        """Search stops by name (partial) or stop number (partial).

        Returns groups of stops sharing the same name, so that all platforms
        at the same physical location appear as a single result.
        Each group has: name, stop_keys, display_name.
        """
        query_stripped = query.strip()
        query_lower = query_stripped.lower()

        groups: dict[str, list[str]] = {}
        for key, stop in self._stops.items():
            name_match = query_lower in stop["name"].lower()
            code_match = query_stripped in stop["number"]
            if name_match or code_match:
                groups.setdefault(stop["name"], []).append(key)

        results = []
        for name, keys in groups.items():
            # Build number list with icons for special stop types
            number_parts = []
            warnings = []
            for k in keys:
                stop = self._stops[k]
                cls = stop.get("classificatie", "REGULIER")
                num = stop["haltenummer"]
                if cls == "TIJDELIJK":
                    number_parts.append(f"⚠️ {num}")
                    if "TIJDELIJK" not in warnings:
                        warnings.append("TIJDELIJK")
                elif cls == "FLEX":
                    number_parts.append(f"ℹ️ {num}")
                    if "FLEX" not in warnings:
                        warnings.append("FLEX")
                else:
                    number_parts.append(num)

            numbers = ", ".join(number_parts)
            display = f"{name} ({numbers})"

            results.append({
                "name": name,
                "display_name": display,
                "stop_keys": keys,
                "any_tijdelijk": "TIJDELIJK" in warnings,
                "warnings": warnings,  # list of special types present in this group
            })

        return results[:_MAX_SEARCH_RESULTS]

    def get_stop(self, stop_key: str) -> dict | None:
        """Return stop info for a given stop_key."""
        return self._stops.get(stop_key)

    def get_stop_name(self, stop_key: str) -> str:
        """Return the human-readable name for a stop_key."""
        stop = self._stops.get(stop_key)
        return stop["name"] if stop else stop_key

    def is_loaded(self) -> bool:
        return bool(self._stops)

    # ------------------------------------------------------------------
    # Download + parse
    # ------------------------------------------------------------------

    async def _download_and_cache(self) -> None:
        try:
            raw_stops = await self._api_client.fetch_all_stops()
        except DeLijnApiError as err:
            raise RuntimeError(f"Stop data download failed: {err}") from err

        parsed = self._parse_stops(raw_stops)
        self._stops = parsed
        await self._hass.async_add_executor_job(self._save_to_disk, parsed)
        _LOGGER.info("Stop cache updated: %d stops", len(parsed))

    def _parse_stops(self, raw_stops: list[dict]) -> dict[str, dict]:
        stops = {}
        for s in raw_stops:
            entity = str(s.get("entiteitnummer", ""))
            number = str(s.get("haltenummer", ""))
            if not entity or not number:
                continue
            key = f"{entity}_{number}"
            geo = s.get("geoCoordinaat") or {}
            stops[key] = {
                "key": key,
                "entiteitnummer": entity,
                "haltenummer": number,
                "number": number,  # alias for search
                "name": (s.get("omschrijvingLang") or s.get("omschrijving") or number).strip(),
                "short_name": (s.get("omschrijving") or number).strip(),
                "municipality": (s.get("omschrijvingGemeente") or "").strip(),
                "classificatie": s.get("classificatie") or "REGULIER",
                "taal": s.get("taal") or "NEDERLANDS",
                "lat": float(geo.get("latitude") or 0),
                "lon": float(geo.get("longitude") or 0),
                "vehicle_types": s.get("bedieningsTypes") or [],
                "accessibility": s.get("halteToegankelijkheden") or [],
            }
        return stops

    # ------------------------------------------------------------------
    # Cache I/O
    # ------------------------------------------------------------------

    def _save_to_disk(self, stops: dict) -> None:
        path = self._cache_dir / CACHE_STOPS_FILE
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(stops, f)
        _LOGGER.debug("Stop cache saved: %s", path)

    def _load_from_disk(self) -> None:
        path = self._cache_dir / CACHE_STOPS_FILE
        with gzip.open(path, "rt", encoding="utf-8") as f:
            self._stops = json.load(f)
        _LOGGER.debug("Stop cache loaded: %d stops", len(self._stops))
