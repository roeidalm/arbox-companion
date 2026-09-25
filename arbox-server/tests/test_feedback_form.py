import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.feedback_form import FeedbackBody, FeedbackForm
from app.settings import Settings
from app.store import Store


@pytest.fixture
async def form(tmp_path):
    store = Store(str(tmp_path / "arbox.db"))
    await store.open()
    store.active_box_id = 10
    await store.upsert_sessions([{
        "id": 9123, "date": date.today().isoformat(), "time": "18:00", "end_time": "19:00",
        "coach": {"id": 1, "first_name": "Dana"}, "box_categories": {"id": 2, "name": "Strength"},
        "series": {}, "booking_option": "past", "enable_registration_time": 168,
        "user_booked": 99, "user_in_standby": None,
    }], box_id=10)
    settings = Settings(str(tmp_path))
    settings.update({"base_url": "http://localhost:8178"})
    notifier = SimpleNamespace(settings=settings, send_preview=AsyncMock())
    yield FeedbackForm(store, notifier)
    await store.close()


async def test_real_save_bound_to_original_studio_survives_restart_and_is_idempotent(form):
    session = await form.store.get_session(9123)
    token, url = await form.create(session, "full")
    assert url == f"http://localhost:8178/feedback#{token}"
    assert len(token) >= 40
    restarted = FeedbackForm(form.store, form.notifier)
    form.store.active_box_id = 20
    assert (await restarted.read(token))["session"]["schedule_id"] == 9123
    body = FeedbackBody(coach_feedback="positive", class_feedback="neutral", notes="התקדמות",
                        exercises=[{"name": "סקוואט", "metric_type": "strength", "sets": 3, "reps": 8, "weight": 60},
                                   {"name": "פלאנק", "metric_type": "duration", "duration_seconds": 45}])
    results = await asyncio.gather(restarted.save(token, body), restarted.save(token, body))
    assert sum(bool(r.get("already_saved")) for r in results) == 1
    assert form.store.active_box_id == 20
    assert await form.store.workout_journal(9123) is None
    form.store.active_box_id = 10
    saved = await form.store.workout_journal(9123)
    assert saved["notes"] == "התקדמות" and len(saved["exercises"]) == 2
    assert saved["exercises"][0]["weight"] == 60
    assert (await form.store.get_training_outcome(9123))["status"] == "attended"
    assert (await restarted.read(token))["complete"] is True
    await restarted.save(token, FeedbackBody(notes="must not overwrite"))
    assert (await form.store.workout_journal(9123))["notes"] == "התקדמות"
    form.notifier.send_preview.assert_not_called()


async def test_invalid_and_expired_capabilities_cannot_read_or_write(form):
    for token in (None, "bad", "x" * 101):
        with pytest.raises(HTTPException) as error:
            await form.read(token)
        assert error.value.status_code == 401
    session = await form.store.get_session(9123)
    token, _ = await form.create(session, "feedback")
    row = await form.store.peek_prompt(token)
    payload = json.loads(row["payload"])
    payload["expires"] = 0
    await form.store.add_prompt(token, 9123, "feedback_form", payload=json.dumps(payload))
    with pytest.raises(HTTPException) as error:
        await form.save(token, FeedbackBody(class_feedback="positive"))
    assert error.value.status_code == 410
    assert await form.store.workout_journal(9123) is None
    assert (await form.store.peek_prompt(token))["answered_at"] is None


async def test_validation_is_retryable_and_preview_has_no_workout_effects(form):
    link = await form.start_preview("ha", "full")
    token = link.split("#")[1]
    with pytest.raises(HTTPException):
        await form.save(token, FeedbackBody(coach_feedback="invalid"))
    with pytest.raises(HTTPException):
        await form.save(token, FeedbackBody())
    assert not (await form.read(token))["complete"]
    result = await form.save(token, FeedbackBody(notes="demo"))
    assert result["demo"] is True
    assert not await form.store.workout_journals()
    assert not await form.store.get_training_outcome(9123)
    form.notifier.send_preview.assert_awaited_once()


async def test_missing_base_url_and_failed_delivery_leave_no_live_token(form):
    form.settings.update({"base_url": ""})
    with pytest.raises(ValueError):
        await form.start_preview("ha", "full")
    form.settings.update({"base_url": "http://localhost:8178"})
    form.notifier.send_preview.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError):
        await form.start_preview("ha", "full")
    cur = await form.store.db.execute("SELECT count(*) FROM pending_prompts")
    assert (await cur.fetchone())[0] == 0


