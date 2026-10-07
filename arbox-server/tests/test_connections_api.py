from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.arbox_client import ArboxError
from app.connections import ConnectionError
from test_connections import FakeClient, affiliation


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
