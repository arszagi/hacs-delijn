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
    ATTR_BADGE_BACKGROUND,
    LANG_FR,
    ATTR_BADGE_BORDER,
    ATTR_BADGE_TEXT,
    ATTR_BADGE_TEXT_BORDER,
    ATTR_DELAY_MINUTES,
    ATTR_DESTINATION,
    ATTR_DESTINATION_FR,
    ATTR_DIRECTION,
    ATTR_LAST_UPDATED,
    ATTR_LINE,
    ATTR_NEXT_DEPARTURES,
    ATTR_PREDICTION,
    ATTR_REALTIME,
    ATTR_SCHEDULED,
    ATTR_STOP_NAME,
    ATTR_STOP_NUMBER,
    ATTR_STOP_TYPE,
    ATTR_VEHICLE_ID,
    CLASSIFICATIE_TIJDELIJK,
    DOMAIN,
    PREDICTION_CANCELLED,
)
from .coordinator import DeLijnCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors and register a listener for dynamic entity discovery."""
    coordinator: DeLijnCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    created_departure_keys: set[tuple] = set()
    created_alert_keys: set[str] = set()

    @callback
    def _discover_entities() -> None:
        if not coordinator.data:
            return

        new_entities: list[SensorEntity] = []

        for stop in coordinator.stops:
            stop_key = stop["key"]
            stop_data = coordinator.data.get(stop_key, {})

            for departure in stop_data.get("departures", []):
                sensor_key = (stop_key, departure["line"], departure["direction"])
                if sensor_key not in created_departure_keys:
                    created_departure_keys.add(sensor_key)
                    new_entities.append(
                        DeLijnDepartureSensor(
                            coordinator=coordinator,
                            stop=stop,
                            line=departure["line"],
                            direction=departure["direction"],
                            destination=departure["destination"],
                        )
                    )

            if stop_key not in created_alert_keys:
                created_alert_keys.add(stop_key)
                new_entities.append(DeLijnAlertSensor(coordinator, stop))

        if new_entities:
            async_add_entities(new_entities)

    coordinator.async_add_listener(_discover_entities)
    _discover_entities()


# ------------------------------------------------------------------
# Departure sensor — one per (stop, line, direction)
# ------------------------------------------------------------------

class DeLijnDepartureSensor(CoordinatorEntity[DeLijnCoordinator], SensorEntity):
    """Shows the next departure for a bus/tram line at a stop.

    State: minutes until next departure (int), or None/unavailable when no service.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "min"
    _attr_icon = "mdi:bus-clock"

    def __init__(
        self,
        coordinator: DeLijnCoordinator,
        stop: dict,
        line: str,
        direction: str,
        destination: str,
    ) -> None:
        super().__init__(coordinator)
        self._stop = stop
        self._line = line
        self._direction = direction
        self._destination = destination
        # Last known attributes — preserved when no buses are running
        self._cached_attrs: dict = {}

        stop_key = stop["key"]
        dir_label = direction.lower() if direction else "unknown"

        self._attr_unique_id = f"{DOMAIN}_{stop_key}_line_{line}_{dir_label}"
        self._attr_name = f"Line {line} → {destination}" if destination else f"Line {line}"
        self.entity_id = (
            f"sensor.delijn_line_{_slug(line)}_{_slug(stop['name'])}_to_{_slug(destination)}"
            if destination
            else f"sensor.delijn_line_{_slug(line)}_{_slug(stop['name'])}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        stop_name = self._stop["name"]
        return DeviceInfo(
            identifiers={(DOMAIN, _slug(stop_name))},
            name=stop_name,
            manufacturer="De Lijn",
            model=_group_model(stop_name, self.coordinator.stops, self.coordinator.language),
        )

    @property
    def available(self) -> bool:
        # Only mark unavailable when the coordinator itself fails,
        # not when no buses are running — attributes must remain visible at night
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> int | None:
        dep = self._next_departure()
        if dep is None:
            return None  # No service — state empty but attributes still exposed
        effective_time = dep.get("realtime") or dep.get("scheduled")
        if not effective_time:
            return None
        dt = _parse_dt(effective_time)
        if dt is None:
            return None
        return max(0, int((dt - datetime.now(timezone.utc)).total_seconds() / 60))

    @property
    def extra_state_attributes(self) -> dict:
        dep = self._next_departure()
        all_deps = self._all_departures()

        # Static attributes — always present
        attrs: dict = {
            ATTR_STOP_NAME: self._stop["name"],
            ATTR_STOP_NUMBER: self._stop["haltenummer"],
            ATTR_STOP_TYPE: _translate_stop_type(self._stop.get("classificatie", ""), self.coordinator.language),
            ATTR_LINE: self._line,
            ATTR_DIRECTION: _translate_direction(self._direction, self.coordinator.language),
            ATTR_LAST_UPDATED: datetime.now(timezone.utc).isoformat(),
        }

        if dep:
            # Build live departure attributes and update the persistent cache
            live = {
                ATTR_SCHEDULED:    _format_time(dep.get("scheduled")),
                ATTR_REALTIME:     _format_time(dep.get("realtime")),
                ATTR_DELAY_MINUTES: dep.get("delay_minutes"),
                ATTR_DESTINATION:  dep.get("destination", ""),
                ATTR_DESTINATION_FR: dep.get("destination_fr", ""),
                ATTR_VEHICLE_ID:   dep.get("vehicle_id", ""),
                ATTR_PREDICTION:   _translate_prediction(dep.get("prediction", ""), self.coordinator.language),
                ATTR_NEXT_DEPARTURES: [
                    {
                        "scheduled":    _format_time(d.get("scheduled")),
                        "realtime":     _format_time(d.get("realtime")),
                        "delay_minutes": d.get("delay_minutes"),
                        "destination":  d.get("destination", ""),
                        "prediction":   _translate_prediction(d.get("prediction", ""), self.coordinator.language),
                        "cancelled":    d.get("cancelled", False),
                    }
                    for d in all_deps[1:5]
                ],
            }
            for color_attr in (ATTR_BADGE_BACKGROUND, ATTR_BADGE_TEXT, ATTR_BADGE_BORDER, ATTR_BADGE_TEXT_BORDER):
                if val := dep.get(color_attr):
                    live[color_attr] = val

            # Persist so attributes survive when no buses are running
            self._cached_attrs = live
            attrs.update(live)
        else:
            # No current departure — expose last known data (line, colors, destination)
            # so Lovelace cards keep working at night
            attrs.update(self._cached_attrs)
            attrs[ATTR_NEXT_DEPARTURES] = []

        return attrs

    def _all_departures(self) -> list[dict]:
        if not self.coordinator.data:
            return []
        stop_data = self.coordinator.data.get(self._stop["key"], {})
        return [
            d for d in stop_data.get("departures", [])
            if d["line"] == self._line and d["direction"] == self._direction
            and not d.get("cancelled")
        ]

    def _next_departure(self) -> dict | None:
        deps = self._all_departures()
        return deps[0] if deps else None


# ------------------------------------------------------------------
# Alert sensor — one per stop
# ------------------------------------------------------------------

class DeLijnAlertSensor(CoordinatorEntity[DeLijnCoordinator], SensorEntity):
    """Shows active disruptions and diversions for a stop."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: DeLijnCoordinator, stop: dict) -> None:
        super().__init__(coordinator)
        self._stop = stop
        stop_key = stop["key"]
        number = stop["haltenummer"]

        self._attr_unique_id = f"{DOMAIN}_{stop_key}_alerts"
        self._attr_name = f"Service alerts ({number})"
        self.entity_id = f"sensor.delijn_alerts_{_slug(stop['name'])}_{number}"

    @property
    def device_info(self) -> DeviceInfo:
        stop_name = self._stop["name"]
        return DeviceInfo(
            identifiers={(DOMAIN, _slug(stop_name))},
            name=stop_name,
            manufacturer="De Lijn",
            model=_group_model(stop_name, self.coordinator.stops, self.coordinator.language),
        )

    @property
    def native_value(self) -> int:
        if not self.coordinator.data:
            return 0
        return len(self.coordinator.data.get(self._stop["key"], {}).get("alerts", []))

    @property
    def extra_state_attributes(self) -> dict:
        if not self.coordinator.data:
            return {ATTR_ALERTS: [], ATTR_STOP_TYPE: self._stop.get("classificatie", "")}
        alerts = self.coordinator.data.get(self._stop["key"], {}).get("alerts", [])
        return {
            ATTR_STOP_TYPE: _translate_stop_type(self._stop.get("classificatie", ""), self.coordinator.language),
            ATTR_ALERTS: [
                {
                    "type": a["type"],
                    "title": a["title"],
                    "description": a["description"],
                    "start": a.get("start"),
                    "end": a.get("end") or "ongoing",
                    "lines": a.get("lines", []),
                }
                for a in alerts
            ],
        }


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

def _group_model(stop_name: str, all_stops: list[dict], language: str) -> str:
    """Build the device model string for a stop group.

    Shows the stop numbers and combined stop types for all platforms
    sharing the same name, e.g. '304660, 304661, 354661 · Régulier + Temporaire'.
    """
    group = [s for s in all_stops if s.get("name") == stop_name]
    if not group:
        return ""

    numbers = ", ".join(s["haltenummer"] for s in group)

    # Unique classifications in order of appearance
    seen = []
    for s in group:
        c = s.get("classificatie", "REGULIER")
        if c not in seen:
            seen.append(c)

    types = " + ".join(_translate_stop_type(c, language) for c in seen)
    return f"{numbers} · {types}"


_DIRECTION_LABELS = {
    LANG_FR: {"HEEN": "Aller",  "TERUG": "Retour"},
    "nl":    {"HEEN": "Heen",   "TERUG": "Terug"},
}

_PREDICTION_LABELS = {
    LANG_FR: {
        "REALTIME":    "Temps réel",
        "GEENREALTIME": "Horaire",
        "GESCHRAPT":   "Annulé",
        "VERSTREKEN":  "Passé",
    },
    "nl": {
        "REALTIME":    "Realtime",
        "GEENREALTIME": "Dienstregeling",
        "GESCHRAPT":   "Geschrapt",
        "VERSTREKEN":  "Verstreken",
    },
}


def _translate_direction(direction: str, language: str) -> str:
    lang = LANG_FR if language == LANG_FR else "nl"
    return _DIRECTION_LABELS[lang].get(direction, direction)


def _translate_prediction(prediction: str, language: str) -> str:
    lang = LANG_FR if language == LANG_FR else "nl"
    return _PREDICTION_LABELS[lang].get(prediction, prediction)


_STOP_TYPE_LABELS = {
    LANG_FR: {
        "REGULIER": "Régulier",
        "TIJDELIJK": "Temporaire",
        "FLEX": "Flexbus (à la demande)",
        "COMBI": "Régulier + Flexbus",
    },
    "nl": {
        "REGULIER": "Regulier",
        "TIJDELIJK": "Tijdelijk",
        "FLEX": "Flexbus",
        "COMBI": "Regulier + Flexbus",
    },
}


def _translate_stop_type(classificatie: str, language: str) -> str:
    """Return a translated label for a stop classification."""
    lang = LANG_FR if language == LANG_FR else "nl"
    return _STOP_TYPE_LABELS[lang].get(classificatie, classificatie)


def _slug(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _parse_dt(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        import zoneinfo
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zoneinfo.ZoneInfo("Europe/Brussels"))
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _format_time(iso_str: str | None) -> str | None:
    dt = _parse_dt(iso_str)
    if dt is None:
        return None
    return dt.astimezone().strftime("%H:%M")


