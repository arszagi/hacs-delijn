"""Config flow and options flow for the De Lijn integration."""

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import DeLijnApiClient
from .const import (
    CONF_API_KEY,
    CONF_SCAN_INTERVAL,
    CONF_STOP_IDS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .gtfs_static import GtfsStaticManager

_LOGGER = logging.getLogger(__name__)

# Maximum number of stop results shown to the user during search
_MAX_SEARCH_RESULTS = 20


class DeLijnConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step config flow: API key → GTFS download → stop search → confirm."""

    VERSION = 1

    def __init__(self) -> None:
        self._api_key: str = ""
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL
        self._selected_groups: dict[str, list[str]] = {}   # display_name → [stop_ids]
        self._search_results: dict[str, dict] = {}          # display_name → group dict
        self._gtfs_manager: GtfsStaticManager | None = None

    # ------------------------------------------------------------------
    # Step 1 — API key + scan interval
    # ------------------------------------------------------------------

    async def async_step_user(self, user_input: dict | None = None):
        errors = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            scan_interval = user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

            if scan_interval < MIN_SCAN_INTERVAL:
                errors[CONF_SCAN_INTERVAL] = "interval_too_low"
            else:
                session = async_get_clientsession(self.hass)
                client = DeLijnApiClient(api_key, session)
                if not await client.validate_api_key():
                    errors[CONF_API_KEY] = "invalid_api_key"
                else:
                    self._api_key = api_key
                    self._scan_interval = scan_interval
                    # Download GTFS data (shows loading spinner in UI)
                    return await self._async_init_gtfs_and_step_add_stop()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    int, vol.Range(min=MIN_SCAN_INTERVAL)
                ),
            }),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 — Download GTFS then search for a stop
    # ------------------------------------------------------------------

    async def _async_init_gtfs_and_step_add_stop(self):
        """Download GTFS static data (if needed) and proceed to stop search."""
        session = async_get_clientsession(self.hass)
        api_client = DeLijnApiClient(self._api_key, session)
        self._gtfs_manager = GtfsStaticManager(self.hass, api_client)
        try:
            await self._gtfs_manager.initialize()
        except Exception as err:
            _LOGGER.error("GTFS download failed during config flow: %s", err)
            return self.async_abort(reason="gtfs_download_failed")
        return await self.async_step_add_stop()

    async def async_step_add_stop(self, user_input: dict | None = None):
        errors = {}

        if user_input is not None:
            query = user_input.get("stop_search", "").strip()
            if len(query) < 2:
                errors["stop_search"] = "query_too_short"
            else:
                results = self._gtfs_manager.search_stops(query)[:_MAX_SEARCH_RESULTS]
                if not results:
                    errors["stop_search"] = "no_stops_found"
                else:
                    self._search_results = {r["display_name"]: r for r in results}
                    if len(results) == 1:
                        return await self._add_group_and_confirm(results[0])
                    return await self.async_step_select_stop()

        return self.async_show_form(
            step_id="add_stop",
            data_schema=vol.Schema({vol.Required("stop_search"): str}),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3 — Select stop from search results
    # ------------------------------------------------------------------

    async def async_step_select_stop(self, user_input: dict | None = None):
        if user_input is not None:
            group = self._search_results[user_input["stop_id"]]
            return await self._add_group_and_confirm(group)

        options = {display_name: display_name for display_name in self._search_results}
        return self.async_show_form(
            step_id="select_stop",
            data_schema=vol.Schema({
                vol.Required("stop_id"): vol.In(options)
            }),
        )

    # ------------------------------------------------------------------
    # Step 4 — Confirm stops + add more or finish
    # ------------------------------------------------------------------

    async def _add_group_and_confirm(self, group: dict):
        self._selected_groups[group["display_name"]] = group["stop_ids"]
        return await self.async_step_confirm_stops()

    async def async_step_confirm_stops(self, user_input: dict | None = None):
        if user_input is not None:
            action = user_input.get("action")
            if action == "add_another":
                return await self.async_step_add_stop()
            return self._create_entry()

        stops_summary = "\n".join(f"• {name}" for name in self._selected_groups)
        return self.async_show_form(
            step_id="confirm_stops",
            data_schema=vol.Schema({
                vol.Required("action", default="finish"): vol.In({
                    "add_another": "Add another stop",
                    "finish": "Finish",
                })
            }),
            description_placeholders={"stops": stops_summary},
        )

    def _create_entry(self):
        # Flatten all group stop_ids into a single deduplicated list
        all_stop_ids = list({
            sid
            for ids in self._selected_groups.values()
            for sid in ids
        })
        return self.async_create_entry(
            title="De Lijn",
            data={
                CONF_API_KEY: self._api_key,
                CONF_SCAN_INTERVAL: self._scan_interval,
                CONF_STOP_IDS: all_stop_ids,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "DeLijnOptionsFlow":
        return DeLijnOptionsFlow(config_entry)


# ------------------------------------------------------------------
# Options flow — post-installation management
# ------------------------------------------------------------------

class DeLijnOptionsFlow(OptionsFlow):
    """Allows the user to manage stops, API key and refresh interval after setup."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._search_results: dict[str, dict] = {}   # display_name → group dict
        self._gtfs_manager: GtfsStaticManager | None = None

    async def async_step_init(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "add_stop": "Add a stop",
                "remove_stop": "Remove a stop",
                "change_api_key": "Change API key",
                "change_interval": "Change refresh interval",
                "force_gtfs_refresh": "Force schedule data refresh",
            },
        )

    # ------------------------------------------------------------------
    # Add stop
    # ------------------------------------------------------------------

    async def async_step_add_stop(self, user_input: dict | None = None):
        errors = {}

        if user_input is not None:
            query = user_input.get("stop_search", "").strip()
            if len(query) < 2:
                errors["stop_search"] = "query_too_short"
            else:
                gtfs = await self._get_gtfs_manager()
                results = gtfs.search_stops(query)[:_MAX_SEARCH_RESULTS]
                if not results:
                    errors["stop_search"] = "no_stops_found"
                else:
                    self._search_results = {r["display_name"]: r for r in results}
                    if len(results) == 1:
                        return await self._save_new_group(results[0])
                    return await self.async_step_select_stop()

        return self.async_show_form(
            step_id="add_stop",
            data_schema=vol.Schema({vol.Required("stop_search"): str}),
            errors=errors,
        )

    async def async_step_select_stop(self, user_input: dict | None = None):
        if user_input is not None:
            group = self._search_results[user_input["stop_id"]]
            return await self._save_new_group(group)

        options = {name: name for name in self._search_results}
        return self.async_show_form(
            step_id="select_stop",
            data_schema=vol.Schema({
                vol.Required("stop_id"): vol.In(options)
            }),
        )

    async def _save_new_group(self, group: dict):
        current_stops = list(self._config_entry.data.get(CONF_STOP_IDS, []))
        for stop_id in group["stop_ids"]:
            if stop_id not in current_stops:
                current_stops.append(stop_id)
        return self._save_data({CONF_STOP_IDS: current_stops})

    # ------------------------------------------------------------------
    # Remove stop
    # ------------------------------------------------------------------

    async def async_step_remove_stop(self, user_input: dict | None = None):
        current_stops = self._config_entry.data.get(CONF_STOP_IDS, [])

        if user_input is not None:
            stops_to_remove = set(user_input.get(CONF_STOP_IDS, []))
            remaining = [s for s in current_stops if s not in stops_to_remove]

            # Clean up entities from the registry for removed stops
            registry = er.async_get(self.hass)
            for entity_entry in er.async_entries_for_config_entry(registry, self._config_entry.entry_id):
                for removed_stop in stops_to_remove:
                    # unique_id pattern: delijn_{stop_id}_{...}
                    if f"_{removed_stop}_" in entity_entry.unique_id:
                        registry.async_remove(entity_entry.entity_id)
                        break

            return self._save_data({CONF_STOP_IDS: remaining})

        gtfs = await self._get_gtfs_manager()
        stop_options = {
            stop_id: gtfs.get_stop_name(stop_id)
            for stop_id in current_stops
        }

        return self.async_show_form(
            step_id="remove_stop",
            data_schema=vol.Schema({
                vol.Required(CONF_STOP_IDS): vol.In(stop_options)
            }),
        )

    # ------------------------------------------------------------------
    # Change API key
    # ------------------------------------------------------------------

    async def async_step_change_api_key(self, user_input: dict | None = None):
        errors = {}

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            session = async_get_clientsession(self.hass)
            client = DeLijnApiClient(api_key, session)
            if not await client.validate_api_key():
                errors[CONF_API_KEY] = "invalid_api_key"
            else:
                return self._save_data({CONF_API_KEY: api_key})

        return self.async_show_form(
            step_id="change_api_key",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Change scan interval
    # ------------------------------------------------------------------

    async def async_step_change_interval(self, user_input: dict | None = None):
        errors = {}

        if user_input is not None:
            interval = user_input[CONF_SCAN_INTERVAL]
            if interval < MIN_SCAN_INTERVAL:
                errors[CONF_SCAN_INTERVAL] = "interval_too_low"
            else:
                return self._save_data({CONF_SCAN_INTERVAL: interval})

        current = self._config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="change_interval",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    int, vol.Range(min=MIN_SCAN_INTERVAL)
                )
            }),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Force GTFS refresh
    # ------------------------------------------------------------------

    async def async_step_force_gtfs_refresh(self, user_input: dict | None = None):
        if user_input is not None:
            if user_input.get("confirm"):
                entry_data = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id, {})
                gtfs = entry_data.get("gtfs_manager")
                if gtfs:
                    try:
                        await gtfs.force_refresh()
                    except Exception as err:
                        _LOGGER.error("Force GTFS refresh failed: %s", err)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="force_gtfs_refresh",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_gtfs_manager(self) -> GtfsStaticManager:
        """Return the live GtfsStaticManager if available, or create a temporary one."""
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id, {})
        if "gtfs_manager" in entry_data:
            return entry_data["gtfs_manager"]

        # Fallback: create a temporary manager using the stored API key
        session = async_get_clientsession(self.hass)
        api_client = DeLijnApiClient(self._config_entry.data[CONF_API_KEY], session)
        manager = GtfsStaticManager(self.hass, api_client)
        await manager.initialize()
        return manager

    def _save_data(self, updates: dict):
        """Merge updates into config entry data and close the options flow."""
        new_data = {**self._config_entry.data, **updates}
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        return self.async_create_entry(title="", data={})
