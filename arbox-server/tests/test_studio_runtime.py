import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.settings import Settings
from app.studio_runtime import StudioRuntime, StudioUnavailable
from app.sync import Syncer


class ScopedMemoryStore:
    def __init__(self):
        self.active_box_id = 10
        self.studios = [
            {"id": 10, "location_id": 11, "name": "First"},
            {"id": 20, "location_id": 21, "name": "Second"},
            {"id": 30, "location_id": 31, "name": "Ignored"},
        ]
        self.meta = {}
        for studio in self.studios:
            box_id = studio["id"]
            member = {"id": box_id * 10, "active": True}
            self.meta[box_id] = {
                "identity": {"box_id": box_id, "location_id": studio["location_id"],
                             "membership_user_id": member["id"],
                             "studio_name": studio["name"]},
                "memberships": [member], "membership": member,
                "profile": {"studio": {"name": studio["name"]}},
            }
        self.reads = []

    async def get_meta(self, key):
        self.reads.append((self.active_box_id, key))
        if key == "studios":
            return deepcopy(self.studios)
        return deepcopy(self.meta.setdefault(self.active_box_id, {}).get(key))

    async def set_meta(self, key, value):
        assert key != "studios"
        self.meta.setdefault(self.active_box_id, {})[key] = deepcopy(value)


@pytest.fixture
def runtime(tmp_path):
    store = ScopedMemoryStore()
    settings = Settings(str(tmp_path))
    settings._active_studio_id = 10
    settings.update({"preferred_membership_id": 100})
    settings._data["preferred_studio_id"] = 10
    settings._data["studio_membership_preferences"] = {"10": 100, "20": 200}
    settings.set_ignored_studios([30])
    client = SimpleNamespace(memberships=AsyncMock(), profile=AsyncMock())
    syncer = Syncer(client, store, settings=settings)
    syncer.box_id = 10
    syncer.location_id = 11
    syncer.membership_user_id = 100
    syncer.memberships = [{"id": 100, "active": True}]
    syncer.studio_name = "First"
    syncer.address = "Original address"
    syncer.studio_count = 2
    return StudioRuntime(syncer, object(), settings, store)


def selected_snapshot(runtime):
    return (runtime.syncer.box_id, runtime.syncer.location_id,
            runtime.syncer.membership_user_id, runtime.syncer.memberships,
            runtime.syncer.studio_name, runtime.syncer.address,
            runtime.store.active_box_id, runtime.settings._active_studio_id,
            runtime.settings.preferred_membership_id,
            runtime.settings.preferred_studio_id)


@pytest.mark.asyncio
async def test_all_studios_are_processed_with_their_own_memberships(runtime):
    original = selected_snapshot(runtime)
    runtime.settings.save = lambda: pytest.fail("Temporary studio switch wrote settings")
    seen = []

    async def job(label, *, suffix):
        await runtime.syncer.ensure_identity()
        identity = await runtime.store.get_meta("identity")
        seen.append((runtime.syncer.box_id, runtime.store.active_box_id,
                     runtime.settings._active_studio_id,
                     runtime.settings.preferred_membership_id,
                     identity["membership_user_id"]))
        return label + suffix

    assert await runtime.run_all(job, "a", suffix="b") == {10: "ab", 20: "ab"}
    assert seen == [(10, 10, 10, 100, 100), (20, 20, 20, 200, 200)]
    assert selected_snapshot(runtime) == original
    assert not any(box == 30 for box, key in runtime.store.reads)
    runtime.syncer.client.memberships.assert_not_awaited()
    runtime.syncer.client.profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_studio_does_not_block_others_and_selection_is_restored(runtime, caplog):
    original = selected_snapshot(runtime)
    seen = []

    async def job():
        seen.append(runtime.syncer.box_id)
        if runtime.syncer.box_id == 10:
            runtime.syncer.memberships.append({"id": 999})
            raise RuntimeError("Connection unavailable")
        return "ok"

    assert await runtime.run_all(job) == {20: "ok"}
    assert seen == [10, 20]
    assert "failed for studio 10" in caplog.text
    assert selected_snapshot(runtime) == original


