import asyncio
import json
from contextvars import ContextVar

import pytest

from app.arbox_client import ArboxClient, ArboxError
from app.connections import ConnectionError, Connections


def affiliation(box_id, name):
    return {"box_fk": box_id, "locations_box_fk": box_id + 100,
            "box": {"id": box_id, "name": name}}


class FakeClient(ArboxClient):
    profiles = {}
    accounts = {}
    failures = set()
    inactive = set()

    def __init__(self, data_dir, whitelabel="Arbox"):
        super().__init__(data_dir, whitelabel)
        self.calls = []

    async def login(self, email=None, password=None, **kwargs):
        self.email = email or self.email
        self._password = password or self._password
        self.user_id = self.accounts.get(self.whitelabel, 123)
        self._access_token = "secret-" + self.whitelabel
        self._save_creds()
        return {"id": self.user_id}

    async def profile(self):
        self.calls.append("profile")
        if self.whitelabel in self.failures:
            raise ArboxError("secret-token-and-password")
        return {"id": self.user_id, "full_name": "Person", "token": "private",
                "users_boxes": self.profiles.get(self.whitelabel, [])}

    async def memberships(self, box_id):
        self.calls.append(("memberships", box_id))
        if self.whitelabel in self.failures:
            raise ArboxError("secret")
        return [{"id": box_id * 10, "active": box_id not in self.inactive}]

    async def schedule_between(self, *args):
        return [{"brand": self.whitelabel, "box": args[0]}]

    async def book(self, *args):
        await asyncio.sleep(0)
        return {"brand": self.whitelabel}


@pytest.fixture
async def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(FakeClient, "profiles", {
        "Moveom": [affiliation(1, "Moveom")],
        "Arbox": [affiliation(2, "Rashty")],
    })
    monkeypatch.setattr(FakeClient, "accounts", {})
    monkeypatch.setattr(FakeClient, "failures", set())
    monkeypatch.setattr(FakeClient, "inactive", set())
    legacy = FakeClient(str(tmp_path), "Moveom")
    await legacy.login("person@example.test", "top-secret")
    manager = Connections(str(tmp_path), legacy, FakeClient)
    yield manager
    await manager.close()


def brand_entry(public, whitelabel="Arbox"):
    return next(c for c in public["connections"] if c["whitelabel"] == whitelabel)


async def test_discovery_isolated_and_explicit_enable_survives_restart(registry, tmp_path):
    original_credentials = (tmp_path / "credentials.json").read_bytes()
    public = await registry.discover()
    arbox = brand_entry(public)
    assert arbox["status"] == "connected"
    assert arbox["studios"][0]["enabled"] is False
    assert [b["box_fk"] for b in (await registry.profile())["users_boxes"]] == [1]
    registry.enable(arbox["id"], 2)
    assert [b["box_fk"] for b in (await registry.profile())["users_boxes"]] == [1, 2]
    assert (tmp_path / "credentials.json").read_bytes() == original_credentials
    assert (tmp_path / "connections.json").stat().st_mode & 0o777 == 0o600
    extra_credentials = tmp_path / "connections" / arbox["id"] / "credentials.json"
    assert extra_credentials.stat().st_mode & 0o777 == 0o600
    assert extra_credentials.parent.stat().st_mode & 0o777 == 0o700
    loaded = Connections(str(tmp_path), registry.legacy, FakeClient)
    assert loaded.for_box(1).whitelabel == "Moveom"
    assert loaded.for_box(2).whitelabel == "Arbox"
    assert await loaded.schedule_between(2, 102, "2026-10-07", "2026-10-08") == [
        {"brand": "Arbox", "box": 2}]
    assert "secret" not in json.dumps(public)
    assert "token" not in (tmp_path / "connections.json").read_text()
    await loaded.close()


async def test_mismatched_account_never_merged_or_enabled(registry, tmp_path):
    FakeClient.accounts["Arbox"] = 987
    public = await registry.discover()
    arbox = brand_entry(public)
    assert arbox["status"] == "error"
    assert arbox["studios"] == []
    assert "secret" not in json.dumps(public)
    with pytest.raises(ConnectionError):
        registry.enable(arbox["id"], 2)
    assert [b["box_fk"] for b in (await registry.profile())["users_boxes"]] == [1]
    assert not (tmp_path / "connections" / arbox["id"] / "credentials.json").exists()


