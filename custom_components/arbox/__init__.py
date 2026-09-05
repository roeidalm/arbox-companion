"""Arbox integration — thin client of arbox-server."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN
from .coordinator import ArboxCoordinator
from .services import async_register_services, async_unregister_services
from .feedback import async_setup_feedback, async_unload_feedback

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.CALENDAR, Platform.SENSOR]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a v1 entry (direct-to-Arbox, email/password) to v2 (arbox-server).

    The old credentials are useless to the thin client — and dropping them
    also purges the stored password. The default base_url matches the
    documented deployment (arbox-server on the shared Docker network); the
    API key must be added by re-adding the integration.
    """
    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry,
            data={CONF_BASE_URL: DEFAULT_BASE_URL, CONF_API_KEY: ""},
            version=2,
        )
        _LOGGER.warning(
            "Arbox entry migrated to v2 (thin client). Old cloud credentials "
            "were discarded. If your arbox-server is not at %s, or to add the "
            "API key, use Reconfigure on the integration",
            DEFAULT_BASE_URL,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Arbox from a config entry."""
    coordinator = ArboxCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    await async_setup_feedback(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
            async_unregister_services(hass)
            async_unload_feedback(hass)
    return unload_ok
