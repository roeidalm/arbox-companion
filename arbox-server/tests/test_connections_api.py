from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.arbox_client import ArboxError
from app.connections import ConnectionError
from app.studio_runtime import StudioUnavailable
from test_connections import FakeClient, affiliation
from test_store import raw_session


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(FakeClient, "profiles", {
        "Arbox": [affiliation(2, "Rashty")], "Moveom": [affiliation(1, "Moveom")],
    })
    monkeypatch.setattr(FakeClient, "accounts", {})
    monkeypatch.setattr(FakeClient, "failures", set())
    monkeypatch.setattr(FakeClient, "inactive", set())
    with TestClient(main.app) as value:
        state = value.app.state
        legacy = state.client.legacy
        legacy.email, legacy._password = "person@example.test", "private-password"
        legacy._whitelabel, legacy._access_token, legacy.user_id = "Moveom", "private-token", 123
        legacy.profile = AsyncMock(return_value={
            "id": 123, "users_boxes": [affiliation(1, "Moveom")]})
        legacy.memberships = AsyncMock(return_value=[{
            "id": 10, "active": True, "membership_types": {"name": "Training"}}])
        state.connections._factory = FakeClient
        value.portal.call(state.syncer.ensure_identity)
        state.rules_engine.refresh_planning_evidence = AsyncMock()
        state.rules_engine.reconcile_planned_quota = AsyncMock()
        state.rules_engine.schedule_openings = AsyncMock()
        state.rules_engine.review_plans = AsyncMock()
        original_on_sync = state.syncer.on_sync

        async def window():
            assert state.syncer.box_id == 2
            assert state.store.active_box_id == 2
            assert state.syncer.on_sync is None
            await state.store.set_meta("last_sync", "new-studio-synced")

        state.syncer.window_sync = AsyncMock(side_effect=window)
        yield value
        assert state.syncer.on_sync is original_on_sync


def headers(client, *, guard=False):
    result = {"X-Api-Key": client.app.state.settings.api_key}
    if guard:
        result["X-Arbox-Studio-Id"] = "1"
    return result