async def test_ha_hosted_preview_needs_no_public_server_url(form):
    form.settings.update({"base_url": "", "ha": {"feedback_in_ha": True}})
    link = await form.start_preview("ha", "full")
    assert link.startswith("/arbox-feedback#")
    token = link.split("#")[1]
    assert (await form.read(token))["demo"] is True
    sent = form.notifier.send_preview.await_args
    assert sent.args[0] == "ha"
    assert sent.args[2] == [[{"text": "מילוי משוב", "uri": link}]]
    with pytest.raises(ValueError):
        await form.start_preview("telegram", "full")
    form.settings.update({"base_url": "https://arbox.example"})
    assert (await form.start_preview("telegram", "full")).startswith("https://arbox.example/feedback#")


async def test_real_ha_link_and_explicit_fallback(form):
    session = await form.store.get_session(9123)
    form.settings.update({"base_url": "", "ha": {"feedback_in_ha": True}})
    token, link = await form.create(session, "full", channel="ha")
    assert link == f"/arbox-feedback#{token}"
    assert (await form.read(token))["demo"] is False
    form.settings.update({"ha": {"feedback_in_ha": False}})
    with pytest.raises(ValueError):
        await form.create(session, "full", channel="ha")


async def test_absence_records_missed_without_feedback_or_followup(form):
    session = await form.store.get_session(9123)
    token, _ = await form.create(session, 'feedback')
    await form.store.add_prompt('old-attendance', 9123, 'attendance')
    result = await form.save(token, FeedbackBody(attended=False))
    assert result['attended'] is False
    assert (await form.store.get_training_outcome(9123))['status'] == 'missed'
    saved = await form.store.workout_journal(9123)
    assert saved['dismissed_at']
    assert not saved['coach_feedback'] and not saved['class_feedback']
    assert not await form.store.live_prompt_for(9123, 'attendance')
    assert not await form.store.journal_candidates()
    assert (await form.read(token))['attended'] is False
    await form.save(token, FeedbackBody(class_feedback='positive'))
    assert (await form.store.get_training_outcome(9123))['status'] == 'missed'


async def test_absence_preview_and_conflicting_feedback(form):
    token = (await form.start_preview('telegram', 'quick')).split('#')[1]
    with pytest.raises(HTTPException):
        await form.save(token, FeedbackBody(attended=False, class_feedback='positive'))
    assert not (await form.read(token))['complete']
    await form.save(token, FeedbackBody(attended=False))
    assert not await form.store.get_training_outcome(9123)
    assert not await form.store.workout_journals()


async def test_cancelled_workout_cannot_be_overwritten_from_old_feedback_link(form):
    token, _ = await form.create(await form.store.get_session(9123), 'full')
    await form.store.set_training_outcome(9123, 'cancelled_on_time', 'manual')
    for body in (FeedbackBody(attended=False), FeedbackBody(class_feedback='positive')):
        with pytest.raises(HTTPException) as error:
            await form.save(token, body)
        assert error.value.status_code == 409
    assert not await form.store.workout_journal(9123)


async def test_feedback_is_offered_without_prior_attendance_and_only_once(form, monkeypatch):
    from datetime import datetime
    import app.rules as rules_module

    class Evening(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.combine(date.today(), cls.min.time()).replace(hour=20)

    monkeypatch.setattr(rules_module, 'datetime', Evening)
    history = form.store.training_history
    monkeypatch.setattr(form.store, 'training_history', lambda: history(now=Evening.now()))
    await form.store.set_meta('attendance_tracking_since', date.today().isoformat())
    form.settings.update({'journal': {'level': 'feedback'}})
    form.notifier.journal_form_channel = lambda: 'telegram'
    form.notifier.send_journal_form = AsyncMock(return_value=True)
    engine = rules_module.RulesEngine(form.store, object(), object(), form.notifier)
    assert await form.store.get_training_outcome(9123) is None
    await engine.journal_tick()
    await engine.journal_tick()
    form.notifier.send_journal_form.assert_awaited_once()
    assert await form.store.get_training_outcome(9123) is None
    assert (await form.store.workout_journal(9123))['prompted_at']
