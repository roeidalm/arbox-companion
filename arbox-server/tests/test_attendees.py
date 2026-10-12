from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from fastapi import FastAPI

from app.api import router
from app.arbox_client import ArboxError
from app.attendees import roster
from app.studio_context import ReentrantAsyncLock


def session(**extra):
    return {"id": 42, "box_fk": 10, "locations_box_fk": 11, "registered": 2,
            "booked_users": [
                {"id": 1, "schedule_user_id": 101, "full_name": "דנה לוי", "email": "private@example.test"},
                {"id": 2, "schedule_user_id": 102, "first_name": "יובל", "last_name": None},
            ], **extra}


def test_roster_only_returns_names_and_preserves_distinct_people_with_same_name():
    data = session()
    data["booked_users"] += [data["booked_users"][0],
                              {"id": 3, "schedule_user_id": 103, "full_name": "דנה לוי"},
                              {"id": 4, "full_name": "Cancelled", "late_cancellation": 1}]
    assert roster(data) == {"status": "available", "registered": 2,
                            "attendees": [{"name": "דנה לוי"}, {"name": "יובל"}, {"name": "דנה לוי"}]}


@pytest.mark.parametrize("nested", [False, True])
def test_hidden_names_are_never_exposed_even_when_upstream_returns_them(nested):
    flags = {"disable_pages_app": [{"area": "schedule", "section_name": "whoIsBooked"}]}
    data = session(**({"locations_box": flags} if nested else flags))
    assert roster(data) == {"status": "hidden", "registered": 2, "attendees": []}


def test_empty_missing_and_partially_visible_rosters_are_distinct():
    assert roster(session(booked_users=[], registered=0))["status"] == "available"
    assert roster(session(booked_users=[]))["status"] == "unavailable"
    assert roster(session(booked_users=None))["status"] == "unavailable"
    assert roster(session(booked_users=[{"id": 1}]))["status"] == "unavailable"
    data = session(); data["schedule_user"] = data.pop("booked_users")
    assert len(roster(data)["attendees"]) == 2
    data["booked_users"] = []
    assert roster(data)["status"] == "unavailable"  # don't bypass a withheld preferred list
    with pytest.raises(ArboxError):
        roster(session(booked_users={"unexpected": "shape"}))


@pytest.fixture
def app():
    app = FastAPI()
    lock = ReentrantAsyncLock()
    app.state.syncer = SimpleNamespace(box_id=10, location_id=11, exclusive=lambda: lock)
    app.state.settings = SimpleNamespace(api_key="secret")
    app.state.store = SimpleNamespace(get_session=AsyncMock(return_value={
        "schedule_id": 42, "box_id": 10, "date": "2026-10-12"}))
    app.state.client = SimpleNamespace(schedule_between=AsyncMock(return_value=[session()]))
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_endpoint_requires_auth_and_current_studio_before_upstream(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/schedule/42/attendees")).status_code == 401
        headers = {"X-Api-Key": "secret", "X-Arbox-Studio-Id": "20"}
        assert (await c.get("/api/schedule/42/attendees", headers=headers)).status_code == 409
        app.state.client.schedule_between.assert_not_awaited()
        headers["X-Arbox-Studio-Id"] = "10"
        result = await c.get("/api/schedule/42/attendees", headers=headers)
        assert result.status_code == 200
        assert result.headers["Cache-Control"] == "no-store"
        assert result.json() == {"studio_id": 10, "schedule_id": 42, **roster(session())}
        app.state.client.schedule_between.assert_awaited_once_with(10, 11, "2026-10-12", "2026-10-12")
        # Opening again fetches again; this route never writes a cache or runs a sync.
        await c.get("/api/schedule/42/attendees", headers=headers)
        assert app.state.client.schedule_between.await_count == 2


@pytest.mark.asyncio
async def test_gone_or_wrong_studio_sessions_do_not_leak_a_roster(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        headers = {"X-Api-Key": "secret"}
        app.state.store.get_session.return_value["box_id"] = 20
        assert (await c.get("/api/schedule/42/attendees", headers=headers)).status_code == 404
        app.state.client.schedule_between.assert_not_awaited()
        app.state.store.get_session.return_value["box_id"] = 10
        for upstream in [[], [session(id=99)]]:
            app.state.client.schedule_between.return_value = upstream
            assert (await c.get("/api/schedule/42/attendees", headers=headers)).status_code == 404
        for upstream in [[session(box_fk=20)], [session(locations_box_fk=21)], {"bad": "shape"}]:
            app.state.client.schedule_between.return_value = upstream
            assert (await c.get("/api/schedule/42/attendees", headers=headers)).status_code == 502
        app.state.client.schedule_between.side_effect = ArboxError("private upstream detail", status=503)
        result = await c.get("/api/schedule/42/attendees", headers=headers)
        assert result.status_code == 502 and "private upstream detail" not in result.text
