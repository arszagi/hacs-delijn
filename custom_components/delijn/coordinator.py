"""DataUpdateCoordinator — fetches real-time departures and alerts for configured stops."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import DeLijnApiClient, DeLijnApiError
from .const import (
    DOMAIN,
    MAX_DEPARTURES,
    PREDICTION_CANCELLED,
    PREDICTION_PASSED,
)

_LOGGER = logging.getLogger(__name__)


class DeLijnCoordinator(DataUpdateCoordinator):
    """Polls the De Lijn V1 Core API for all configured stops.

    Data structure returned by _async_update_data:
    {
        "3_354661": {
            "departures": [
                {
                    "line": "171",
                    "direction": "TERUG",
                    "destination": "Anderlecht Het Rad",
                    "destination_fr": "Anderlecht La Roue",
                    "scheduled": "2026-05-06T12:35:00",
                    "realtime": "2026-05-06T12:41:07" | None,
                    "delay_minutes": 6,
                    "vehicle_id": "5190",
                    "prediction": "REALTIME",
                    "cancelled": False,
                },
                ...
            ],
            "alerts": [
                {
                    "title": "Werken op de Bergensesteenweg fase 6",
                    "description": "Periode van vrijdag 9 mei 2025 ...",
                    "start": "2025-05-09T00:00:00",
                    "end": None,
                    "type": "storing",
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
        stops: list[dict],
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self._api_client = api_client
        self.stops = stops  # mutable — updated when stops are added/removed
        # Cache: (entiteitnummer, lijnnummer) → public line number (e.g. "R70")
        self._public_line_cache: dict[tuple, str] = {}

    async def _async_update_data(self) -> dict:
        """Fetch real-time data for all configured stops in parallel."""
        tasks = {
            stop["key"]: self._fetch_stop_data(stop)
            for stop in self.stops
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        data = {}
        for stop_key, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                _LOGGER.warning("Failed to fetch data for stop %s: %s", stop_key, result)
                data[stop_key] = {"departures": [], "alerts": []}
            else:
                data[stop_key] = result

        return data

    async def _fetch_stop_data(self, stop: dict) -> dict:
        """Fetch departures and alerts for a single stop."""
        entity = stop["entiteitnummer"]
        number = stop["haltenummer"]

        try:
            realtime_data, disruption_data = await asyncio.gather(
                self._api_client.fetch_realtime(entity, number, MAX_DEPARTURES),
                self._api_client.fetch_disruptions(entity, number),
            )
        except DeLijnApiError as err:
            raise UpdateFailed(f"API error for stop {stop['key']}: {err}") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Connection error: {err}") from err

        return {
            "departures": await self._parse_departures(realtime_data, entity),
            "alerts": self._parse_alerts(disruption_data),
        }

    # ------------------------------------------------------------------
    # Departures
    # ------------------------------------------------------------------

    async def _parse_departures(self, data: dict, entiteitnummer: str) -> list[dict]:
        """Extract and sort upcoming departures from real-time API response."""
        departures = []
        now = datetime.now(timezone.utc)

        for halte_doorkomst in data.get("halteDoorkomsten", []):
            for doorkomst in halte_doorkomst.get("doorkomsten", []):
                statuses = doorkomst.get("predictionStatussen") or []
                if isinstance(statuses, str):
                    statuses = [statuses]

                # Skip already-passed departures
                if PREDICTION_PASSED in statuses:
                    continue

                cancelled = (
                    doorkomst.get("status") == "CANCELLED"
                    or PREDICTION_CANCELLED in statuses
                )

                scheduled_str = doorkomst.get("dienstregelingTijdstip")
                realtime_str = doorkomst.get("real-timeTijdstip")

                scheduled_dt = _parse_datetime(scheduled_str)
                realtime_dt = _parse_datetime(realtime_str)

                # Skip past departures (unless cancelled — show those for awareness)
                effective_dt = realtime_dt or scheduled_dt
                if effective_dt and effective_dt < now and not cancelled:
                    continue

                delay_minutes = None
                if scheduled_dt and realtime_dt:
                    delta = realtime_dt - scheduled_dt
                    delay_minutes = round(delta.total_seconds() / 60, 1)

                internal_num = str(doorkomst.get("lijnnummer") or "")
                public_num = await self._resolve_public_line(entiteitnummer, internal_num)

                departures.append({
                    "line": public_num or internal_num,
                    "direction": doorkomst.get("richting") or "",
                    "destination": (doorkomst.get("bestemmingKort") or doorkomst.get("bestemming") or "").strip(),
                    "destination_fr": (doorkomst.get("bestemmingKortFrans") or "").strip(),
                    "scheduled": scheduled_str,
                    "realtime": realtime_str if realtime_str else None,
                    "delay_minutes": delay_minutes,
                    "vehicle_id": doorkomst.get("vrtnum") or "",
                    "prediction": statuses[0] if statuses else "",
                    "cancelled": cancelled,
                })

        # Sort by effective departure time
        departures.sort(key=lambda d: d.get("realtime") or d.get("scheduled") or "")
        return departures[:MAX_DEPARTURES]

    async def _resolve_public_line(self, entiteitnummer: str, lijnnummer: str) -> str | None:
        """Return the public line number (e.g. 'R70') for an internal lijnnummer.

        Results are cached in memory so each unique line is only fetched once.
        """
        if not lijnnummer:
            return None
        cache_key = (entiteitnummer, lijnnummer)
        if cache_key in self._public_line_cache:
            return self._public_line_cache[cache_key]

        public = await self._api_client.fetch_public_line_number(entiteitnummer, lijnnummer)
        if public:
            self._public_line_cache[cache_key] = public
            _LOGGER.debug("Line %s (entity %s) → public: %s", lijnnummer, entiteitnummer, public)
        return public

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def _parse_alerts(self, data: dict) -> list[dict]:
        """Extract active disruptions and diversions."""
        alerts = []

        for alert_type, items in [("storing", data.get("storingen") or []), ("omleiding", data.get("omleidingen") or [])]:
            if not isinstance(items, list):
                items = [items] if items else []
            for item in items:
                if not item or not item.get("titel"):
                    continue
                periode = item.get("periode") or {}
                affected_lines = [
                    lr.get("lijnNummerPubliek")
                    for lr in (item.get("lijnrichtingen") or [])
                    if lr.get("lijnNummerPubliek")
                ]
                alerts.append({
                    "title": item.get("titel") or "",
                    "description": item.get("omschrijving") or "",
                    "start": periode.get("startDatum"),
                    "end": periode.get("eindDatum") or None,
                    "type": alert_type,
                    "lines": affected_lines,
                })

        return alerts


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO datetime string from the De Lijn API into a UTC-aware datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            # De Lijn times are Europe/Brussels — treat as local and convert
            import zoneinfo
            brussels = zoneinfo.ZoneInfo("Europe/Brussels")
            dt = dt.replace(tzinfo=brussels)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None
