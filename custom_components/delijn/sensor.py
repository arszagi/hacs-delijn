"""Sensor entities for the De Lijn integration."""

import logging
import re
from datetime import datetime, timezone

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ALERTS,
    ATTR_DELAY_MINUTES,
    ATTR_DIRECTION_ID,
    ATTR_HEADSIGN,
    ATTR_LAST_UPDATED,
    ATTR_LINE,
    ATTR_NEXT_DEPARTURES,
    ATTR_REALTIME_DEPARTURE,
    ATTR_ROUTE_ID,
    ATTR_SCHEDULED_DEPARTURE,
    ATTR_STOP_ID,
    ATTR_STOP_NAME,
    ATTR_VEHICLE_ID,
    DOMAIN,
    MAX_UPCOMING_DEPARTURES,
)
from .coordinator import DeLijnCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up De Lijn sensors and register a listener for dynamic entity discovery."""
    coordinator: DeLijnCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    gtfs_manager = hass.data[DOMAIN][entry.entry_id]["gtfs_manager"]

    # Track which (stop_id, line, direction_id) sensors have already been created
    created_departure_keys: set[tuple] = set()
    created_alert_keys: set[str] = set()

    @callback
    def _discover_and_add_entities() -> None:
        """Called on every coordinator update to discover new sensors."""
        if not coordinator.data:
            return

        new_entities: list[SensorEntity] = []

        for stop_id, stop_data in coordinator.data.items():
            stop_name = gtfs_manager.get_stop_name(stop_id)

            # One departure sensor per (line, direction_id) combo at this stop
            for departure in stop_data.get("departures", []):
                key = (stop_id, departure["line"], departure["direction_id"])
                if key not in created_departure_keys:
                    created_departure_keys.add(key)
                    new_entities.append(
                        DeLijnDepartureSensor(
                            coordinator=coordinator,
                            stop_id=stop_id,
                            stop_name=stop_name,
                            line=departure["line"],
                            headsign=departure["headsign"],
                            direction_id=departure["direction_id"],
                        )
                    )

            # One alert sensor per stop
            if stop_id not in created_alert_keys:
                created_alert_keys.add(stop_id)
                new_entities.append(
                    DeLijnAlertSensor(
                        coordinator=coordinator,
                        stop_id=stop_id,
                        stop_name=stop_name,
                    )
                )

        if new_entities:
            async_add_entities(new_entities)

    # Register listener so new sensors are created on each coordinator update
    coordinator.async_add_listener(_discover_and_add_entities)

    # Run discovery immediately with data already fetched
    _discover_and_add_entities()


# ------------------------------------------------------------------
# Departure sensor — one per (stop, line, direction)
# ------------------------------------------------------------------

class DeLijnDepartureSensor(CoordinatorEntity[DeLijnCoordinator], SensorEntity):
    """Shows the next departure for a specific bus/tram line at a stop.

    State: minutes until the next departure (int), or None when no service.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "min"
    _attr_icon = "mdi:bus-clock"

    def __init__(
        self,
        coordinator: DeLijnCoordinator,
        stop_id: str,
        stop_name: str,
        line: str,
        headsign: str,
        direction_id: int,
    ) -> None:
        super().__init__(coordinator)
        self._stop_id = stop_id
        self._stop_name = stop_name
        self._line = line
        self._headsign = headsign
        self._direction_id = direction_id

        slug_stop = _slugify(stop_name)
        slug_headsign = _slugify(headsign)
        direction_label = "inbound" if direction_id == 1 else "outbound"

        # Unique ID stable across restarts
        self._attr_unique_id = f"{DOMAIN}_{stop_id}_line_{line}_{direction_label}"

        # Human-readable name: "Line R70 → Bruxelles-Midi"
        self._attr_name = f"Line {line} → {headsign}" if headsign else f"Line {line}"

        # Entity ID: sensor.delijn_line_r70_sint_pieters_leeuw_to_bruxelles_midi
        direction_part = f"to_{slug_headsign}" if headsign else direction_label
        self.entity_id = f"sensor.delijn_line_{_slugify(line)}_{slug_stop}_{direction_part}"

    @property
    def device_info(self) -> DeviceInfo:
        # Group all platforms with the same stop name under one device
        return DeviceInfo(
            identifiers={(DOMAIN, _slugify(self._stop_name))},
            name=self._stop_name,
            manufacturer="De Lijn",
            model="Bus Stop",
        )

    @property
    def native_value(self) -> int | None:
        """Minutes until the next departure, or None when no service."""
        next_dep = self._get_next_departure()
        if next_dep is None:
            return None
        now = datetime.now(timezone.utc).timestamp()
        minutes = max(0, int((next_dep["departure_time"] - now) / 60))
        return minutes

    @property
    def extra_state_attributes(self) -> dict:
        next_dep = self._get_next_departure()
        all_deps = self._get_all_departures()

        attrs = {
            ATTR_STOP_ID: self._stop_id,
            ATTR_STOP_NAME: self._stop_name,
            ATTR_LINE: self._line,
            ATTR_HEADSIGN: self._headsign,
            ATTR_DIRECTION_ID: self._direction_id,
            ATTR_LAST_UPDATED: datetime.now(timezone.utc).isoformat(),
        }

        if next_dep:
            attrs[ATTR_REALTIME_DEPARTURE] = _format_time(next_dep["departure_time"])
            attrs[ATTR_DELAY_MINUTES] = round(next_dep["delay_seconds"] / 60, 1)
            attrs[ATTR_VEHICLE_ID] = next_dep.get("vehicle_id", "")
            attrs[ATTR_ROUTE_ID] = next_dep.get("route_id", "")

        # Next departures list (excludes the first one already shown in state)
        attrs[ATTR_NEXT_DEPARTURES] = [
            {
                "realtime_departure": _format_time(d["departure_time"]),
                "delay_minutes": round(d["delay_seconds"] / 60, 1),
            }
            for d in all_deps[1:MAX_UPCOMING_DEPARTURES]
        ]

        return attrs

    def _get_all_departures(self) -> list[dict]:
        """Return all upcoming departures for this (line, direction) at this stop."""
        if not self.coordinator.data:
            return []
        stop_data = self.coordinator.data.get(self._stop_id, {})
        return [
            d for d in stop_data.get("departures", [])
            if d["line"] == self._line and d["direction_id"] == self._direction_id
        ]

    def _get_next_departure(self) -> dict | None:
        deps = self._get_all_departures()
        return deps[0] if deps else None


