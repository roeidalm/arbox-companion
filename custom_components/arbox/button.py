"""Buttons — the actionable half of the integration.

Sensors tell you what's happening; these let you act on it from a dashboard
or an automation without having to know a schedule_id.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import ArboxCoordinator
from .entity import ArboxEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Arbox buttons."""
    # Remove the old one-tap destructive action from existing installations too.
    registry = er.async_get(hass)
    legacy = registry.async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_cancel_next")
    if legacy:
        registry.async_remove(legacy)
    coordinator: ArboxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ArboxRefreshButton(coordinator, entry.entry_id),
            ArboxGoogleCalendarSyncButton(coordinator, entry.entry_id),
        ]
    )


class ArboxRefreshButton(ArboxEntity, ButtonEntity):
    """Pull a fresh schedule from Arbox right now."""

    _attr_name = "Refresh schedule"
    _attr_icon = "mdi:refresh"

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "refresh")

    async def async_press(self) -> None:
        if not await self.coordinator.refresh_from_arbox():
            raise HomeAssistantError("Refresh failed — see log")


class ArboxGoogleCalendarSyncButton(ArboxEntity, ButtonEntity):
    _attr_name = "Sync Google Calendar"
    _attr_icon = "mdi:calendar-sync"

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "google_calendar_sync")

    @property
    def available(self) -> bool:
        status = (self.coordinator.data or {}).get("google_calendar") or {}
        return super().available and bool(status.get("connected") and status.get("enabled"))

    async def async_press(self) -> None:
        await self.coordinator.sync_google_calendar()
