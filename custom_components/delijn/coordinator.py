"""DataUpdateCoordinator — fetches real-time departures and alerts for configured stops."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api_client import DeLijnApiClient, DeLijnApiError
from .const import (
    DEFAULT_LANGUAGE,
    DOMAIN,
    LANG_FR,
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
        self.language = DEFAULT_LANGUAGE  # set after init from config entry
        # Cache: (entiteitnummer, lijnnummer) → public line number (e.g. "R70")
        self._public_line_cache: dict[tuple, str] = {}
        # Cache: color_code → hex string (e.g. "LG" → "#BBDD00")
        self._color_hex_cache: dict[str, str] = {}
        # Cache: (entiteitnummer, lijnnummer) → {badge_background, badge_text, badge_border, badge_text_border}
        self._line_color_cache: dict[tuple, dict] = {}

    async def _async_update_data(self) -> dict:
        """Fetch real-time data for all configured stops using batch API calls."""
        if not self.stops:
            return {}

        # One string with all stop keys: "3_354661_3_304660_3_304661"
        batch_key = "_".join(
            f"{s['entiteitnummer']}_{s['haltenummer']}" for s in self.stops
        )

        try:
            rt_data, dis_data = await asyncio.gather(
                self._api_client.fetch_realtime_batch(batch_key, MAX_DEPARTURES),
                self._api_client.fetch_disruptions_batch(batch_key),
            )
        except DeLijnApiError as err:
            raise UpdateFailed(f"De Lijn API error: {err}") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Connection error: {err}") from err

        # Build lookups: haltenummer → data
        rt_lookup = _build_rt_lookup(rt_data)
        dis_lookup = _build_disruption_lookup(dis_data)

        result = {}
        for stop in self.stops:
            number = stop["haltenummer"]
            entity = stop["entiteitnummer"]

            departures = await self._parse_departures(
                {"halteDoorkomsten": rt_lookup.get(number, [])}, entity
            )

            # Fallback to full-day timetable when RT has no upcoming data
            if not departures:
                departures = await self._fetch_timetable_fallback(stop)

            result[stop["key"]] = {
                "departures": departures,
                "alerts": self._parse_alerts(dis_lookup.get(number, {})),
            }

        # Pre-warm color cache for any newly discovered lines
        await self._batch_resolve_new_line_colors(result)

        return result

    async def _fetch_timetable_fallback(self, stop: dict) -> list[dict]:
        """Load the scheduled timetable when RT has no upcoming departures.

        Returns departures with prediction='GEENREALTIME' so the sensor
        can indicate these are scheduled times, not live data.
        """
        try:
            data = await self._api_client.fetch_timetable(
                stop["entiteitnummer"], stop["haltenummer"]
            )
        except DeLijnApiError:
            return []

        departures = await self._parse_departures(data, stop["entiteitnummer"])
        for dep in departures:
            if not dep.get("prediction"):
                dep["prediction"] = "GEENREALTIME"
        return departures

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
                colors = await self._resolve_line_colors(entiteitnummer, internal_num)

                dest_nl = (doorkomst.get("bestemmingKort") or doorkomst.get("bestemming") or "").strip()
                dest_fr = (doorkomst.get("bestemmingKortFrans") or "").strip()
                # Use French destination if language is FR and French name is available
                display_dest = (dest_fr if self.language == LANG_FR and dest_fr else dest_nl)

                departures.append({
                    "_internal_line": internal_num,  # kept for batch color lookup
                    "line": public_num or internal_num,
                    "direction": doorkomst.get("richting") or "",
                    "destination": display_dest,
                    "destination_nl": dest_nl,
                    "destination_fr": dest_fr,
                    "scheduled": scheduled_str,
                    "realtime": realtime_str if realtime_str else None,
                    "delay_minutes": delay_minutes,
                    "vehicle_id": doorkomst.get("vrtnum") or "",
                    "prediction": statuses[0] if statuses else "",
                    "cancelled": cancelled,
                    **colors,
                })

        # Sort by effective departure time
        departures.sort(key=lambda d: d.get("realtime") or d.get("scheduled") or "")
        return departures[:MAX_DEPARTURES]

    async def _batch_resolve_new_line_colors(self, result: dict) -> None:
        """Pre-warm the color cache for all new lines discovered in this update cycle.

        Collects uncached (entity, internal_line) pairs across all stops and
        fetches their colors in a single batch call.
        """
        new_keys = []
        for stop in self.stops:
            for dep in result.get(stop["key"], {}).get("departures", []):
                internal = dep.get("_internal_line", "")
                entity = stop["entiteitnummer"]
                if internal and (entity, internal) not in self._line_color_cache:
                    new_keys.append((entity, internal))

        if not new_keys:
            return

        lijnsleutels = "_".join(f"{e}_{l}" for e, l in dict.fromkeys(new_keys))
        try:
            data = await self._api_client.fetch_line_colors_batch(lijnsleutels)
            # The spec has a typo: "lijnLijnkleurCodesijst" (missing "l")
            items = data.get("lijnLijnkleurCodesijst") or data.get("lijnLijnkleurCodeslijst") or []
            for item in items:
                lijn = item.get("lijn") or {}
                e = str(lijn.get("entiteitnummer", ""))
                n = str(lijn.get("lijnnummer", ""))
                codes = item.get("lijnkleurCodes") or {}
                if e and n:
                    colors = await self._resolve_colors_from_codes(codes)
                    self._line_color_cache[(e, n)] = colors
                    _LOGGER.debug("Batch cached colors for line %s: %s", n, colors)
        except DeLijnApiError as err:
            _LOGGER.debug("Batch color fetch failed, will fall back to individual: %s", err)

    async def _resolve_colors_from_codes(self, codes: dict) -> dict:
        """Resolve a lijnkleurCodes object to hex values."""
        mapping = {
            "badge_background": (codes.get("achtergrond") or {}).get("code"),
            "badge_text": (codes.get("voorgrond") or {}).get("code"),
            "badge_border": (codes.get("achtergrondRand") or {}).get("code"),
            "badge_text_border": (codes.get("voorgrondRand") or {}).get("code"),
        }
        return {
            attr: await self._resolve_color_code(code)
            for attr, code in mapping.items()
            if code and await self._resolve_color_code(code)
        }

    async def _resolve_line_colors(self, entiteitnummer: str, lijnnummer: str) -> dict:
        """Return the 4 badge colors for a line, fetched once and cached.

        Keys: badge_background, badge_text, badge_border, badge_text_border.
        """
        if not lijnnummer:
            return {}
        cache_key = (entiteitnummer, lijnnummer)
        if cache_key in self._line_color_cache:
            return self._line_color_cache[cache_key]

        raw = await self._api_client.fetch_line_colors(entiteitnummer, lijnnummer)
        if not raw:
            self._line_color_cache[cache_key] = {}
            return {}

        mapping = {
            "badge_background": raw.get("achtergrond", {}).get("code"),
            "badge_text": raw.get("voorgrond", {}).get("code"),
            "badge_border": raw.get("achtergrondRand", {}).get("code"),
            "badge_text_border": raw.get("voorgrondRand", {}).get("code"),
        }

        # Resolve each code to hex
        colors = {}
        for attr, code in mapping.items():
            if code:
                colors[attr] = await self._resolve_color_code(code)

        self._line_color_cache[cache_key] = colors
        _LOGGER.debug("Line %s colors: %s", lijnnummer, colors)
        return colors

    async def _resolve_color_code(self, code: str) -> str | None:
        """Return hex value for a De Lijn color code, cached."""
        if code in self._color_hex_cache:
            return self._color_hex_cache[code]
        hex_val = await self._api_client.fetch_color(code)
        if hex_val:
            self._color_hex_cache[code] = hex_val
        return hex_val

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


def _build_rt_lookup(batch_data: dict) -> dict:
    """Build haltenummer → list[HalteDoorkomst] from a batch real-time response."""
    lookup: dict[str, list] = {}
    for item in batch_data.get("halteDoorkomstenLijst", []):
        for hdc in item.get("halteDoorkomsten", []):
            number = str(hdc.get("haltenummer", ""))
            if number:
                lookup.setdefault(number, []).append(hdc)
    return lookup


def _build_disruption_lookup(batch_data: dict) -> dict:
    """Build haltenummer → disruption dict from a batch storingen response."""
    lookup: dict[str, dict] = {}
    for item in batch_data.get("halteOmleidingen", []):
        halte = item.get("halte") or {}
        number = str(halte.get("haltenummer", ""))
        if number:
            # Merge omleidingen from this item into a single dict matching single-stop format
            existing = lookup.setdefault(number, {"omleidingen": [], "storingen": []})
            existing["omleidingen"].extend(item.get("omleidingen") or [])
    return lookup


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