def discover(client):
    response = client.post("/api/connections/discover", json={}, headers=headers(client))
    assert response.status_code == 200
    return next(c for c in response.json()["connections"] if c["whitelabel"] == "Arbox")


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/connections", None),
    ("POST", "/api/connections/discover", {}),
    ("POST", "/api/connections/enable", {"connection_id": "legacy", "studio_id": 1}),
])
def test_connections_require_api_key(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401


def test_discovery_does_not_select_or_enable_candidate(client):
    state = client.app.state
    selected = state.settings.preferred_studio_id
    candidate = discover(client)
    assert candidate["studios"][0]["enabled"] is False
    assert state.syncer.box_id == state.store.active_box_id == 1
    assert state.settings.preferred_studio_id == selected
    studios = client.portal.call(state.store.get_meta, "studios")
    assert [s["id"] for s in studios] == [1]
    state.syncer.window_sync.assert_not_awaited()


def test_enable_initializes_target_but_preserves_selected_panel_and_metadata(client):
    state = client.app.state
    candidate = discover(client)
    client.portal.call(state.store.set_meta, "last_sync", "original-studio-synced")
    response = client.post("/api/connections/enable", headers=headers(client, guard=True), json={
        "connection_id": candidate["id"], "studio_id": 2})
    assert response.status_code == 200, response.text
    assert response.headers["X-Arbox-Studio-Id"] == "1"
    assert response.json()["studio_count"] == 2
    assert state.syncer.box_id == state.store.active_box_id == state.settings.preferred_studio_id == 1
    assert client.portal.call(state.store.get_meta, "last_sync") == "original-studio-synced"
    assert [s["id"] for s in client.portal.call(state.store.get_meta, "studios")] == [1, 2]
    state.rules_engine.review_plans.assert_not_awaited()
    state.rules_engine.refresh_planning_evidence.assert_awaited_once()
    state.rules_engine.schedule_openings.assert_awaited_once()
    # Idempotent adds do not rerun initialization or registration checks.
    again = client.post("/api/connections/enable", headers=headers(client), json={
        "connection_id": candidate["id"], "studio_id": 2})
    assert again.status_code == 200
    state.syncer.window_sync.assert_awaited_once()


def test_enable_upstream_error_retains_addition_with_safe_pending_status(client):
    state = client.app.state
    candidate = discover(client)
    state.syncer.window_sync = AsyncMock(side_effect=ArboxError("private-token"))
    response = client.post("/api/connections/enable", headers=headers(client, guard=True), json={
        "connection_id": candidate["id"], "studio_id": 2})
    assert response.status_code == 200
    assert response.json()["sync_pending"] is True
    assert "private-token" not in response.text
    assert state.syncer.box_id == state.store.active_box_id == 1
    state.rules_engine.refresh_planning_evidence.assert_not_awaited()


def test_discovery_error_sanitized(client):
    client.app.state.connections.discover = AsyncMock(side_effect=ArboxError("private-token"))
    response = client.post("/api/connections/discover", headers=headers(client))
    assert response.status_code == 502
    assert "private-token" not in response.text


@pytest.mark.parametrize("body", [{"whitelabel": "../../escape"}, {"whitelabel": "x" * 65}])
def test_discovery_rejects_invalid_brand(client, body):
    response = client.post("/api/connections/discover", headers=headers(client), json=body)
    assert response.status_code == 422


def test_enable_rejects_undiscovered_studio(client):
    response = client.post("/api/connections/enable", headers=headers(client), json={
        "connection_id": "legacy", "studio_id": 999})
    assert response.status_code == 422
    assert client.app.state.syncer.box_id == 1


def test_manual_refresh_debounce_is_scoped_to_studio(client):
    state = client.app.state
    state.syncer.sync_range = AsyncMock(return_value=3)
    first = client.post("/api/refresh", headers=headers(client), json={})
    assert first.status_code == 200 and first.json()["synced_sessions"] == 3
    state.syncer.box_id = state.store.active_box_id = 2
    second = client.post("/api/refresh", headers=headers(client), json={})
    assert second.status_code == 200 and second.json()["synced_sessions"] == 3
    third = client.post("/api/refresh", headers=headers(client), json={})
    assert third.json()["skipped"] == "debounced"
    assert state.syncer.sync_range.await_count == 2


def enable_second(client):
    candidate = discover(client)
    response = client.post("/api/connections/enable", headers=headers(client), json={
        "connection_id": candidate["id"], "studio_id": 2})
    assert response.status_code == 200
    client.app.state.syncer.window_sync = AsyncMock()


def preferences(client, default, ignored, **kwargs):
    return client.post("/api/studios/preferences", headers=headers(client, **kwargs), json={
        "default_studio_id": default, "ignored_studio_ids": ignored})


def test_remove_default_preserves_history_and_stops_only_its_background_work(client):
    from datetime import datetime, timedelta

    enable_second(client)
    state = client.app.state
    client.portal.call(state.store.upsert_sessions, [raw_session(100, "2026-10-18", user_booked=99)])
    rule_id = client.portal.call(state.store.save_rule, {"name": "Original rule", "mode": "autobook"})

    async def add_jobs():
        for box_id in (1, 2):
            state.scheduler.add_job(AsyncMock(), "date", id=f"open_{box_id}_100",
                                    run_date=datetime.now() + timedelta(hours=1))

    client.portal.call(add_jobs)
    response = preferences(client, 2, [1])
    assert response.status_code == 200, response.text
    assert state.syncer.box_id == state.store.active_box_id == state.settings.preferred_studio_id == 2
    assert state.settings.ignored_studio_ids == [1]
    assert state.scheduler.get_job("open_1_100") is None
    assert state.scheduler.get_job("open_2_100") is not None
    state.syncer.window_sync.assert_not_awaited()
    state.rules_engine.review_plans.assert_not_awaited()
    work = AsyncMock()
    with pytest.raises(StudioUnavailable):
        client.portal.call(state.studio_runtime.run_for, 1, work)
    work.assert_not_awaited()
    studios = client.get("/api/studios", headers=headers(client)).json()
    assert [s["id"] for s in studios["studios"]] == [2]
    assert next(s for s in studios["affiliations"] if s["id"] == 1)["ignored"]
    connections = client.get("/api/connections", headers=headers(client)).json()
    assert connections["connections"][0]["studios"][0]["ignored"]

    # Restore through the same public action, without changing the new default.
    assert preferences(client, 2, []).status_code == 200
    assert state.settings.preferred_studio_id == state.syncer.box_id == 2
    async def read_original():
        return await state.store.get_session(100), await state.store.list_rules()
    session, rules = client.portal.call(state.studio_runtime.run_for, 1, read_original)
    assert session["user_booked"] == 99
    assert any(r["id"] == rule_id and r["enabled"] for r in rules)


def test_remove_nonselected_studio_preserves_temporary_selection(client):
    enable_second(client)
    state = client.app.state
    # The ignored studio may already be inactive. Removing it must not change
    # a user's deliberate browsing of a different, non-default studio.
    state.client.legacy.profile.return_value["users_boxes"].append(affiliation(3, "Old studio"))
    client.portal.call(state.syncer.select_studio, 2)
    state.syncer.window_sync.reset_mock()
    assert preferences(client, 1, [3]).status_code == 200
    assert state.syncer.box_id == 2
    assert state.settings.preferred_studio_id == 1
    state.syncer.window_sync.assert_not_awaited()


@pytest.mark.parametrize("default,ignored", [(1, [1]), (2, [1, 2]), (999, [1])])
def test_invalid_removal_leaves_preferences_and_identity_unchanged(client, default, ignored):
    enable_second(client)
    response = preferences(client, default, ignored)
    assert response.status_code == 422
    state = client.app.state
    assert state.settings.ignored_studio_ids == []
    assert state.syncer.box_id == state.store.active_box_id == state.settings.preferred_studio_id == 1


def test_guarded_removal_cannot_change_the_callers_studio(client):
    enable_second(client)
    assert preferences(client, 2, [1], guard=True).status_code == 409
    state = client.app.state
    assert state.settings.ignored_studio_ids == []
    assert state.syncer.box_id == state.settings.preferred_studio_id == 1


def test_removal_failed_membership_validation_rolls_back(client):
    enable_second(client)
    state = client.app.state
    state.syncer._store_membership = AsyncMock(side_effect=ArboxError("inventory unavailable"))
    assert preferences(client, 2, [1]).status_code == 422
    assert state.settings.ignored_studio_ids == []
    assert state.syncer.box_id == state.store.active_box_id == state.settings.preferred_studio_id == 1
    assert [s["id"] for s in client.get("/api/studios", headers=headers(client)).json()["studios"]] == [1, 2]


def test_restoring_inactive_studio_keeps_it_visible_without_enabling_bookings(client):
    enable_second(client)
    assert preferences(client, 1, [2]).status_code == 200
    FakeClient.inactive.add(2)
    assert preferences(client, 1, []).status_code == 200
    data = client.get("/api/studios", headers=headers(client)).json()
    assert [s["id"] for s in data["studios"]] == [1]
    restored = next(s for s in data["affiliations"] if s["id"] == 2)
    assert restored["ignored"] is False
    assert restored["has_active_membership"] is False
    assert client.app.state.syncer.box_id == 1