async def test_duplicates_keep_legacy_owner(registry):
    FakeClient.profiles["Arbox"] = [affiliation(1, "Duplicate"), affiliation(2, "Rashty")]
    arbox = brand_entry(await registry.discover())
    assert arbox["studios"][0]["duplicate"] is True
    registry.enable(arbox["id"], 1)
    registry.enable(arbox["id"], 2)
    profile = await registry.profile()
    assert [b["box_fk"] for b in profile["users_boxes"]] == [1, 2]
    assert registry.for_box(1) is registry.legacy


async def test_temporary_error_retains_studio_and_owner(registry):
    arbox = brand_entry(await registry.discover())
    registry.enable(arbox["id"], 2)
    FakeClient.failures.add("Arbox")
    result = brand_entry(await registry.discover())
    assert result["status"] == "error"
    assert result["studios"][0]["enabled"] is True
    assert "secret" not in json.dumps(result)
    assert [b["box_fk"] for b in (await registry.profile())["users_boxes"]] == [1, 2]
    assert registry.for_box(2).whitelabel == "Arbox"
    with pytest.raises(ArboxError):
        await registry.memberships(2)
    # No fallback to Moveom, even while another connection is down.
    assert ("memberships", 2) not in registry.legacy.calls


async def test_bookings_follow_task_local_studio_context(registry):
    arbox = brand_entry(await registry.discover())
    registry.enable(arbox["id"], 2)
    active = ContextVar("box", default=None)
    registry.active_box_id = active.get

    async def booking(box):
        token = active.set(box)
        try:
            return await registry.book(123, 456)
        finally:
            active.reset(token)

    assert await asyncio.gather(booking(1), booking(2)) == [
        {"brand": "Moveom"}, {"brand": "Arbox"}]
    with pytest.raises(ConnectionError):
        registry.for_box(999)


@pytest.mark.parametrize("brand", ["../outside", "Arbox\nAuthorization: x", "", "x" * 65,
                                  "https://example.test", "../Moveom"])
async def test_header_and_path_injection_rejected(registry, brand):
    with pytest.raises(ConnectionError):
        await registry.discover(brand)


async def test_brand_names_deduplicate_case_insensitively(registry):
    await registry.discover("arbox")
    await registry.discover("moveom")
    assert len(registry.public()["connections"]) == 2


async def test_inactive_membership_cannot_enable(registry):
    FakeClient.inactive.add(2)
    arbox = brand_entry(await registry.discover())
    with pytest.raises(ConnectionError):
        registry.enable(arbox["id"], 2)


async def test_logout_erases_additional_credentials(registry, tmp_path):
    arbox = brand_entry(await registry.discover())
    registry.enable(arbox["id"], 2)
    registry.clear_creds()
    assert not registry.configured
    assert not (tmp_path / "connections.json").exists()
    assert not list((tmp_path / "connections").rglob("credentials.json"))


async def test_profile_identity_mismatch_retains_display_but_blocks_routing(registry, monkeypatch):
    arbox = brand_entry(await registry.discover())
    registry.enable(arbox["id"], 2)
    client = registry.for_box(2)

    async def wrong_profile():
        return {"id": 999, "users_boxes": [affiliation(3, "Wrong account")]}

    monkeypatch.setattr(client, "profile", wrong_profile)
    public = brand_entry(await registry.discover())
    assert public["status"] == "error"
    assert [s["id"] for s in public["studios"]] == [2]
    with pytest.raises(ConnectionError):
        registry.for_box(2)
    registry.active_box_id = lambda: 2
    assert registry.authenticated is False
    assert registry.whitelabel == "Arbox"
    assert [b["box_fk"] for b in (await registry.profile())["users_boxes"]] == [1, 2]


async def test_manifest_from_another_account_is_not_reused(registry, tmp_path):
    arbox = brand_entry(await registry.discover())
    registry.enable(arbox["id"], 2)
    other_legacy = FakeClient(str(tmp_path), "Moveom")
    other_legacy.user_id = 456
    other = Connections(str(tmp_path), other_legacy, FakeClient)
    assert len(other.public()["connections"]) == 1
    assert other.public()["studio_count"] == 0


async def test_connection_count_is_bounded(registry):
    await registry.discover()
    for number in range(6):
        await registry.discover(f"Brand{number}")
    assert len(registry.public()["connections"]) == 8
    with pytest.raises(ConnectionError):
        await registry.discover("BrandTooMany")
    assert len(registry.public()["connections"]) == 8