@pytest.mark.asyncio
async def test_run_for_restores_context_on_exception_and_cancellation(runtime):
    original = selected_snapshot(runtime)

    async def fail(error):
        assert runtime.syncer.box_id == 20
        runtime.settings._data["preferred_membership_id"] = 201
        raise error

    with pytest.raises(ValueError, match="failed"):
        await runtime.run_for(20, fail, ValueError("failed"))
    assert selected_snapshot(runtime) == original
    with pytest.raises(asyncio.CancelledError):
        await runtime.run_for(20, fail, asyncio.CancelledError())
    assert selected_snapshot(runtime) == original


@pytest.mark.asyncio
async def test_run_for_rejects_ignored_and_unknown_studios(runtime):
    job = AsyncMock()
    for box_id in (30, 99):
        with pytest.raises(StudioUnavailable):
            await runtime.run_for(box_id, job)
    job.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_studio_bootstraps_before_running_job(runtime):
    runtime.store.meta[20] = {}
    runtime.syncer.client.memberships.return_value = [{"id": 200, "active": True}]
    runtime.syncer.client.profile.return_value = {
        "users_boxes": [{"box_fk": 20, "box": {"name": "Second"}}]}

    async def job():
        await runtime.syncer.ensure_identity()
        assert runtime.syncer.box_id == runtime.store.active_box_id == 20
        assert runtime.syncer.membership_user_id == 200
        assert (await runtime.store.get_meta("identity"))["box_id"] == 20
        assert (await runtime.store.get_meta("profile"))["studio"]["name"] == "Second"

    await runtime.run_for(20, job)
    runtime.syncer.client.memberships.assert_awaited_once_with(20)
    runtime.syncer.client.profile.assert_awaited_once()
    assert runtime.syncer.box_id == 10


@pytest.mark.asyncio
async def test_empty_membership_cannot_fall_back_to_preferred_studio(runtime):
    runtime.store.meta[20] = {}
    runtime.syncer.client.memberships.return_value = []
    job = AsyncMock()
    with pytest.raises(StudioUnavailable, match="no usable membership"):
        await runtime.run_for(20, job)
    job.assert_not_awaited()
    runtime.syncer.client.profile.assert_not_awaited()
    assert runtime.syncer.box_id == runtime.store.active_box_id == 10


@pytest.mark.asyncio
async def test_inactive_membership_cannot_enable_background_bookings(runtime):
    runtime.store.meta[20]["memberships"][0]["active"] = False
    runtime.syncer.client.memberships.return_value = [{"id": 200, "active": False}]
    job = AsyncMock()
    with pytest.raises(StudioUnavailable, match="no usable membership"):
        await runtime.run_for(20, job)
    job.assert_not_awaited()
    assert runtime.syncer.box_id == runtime.store.active_box_id == 10


@pytest.mark.asyncio
async def test_bootstrap_rejects_profile_without_target_affiliation(runtime):
    runtime.store.meta[20].pop("profile")
    runtime.syncer.client.profile.return_value = {
        "users_boxes": [{"box_fk": 10, "box": {"name": "First"}}]}
    job = AsyncMock()
    with pytest.raises(StudioUnavailable, match="absent from the account profile"):
        await runtime.run_for(20, job)
    job.assert_not_awaited()
    assert "profile" not in runtime.store.meta[20]
    assert runtime.syncer.box_id == runtime.store.active_box_id == 10


@pytest.mark.asyncio
async def test_runtime_keeps_membership_updates_without_persisting_temporary_selection(runtime):
    async def job():
        runtime.settings.update({"preferred_membership_id": runtime.syncer.box_id * 10 + 1})
        reloaded = Settings(str(Path(runtime.settings._path).parent))
        assert reloaded.preferred_studio_id == 10
        assert reloaded.preferred_membership_id == 101

    assert await runtime.run_all(job) == {10: None, 20: None}
    assert runtime.settings._active_studio_id == 10
    assert runtime.settings.preferred_membership_id == 101
    assert runtime.settings._data["studio_membership_preferences"] == {"10": 101, "20": 201}


