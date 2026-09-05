from datetime import date, datetime, timedelta
import sqlite3

import pytest

from app.store import Store, flatten_session


def raw_session(schedule_id: int, day: str, **patch):
    session = {
        "id": schedule_id,
        "date": day,
        "time": "18:00",
        "end_time": "19:00",
        "coach": {"id": 7, "first_name": "Dana", "last_name": None},
        "box_categories": {"id": 4, "name": "Pilates"},
        "series": {"id": 2, "block_registration_time": 1},
        "enable_registration_time": 168,
        "disable_cancellation_time": 12,
        "max_users": 12,
        "free": 3,
        "registered": 9,
        "stand_by": 0,
        "booking_option": "insertScheduleUser",
        "user_booked": None,
        "user_in_standby": None,
        "stand_by_position": None,
    }
    session.update(patch)
    return session


def test_flatten_session_preserves_zero_and_cleans_coach_name():
    row = flatten_session(raw_session(1, "2026-09-07", enable_registration_time=0))
    assert row["advance_hours"] == 0
    assert row["registration_opens"] is None
    assert row["coach_name"] == "Dana"
    assert row["cancel_hours"] == 12


def test_flatten_session_extracts_membership_from_own_booking():
    row = flatten_session(raw_session(
        2, "2026-09-07", user_booked=900,
        schedule_user=[
            {"schedule_user_id": 899, "membership_user_fk": 111},
            {"schedule_user_id": 900, "membership_user_fk": 222},
        ]))
    assert row["membership_user_id"] == 222


@pytest.fixture
async def store(tmp_path):
    value = Store(str(tmp_path / "arbox.db"))
    await value.open()
    try:
        yield value
    finally:
        await value.close()


async def test_first_seen_survives_subsequent_syncs(store):
    item = raw_session(1, "2026-09-07")
    await store.upsert_sessions([item])
    await store.db.execute(
        "UPDATE sessions SET first_seen = '2026-01-01 00:00:00' WHERE schedule_id = 1"
    )
    await store.db.commit()

    await store.upsert_sessions([{**item, "free": 2}])
    row = await store.get_session(1)
    assert row["first_seen"] == "2026-01-01 00:00:00"
    assert row["free"] == 2


async def test_open_adds_stable_exercise_id_to_existing_database(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE workout_exercises ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id INTEGER NOT NULL, "
            "box_id INTEGER, position INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL, "
            "metric_type TEXT NOT NULL DEFAULT 'note', sets INTEGER, reps INTEGER, "
            "weight REAL, weight_unit TEXT, duration_seconds INTEGER, attempts INTEGER, "
            "distance REAL, distance_unit TEXT, notes TEXT, created_at TEXT, updated_at TEXT)"
        )
    value = Store(str(path))
    await value.open()
    try:
        cursor = await value.db.execute("PRAGMA table_info(workout_exercises)")
        assert "exercise_id" in {row[1] for row in await cursor.fetchall()}
    finally:
        await value.close()


async def test_sync_without_membership_does_not_erase_known_assignment(store):
    item = raw_session(30, "2026-09-07", user_booked=900,
                       _selected_membership_id=222)
    await store.upsert_sessions([item])
    item.pop("_selected_membership_id")
    await store.upsert_sessions([item])
    assert (await store.get_session(30))["membership_user_id"] == 222


async def test_active_studio_scopes_schedule_and_rules(store):
    store.active_box_id = 10
    await store.upsert_sessions([raw_session(40, "2026-09-07")], box_id=10)
    await store.save_rule({"name": "Studio A", "mode": "autobook"})
    store.active_box_id = 20
    await store.upsert_sessions([raw_session(41, "2026-09-07")], box_id=20)
    await store.save_rule({"name": "Studio B", "mode": "autobook"})

    assert [s["schedule_id"] for s in await store.get_sessions()] == [41]
    assert [r["name"] for r in await store.list_rules()] == ["Studio B"]
    store.active_box_id = 10
    assert [s["schedule_id"] for s in await store.get_sessions()] == [40]
    assert [r["name"] for r in await store.list_rules()] == ["Studio A"]


async def test_quota_commitments_separate_used_reserved_and_standby(store):
    await store.upsert_sessions([
        raw_session(31, "2026-09-01", time="08:00", user_booked=1,
                    _selected_membership_id=100),
        raw_session(32, "2026-09-03", time="08:00", user_booked=2,
                    _selected_membership_id=100),
        raw_session(33, "2026-09-03", time="09:00", user_in_standby=3),
        raw_session(34, "2026-09-04", time="09:00", user_booked=None,
                    _selected_membership_id=100),
    ])
    await store.set_training_outcome(
        34, "cancelled_late", "cancellation", counts_entry=True)
    rows = await store.quota_commitments(
        "2026-09-01", "2026-09-30", "2026-09-02 12:00")
    assert sorted(r["commitment"] for r in rows) == [
        "reserved", "standby", "used", "used"]


