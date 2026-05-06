"""Config flow and options flow for the De Lijn integration."""

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import DeLijnApiClient
from .const import (
    CLASSIFICATIE_FLEX,
    CLASSIFICATIE_TIJDELIJK,
    CONF_API_KEY,
    CONF_LANGUAGE,
    CONF_SCAN_INTERVAL,
    CONF_STOPS,
    DEFAULT_LANGUAGE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LANG_FR,
    LANG_NL,
    MIN_SCAN_INTERVAL,
)
from .stop_cache import StopCache

_LOGGER = logging.getLogger(__name__)


class DeLijnConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step config flow: API key → load stops → search → confirm."""

    VERSION = 1

    def __init__(self) -> None:
        self._api_key: str = ""
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL
        self._language: str = DEFAULT_LANGUAGE
        self._selected_stops: list[dict] = []
        self._search_results: dict[str, dict] = {}  # display_name → group dict
        self._pending_stop: dict | None = None       # stop being confirmed
        self._stop_cache: StopCache | None = None

    # ------------------------------------------------------------------
    # Step 1 — API key + interval
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
                    self._language = user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
                    return await self._init_cache_and_search()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_API_KEY): str,
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    int, vol.Range(min=MIN_SCAN_INTERVAL)
                ),
                vol.Required(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): vol.In({
                    LANG_NL: "Nederlands",
                    LANG_FR: "Français",
                }),
            }),
            errors=errors,
        )

    async def _init_cache_and_search(self):
        """Download stop cache then go to stop search."""
        session = async_get_clientsession(self.hass)
        api_client = DeLijnApiClient(self._api_key, session)
        self._stop_cache = StopCache(self.hass, api_client)
        try:
            await self._stop_cache.initialize()
        except Exception as err:
            _LOGGER.error("Stop cache init failed: %s", err)
            return self.async_abort(reason="cache_download_failed")
        return await self.async_step_add_stop()

    # ------------------------------------------------------------------
    # Step 2 — Search
    # ------------------------------------------------------------------

    async def async_step_add_stop(self, user_input: dict | None = None):
        errors = {}
        if user_input is not None:
            query = user_input.get("stop_search", "").strip()
            if len(query) < 2:
                errors["stop_search"] = "query_too_short"
            else:
                results = self._stop_cache.search(query)
                if not results:
                    errors["stop_search"] = "no_stops_found"
                else:
                    self._search_results = {r["display_name"]: r for r in results}
                    if len(results) == 1:
                        return await self._confirm_group(results[0])
                    return await self.async_step_select_stop()

        return self.async_show_form(
            step_id="add_stop",
            data_schema=vol.Schema({vol.Required("stop_search"): str}),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3 — Select from results
    # ------------------------------------------------------------------

    async def async_step_select_stop(self, user_input: dict | None = None):
        if user_input is not None:
            key = user_input["stop_key"]
            if key == "__back__":
                return await self.async_step_add_stop()
            return await self._confirm_group(self._search_results[key])

        options = {"__back__": "← Search again"} | {
            name: name for name in self._search_results
        }
        legend = _build_legend(self._search_results, self._language)
        return self.async_show_form(
            step_id="select_stop",
            data_schema=vol.Schema({vol.Required("stop_key"): vol.In(options)}),
            description_placeholders={"legend": legend},
        )

    # ------------------------------------------------------------------
    # Step 4 — Confirm (with warnings for TIJDELIJK / FLEX / non-served)
    # ------------------------------------------------------------------

    async def _confirm_group(self, group: dict):
        """Build warning message and ask for confirmation."""
        self._pending_stop = group
        warning = await _build_stop_warning(group, self._stop_cache, self._language)
        return await self.async_step_confirm_stop(warning=warning)

    async def async_step_confirm_stop(self, user_input: dict | None = None, warning: str = ""):
        if user_input is not None:
            action = user_input.get("action")
            if action == "cancel":
                return await self.async_step_add_stop()
            # Add all stop keys from this group
            for key in self._pending_stop["stop_keys"]:
                stop_info = self._stop_cache.get_stop(key)
                if stop_info and not any(s["key"] == key for s in self._selected_stops):
                    self._selected_stops.append(stop_info)
            return await self.async_step_confirm_stops()

        return self.async_show_form(
            step_id="confirm_stop",
            data_schema=vol.Schema({
                vol.Required("action", default="add"): vol.In({
                    "add": "Add this stop",
                    "cancel": "Search again",
                })
            }),
            description_placeholders={"warning": warning},
        )

    # ------------------------------------------------------------------
    # Step 5 — Summary + add more / finish
    # ------------------------------------------------------------------

    async def async_step_confirm_stops(self, user_input: dict | None = None):
        if user_input is not None:
            action = user_input.get("action")
            if action == "add_another":
                return await self.async_step_add_stop()
            return self._create_entry()

        stops_summary = "\n".join(
            f"• {s['name']} ({s['classificatie']})" for s in self._selected_stops
        )
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
        return self.async_create_entry(
            title="De Lijn",
            data={
                CONF_API_KEY: self._api_key,
                CONF_SCAN_INTERVAL: self._scan_interval,
                CONF_LANGUAGE: self._language,
                CONF_STOPS: self._selected_stops,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "DeLijnOptionsFlow":
        return DeLijnOptionsFlow(config_entry)


# ------------------------------------------------------------------
# Options flow
# ------------------------------------------------------------------

class DeLijnOptionsFlow(OptionsFlow):
    """Post-installation management: add/remove stops, change settings."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._search_results: dict[str, dict] = {}
        self._pending_stop: dict | None = None

    async def async_step_init(self, user_input: dict | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options={
                "add_stop": "Add a stop",
                "remove_stop": "Remove a stop",
                "change_api_key": "Change API key",
                "change_interval": "Change refresh interval",
                "change_language": "Change display language",
                "force_cache_refresh": "Force stop data refresh",
            },
        )

    # Add stop
    async def async_step_add_stop(self, user_input: dict | None = None):
        errors = {}
        if user_input is not None:
            query = user_input.get("stop_search", "").strip()
            if len(query) < 2:
                errors["stop_search"] = "query_too_short"
            else:
                cache = await self._get_cache()
                results = cache.search(query)
                if not results:
                    errors["stop_search"] = "no_stops_found"
                else:
                    self._search_results = {r["display_name"]: r for r in results}
                    if len(results) == 1:
                        return await self._confirm_group(results[0])
                    return await self.async_step_select_stop()

        return self.async_show_form(
            step_id="add_stop",
            data_schema=vol.Schema({vol.Required("stop_search"): str}),
            errors=errors,
        )

    async def async_step_select_stop(self, user_input: dict | None = None):
        if user_input is not None:
            key = user_input["stop_key"]
            if key == "__back__":
                return await self.async_step_add_stop()
            return await self._confirm_group(self._search_results[key])

        options = {"__back__": "← Search again"} | {
            name: name for name in self._search_results
        }
        lang = self._config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        legend = _build_legend(self._search_results, lang)
        return self.async_show_form(
            step_id="select_stop",
            data_schema=vol.Schema({vol.Required("stop_key"): vol.In(options)}),
            description_placeholders={"legend": legend},
        )

    async def _confirm_group(self, group: dict):
        self._pending_stop = group
        cache = await self._get_cache()
        lang = self._config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        warning = await _build_stop_warning(group, cache, lang)
        return await self.async_step_confirm_stop(warning=warning)

    async def async_step_confirm_stop(self, user_input: dict | None = None, warning: str = ""):
        if user_input is not None:
            if user_input.get("action") == "cancel":
                return await self.async_step_add_stop()

            cache = await self._get_cache()
            current_stops = list(self._config_entry.data.get(CONF_STOPS, []))
            for key in self._pending_stop["stop_keys"]:
                stop_info = cache.get_stop(key)
                if stop_info and not any(s["key"] == key for s in current_stops):
                    current_stops.append(stop_info)
            return self._save({CONF_STOPS: current_stops})

        return self.async_show_form(
            step_id="confirm_stop",
            data_schema=vol.Schema({
                vol.Required("action", default="add"): vol.In({
                    "add": "Add this stop",
                    "cancel": "Search again",
                })
            }),
            description_placeholders={"warning": warning},
        )

    # Remove stop
    async def async_step_remove_stop(self, user_input: dict | None = None):
        current_stops = self._config_entry.data.get(CONF_STOPS, [])
        if user_input is not None:
            key_to_remove = user_input["stop_key"]
            stop_to_remove = next((s for s in current_stops if s["key"] == key_to_remove), None)
            remaining = [s for s in current_stops if s["key"] != key_to_remove]

            # Remove entities from entity registry
            entity_reg = er.async_get(self.hass)
            for entity_entry in er.async_entries_for_config_entry(entity_reg, self._config_entry.entry_id):
                if f"_{key_to_remove}_" in entity_entry.unique_id or entity_entry.unique_id.endswith(f"_{key_to_remove}"):
                    entity_reg.async_remove(entity_entry.entity_id)

            # Remove device if no remaining stop shares the same stop name (= same device)
            if stop_to_remove:
                stop_name = stop_to_remove.get("name", "")
                same_name_still_exists = any(s.get("name") == stop_name for s in remaining)
                if not same_name_still_exists and stop_name:
                    device_reg = dr.async_get(self.hass)
                    device_id = _slug(stop_name)
                    device = device_reg.async_get_device({(DOMAIN, device_id)})
                    if device:
                        device_reg.async_remove_device(device.id)

            return self._save({CONF_STOPS: remaining})

        stop_options = {s["key"]: f"{s['name']} ({s['haltenummer']})" for s in current_stops}
        if not stop_options:
            return self._save({})

        return self.async_show_form(
            step_id="remove_stop",
            data_schema=vol.Schema({vol.Required("stop_key"): vol.In(stop_options)}),
        )

    # Change API key
    async def async_step_change_api_key(self, user_input: dict | None = None):
        errors = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            session = async_get_clientsession(self.hass)
            if not await DeLijnApiClient(api_key, session).validate_api_key():
                errors[CONF_API_KEY] = "invalid_api_key"
            else:
                return self._save({CONF_API_KEY: api_key})

        return self.async_show_form(
            step_id="change_api_key",
            data_schema=vol.Schema({vol.Required(CONF_API_KEY): str}),
            errors=errors,
        )

    # Change interval
    async def async_step_change_interval(self, user_input: dict | None = None):
        errors = {}
        if user_input is not None:
            interval = user_input[CONF_SCAN_INTERVAL]
            if interval < MIN_SCAN_INTERVAL:
                errors[CONF_SCAN_INTERVAL] = "interval_too_low"
            else:
                return self._save({CONF_SCAN_INTERVAL: interval})

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

    # Change language
    async def async_step_change_language(self, user_input: dict | None = None):
        if user_input is not None:
            return self._save({CONF_LANGUAGE: user_input[CONF_LANGUAGE]})

        current = self._config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        return self.async_show_form(
            step_id="change_language",
            data_schema=vol.Schema({
                vol.Required(CONF_LANGUAGE, default=current): vol.In({
                    LANG_NL: "Nederlands",
                    LANG_FR: "Français",
                }),
            }),
        )

    # Force cache refresh
    async def async_step_force_cache_refresh(self, user_input: dict | None = None):
        if user_input is not None:
            if user_input.get("confirm"):
                entry_data = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id, {})
                cache = entry_data.get("stop_cache")
                if cache:
                    try:
                        await cache.force_refresh()
                    except Exception as err:
                        _LOGGER.error("Cache refresh failed: %s", err)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="force_cache_refresh",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
        )

    # Helpers
    async def _get_cache(self) -> StopCache:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id, {})
        if "stop_cache" in entry_data:
            return entry_data["stop_cache"]
        session = async_get_clientsession(self.hass)
        api_client = DeLijnApiClient(self._config_entry.data[CONF_API_KEY], session)
        cache = StopCache(self.hass, api_client)
        await cache.initialize()
        return cache

    def _save(self, updates: dict):
        new_data = {**self._config_entry.data, **updates}
        self.hass.config_entries.async_update_entry(self._config_entry, data=new_data)
        return self.async_create_entry(title="", data={})


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

