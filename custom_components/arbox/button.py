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
    coordinator: ArboxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ArboxRefreshButton(coordinator, entry.entry_id),
            ArboxCancelNextButton(coordinator, entry.entry_id),
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


class ArboxCancelNextButton(ArboxEntity, ButtonEntity):
    """Cancel the next booked class, without needing its schedule_id."""

    _attr_name = "Cancel next class"
    _attr_icon = "mdi:calendar-remove"

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "cancel_next")

    @property
    def available(self) -> bool:
        """Only pressable when there is actually something to cancel."""
        data = self.coordinator.data or {}
        nxt = data.get("next_class")
        return super().available and bool(nxt and nxt.get("schedule_id"))

    async def async_press(self) -> None:
        nxt = (self.coordinator.data or {}).get("next_class")
        if not nxt:
            raise HomeAssistantError("No upcoming booked class")
        if not await self.coordinator.cancel_booking(nxt["schedule_id"]):
            raise HomeAssistantError(
                "Cancel failed — inside the late-cancel window? Use the "
                "arbox.cancel_booking service with late_cancel: true"
            )