async def test_future_prune_preserves_booked_standby_and_pinned_classes(store):
    far = (date.today() + timedelta(days=60)).isoformat()
    await store.upsert_sessions([
        raw_session(1, far),
        raw_session(2, far, user_booked=22),
        raw_session(3, far, user_in_standby=33),
        raw_session(4, far),
    ])
    await store.watch(4)

    counts = await store.prune_sessions(attended_days=365, past_days=30, future_days=30)
    ids = {row["schedule_id"] for row in await store.get_sessions()}

    assert counts["future"] == 1
    assert ids == {2, 3, 4}


async def test_retry_counter_can_be_cleared(store):
    assert await store.bump_retry(10, "autobook") == 1
    assert await store.bump_retry(10, "autobook") == 2
    await store.clear_retry(10, "autobook")
    assert await store.bump_retry(10, "autobook") == 1


async def test_completed_class_enters_history_before_midnight(store):
    day = "2026-09-01"
    await store.set_meta("attendance_tracking_since", day)
    await store.upsert_sessions([
        raw_session(7, day, time="20:15", end_time="21:30",
                    user_booked=183699898, booking_option="past")
    ])

    rows = await store.training_history(datetime(2026, 9, 1, 22, 0))

    assert len(rows) == 1
    assert rows[0]["schedule_id"] == 7
    assert rows[0]["status"] == "pending"


async def test_explicit_outcome_survives_arbox_clearing_booking(store):
    day = "2026-09-01"
    booked = raw_session(8, day, user_booked=44,
                         booking_option="cancelScheduleUser")
    await store.upsert_sessions([booked])
    await store.set_training_outcome(
        8, "cancelled_late", "cancellation", "work")
    await store.upsert_sessions([
        {**booked, "user_booked": None, "booking_option": "insertScheduleUser"}
    ])
    # A studio can remove a cancelled class from the schedule entirely; the
    # local outcome must retain enough of the class to remain meaningful.
    await store.db.execute("DELETE FROM sessions WHERE schedule_id=8")
    await store.db.commit()

    rows = await store.training_history(datetime(2026, 8, 31, 12, 0))

    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled_late"
    assert rows[0]["reason_code"] == "work"
    assert rows[0]["category_name"] == "Pilates"


async def test_rebooking_clears_current_cancellation_but_keeps_timeline(store):
    day = (date.today() + timedelta(days=2)).isoformat()
    original = raw_session(
        9, day, user_booked=101, booking_option="cancelScheduleUser")
    await store.upsert_sessions([original])
    await store.set_training_outcome(
        9, "cancelled_safe", "cancellation", "work",
        booking_id=101, counts_entry=False,
        deadline_at=f"{day}T06:00",
    )
    await store.upsert_sessions([{
        **original, "user_booked": 202,
        "booking_option": "cancelScheduleUser",
    }])

    event_type = await store.record_booking_success(9, "book", "manual")

    assert event_type == "rebooked"
    assert await store.get_training_outcome(9) is None
    events = await store.training_events()
    assert [e["event_type"] for e in events] == ["cancelled_safe", "rebooked"]
    assert [e["booking_id"] for e in events] == [101, 202]
    assert events[0]["counts_entry"] == 0
    assert events[0]["reason_code"] == "work"
    assert events[0]["deadline_at"] == f"{day}T06:00"


async def test_attendance_correction_is_an_append_only_timeline(store):
    day = (date.today() - timedelta(days=1)).isoformat()
    await store.upsert_sessions([raw_session(10, day, user_booked=303)])

    await store.set_training_outcome(10, "missed", "manual", "work")
    await store.set_training_outcome(10, "attended", "manual")

    outcome = await store.get_training_outcome(10)
    assert outcome["status"] == "attended"
    assert outcome["reason_code"] is None
    events = await store.training_events()
    assert [e["event_type"] for e in events] == ["missed", "attended"]
    assert events[0]["reason_code"] == "work"


async def test_attendance_stats_exclude_missed_and_cancelled(store):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    await store.set_meta("attendance_tracking_since", yesterday)
    await store.upsert_sessions([
        raw_session(11, yesterday, user_booked=1),
        raw_session(12, yesterday, user_booked=2),
        raw_session(13, yesterday, user_booked=None),
    ])
    await store.set_training_outcome(11, "attended", "manual")
    await store.set_training_outcome(12, "missed", "manual", "work")
    await store.set_training_outcome(13, "cancelled_safe", "cancellation", "none")

    stats = await store.attendance_stats()

    assert stats["total"] == 1


async def test_attendance_stats_include_explicit_arrival_today(store):
    today = date.today().isoformat()
    await store.set_meta("attendance_tracking_since", today)
    await store.upsert_sessions([
        raw_session(21, today, user_booked=1, time="08:00", end_time="09:00")
    ])
    await store.set_training_outcome(21, "attended", "manual")

    assert (await store.attendance_stats())["total"] == 1