import re as _re

def _slug(text: str) -> str:
    """Convert a stop name to a lowercase underscore-separated slug (same as sensor.py)."""
    text = text.lower().strip()
    text = _re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


# ------------------------------------------------------------------
# Shared warning builder
# ------------------------------------------------------------------

_LEGEND_STRINGS = {
    "fr": {
        "TIJDELIJK": "⚠️ Arrêt temporaire — peut être supprimé à la fin des travaux",
        "FLEX": "ℹ️ Arrêt Flexbus — réservation requise (015 40 88 88 ou app De Lijn Flex)",
    },
    "nl": {
        "TIJDELIJK": "⚠️ Tijdelijke halte — kan worden verwijderd na de werken",
        "FLEX": "ℹ️ Flexbus halte — reservering vereist (015 40 88 88 of De Lijn Flex app)",
    },
}

_WARNING_STRINGS = {
    "fr": {
        "TIJDELIJK": "⚠️ L'arrêt **{num}** ({name}) est un **arrêt temporaire**. Il peut disparaître ou ne pas avoir de données temps réel fiables.",
        "FLEX": "ℹ️ L'arrêt **{num}** est un **arrêt Flexbus** (à la demande). Réservation requise : appelez le **015 40 88 88** ou utilisez l'app **De Lijn Flex**. Aucune donnée temps réel disponible dans Home Assistant.",
    },
    "nl": {
        "TIJDELIJK": "⚠️ Halte **{num}** ({name}) is een **tijdelijke halte**. Deze kan verdwijnen of geen betrouwbare realtimedata hebben.",
        "FLEX": "ℹ️ Halte **{num}** is een **Flexbus halte** (op aanvraag). Reservering vereist: bel **015 40 88 88** of gebruik de **De Lijn Flex** app. Geen realtimedata beschikbaar in Home Assistant.",
    },
}


