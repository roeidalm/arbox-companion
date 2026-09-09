import json
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.api import _annotate, _clean_reason


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", str(tmp_path))
    with TestClient(main.app) as value:
        yield value


def api_key(client):
    with open(client.app.state.settings._path) as settings_file:
        return json.load(settings_file)["api_key"]


def test_health_and_spa_routes_work_before_setup(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["version"] == "dev"
    assert health.json()["revision"] == "unknown"
    assert health.json()["build"]["schema"] == 1
    assert health.json()["build"]["version"] == "dev"
    assert health.json()["build"]["revision"] == "unknown"
    assert health.json()["configured"] is False
    assert health.json()["authenticated"] is False

    assert client.get("/").status_code == 200
    assert client.get("/schedule").status_code == 200
    assert client.get("/journal").status_code == 200
    assert client.get("/system").status_code == 200
    assert client.get("/not-a-page").status_code == 404


def test_summary_requires_the_current_api_key(client):
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers={"X-Api-Key": "wrong"}).status_code == 401

    response = client.get("/api/summary", headers={"X-Api-Key": api_key(client)})
    assert response.status_code == 200
    assert {"membership", "my_sessions", "week"} <= response.json().keys()


def test_journal_accepts_neutral_and_not_applicable_feedback(client):
    day = (date.today() - timedelta(days=1)).isoformat()
    store = client.app.state.store
    client.portal.call(store.upsert_sessions, [{
        "id": 8701, "date": day, "time": "18:00", "end_time": "19:00",
        "coach": {"id": 1, "first_name": "Dana", "last_name": None},
        "box_categories": {"id": 2, "name": "Movement"}, "series": {},
        "booking_option": "past", "enable_registration_time": 168,
        "user_booked": 99, "user_in_standby": None,
    }])
    headers = {"X-Api-Key": api_key(client)}

    response = client.put("/api/journal/8701", headers=headers, json={
        "coach_feedback": "not_applicable", "class_feedback": "neutral",
        "notes": "בדיקה", "exercises": [],
    })

    assert response.status_code == 200
    assert response.json()["journal"]["coach_feedback"] == "not_applicable"
    assert response.json()["journal"]["class_feedback"] == "neutral"

    invalid = client.put("/api/journal/8701", headers=headers, json={
        "coach_feedback": "maybe", "exercises": [],
    })
    assert invalid.status_code == 422


@pytest.mark.parametrize("channel", ["ha", "telegram"])
def test_journal_rehearsal_uses_real_transport_and_single_form_without_workout_writes(client, monkeypatch, channel):
    from pathlib import Path
    import sqlite3
    from app.notify import TG_API

    notifier = client.app.state.notifier
    settings = client.app.state.settings
    settings.update({
        "telegram": {"enabled": False, "bot_token": "test-token", "chat_id": "123", "kinds": []},
        "ha": {"enabled": False, "webhook_url": "http://ha.test/webhook", "kinds": []},
        "journal": {"level": "off"},
        "base_url": "http://testserver",
    })
    original_settings = Path(settings._path).read_bytes()
    def stored_workouts():
        with sqlite3.connect(Path(settings._path).parent / "arbox.db") as db:
            return {table: db.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("training_outcomes", "training_events", "workout_journals",
                                  "workout_exercises")}

    original_workouts = stored_workouts()
    sent = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            return {"ok": True}

    class Transport:
        def post(self, url, json):
            sent.append((url, json))
            return Response()

    monkeypatch.setattr(notifier, "_http", AsyncMock(return_value=Transport()))
    monkeypatch.setattr(notifier, "start_telegram_poller", lambda: None)
    headers = {"X-Api-Key": api_key(client)}
    before = client.get("/api/journal", headers=headers).json()["entries"]
    response = client.post(f"/api/settings/test-journal/{channel}", headers=headers, json={"level": "full"})
    assert response.status_code == 200

    assert len(sent) == 1
    message = sent[0][1]
    if channel == "ha":
        assert len(message["actions"]) == 1
        assert message["actions"][0]["action"] == "URI"
        link = message["actions"][0]["uri"]
    else:
        buttons = message["reply_markup"]["inline_keyboard"]
        assert len(buttons) == len(buttons[0]) == 1
        link = buttons[0][0]["url"]
    assert link == response.json()["url"]
    assert api_key(client) not in link
    assert client.get("/feedback").status_code == 200
    assert client.get("/api/feedback").status_code == 401
    capability = {"X-Feedback-Token": link.split("#")[1]}
    form = client.get("/api/feedback", headers=capability)
    assert form.status_code == 200 and form.json()["demo"] is True
    assert form.headers["cache-control"] == "no-store"
    saved = client.put("/api/feedback", headers=capability, json={
        "coach_feedback": "positive", "class_feedback": "neutral",
        "notes": "היה נהדר", "exercises": [{"name": "סקוואט", "metric_type": "strength", "sets": 3, "reps": 8, "weight": 60}],
    })
    assert saved.status_code == 200 and saved.json()["demo"] is True
    assert client.get("/api/feedback", headers=capability).json()["complete"]
    assert len(sent) == 1  # Saving never sends an additional notification.
    assert all(url == ("http://ha.test/webhook" if channel == "ha" else
                       f"{TG_API}/bottest-token/sendMessage") for url, _ in sent)
    assert Path(settings._path).read_bytes() == original_settings
    assert stored_workouts() == original_workouts
    assert client.get("/api/journal", headers=headers).json()["entries"] == before
    assert not client.app.state.rules_engine.journal_preview.sessions


