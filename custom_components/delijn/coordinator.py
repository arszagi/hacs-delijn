"""DataUpdateCoordinator — fetches and processes GTFS-RT data for all configured stops."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import DeLijnApiClient, DeLijnApiError
from .const import (
    DOMAIN,
    MAX_UPCOMING_DEPARTURES,
    SCHEDULE_RELATIONSHIP_SKIPPED,
)
from .gtfs_static import GtfsStaticManager

_LOGGER = logging.getLogger(__name__)


class DeLijnCoordinator(DataUpdateCoordinator):
    """Polls the GTFS-RT API and distributes data to all sensor entities.

    Data structure returned by _async_update_data:
    {
        stop_id: {
            "departures": [
                {
                    "trip_id", "line", "headsign", "direction_id", "route_id",
                    "departure_time" (unix float), "delay_seconds" (int),
                    "vehicle_id" (str), "schedule_relationship" (int)
                },
                ...  (sorted by departure_time, upcoming only)
            ],
            "alerts": [
                {
                    "header", "description", "url",
                    "cause", "effect",
                    "active_from" (unix float), "active_until" (unix float)
                },
                ...
            ]
        },
        ...
    }
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api_client: DeLijnApiClient,
        gtfs_manager: GtfsStaticManager,
        stop_ids: set[str],
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self._api_client = api_client
        self._gtfs_manager = gtfs_manager
        self.stop_ids = stop_ids  # mutable — updated when stops are added/removed

    async def _async_update_data(self) -> dict:
        """Fetch RT feeds and build per-stop departure and alert data."""
        try:
            trip_feed, alert_feed = await asyncio.gather(
                self._api_client.fetch_trip_updates(),
                self._api_client.fetch_alerts(),
            )
        except DeLijnApiError as err:
            raise UpdateFailed(f"De Lijn API error: {err}") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Connection error: {err}") from err

        now = datetime.now(timezone.utc).timestamp()
        result = {}

        for stop_id in self.stop_ids:
            result[stop_id] = {
                "departures": self._extract_departures(trip_feed, stop_id, now),
                "alerts": self._extract_alerts(alert_feed, stop_id, now),
            }

        return result

    # ------------------------------------------------------------------
    # Departure extraction
    # ------------------------------------------------------------------

    def _extract_departures(self, feed: dict, stop_id: str, now: float) -> list[dict]:
        """Find all upcoming departures at stop_id from the RT trip-update feed."""
        departures = []

        for entity in feed.get("entity", []):
            trip_update = entity.get("tripUpdate", {})
            trip = trip_update.get("trip", {})
            trip_id = trip.get("tripId", "")

            for stop_update in trip_update.get("stopTimeUpdate", []):
                if stop_update.get("stopId") != stop_id:
                    continue

                # Skip stops the vehicle will not serve
                if stop_update.get("scheduleRelationship") == SCHEDULE_RELATIONSHIP_SKIPPED:
                    break

                dep_time = _parse_timestamp(stop_update.get("departure", {}).get("time"))
                if dep_time is None or dep_time < now:
                    break  # Departure already passed or no data for this stop

                delay_seconds = stop_update.get("departure", {}).get("delay") or 0
                trip_info = self._gtfs_manager.get_trip_info(trip_id) or {}

                departures.append({
                    "trip_id": trip_id,
                    "line": trip_info.get("route_short_name") or "?",
                    "headsign": trip_info.get("headsign", ""),
                    "direction_id": trip_info.get("direction_id", 0),
                    "route_id": trip_info.get("route_id", ""),
                    "departure_time": dep_time,
                    "delay_seconds": int(delay_seconds),
                    "vehicle_id": trip_update.get("vehicle", {}).get("id", ""),
                    "schedule_relationship": stop_update.get("scheduleRelationship", 0),
                })
                break  # Only one stopTimeUpdate entry per trip per stop

        departures.sort(key=lambda d: d["departure_time"])
        return departures[:MAX_UPCOMING_DEPARTURES * 5]  # Keep a generous buffer for sensors

    # ------------------------------------------------------------------
    # Alert extraction
    # ------------------------------------------------------------------

    def _extract_alerts(self, feed: dict, stop_id: str, now: float) -> list[dict]:
        """Find active alerts relevant to the routes serving stop_id.

        If no route_ids are known yet (no buses in RT feed), returns an empty
        list rather than dumping the entire network alert feed.
        """
        relevant_route_ids = self._get_route_ids_for_stop(stop_id)

        # Without known routes we cannot filter meaningfully — show nothing
        if not relevant_route_ids:
            return []

        active_alerts = []

        for entity in feed.get("entity", []):
            alert = entity.get("alert", {})
            active_periods = alert.get("activePeriod", [])

            if active_periods and not _is_alert_active(active_periods, now):
                continue

            informed = alert.get("informedEntity", [])
            if not _alert_affects_routes(informed, relevant_route_ids):
                continue

            end_ts = _parse_timestamp(active_periods[0].get("end")) if active_periods else None
            active_alerts.append({
                "header": _get_translation(alert.get("headerText")),
                "description": _get_translation(alert.get("descriptionText")),
                "url": _get_translation(alert.get("url")),
                "cause": alert.get("cause", 0),
                "effect": alert.get("effect", 0),
                "active_from": _parse_timestamp(active_periods[0].get("start")) if active_periods else None,
                # Treat timestamp 0 as "no end date" (open-ended alert)
                "active_until": end_ts if end_ts else None,
            })

        return active_alerts

    def _get_route_ids_for_stop(self, stop_id: str) -> set[str]:
        """Return route_ids currently serving stop_id based on coordinator data."""
        if not self.data:
            return set()
        stop_data = self.data.get(stop_id, {})
        return {dep["route_id"] for dep in stop_data.get("departures", []) if dep.get("route_id")}


# ------------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------------

def _parse_timestamp(value) -> float | None:
    """Parse a GTFS-RT timestamp.

    The API returns timestamps as either a plain integer or a protobuf-style
    object { "low": int, "high": int, "unsigned": bool } due to int64 encoding.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        low = value.get("low", 0) or 0
        high = value.get("high", 0) or 0
        return float(low + (high << 32))
    return None


def _is_alert_active(active_periods: list, now: float) -> bool:
    for period in active_periods:
        start = _parse_timestamp(period.get("start")) or 0
        end = _parse_timestamp(period.get("end"))
        if start <= now and (end is None or now <= end):
            return True
    return False


def _alert_affects_routes(informed_entities: list, route_ids: set[str]) -> bool:
    for entity in informed_entities:
        if entity.get("routeId") in route_ids:
            return True
        trip_route = entity.get("trip", {}).get("routeId")
        if trip_route and trip_route in route_ids:
            return True
    return False


def _get_translation(text_obj: dict | None, lang: str = "en") -> str:
    """Extract a translated string from a GTFS-RT TranslatedString object."""
    if not text_obj:
        return ""
    for translation in text_obj.get("translation", []):
        if translation.get("language") == lang:
            return translation.get("text", "")
    # Fallback to first available translation
    translations = text_obj.get("translation", [])
    return translations[0].get("text", "") if translations else ""
