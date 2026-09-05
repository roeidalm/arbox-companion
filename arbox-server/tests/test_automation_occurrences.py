"""One-off automation exclusions must affect both planning and execution."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.rules import RulesEngine
from app.store import Store
from test_api import client, api_key


def session(sid, **patch):
    return {
        "id": sid, "date": (date.today() + timedelta(days=2)).isoformat(),
        "time": "18:00", "end_time": "19:00",
        "coach": {"first_name": "Dana"}, "box_categories": {"name": "Pilates"},
        "series": {}, "booking_option": "insertScheduleUser",
        "enable_registration_time": 0, "user_booked": None,
        "user_in_standby": None, **patch,
    }


def test_mine_projection_skip_restore_and_booked_sensor_isolation(client):
    store = client.app.state.store
    engine = client.app.state.rules_engine
    client.portal.call(store.upsert_sessions, [session(9101), session(9102), session(9103)])
    client.portal.call(store.save_rule, {"name": "All Pilates", "mode": "autobook"})
    client.portal.call(store.watch, 9103)
    headers = {"X-Api-Key": api_key(client)}

    rows = client.get("/api/me", headers=headers).json()["sessions"]
    assert {r["schedule_id"] for r in rows} == {9101, 9102, 9103}
    assert sum(r.get("planning_source") == "autobook" for r in rows) == 2
    assert client.get("/api/summary", headers=headers).json()["my_sessions"] == []
    assert client.put("/api/automations/occurrences/9101/skip").status_code == 401
    assert client.put("/api/automations/occurrences/9103/skip", headers=headers).status_code == 409

    assert client.put("/api/automations/occurrences/9101/skip", headers=headers).status_code == 200
    rows = client.get("/api/me", headers=headers).json()["sessions"]
    assert next(r for r in rows if r["schedule_id"] == 9101)["automation_skipped"] is True
    schedule = client.get("/api/schedule", headers=headers).json()["sessions"]
    skipped_row = next(r for r in schedule if r["schedule_id"] == 9101)
    assert skipped_row["automation_skipped"] is True
    assert skipped_row["autobook_match"] is False
    plans = client.portal.call(engine._planned_sessions, date.today().isoformat(), "9999-12-31")
    assert {r["schedule_id"] for r in plans} == {9102, 9103}
    assert client.delete("/api/automations/occurrences/9101/skip", headers=headers).status_code == 200
    plans = client.portal.call(engine._planned_sessions, date.today().isoformat(), "9999-12-31")
    assert {r["schedule_id"] for r in plans} == {9101, 9102, 9103}

    client.portal.call(store.upsert_sessions, [session(9101, user_booked=42)])
    assert client.put("/api/automations/occurrences/9101/skip", headers=headers).status_code == 409


@pytest.mark.asyncio
async def test_skip_survives_sync_restart_and_prevents_booking_only_one_occurrence(tmp_path):
    path = str(tmp_path / "skips.db")
    store = Store(path)
    await store.open()
    await store.upsert_sessions([session(1), session(2)], box_id=73)
    store.active_box_id = 73
    await store.save_rule({"name": "Pilates", "mode": "autobook"})
    await store.set_automation_skip(1, True)
    await store.upsert_sessions([session(1), session(2)], box_id=73)
    await store.close()

    store = Store(path)
    await store.open()
    try:
        store.active_box_id = 74
        assert await store.automation_skip_ids() == set()
        store.active_box_id = 73
        assert await store.automation_skip_ids() == {1}
        notifier = SimpleNamespace(
            settings=SimpleNamespace(is_blocked=lambda _: False),
            on_callback=None, on_message=None)
        engine = RulesEngine(store, object(), object(), notifier)
        engine._try_autobook = AsyncMock()
        await engine.autobook_tick()
        assert [call.args[0]["schedule_id"] for call in engine._try_autobook.await_args_list] == [2]
        assert not await store.autobook_attempted(1)
        await store.set_automation_skip(1, False)
        engine._try_autobook.reset_mock()
        await engine.autobook_tick()
        assert {call.args[0]["schedule_id"] for call in engine._try_autobook.await_args_list} == {1, 2}
    finally:
        await store.close()
