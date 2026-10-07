import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

import app.sync as sync_module
from app.settings import Settings
from app.arbox_client import ArboxError
from app.store import Store
from app.sync import Syncer


@pytest.mark.asyncio
async def test_sync_tiers_keep_frequent_window_at_14_and_daily_tail_at_30(
    monkeypatch,
):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 4)

    monkeypatch.setattr(sync_module, "date", FixedDate)
    syncer = Syncer(object(), object())
    syncer._pull_range = AsyncMock(return_value=0)

    await syncer.window_sync()
    await syncer.far_range_sync()
    await syncer.sync_range(None, None)

    assert syncer._pull_range.await_args_list == [
        call("2026-09-04", "2026-09-18"),
        call("2026-09-19", "2026-10-04"),
        call("2026-09-04", "2026-10-04"),
    ]


class MultiStudioClient:
    def __init__(self):
        self.membership_calls = []

    async def profile(self):
        return {
            "full_name": "Test User",
            "users_boxes": [
                {"box_fk": 1, "locations_box_fk": 11,
                 "rolesArray": [2], "box": {"name": "Coach only"}},
                {"box_fk": 2, "locations_box_fk": 22,
                 "rolesArray": [3], "box": {"name": "Training studio"}},
            ],
        }

    async def memberships(self, box_id):
        self.membership_calls.append(box_id)
        if box_id == 1:
            return []
        return [{
            "id": 222, "active": True, "start": "2026-01-01",
            "membership_types": {"id": 9, "name": "5 בחודש"},
        }]


@pytest.mark.asyncio
async def test_identity_skips_coach_only_affiliation(tmp_path):
    store = Store(str(tmp_path / "arbox.db"))
    await store.open()
    try:
        syncer = Syncer(MultiStudioClient(), store, settings=Settings(str(tmp_path)))
        await syncer.ensure_identity()
        assert syncer.box_id == 2
        assert syncer.location_id == 22
        assert syncer.membership_user_id == 222
        studios = await store.get_meta("studios")
        assert [s["name"] for s in studios] == ["Training studio"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ignored_affiliation_is_not_queried(tmp_path):
    store = Store(str(tmp_path / "arbox.db"))
    await store.open()
    try:
        settings = Settings(str(tmp_path))
        settings.set_ignored_studios([1])
        client = MultiStudioClient()
        syncer = Syncer(client, store, settings=settings)
        studios = await syncer.discover_studios()
        assert client.membership_calls == [2]
        assert [s["id"] for s in studios] == [2]
        affiliations = await store.get_meta("studio_affiliations")
        assert affiliations[0]["ignored"] is True
        assert affiliations[0]["has_active_membership"] is None
    finally:
        await store.close()


@pytest.fixture
async def switchable_syncer(tmp_path):
    store = Store(str(tmp_path / "switch.db"))
    await store.open()
    store.active_box_id = 10
    original_member = {"id": 100, "active": True}
    await store.set_meta("identity", {"box_id": 10, "location_id": 11,
                                      "membership_user_id": 100, "studio_name": "First"})
    await store.set_meta("memberships", [original_member])
    await store.set_meta("membership", original_member)
    await store.set_meta("profile", {"studio": {"name": "First"}})
    await store.enable_studio_metadata()
    settings = Settings(str(tmp_path))
    settings.select_studio(10)
    settings.update({"preferred_membership_id": 100})
    client = SimpleNamespace(
        profile=AsyncMock(return_value={"users_boxes": [
            {"box_fk": 10, "locations_box_fk": 11, "box": {"name": "First"}},
            {"box_fk": 20, "locations_box_fk": 21, "box": {"name": "Second"}}]}),
        memberships=AsyncMock(side_effect=lambda box: [{"id": box * 10, "active": True}]))
    syncer = Syncer(client, store, settings=settings)
    syncer._apply_studio({"id": 10, "location_id": 11, "name": "First"})
    syncer.membership_user_id = 100
    syncer.memberships = [original_member]
    syncer.window_sync = AsyncMock()
    try:
        yield syncer
    finally:
        await store.close()


async def assert_original_selection(syncer, original_settings):
    assert syncer.box_id == syncer.store.active_box_id == 10
    assert syncer.location_id == 11
    assert syncer.membership_user_id == 100
    assert syncer.memberships == [{"id": 100, "active": True}]
    assert syncer.settings._active_studio_id == 10
    assert syncer.settings._data == original_settings
    assert json.loads(Path(syncer.settings._path).read_text()) == original_settings
    assert (await syncer.store.get_meta("identity"))["membership_user_id"] == 100
    syncer.window_sync.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["memberships", "profile", "cancelled", "empty"])