def test_journal_rehearsal_auth_validation_and_missing_configuration(client):
    url = "/api/settings/test-journal/ha"
    assert client.post(url, json={"level": "full"}).status_code == 401
    headers = {"X-Api-Key": api_key(client)}
    assert client.post(url, headers=headers, json={"level": "off"}).status_code == 422
    assert client.post("/api/settings/test-journal/other", headers=headers,
                       json={"level": "full"}).status_code == 422
    assert client.post(url, headers=headers, json={"level": "full"}).status_code == 502
    assert not client.app.state.rules_engine.journal_preview.sessions


def test_exercise_catalogue_search_and_shortcut_management(client):
    client.app.state.settings.select_studio(10)
    headers = {"X-Api-Key": api_key(client)}
    response = client.get(
        "/api/journal/exercise-catalog?q=deadlift&kind=strength",
        headers=headers,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["count"] == 1324
    exercise = next(x for x in payload["items"] if x["id"].startswith("exdb-"))

    added = client.post(
        f"/api/journal/exercise-catalog/{exercise['id']}",
        headers=headers, json={"action": "add"},
    )
    assert added.status_code == 200
    settings = client.get("/api/settings", headers=headers).json()
    assert exercise["id"] in {x["id"] for x in settings["exercise_shortcuts"]}

    removed = client.post(
        f"/api/journal/exercise-catalog/{exercise['id']}",
        headers=headers, json={"action": "remove"},
    )
    assert removed.status_code == 200
    settings = client.get("/api/settings", headers=headers).json()
    assert exercise["id"] in {x["id"] for x in settings["hidden_exercises"]}


def test_host_guard_blocks_public_dns_names_only(client):
    client.app.state.settings.update({"base_url": "https://arbox.example"})

    assert client.get("/api/health", headers={"Host": "arbox.example"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "arbox-server"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400


def test_cancellation_reason_validation():
    assert _clean_reason("work", "ignored") == ("work", None)
    assert _clean_reason("other", "  train delay  ") == ("other", "train delay")
    with pytest.raises(Exception, match="reason_text is required"):
        _clean_reason("other", "  ")
    with pytest.raises(Exception, match="invalid reason_code"):
        _clean_reason("invented", None)


def test_calendar_marks_rule_match_as_vacation_not_autobook():
    session = {
        "schedule_id": 1, "date": "2026-09-07", "start_time": "09:30",
        "coach_name": "ליאור מרפי", "category_name": "Flex- Back\\Arches",
        "booking_option": "insertScheduleUser", "advance_hours": 168,
    }
    rule = {
        "name": "ליאור ביום שני", "weekdays": [0], "coaches": [],
        "categories": [], "time_from": None, "time_to": None,
    }
    vacation = {
        "date_from": "2026-09-05", "date_to": "2026-09-10",
        "block_autobook": 1,
    }

    annotated = _annotate([session], autobook_rules=[rule], vacations=[vacation])[0]
    assert annotated["autobook_match"] is False
    assert annotated["autobook_blocked_by_vacation"] is True


def test_scheduling_over_quota_preserves_intent_but_never_bypasses_gate(client):
    day = (date.today() + timedelta(days=14)).isoformat()
    store = client.app.state.store
    client.portal.call(store.upsert_sessions, [{
        "id": 8801, "date": day, "time": "20:00", "end_time": "21:00",
        "coach": {"id": 1, "first_name": "Dana", "last_name": None},
        "box_categories": {"id": 2, "name": "Movement"}, "series": {},
        "booking_option": "insertScheduleUser", "enable_registration_time": 168,
        "user_booked": None, "user_in_standby": None,
    }])
    engine = client.app.state.rules_engine
    engine.quota_status = AsyncMock(return_value={
        "quota": 5, "used": 1, "reserved": 0,
        "planned": 4, "planned_total": 5,
        "overcommitted": True, "uncovered_plans": [8801],
        "plan_states": {"8801": {"state":"no_capacity", "reason":"המכסה מלאה — ההרשמה מושהית"}},
    })
    engine.schedule_openings = AsyncMock()
    engine.watchlist_tick = AsyncMock()
    engine.reconcile_planned_quota = AsyncMock()
    headers = {"X-Api-Key": api_key(client)}

    warning = client.post("/api/watchlist", headers=headers, json={
        "schedule_id": 8801, "allow_standby": True,
    })
    assert warning.status_code == 200
    assert warning.json()['ok'] is True
    assert warning.json()['planning']['state'] == 'no_capacity'
    assert 'מושהית' in warning.json()['quota_note']
    assert len(client.portal.call(store.list_watchlist)) == 1

    accepted = client.post("/api/watchlist", headers=headers, json={
        "schedule_id": 8801, "allow_standby": True,
        "confirm_over_quota": True,
    })
    assert accepted.status_code == 200
    assert accepted.json()["ok"] is True
    assert [row["schedule_id"] for row in
            client.portal.call(store.list_watchlist)] == [8801]


def test_completed_session_can_be_classified_from_history(client):
    day = (date.today() - timedelta(days=1)).isoformat()
    session = {
        "id": 7001, "date": day, "time": "20:00", "end_time": "21:00",
        "coach": {"id": 1, "first_name": "Dana", "last_name": None},
        "box_categories": {"id": 2, "name": "Movement"},
        "series": {}, "booking_option": "past", "user_booked": 99,
        "user_in_standby": None,
    }
    store = client.app.state.store
    client.portal.call(store.set_meta, "attendance_tracking_since", day)
    client.portal.call(store.upsert_sessions, [session])
    headers = {"X-Api-Key": api_key(client)}

    before = client.get("/api/history", headers=headers).json()["sessions"]
    assert before[0]["status"] == "pending"

    response = client.put(
        "/api/history/7001/attendance", headers=headers,
        json={"status": "missed", "reason_code": "other",
              "reason_text": "stuck at work"},
    )
    assert response.status_code == 200
    after = client.get("/api/history", headers=headers).json()["sessions"]
    assert after[0]["status"] == "missed"
    assert after[0]["reason_text"] == "stuck at work"
    events = client.get("/api/history", headers=headers).json()["events"]
    assert events[-1]["schedule_id"] == 7001
    assert events[-1]["event_type"] == "missed"
    assert events[-1]["reason_text"] == "stuck at work"


def test_ha_calendar_export_preserves_event_alarms_location_and_auth(client):
    headers = {"X-Api-Key": api_key(client)}
    assert client.get('/api/calendar/export?schedule_id=987').status_code == 401
    assert client.get('/api/calendar/export?schedule_id=987', headers=headers).status_code == 404
    store = client.app.state.store
    client.portal.call(store.upsert_sessions, [{
        'id': 987, 'date': '2026-09-16', 'time': '08:00', 'end_time': '09:15',
        'coach': {'id': 1, 'first_name': 'Coach', 'last_name': None},
        'box_categories': {'id': 2, 'name': 'Movement'}, 'series': {},
        'booking_option': 'insertScheduleUser', 'user_booked': None,
    }])
    client.portal.call(store.set_meta, 'identity', {'studio_name': 'Studio', 'address': 'Test street'})
    client.app.state.settings.update({'calendar_alarms': [30, 60]})
    result = client.get('/api/calendar/export?schedule_id=987', headers=headers)
    assert result.status_code == 200
    exported = result.json()
    assert 'BEGIN:VALARM' in exported['ics']
    assert 'LOCATION:Studio\\, Test street' in exported['ics']
    # Same renderer/fields as the existing links, including configured alarms.
    import re
    clean = lambda text: re.sub(r'DTSTAMP:[^\r\n]+', '', text)
    assert clean(exported['ics']) == clean(client.get('/api/calendar/event/987.ics').text)
    google = client.get('/api/calendar/event/987/google', follow_redirects=False)
    assert exported['google'] == google.headers['location']
    assert api_key(client) not in result.text
