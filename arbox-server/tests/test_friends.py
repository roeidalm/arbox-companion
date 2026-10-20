from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2 as httpx
import pytest
from fastapi import FastAPI

from app.api import router
from app.arbox_client import ArboxError
from app.friends import relationships, session_connections, load_friends
from app.studio_context import ReentrantAsyncLock


def profile():
    return {"id": 1, "friend_connection": [{
        "id": 100, "users_id": 2, "friend_users_id": 1, "status": 1,
        "user": {"id": 2, "full_name": "דנה", "email": "private@example.test",
                 "users_boxes_active": {"2": {"box_fk": 10}}},
    }]}


def session(**extra):
    return {"id": 42, "box_fk": 10, "locations_box_fk": 11, "registered": 2,
            "booked_users": [{"id": 2, "schedule_user_id": 200, "full_name": "דנה"},
                             {"id": 3, "schedule_user_id": 300, "full_name": "דנה"}],
            **extra}


def test_direction_is_not_misinterpreted_as_approval_and_private_fields_are_removed():
    p = profile()
    expected = [{"user_id": 2, "name": "דנה", "status": "unverified"}]
    assert relationships(p, 10) == expected
    p["friend_connection"][0].update(users_id=1, friend_users_id=2, status=0)
    assert relationships(p, 10) == expected
    assert relationships(p, 20) == []
    for value in (None, [], {}, [{"box_fk": 20}]):
        p["friend_connection"][0]["user"]["users_boxes_active"] = value
        assert relationships(p, 10) == []


def test_deleted_self_foreign_and_mismatched_relationships_are_filtered():
    for changes in ({"deleted_at": "yesterday"}, {"users_id": 3, "friend_users_id": 2},
                    {"users_id": 1, "friend_users_id": 1}, {"user": {"id": 3}}):
        p = profile(); p["friend_connection"][0].update(changes)
        assert relationships(p, 10) == []
    p = profile(); p["friend_connection"] *= 2
    assert len(relationships(p, 10)) == 1
    assert relationships({"id": 1}, 10) is None
    with pytest.raises(ArboxError):
        relationships({"id": 1, "friend_connection": {}}, 10)


def test_roster_visibility_preference_and_exact_id_matching():
    people = relationships(profile(), 10)
    assert session_connections(session(), people, 10, 11) == people
    for extra in (
        {"booked_users": [{"id": 3, "full_name": "דנה"}]},
        {"booked_users": [{"id": 2, "full_name": "דנה", "late_cancellation": 1}]},
        {"booked_users": [], "schedule_user": [{"id": 2, "full_name": "דנה"}]},
        {"booked_users": None}, {"box_fk": 20}, {"locations_box_fk": 20},
        {"disable_pages_app": [{"area": "schedule", "section_name": "whoIsBooked"}]},
        {"locations_box": {"disable_pages_app": [
            {"area": "schedule", "section_name": "whoIsBooked"}]}},
    ):
        assert session_connections(session(**extra), people, 10, 11) == []


@pytest.fixture
def app():
    app = FastAPI()
    lock = ReentrantAsyncLock()
    app.state.syncer = SimpleNamespace(box_id=10, location_id=11, exclusive=lambda: lock)
    app.state.settings = SimpleNamespace(api_key="secret", timezone="UTC")
    today = datetime.now(timezone.utc).date().isoformat()
    app.state.store = SimpleNamespace(get_session=AsyncMock(return_value={
        "schedule_id": 42, "box_id": 10, "date": today, "start_time": "18:00",
        "category_name": "Flex"}))
    owner = SimpleNamespace(profile=AsyncMock(return_value=profile()))
    app.state.client = SimpleNamespace(for_box=Mock(return_value=owner),
                                      schedule_between=AsyncMock(return_value=[session(), session()]))
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_authenticated_scoped_read_deduplicates_and_has_no_write_capabilities(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/friends")).status_code == 401
        assert (await c.get("/api/friends", headers={
            "X-Api-Key": "secret", "X-Arbox-Studio-Id": "20"})).status_code == 409
        app.state.client.for_box.assert_not_called()
        r = await c.get("/api/friends", headers={"X-Api-Key": "secret", "X-Arbox-Studio-Id": "10"})
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "no-store"
        data = r.json()
        assert data["studio_id"] == 10
        assert not any(data["capabilities"].values())
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["connections"] == data["people"]
        assert "private@example.test" not in r.text
        app.state.client.for_box.assert_called_once_with(10)
        assert (await c.post("/api/friends", headers={"X-Api-Key": "secret"})).status_code == 405


@pytest.mark.asyncio
async def test_invalid_dates_fail_before_upstream_and_errors_are_sanitized(app):
    today = datetime.now(timezone.utc).date()
    headers = {"X-Api-Key": "secret"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        for params in ({"date_from": "broken"}, {"date_from": "9999-12-31"},
                       {"date_from": str(today - timedelta(days=1))},
                       {"date_to": str(today + timedelta(days=43))}):
            assert (await c.get("/api/friends", params=params, headers=headers)).status_code == 422
        app.state.client.for_box.assert_not_called()
        app.state.client.for_box.return_value.profile.side_effect = ArboxError("private token")
        r = await c.get("/api/friends", headers=headers)
        assert r.status_code == 502 and "private token" not in r.text


@pytest.mark.asyncio
async def test_no_relationships_avoids_schedule_call_and_stale_or_foreign_cards_are_omitted(app):
    s = app.state
    start = s.store.get_session.return_value["date"]
    for extra in ({"box_id": 20}, {"date": "2000-01-01"}, {"status": "cancelled_studio"}):
        original = deepcopy(s.store.get_session.return_value)
        s.store.get_session.return_value.update(extra)
        assert not (await load_friends(s.client, s.store, studio_id=10, location_id=11,
                                      start=start, end=start))["sessions"]
        s.store.get_session.return_value = original
    s.client.for_box.return_value.profile.return_value = {"id": 1, "friend_connection": []}
    s.client.schedule_between.reset_mock()
    assert not (await load_friends(s.client, s.store, studio_id=10, location_id=11,
                                  start=start, end=start))["people"]
    s.client.schedule_between.assert_not_awaited()
