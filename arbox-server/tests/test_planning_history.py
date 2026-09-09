import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from test_membership_policy import engine, raw
from test_api import client, api_key


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['accept', 'cancel', 'skip'])
async def test_change_history_survives_decision_restart_and_deleted_class(engine, decision):
    store = engine.store
    await store.watch(77)
    await store.upsert_sessions([raw(cat=2)], box_id=73)
    session = await store.get_session(77)
    await asyncio.gather(*(store.intent_change(session) for _ in range(4)))
    assert len(await store.training_events()) == 1
    if decision == 'accept':
        await store.accept_intent(session)
        await store.accept_intent(session)
        assert not await store.intent_change(session)
    elif decision == 'cancel':
        await store.unwatch(77)
        await store.unwatch(77)
        assert await store.get_intent(77) is None
    else:
        await store.set_automation_skip(77, True)
        await store.set_automation_skip(77, True)
    events = await store.training_events()
    assert len(events) == 2
    assert events[0]['change']['before']['category_name'] == 'Class 1'
    assert events[0]['change']['after']['category_name'] == 'Class 2'
    assert events[-1]['event_type'] == ('planning_change_accepted' if decision == 'accept' else 'planning_change_cancelled')
    assert events[-1]['occurred_at']
    assert 'Class 1' in events[-1]['reason_text'] and 'Class 2' in events[-1]['reason_text']
    # Planning decisions cannot become attendance, journal candidates, or quota usage.
    assert await store.training_history() == []
    assert await store.journal_candidates() == []
    assert await store.get_training_outcome(77) is None
    await store.db.execute('DELETE FROM sessions WHERE schedule_id=77')
    await store.db.commit()
    await store.close(); await store.open()
    store.active_box_id = 73
    history = await store.planning_history()
    assert len(history) == 1 and history[0]['category_name'] == 'Class 2'
    assert history[0]['planning_change']['before']['category_name'] == 'Class 1'
    store.active_box_id = 74
    assert await store.planning_history() == []
    assert await store.training_events() == []


@pytest.mark.asyncio
async def test_successive_changes_keep_each_transition_and_the_original_acceptance(engine):
    store = engine.store
    await store.watch(77)
    for cat in (2, 3, 1, 1):
        await store.upsert_sessions([raw(cat=cat)], box_id=73)
    events = await store.training_events()
    assert [(e['change']['before']['category_id'],e['change']['after']['category_id']) for e in events] == [(1,2),(2,3),(3,1)]
    await store.accept_intent(await store.get_session(77))
    assert len(await store.training_events()) == 4
    await store.upsert_sessions([raw(cat=2)], box_id=73)
    assert len(await store.training_events()) == 5
    engine.notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_pin_is_not_recreated_by_an_overlapping_rule(engine):
    await engine.store.save_rule({'name':'All classes', 'mode':'autobook'})
    await engine.store.watch(77)
    await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
    await engine.store.unwatch(77)
    plans = await engine._planned_sessions('2026-09-01', '2026-09-30')
    assert 77 not in {p['schedule_id'] for p in plans}
    assert {78,79} <= {p['schedule_id'] for p in plans}


@pytest.mark.parametrize('accept', [True, False])
def test_mine_and_history_show_new_name_old_name_and_reason_without_booking(client, accept):
    state, store = client.app.state, client.app.state.store
    store.active_box_id = 73
    day = (date.today() + timedelta(days=7)).isoformat()
    client.portal.call(store.upsert_sessions, [raw(day=day)])
    client.portal.call(store.watch, 77)
    client.portal.call(store.upsert_sessions, [raw(cat=2, day=day)])
    headers = {'X-Api-Key':api_key(client)}
    if accept:
        current = client.portal.call(store.get_session, 77)
        client.portal.call(store.accept_intent, current)
    else:
        state.rules_engine.reconcile_planned_quota = AsyncMock()
        assert client.delete('/api/watchlist/77', headers=headers).status_code == 200
    rows = client.get('/api/me', headers=headers).json()['sessions']
    if accept:
        assert len(rows) == 1 and rows[0]['category_name'] == 'Class 2'
        assert rows[0]['planning_change']['before']['category_name'] == 'Class 1'
    else:
        assert rows == []
    assert client.get('/api/history').status_code == 401
    history = client.get('/api/history', headers=headers).json()
    assert history['sessions'] == []
    assert len(history['changes']) == 1 and len(history['events']) == 2
    assert 'Class 1' in history['changes'][0]['reason_text']