def _build_legend(search_results: dict, language: str = "nl") -> str:
    """Build a legend explaining warning icons shown in search results."""
    lang = "fr" if language == LANG_FR else "nl"
    strings = _LEGEND_STRINGS[lang]
    has_tijdelijk = any("TIJDELIJK" in r.get("warnings", []) for r in search_results.values())
    has_flex = any("FLEX" in r.get("warnings", []) for r in search_results.values())
    lines = []
    if has_tijdelijk:
        lines.append(strings["TIJDELIJK"])
    if has_flex:
        lines.append(strings["FLEX"])
    return "\n".join(lines)


async def _build_stop_warning(group: dict, cache: StopCache, language: str = "nl") -> str:
    """Build a warning string for the confirm step based on stop classification."""
    lang = "fr" if language == LANG_FR else "nl"
    strings = _WARNING_STRINGS[lang]
    warnings = []

    for key in group["stop_keys"]:
        stop = cache.get_stop(key)
        if not stop:
            continue
        classificatie = stop.get("classificatie", "")
        if classificatie == CLASSIFICATIE_TIJDELIJK:
            warnings.append(strings["TIJDELIJK"].format(
                num=stop["haltenummer"], name=stop["name"]
            ))
        elif classificatie == CLASSIFICATIE_FLEX:
            warnings.append(strings["FLEX"].format(num=stop["haltenummer"]))

    return "\n\n".join(warnings) + ("\n\n" if warnings else "")
