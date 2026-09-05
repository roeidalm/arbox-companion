from datetime import date
from unittest.mock import AsyncMock, call

import pytest

import app.sync as sync_module
from app.settings import Settings
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