@pytest.mark.asyncio
async def test_runtime_restoration_uses_updated_selected_membership(runtime):
    runtime.syncer.client.memberships.return_value = [{"id": 101, "active": True}]

    async def refresh():
        await runtime.syncer._store_membership()
        runtime.settings.update({"preferred_membership_id": 101})

    await runtime.run_for(10, refresh)
    assert runtime.syncer.box_id == runtime.store.active_box_id == 10
    assert runtime.syncer.membership_user_id == runtime.settings.preferred_membership_id == 101
    assert [row["id"] for row in runtime.syncer.memberships] == [101]
    assert (await runtime.store.get_meta("identity"))["membership_user_id"] == 101


@pytest.mark.asyncio
async def test_callback_default_change_wins_over_old_identity(runtime):
    alternate = {"id": 101, "active": True}
    runtime.store.meta[10]["memberships"].append(alternate)

    async def select_membership():
        # Matches membership_select: older callback does not update identity.
        runtime.settings.update({"preferred_membership_id": 101})
        runtime.syncer.membership_user_id = 101
        await runtime.store.set_meta("membership", alternate)
        assert (await runtime.store.get_meta("identity"))["membership_user_id"] == 100

    await runtime.run_for(10, select_membership)
    assert runtime.syncer.membership_user_id == runtime.settings.preferred_membership_id == 101
    assert (await runtime.store.get_meta("identity"))["membership_user_id"] == 101


@pytest.mark.asyncio
async def test_background_activation_uses_new_default_despite_old_identity(runtime):
    alternate = {"id": 201, "active": True}
    runtime.store.meta[20]["memberships"].append(alternate)
    runtime.store.meta[20]["membership"] = alternate
    runtime.settings._data["studio_membership_preferences"]["20"] = 201

    async def check():
        assert runtime.syncer.membership_user_id == 201
        assert (await runtime.store.get_meta("identity"))["membership_user_id"] == 201

    await runtime.run_for(20, check)
    runtime.syncer.client.memberships.assert_not_awaited()
    assert runtime.syncer.membership_user_id == 100


@pytest.mark.asyncio
async def test_runtime_does_not_restore_removed_membership_after_later_failure(runtime):
    runtime.syncer.client.memberships.return_value = []

    async def refresh_then_fail():
        await runtime.syncer._store_membership()
        raise RuntimeError("later profile error")

    with pytest.raises(RuntimeError, match="later profile error"):
        await runtime.run_for(10, refresh_then_fail)
    assert runtime.syncer.box_id == runtime.store.active_box_id == 10
    assert runtime.syncer.membership_user_id is None
    assert runtime.syncer.memberships == []


@pytest.mark.asyncio
async def test_runtime_pins_studio_against_internal_refresh_fallback(runtime):
    async def job():
        runtime.syncer._apply_studio({"id": 10})

    with pytest.raises(HTTPException) as error:
        await runtime.run_for(20, job)
    assert error.value.status_code == 409
    assert runtime.syncer.box_id == runtime.store.active_box_id == 10


@pytest.mark.asyncio
async def test_background_work_holds_lock_until_selected_context_restored(runtime):
    entered, release = asyncio.Event(), asyncio.Event()
    observed = []

    async def background():
        assert runtime.syncer.box_id == 20
        entered.set()
        await release.wait()

    async def panel_read():
        async with runtime.syncer.exclusive():
            observed.append(runtime.syncer.box_id)

    task = asyncio.create_task(runtime.run_for(20, background))
    await entered.wait()
    reader = asyncio.create_task(panel_read())
    await asyncio.sleep(0)
    assert observed == []
    release.set()
    await task
    await reader
    assert observed == [10]


@pytest.mark.asyncio
async def test_mismatched_cached_identity_fails_closed(runtime):
    runtime.store.meta[20]["identity"]["box_id"] = 10
    job = AsyncMock()
    with pytest.raises(StudioUnavailable, match="does not match"):
        await runtime.run_for(20, job)
    job.assert_not_awaited()
    assert runtime.syncer.box_id == runtime.store.active_box_id == 10
