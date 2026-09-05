"""iCalendar feed of the user's own classes.

A subscribable .ics is the one calendar integration that needs no per-booking
action: the phone re-fetches it, so a booking made anywhere shows up and a
cancellation disappears on its own. Hand-rolled because the format is small
and this keeps the image dependency-free — the fiddly parts (escaping, 75-
octet folding that must not split a UTF-8 character) are handled below.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

PRODID = "-//arbox-server//Arbox classes//HE"


def _esc(text: str) -> str:
    """RFC 5545 TEXT escaping."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\r\n", r"\n")
        .replace("\n", r"\n")
    )


def _fold(line: str) -> str:
    """Fold to <=75 octets per line without splitting a multi-byte char."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, start = [], 0
    limit = 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        # back off until we land on a UTF-8 boundary
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        out.append(raw[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines carry a leading space
    return "\r\n ".join(out)


def _utc(date_str: str, time_str: str, tz: str) -> str | None:
    try:
        naive = datetime.fromisoformat(f"{date_str}T{time_str}")
    except (TypeError, ValueError):
        return None
    aware = naive.replace(tzinfo=ZoneInfo(tz))
    return aware.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def google_calendar_url(
    session: dict, *, tz: str = "Asia/Jerusalem", location: str = ""
) -> str | None:
    """Google's "create event" link — opens a prefilled event in the browser.

    The .ics file is the right answer on a phone (the OS offers a calendar
    picker), but in a desktop browser it only downloads. This link skips the
    file entirely for anyone living in Google Calendar.
    """
    start = _utc(session.get("date"), session.get("start_time"), tz)
    if not start:
        return None
    end = _utc(session.get("date"), session.get("end_time"), tz) if session.get("end_time") else None
    if not end or end <= start:
        end = (
            datetime.strptime(start, "%Y%m%dT%H%M%SZ") + timedelta(hours=1)
        ).strftime("%Y%m%dT%H%M%SZ")

    title = session.get("category_name") or "שיעור"
    if session.get("coach_name"):
        title += f" · {session['coach_name']}"
    if session.get("user_in_standby") is not None:
        title = f"⏳ המתנה — {title}"

    details = (session.get("category_bio") or "").strip()
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{start}/{end}",
    }
    if details:
        params["details"] = details
    if location:
        params["location"] = location
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def build_calendar(
    sessions: list[dict],
    *,
    tz: str = "Asia/Jerusalem",
    name: str = "Arbox — האימונים שלי",
    location: str = "",
    alarms: list[int] | None = None,
    stamp: datetime | None = None,
) -> str:
    """Render booked/standby sessions as a subscribable VCALENDAR."""
    now = (stamp or datetime.now(ZoneInfo("UTC"))).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_esc(name)}",
        f"X-WR-TIMEZONE:{tz}",
        # hints so subscribers re-poll often enough to catch a cancellation
        "REFRESH-INTERVAL;VALUE=DURATION:PT30M",
        "X-PUBLISHED-TTL:PT30M",
    ]

    for s in sessions:
        start = _utc(s.get("date"), s.get("start_time"), tz)
        if not start:
            continue
        end = _utc(s.get("date"), s.get("end_time"), tz) if s.get("end_time") else None
        if not end or end <= start:
            end = (
                datetime.strptime(start, "%Y%m%dT%H%M%SZ") + timedelta(hours=1)
            ).strftime("%Y%m%dT%H%M%SZ")

        standby = s.get("user_in_standby") is not None
        title = s.get("category_name") or "שיעור"
        if s.get("coach_name"):
            title += f" · {s['coach_name']}"
        if standby:
            pos = s.get("stand_by_position")
            title = f"⏳ המתנה{f' ({pos})' if pos else ''} — {title}"

        desc_parts = []
        if s.get("category_bio"):
            desc_parts.append(str(s["category_bio"]).strip())
        if standby:
            desc_parts.append(
                "את/ה ברשימת ההמתנה — המקום עדיין לא מובטח."
            )
        desc_parts.append(
            f"{s.get('registered', '?')}/{s.get('max_users', '?')} רשומים"
        )

        lines += [
            "BEGIN:VEVENT",
            # stable per class, so re-fetches update instead of duplicating
            f"UID:arbox-{s.get('schedule_id')}@arbox-server",
            f"DTSTAMP:{now}",
            f"DTSTART:{start}",
            f"DTEND:{end}",
            f"SUMMARY:{_esc(title)}",
            f"DESCRIPTION:{_esc(chr(10).join(desc_parts))}",
            "STATUS:" + ("TENTATIVE" if standby else "CONFIRMED"),
            "TRANSP:OPAQUE",
        ]
        if location:
            lines.append(f"LOCATION:{_esc(location)}")
        if not standby:
            # one VALARM per requested lead time, de-duplicated and ordered
            for minutes in sorted({int(m) for m in (alarms or []) if int(m) > 0},
                                  reverse=True):
                lines += [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    f"TRIGGER:-PT{minutes}M",
                    f"DESCRIPTION:{_esc(title)}",
                    "END:VALARM",
                ]
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"
