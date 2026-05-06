"""De Lijn Home Assistant integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import DeLijnApiClient
from .const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_STOPS, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import DeLijnCoordinator
from .stop_cache import StopCache

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up De Lijn from a config entry."""
    api_key = entry.data[CONF_API_KEY]
    stops = entry.data.get(CONF_STOPS, [])
    scan_interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    session = async_get_clientsession(hass)
    api_client = DeLijnApiClient(api_key, session)
    stop_cache = StopCache(hass, api_client)

    try:
        await stop_cache.initialize()
    except Exception as err:
        raise ConfigEntryNotReady(f"Failed to load stop data: {err}") from err

    coordinator = DeLijnCoordinator(hass, api_client, stops, scan_interval)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(f"Could not reach De Lijn API: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "stop_cache": stop_cache,
        "api_client": api_client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
