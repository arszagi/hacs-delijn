"""De Lijn Home Assistant integration."""

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api_client import DeLijnApiClient, DeLijnApiError
from .const import CONF_API_KEY, CONF_SCAN_INTERVAL, CONF_STOP_IDS, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import DeLijnCoordinator
from .gtfs_static import GtfsStaticManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up De Lijn from a config entry."""
    api_key = entry.data[CONF_API_KEY]
    stop_ids = set(entry.data.get(CONF_STOP_IDS, []))
    scan_interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    session = async_get_clientsession(hass)
    api_client = DeLijnApiClient(api_key, session)
    gtfs_manager = GtfsStaticManager(hass, api_client)

    # Load GTFS static data (from cache or download)
    try:
        await gtfs_manager.initialize()
    except Exception as err:
        raise ConfigEntryNotReady(f"Failed to load GTFS static data: {err}") from err

    # Check for GTFS updates on startup (lightweight Last-Modified check)
    try:
        await gtfs_manager.refresh_if_updated()
    except Exception:
        _LOGGER.warning("Could not check for GTFS static updates at startup")

    coordinator = DeLijnCoordinator(hass, api_client, gtfs_manager, stop_ids, scan_interval)

    # Validate connectivity before marking the entry as ready
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(f"Could not reach De Lijn API: {err}") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "gtfs_manager": gtfs_manager,
        "api_client": api_client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and remove all associated entities."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
