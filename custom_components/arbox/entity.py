"""Shared entity base and helpers for the Arbox integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ArboxCoordinator

class ArboxEntity(CoordinatorEntity[ArboxCoordinator]):
    """Base entity: per-entry device info and naming convention."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{key}"
        # Built per entry rather than shared: a fixed identifier merged two
        # configured servers into one device, and the hardcoded URL pointed at
        # a Docker-internal name no browser outside that network can resolve.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Arbox",
            manufacturer="Arbox",
            model="Booking server",
            configuration_url=coordinator.base_url,
        )

    @property
    def _studio_tz(self):
        """The studio's timezone, as the server reports it."""
        return studio_tz((self.coordinator.data or {}).get("timezone"))


def studio_tz(name: str | None):
    """Resolve the server's timezone name, falling back to HA's own.

    The server sends studio-local wall-clock times. Stamping them with HA's
    timezone is only right when the two agree — a HA container left on UTC
    published an 18:00 Jerusalem class as 18:00 UTC, three hours out.
    """
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return dt_util.DEFAULT_TIME_ZONE


def session_dt(session: dict, key: str = "start_time",
               tz=None) -> datetime | None:
    """Session date+time as an aware datetime in the studio's timezone."""
    try:
        naive = datetime.fromisoformat(f"{session['date']}T{session[key]}")
    except (KeyError, TypeError, ValueError):
        return None
    return naive.replace(tzinfo=tz or dt_util.DEFAULT_TIME_ZONE)


def session_attrs(session: dict) -> dict[str, Any]:
    """The attribute payload shared by the session-shaped sensors."""
    return {
        "date": session.get("date"),
        "start_time": session.get("start_time"),
        "end_time": session.get("end_time"),
        "category": session.get("category_name"),
        "description": session.get("category_bio"),
        "coach": session.get("coach_name"),
        "schedule_id": session.get("schedule_id"),
        "registered": session.get("registered"),
        "max": session.get("max_users"),
        "free": session.get("free"),
        "booking_option": session.get("booking_option"),
        "user_booked": session.get("user_booked"),
        "user_in_standby": session.get("user_in_standby"),
        "stand_by_position": session.get("stand_by_position"),
    }
