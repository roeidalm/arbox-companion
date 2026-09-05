"""Calendar — the week's classes from arbox-server, with booking markers."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ArboxCoordinator
from .entity import ArboxEntity, session_dt


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Arbox calendar."""
    coordinator: ArboxCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        ArboxScheduleCalendar(coordinator, entry.entry_id),
        ArboxMyClassesCalendar(coordinator, entry.entry_id),
    ])


def _event(session: dict, tz=None) -> CalendarEvent | None:
    start = session_dt(session, tz=tz)
    if start is None:
        return None
    end = session_dt(session, "end_time", tz=tz)
    if end is None or end <= start:
        end = start + timedelta(hours=1)

    title = session.get("category_name") or "Class"
    if session.get("coach_name"):
        title += f" — {session['coach_name']}"

    booked = session.get("user_booked") is not None
    standby = session.get("user_in_standby") is not None
    free = session.get("free")

    if booked:
        title = f"🔵 {title}"
    elif standby:
        title = f"🟡 {title} (המתנה)"
    elif session.get("booking_option") == "insertStandby" or free == 0:
        title = f"🔴 {title}"
    elif session.get("booking_option") == "insertScheduleUser":
        title = f"🟢 {title}"

    parts = [
        f"{session.get('registered', '?')}/{session.get('max_users', '?')} registered",
        f"{free if free is not None else '?'} free",
        f"Schedule ID: {session.get('schedule_id')}",
        f"booking_option: {session.get('booking_option')}",
    ]
    if booked:
        parts.append("✅ אתה רשום!")
    elif standby:
        parts.append(f"⏳ בהמתנה (מקום {session.get('stand_by_position', '?')})")

    bio = (session.get("category_bio") or "").strip()
    description = " | ".join(parts)
    if bio:
        description = f"{bio}\n\n{description}"

    return CalendarEvent(
        summary=title,
        start=start,
        end=end,
        description=description,
    )


class ArboxCalendarBase(ArboxEntity, CalendarEntity):
    """Shared event plumbing; subclasses choose which sessions to show."""

    _source_key = "week"

    def _events(self) -> list[CalendarEvent]:
        sessions = (self.coordinator.data or {}).get(self._source_key) or []
        tz = self._studio_tz
        events = [ev for session in sessions if (ev := _event(session, tz))]
        return sorted(events, key=lambda e: e.start)

    @property
    def event(self) -> CalendarEvent | None:
        """The class happening now, or the next one.

        end > now, not start > now: CalendarEntity derives its on/off state
        from whether this event spans the present moment, so skipping the
        class in progress meant the entity was never 'on' — and
        `state('calendar.arbox_my_classes') == 'on'` ("am I training right
        now?") could never be true on the calendar built for exactly that.
        """
        now = dt_util.now()
        return next((ev for ev in self._events() if ev.end > now), None)

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Events within a range."""
        return [
            ev for ev in self._events()
            if ev.end > start_date and ev.start < end_date
        ]


class ArboxScheduleCalendar(ArboxCalendarBase):
    """Everything the studio runs this week."""

    _attr_name = "Schedule"
    _source_key = "week"

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "schedule")


class ArboxMyClassesCalendar(ArboxCalendarBase):
    """Only the classes the user is booked into or waiting for — the one
    worth putting on a dashboard or driving automations from."""

    _attr_name = "My classes"
    _source_key = "my_sessions"

    def __init__(self, coordinator: ArboxCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id, "my_classes")