async def test_studio_selection_failure_preserves_panel_and_settings(switchable_syncer, failure):
    syncer = switchable_syncer
    original_settings = json.loads(Path(syncer.settings._path).read_text())
    if failure == "memberships":
        syncer._store_membership = AsyncMock(side_effect=ArboxError("membership outage"))
    elif failure == "profile":
        syncer._store_profile = AsyncMock(side_effect=ArboxError("profile outage"))
    elif failure == "cancelled":
        syncer._store_profile = AsyncMock(side_effect=asyncio.CancelledError())
    else:
        # Discovery succeeds, but the second inventory read is now empty.
        syncer.client.memberships.side_effect = [
            [{"id": 100, "active": True}], [{"id": 200, "active": True}], []]
    error = asyncio.CancelledError if failure == "cancelled" else ArboxError
    with pytest.raises(error):
        await syncer.select_studio(20, make_default=True)
    await assert_original_selection(syncer, original_settings)
    syncer.store.active_box_id = 20
    assert await syncer.store.get_meta("identity") is None
    assert await syncer.store.get_meta("membership") is None


@pytest.mark.asyncio
async def test_failed_selection_of_current_studio_does_not_clear_cached_membership(switchable_syncer):
    syncer = switchable_syncer
    original_settings = json.loads(Path(syncer.settings._path).read_text())
    syncer.client.memberships.side_effect = [
        [{"id": 100, "active": True}], [{"id": 200, "active": True}], []]
    with pytest.raises(ArboxError):
        await syncer.select_studio(10)
    await assert_original_selection(syncer, original_settings)
    assert await syncer.store.get_meta("memberships") == [{"id": 100, "active": True}]


@pytest.mark.asyncio
async def test_failed_settings_commit_rolls_back_new_studio(switchable_syncer, monkeypatch):
    syncer = switchable_syncer
    original_settings = json.loads(Path(syncer.settings._path).read_text())
    save = syncer.settings.save

    def fail_after_write():
        save()
        raise OSError("failure after replace")

    monkeypatch.setattr(syncer.settings, "save", fail_after_write)
    with pytest.raises(OSError, match="failure after replace"):
        await syncer.select_studio(20, make_default=True)
    await assert_original_selection(syncer, original_settings)


@pytest.mark.asyncio
async def test_selection_commits_only_after_valid_inventory(switchable_syncer):
    syncer = switchable_syncer
    original_profile_writer = syncer._store_profile

    async def verify_tentative(profile):
        assert syncer.box_id == 20
        assert syncer.settings._active_studio_id == 20
        saved = json.loads(Path(syncer.settings._path).read_text())
        assert saved["preferred_studio_id"] == 10
        assert saved["preferred_membership_id"] == 100
        await original_profile_writer(profile)

    syncer._store_profile = verify_tentative
    selected = await syncer.select_studio(20, make_default=True)
    assert selected["id"] == syncer.box_id == syncer.store.active_box_id == 20
    assert syncer.membership_user_id == 200
    assert syncer.settings.preferred_studio_id == 20
    assert (await syncer.store.get_meta("identity"))["membership_user_id"] == 200
    syncer.window_sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_calendar_failure_preserves_valid_committed_selection(switchable_syncer):
    syncer = switchable_syncer
    syncer.window_sync.side_effect = ArboxError("calendar outage")
    with pytest.raises(ArboxError, match="calendar outage"):
        await syncer.select_studio(20, make_default=True)
    assert syncer.box_id == syncer.store.active_box_id == 20
    assert syncer.membership_user_id == 200
    assert syncer.settings.preferred_studio_id == 20
    assert (await syncer.store.get_meta("identity"))["membership_user_id"] == 200
