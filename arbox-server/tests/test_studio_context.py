import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from fastapi import FastAPI, Request

from app.api import router
from app.studio_context import ReentrantAsyncLock, StudioContextRoute
from app.sync import Syncer


@pytest.fixture
def panel_app():
    app = FastAPI()
    store = SimpleNamespace(active_box_id=10, get_meta=AsyncMock(return_value=None))
    syncer = Syncer(object(), store)
    syncer.box_id = 10
    syncer.studio_name = "Current studio"
    app.state.syncer = syncer
    app.state.store = store
    app.state.settings = SimpleNamespace(api_key="secret", timezone="Asia/Jerusalem")
    app.include_router(router)
    app.router.route_class = StudioContextRoute
    return app


@pytest.mark.asyncio
async def test_handshake_and_expected_studio_require_auth(panel_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel_app), base_url="http://test") as client:
        assert (await client.get("/api/panel/context")).status_code == 401
        headers = {"X-Api-Key": "secret"}
        result = await client.get("/api/panel/context", headers=headers)
        assert result.json()["studio_id"] == 10
        assert result.json()["capabilities"]["studio_guard"] is True
        for value in ("null", "", "0", "-1", "1.0"):
            assert (await client.get("/api/panel/context", headers={**headers, "X-Arbox-Studio-Id": value})).status_code == 422
        assert (await client.get("/api/panel/context", headers={"X-Arbox-Studio-Id": "10"})).status_code == 401
        result = await client.get("/api/panel/context", headers={**headers, "X-Arbox-Studio-Id": "11"})
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "studio_changed"
        panel_app.state.syncer.box_id = None
        assert (await client.get("/api/panel/context", headers=headers)).json()["studio_id"] is None
        result = await client.get("/api/panel/context", headers={**headers, "X-Arbox-Studio-Id": "10"})
        assert result.json()["detail"]["code"] == "studio_unavailable"


@pytest.mark.asyncio
async def test_action_waits_for_tick_and_blocks_studio_switch(panel_app):
    syncer = panel_app.state.syncer
    tick_entered, release_tick = asyncio.Event(), asyncio.Event()
    action_entered, release_action = asyncio.Event(), asyncio.Event()
    effects = []

    @panel_app.post("/test-action")
    async def action(request: Request):
        # Same lock nesting as an API invoking a tick and membership action.
        async with syncer.exclusive():
            async with syncer.exclusive():
                action_entered.set()
                await release_action.wait()
                effects.append(syncer.box_id)
        return {"ok": True}

    async def tick():
        async with syncer.exclusive():
            tick_entered.set()
            await release_tick.wait()

    # Exercise the actual legacy (headerless) selection path, including its
    # outer lock, rather than merely assigning the active studio in the test.
    syncer.client = SimpleNamespace(profile=AsyncMock(return_value={}))
    syncer.discover_studios = AsyncMock(return_value=[{"id": 20}])
    syncer._store_profile = AsyncMock()
    syncer._store_membership = AsyncMock(return_value={"id": 200})
    syncer.store.set_meta = AsyncMock()
    syncer.window_sync = AsyncMock()

    async def switch():
        await syncer.select_studio(20)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel_app), base_url="http://test") as client:
        tick_task = asyncio.create_task(tick())
        await tick_entered.wait()
        task = asyncio.create_task(client.post("/test-action", headers={"X-Api-Key": "secret", "X-Arbox-Studio-Id": "10"}))
        await asyncio.sleep(0)
        assert not action_entered.is_set()
        release_tick.set()
        await tick_task
        await asyncio.wait_for(action_entered.wait(), 1)
        switch_task = asyncio.create_task(switch())
        await asyncio.sleep(0)
        assert syncer.box_id == 10
        release_action.set()
        assert (await asyncio.wait_for(task, 1)).status_code == 200
        await asyncio.wait_for(switch_task, 1)
        assert effects == [10]
        stale = await client.post("/test-action", headers={"X-Api-Key": "secret", "X-Arbox-Studio-Id": "10"})
        assert stale.status_code == 409
        assert effects == [10]


@pytest.mark.asyncio
async def test_same_task_identity_fallback_cannot_mutate_other_studio(panel_app):
    effects = []

    @panel_app.post("/test-fallback")
    async def fallback():
        panel_app.state.syncer._apply_studio({"id": 20})
        effects.append(20)
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel_app), base_url="http://test") as client:
        guarded = await client.post("/test-fallback", headers={"X-Api-Key": "secret", "X-Arbox-Studio-Id": "10"})
        assert guarded.status_code == 409
        assert panel_app.state.syncer.box_id == 10
        assert effects == []
        # Legacy callers remain free to change studio normally.
        assert (await client.post("/test-fallback")).status_code == 200
        assert effects == [20]


@pytest.mark.asyncio
async def test_reentrant_lock_does_not_give_child_task_parent_ownership():
    lock = ReentrantAsyncLock()
    entered = asyncio.Event()

    async def child():
        async with lock:
            entered.set()

    async with lock:
        async with lock:
            task = asyncio.create_task(child())
            await asyncio.sleep(0)
            assert not entered.is_set()
    await asyncio.wait_for(task, 1)
    assert entered.is_set()
    # Cancelled waiters must not strand the lock.
    async with lock:
        waiting = asyncio.create_task(child())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    async with lock:
        pass


@pytest.mark.asyncio
async def test_real_tick_membership_and_sync_entrypoints_share_serialization(panel_app):
    from app.rules import RulesEngine

    syncer = panel_app.state.syncer
    notifier = SimpleNamespace(settings=panel_app.state.settings)
    engine = RulesEngine(panel_app.state.store, object(), syncer, notifier)
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    async def membership_work(*args):
        # The production membership path refreshes identity under sync lock.
        async with syncer.exclusive():
            events.append("membership:start")
            entered.set()
            await release.wait()
            events.append("membership:end")
        return {}, 42

    async def watched():
        await engine.perform_membership_action({}, "book")

    async def pull(*args):
        events.append("sync")
        return 0

    engine._watchlist_tick = watched
    engine._perform_membership_action = membership_work
    syncer._pull_range = pull
    tick = asyncio.create_task(engine.watchlist_tick())
    await asyncio.wait_for(entered.wait(), 1)
    syncing = asyncio.create_task(syncer.near_term_sync())
    await asyncio.sleep(0)
    assert events == ["membership:start"]
    release.set()
    await asyncio.wait_for(asyncio.gather(tick, syncing), 1)
    assert events == ["membership:start", "membership:end", "sync"]
