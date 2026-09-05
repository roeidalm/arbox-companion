"""Sensors — all read from the arbox-server summary."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ArboxCoordinator
from .entity import ArboxEntity, session_attrs, session_dt


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Arbox sensors."""
    coordinator: ArboxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            ArboxMembershipSensor(coordinator, entry.entry_id),
            ArboxNextClassSensor(coordinator, entry.entry_id),
            ArboxBookedClassesSensor(coordinator, entry.entry_id),
            ArboxNextClassNameSensor(coordinator, entry.entry_id),
            ArboxJournalSensor(coordinator, entry.entry_id),
        ]
    )


def _describe(session: dict) -> str:
    """One-line human summary: what the class is, not when."""
    bits = [session.get("category_name") or "Class"]
    if session.get("coach_name"):
        bits.append(session["coach_name"])
    return " · ".join(bits)


class ArboxMembershipSensor(ArboxEntity, SensorEntity):
    _attr_name = "Membership"
    _attr_icon = "mdi:card-account-details"

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "membership")

    @property
    def native_value(self) -> str | None:
        m = (self.coordinator.data or {}).get("membership") or {}
        return m.get("plan")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = (self.coordinator.data or {}).get("membership") or {}
        memberships = (self.coordinator.data or {}).get("memberships") or []
        quota = (self.coordinator.data or {}).get("quota") or {}
        return {
            "price": m.get("price"),
            "active": m.get("active"),
            "start": m.get("start"),
            "end": m.get("end"),
            "sessions_left": m.get("sessions_left"),
            "membership_user_id": m.get("id"),
            "memberships": [{
                "id": item.get("id"), "name": item.get("plan"),
                "active": item.get("active"), "start": item.get("start"),
                "end": item.get("end"),
                "sessions_left": item.get("sessions_left"),
                "sessions_on_purchase": item.get("sessions_on_purchase"),
            } for item in memberships],
            "used": quota.get("used"),
            "reserved": quota.get("reserved"),
            "planned": quota.get("planned"),
            "remaining": quota.get("remaining"),
            "available_after_planned": quota.get("available_after_planned"),
        }


class ArboxNextClassSensor(ArboxEntity, SensorEntity):
    _attr_name = "Next class"
    _attr_icon = "mdi:run"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "next_class")

    @property
    def native_value(self) -> datetime | None:
        nxt = (self.coordinator.data or {}).get("next_class")
        return session_dt(nxt, tz=self._studio_tz) if nxt else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = (self.coordinator.data or {}).get("next_class")
        return session_attrs(nxt) if nxt else {}


class ArboxNextClassNameSensor(ArboxEntity, SensorEntity):
    """The next class as readable text — a timestamp alone says nothing."""

    _attr_name = "Next class name"
    _attr_icon = "mdi:text-short"

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "next_class_name")

    @property
    def native_value(self) -> str:
        nxt = (self.coordinator.data or {}).get("next_class")
        if not nxt:
            return "אין שיעור קרוב"
        # 255-char state cap: this is always far below it
        return f"{nxt['date']} {nxt['start_time']} · {_describe(nxt)}"[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt = (self.coordinator.data or {}).get("next_class")
        return session_attrs(nxt) if nxt else {}


class ArboxBookedClassesSensor(ArboxEntity, SensorEntity):
    _attr_name = "Booked classes"
    _attr_icon = "mdi:format-list-checks"

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "booked_classes")

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("my_sessions") or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        mine = data.get("my_sessions") or []
        return {
            "sessions": [session_attrs(s) for s in mine],
            # ready-to-render lines, so a markdown card needs no templating
            "summary": [
                f"{s['date']} {s['start_time']}–{s.get('end_time') or ''} · "
                f"{_describe(s)}"
                + (f" (המתנה {s['stand_by_position']})"
                   if s.get("user_in_standby") is not None else "")
                for s in mine
            ],
            "last_sync": data.get("last_sync"),
        }


class ArboxJournalSensor(ArboxEntity, SensorEntity):
    """Read-only journal summary; editing stays in the server web app."""

    _attr_name = "Workout journal"
    _attr_icon = "mdi:notebook-heart-outline"

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "workout_journal")

    @property
    def native_value(self) -> int:
        return int(((self.coordinator.data or {}).get("journal") or {}).get("count") or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        journal = (self.coordinator.data or {}).get("journal") or {}
        return {"level": journal.get("level"),
                "entries": journal.get("entries") or []}