# ------------------------------------------------------------------
# Alert sensor — one per stop
# ------------------------------------------------------------------

class DeLijnAlertSensor(CoordinatorEntity[DeLijnCoordinator], SensorEntity):
    """Shows the number of active service alerts for a stop.

    State: count of active alerts (int).
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(
        self,
        coordinator: DeLijnCoordinator,
        stop_id: str,
        stop_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._stop_id = stop_id
        self._stop_name = stop_name

        self._attr_unique_id = f"{DOMAIN}_{stop_id}_alerts"
        self._attr_name = "Service alerts"
        self.entity_id = f"sensor.delijn_alerts_{_slugify(stop_name)}"

    @property
    def device_info(self) -> DeviceInfo:
        # Same device as the departure sensors — grouped by stop name
        return DeviceInfo(
            identifiers={(DOMAIN, _slugify(self._stop_name))},
            name=self._stop_name,
            manufacturer="De Lijn",
            model="Bus Stop",
        )

    @property
    def native_value(self) -> int:
        """Number of currently active service alerts."""
        if not self.coordinator.data:
            return 0
        return len(self.coordinator.data.get(self._stop_id, {}).get("alerts", []))

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {ATTR_ALERTS: []}
        alerts = self.coordinator.data.get(self._stop_id, {}).get("alerts", [])
        return {
            ATTR_ALERTS: [
                {
                    "header": a["header"],
                    "description": a["description"],
                    "url": a["url"],
                    "active_until": _format_time(a["active_until"]) if a.get("active_until") else "ongoing",
                }
                for a in alerts
            ]
        }


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Convert a human-readable string to a lowercase, underscore-separated slug."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = text.strip("_")
    return text


def _format_time(unix_ts: float | None) -> str | None:
    """Convert a Unix timestamp to a local HH:MM time string."""
    if unix_ts is None:
        return None
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone().strftime("%H:%M")
