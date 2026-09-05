"""Services — proxied to arbox-server through the coordinator."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    SERVICE_BOOK_CLASS,
    SERVICE_CANCEL_BOOKING,
    SERVICE_JOIN_STANDBY,
)
from .coordinator import ArboxCoordinator

_LOGGER = logging.getLogger(__name__)

ATTR_SCHEDULE_ID = "schedule_id"
ATTR_LATE_CANCEL = "late_cancel"
ATTR_REASON_CODE = "reason_code"
ATTR_REASON_TEXT = "reason_text"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"

SERVICE_SCHEMA = vol.Schema({
    vol.Required(ATTR_SCHEDULE_ID): cv.positive_int,
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})

CANCEL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SCHEDULE_ID): cv.positive_int,
        vol.Optional(ATTR_LATE_CANCEL, default=False): cv.boolean,
        vol.Optional(ATTR_REASON_CODE, default="none"): vol.In({
            "work", "illness", "injury", "personal", "fatigue",
            "plans_changed", "other", "none",
        }),
        vol.Optional(ATTR_REASON_TEXT): cv.string,
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


def _get_coordinator(hass: HomeAssistant, entry_id: str | None = None) -> ArboxCoordinator:
    """The server to act on.

    With two entries configured, picking the first one silently sent bookings
    and cancellations to whichever server happened to be first in hass.data —
    so that case now asks instead of guessing.
    """
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("Arbox is not set up")
    if entry_id:
        coordinator = entries.get(entry_id)
        if not coordinator:
            raise HomeAssistantError(f"No Arbox entry with id {entry_id}")
        return coordinator
    if len(entries) > 1:
        raise HomeAssistantError(
            "More than one Arbox server is configured — pass config_entry_id "
            f"to say which one (available: {', '.join(entries)})")
    return next(iter(entries.values()))


def async_register_services(hass: HomeAssistant) -> None:
    """Register Arbox services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_BOOK_CLASS):
        return

    async def handle(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        schedule_id = call.data[ATTR_SCHEDULE_ID]
        if call.service == SERVICE_CANCEL_BOOKING:
            ok = await coordinator.cancel_booking(
                schedule_id, call.data.get(ATTR_LATE_CANCEL, False),
                call.data.get(ATTR_REASON_CODE, "none"),
                call.data.get(ATTR_REASON_TEXT),
            )
        elif call.service == SERVICE_BOOK_CLASS:
            ok = await coordinator.book_class(schedule_id)
        else:
            ok = await coordinator.join_standby(schedule_id)
        if not ok:
            raise HomeAssistantError(
                f"{call.service} failed for schedule {schedule_id} — see log"
            )

    hass.services.async_register(DOMAIN, SERVICE_BOOK_CLASS, handle, schema=SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CANCEL_BOOKING, handle, schema=CANCEL_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_JOIN_STANDBY, handle, schema=SERVICE_SCHEMA)


def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove Arbox services."""
    for service in (SERVICE_BOOK_CLASS, SERVICE_CANCEL_BOOKING, SERVICE_JOIN_STANDBY):
        hass.services.async_remove(DOMAIN, service)
