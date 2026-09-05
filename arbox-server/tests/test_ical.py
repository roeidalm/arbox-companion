from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from app.ical import _fold, build_calendar, google_calendar_url


def session(**patch):
    return {
        "schedule_id": 42,
        "date": "2026-09-07",
        "start_time": "18:00",
        "end_time": "19:15",
        "category_name": "יוגה, כוח; וגמישות",
        "coach_name": "Dana",
        "category_bio": "Line one\nLine two",
        "registered": 8,
        "max_users": 12,
        **patch,
    }


def test_fold_respects_utf8_octet_limit():
    folded = _fold("SUMMARY:" + "שיעור ארוך מאוד " * 12)
    lines = folded.split("\r\n")

    assert all(len(line.encode("utf-8")) <= 75 for line in lines)
    assert all(line.startswith(" ") for line in lines[1:])


def test_calendar_escapes_text_and_deduplicates_alarms():
    calendar = build_calendar(
        [session()],
        alarms=[60, 15, 60],
        location="Main; Hall",
        stamp=datetime(2026, 9, 1, tzinfo=ZoneInfo("UTC")),
    )

    assert "UID:arbox-42@arbox-server" in calendar
    assert "SUMMARY:יוגה\\, כוח\\; וגמישות · Dana" in calendar
    assert "LOCATION:Main\\; Hall" in calendar
    assert calendar.count("BEGIN:VALARM") == 2
    assert calendar.endswith("END:VCALENDAR\r\n")


def test_standby_event_is_tentative_and_has_no_alarm():
    calendar = build_calendar([session(user_in_standby=9, stand_by_position=2)], alarms=[60])

    assert "STATUS:TENTATIVE" in calendar
    assert "BEGIN:VALARM" not in calendar
    assert "המתנה (2)" in calendar


def test_google_url_uses_utc_and_defaults_invalid_end_to_one_hour():
    url = google_calendar_url(session(end_time="17:00"), location="Studio")
    query = parse_qs(urlsplit(url).query)

    assert query["dates"] == ["20260907T150000Z/20260907T160000Z"]
    assert query["location"] == ["Studio"]
    assert query["text"] == ["יוגה, כוח; וגמישות · Dana"]
