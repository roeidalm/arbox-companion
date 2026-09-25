from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from app.rule_monitor import RuleMonitor, evaluate
from app.store import Store
from test_api import client, api_key

TODAY = date(2026, 9, 28)
RULE = {'id': 1, 'name': 'Flex with Nir', 'enabled': True, 'coaches': ['Nir'],
        'categories': ['Flex'], 'weekdays': [0], 'time_from': '09:00', 'time_to': '10:00',
        'mode': 'autobook', 'recurrence_weeks': 1, 'recurrence_anchor': '2026-09-28'}


def session(day, *, category='Flex', coach='Nir', sid=1):
    return {'schedule_id': sid, 'date': day, 'start_time': '09:30', 'end_time': '10:30',
            'category_name': category, 'coach_name': coach}


def test_missing_new_week_is_detected_without_any_prior_occurrence_id():
    periods = evaluate(RULE, [session('2026-09-28'), session('2026-10-05', coach='Other'),
                            session('2026-10-12')], TODAY, '2026-10-12')
    assert [p['state'] for p in periods[:3]] == ['matched', 'missing', 'matched']


def test_biweekly_cycle_has_stable_anchor_and_accepts_second_week():
    rule = {**RULE, 'recurrence_weeks': 2}
    rows = [session('2026-09-28', coach='Other'), session('2026-10-05'),
            session('2026-10-12', coach='Other'), session('2026-10-19', coach='Other')]
    periods = evaluate(rule, rows, TODAY, '2026-10-25')
    assert [(p['start'], p['state']) for p in periods[:2]] == [('2026-09-28', 'matched'), ('2026-10-12', 'missing')]
    shifted = evaluate(rule, rows, date(2026, 10, 5), '2026-10-25')
    assert shifted[0]['start'] == '2026-09-28'
    assert shifted[1]['state'] == 'missing'


def test_closed_day_is_quiet_but_empty_unpublished_or_failed_response_is_not_closure():
    rows = [session('2026-09-29'), session('2026-10-06')]
    assert evaluate(RULE, rows, TODAY, '2026-10-06')[0]['state'] == 'closed'
    assert evaluate(RULE, [], TODAY, None)[0]['state'] == 'unpublished'
    assert evaluate(RULE, rows, TODAY, '2026-10-06', verified=False)[0]['state'] == 'unverified'
    assert evaluate(RULE, rows, TODAY, '2026-09-29')[1]['state'] == 'unpublished'


def test_explicit_skip_or_vacation_is_quiet():
    assert evaluate(RULE, [session('2026-09-28', coach='Other')], TODAY, '2026-10-25',
                    excluded={'2026-09-28'})[0]['state'] == 'skipped'


@pytest.fixture
async def monitor(tmp_path):
    store = Store(str(tmp_path/'db')); await store.open(); store.active_box_id = 73
    await store.save_rule({**RULE, 'id': None})
    engine = SimpleNamespace(store=store, notifier=SimpleNamespace(send=AsyncMock(return_value=True)))
    value = RuleMonitor(engine)
    value.snapshot = AsyncMock(return_value={'sessions': [session('2026-09-28', coach='Other'), session('2026-10-05')],
        'verified': True, 'published_through': '2026-10-05'})
    yield value
    await store.close()


async def test_monitor_persists_delivery_dedup_and_retries_failed_sends(monitor):
    monitor.e.notifier.send.side_effect = [False, True, True]
    await monitor.check(TODAY)
    await monitor.check(TODAY)
    await monitor.check(TODAY)
    assert monitor.e.notifier.send.await_count == 2
    assert '2026-09-28' in monitor.e.notifier.send.await_args.args[0]
    restarted = RuleMonitor(monitor.e); restarted.snapshot = monitor.snapshot
    await restarted.check(TODAY)
    assert monitor.e.notifier.send.await_count == 2
    monitor.snapshot.return_value['sessions'][0] = session('2026-09-28')
    await restarted.check(TODAY)
    monitor.snapshot.return_value['sessions'][0] = session('2026-09-28', coach='Other')
    await restarted.check(TODAY)
    assert monitor.e.notifier.send.await_count == 3


async def test_nightly_rule_uses_digest_routing_and_studio_scoped_state(monitor):
    rule = (await monitor.e.store.list_rules())[0]
    await monitor.e.store.save_rule({**rule, 'mode': 'notify'})
    await monitor.check(TODAY)
    assert monitor.e.notifier.send.await_args.kwargs['kind'] == 'digest'
    monitor.e.store.active_box_id = 74
    await monitor.check(TODAY)
    assert monitor.e.notifier.send.await_count == 1


async def test_validation_reads_upstream_without_booking_and_excludes_past(tmp_path):
    store = Store(str(tmp_path/'db')); await store.open(); store.active_box_id = 73
    tomorrow = date.today() + timedelta(days=1)
    raw = {'id': 99, 'date': tomorrow.isoformat(), 'time': '09:30', 'end_time': '10:30',
           'coach': {'first_name': 'Nir'}, 'box_categories': {'name': 'Flex'}, 'series': {}}
    engine = SimpleNamespace(store=store, client=SimpleNamespace(schedule_between=AsyncMock(return_value=[raw]), book=AsyncMock()),
        syncer=SimpleNamespace(ensure_identity=AsyncMock(), box_id=73, location_id=1))
    try:
        rule = {**RULE, 'weekdays': []}
        monitor = RuleMonitor(engine)
        assert (await monitor.validate(rule))['state'] == 'verified'
        assert (await monitor.validate({**rule, 'coaches': ['No Such Coach']}))['state'] == 'no_match'
        engine.client.schedule_between.side_effect = RuntimeError('offline')
        assert (await monitor.validate(rule))['state'] == 'unverified'
        engine.client.book.assert_not_awaited()
    finally:
        await store.close()


def test_creation_guard_requires_confirmation_or_disabled_draft(client):
    state = client.app.state
    state.rules_engine.rule_monitor.validate = AsyncMock(return_value={
        'state':'no_match', 'message':'אין התאמה', 'date_from':'2026-09-28', 'date_to':'2026-10-25'})
    state.rules_engine.schedule_openings = AsyncMock()
    state.rules_engine.review_plans = AsyncMock()
    headers = {'X-Api-Key': api_key(client)}
    body = {k:v for k,v in RULE.items() if k != 'id'}
    response = client.post('/api/rules', json=body, headers=headers)
    assert response.status_code == 200 and response.json()['needs_confirm']
    assert client.get('/api/rules').json()['rules'] == []
    draft = client.post('/api/rules', json={**body, 'enabled':False}, headers=headers)
    assert draft.json()['ok']
    saved = client.get('/api/rules').json()['rules'][0]
    assert not saved['enabled'] and saved['recurrence_weeks'] == 1
    response = client.post('/api/rules', json={**body, 'id':saved['id'], 'recurrence_weeks':2, 'confirm_unmatched':True}, headers=headers)
    assert response.json()['ok']
    saved = client.get('/api/rules').json()['rules'][0]
    assert saved['enabled'] and saved['recurrence_weeks'] == 2
    assert saved['validation']['state'] == 'no_match'
    assert client.post('/api/rules', json={**body, 'recurrence_weeks':0}, headers=headers).status_code == 422
    assert client.post('/api/rules', json={**body, 'recurrence_anchor':'bad'}, headers=headers).status_code == 422


async def test_suppressed_period_does_not_hide_next_period_or_survive_rule_change(monitor):
    from app.rule_monitor import signature
    rule = (await monitor.e.store.list_rules())[0]
    await monitor.e.store.set_meta(monitor.key(rule['id']) + ':skips', [
        {'start':'2026-09-28', 'end':'2026-10-04', 'signature':signature(rule)}])
    excluded = await monitor.exclusions(rule, TODAY)
    assert '2026-09-28' in excluded and '2026-10-05' not in excluded
    assert await monitor.exclusions({**rule, 'recurrence_weeks':2}, TODAY) == set()
    await monitor.check(TODAY)
    monitor.e.notifier.send.assert_not_awaited()


def test_edit_without_matching_workout_preserves_original_rule(client):
    state = client.app.state
    state.rules_engine.rule_monitor.validate = AsyncMock(return_value={
        'state':'verified', 'match_count':2, 'first_match':date.today().isoformat(), 'message':'נמצאו אימונים'})
    state.rules_engine.schedule_openings = AsyncMock()
    state.rules_engine.review_plans = AsyncMock()
    headers = {'X-Api-Key':api_key(client)}
    body = {k:v for k,v in RULE.items() if k != 'id'}
    rid = client.post('/api/rules', json=body, headers=headers).json()['id']
    state.rules_engine.rule_monitor.validate.return_value = {
        'state':'no_match', 'message':'אין התאמה', 'date_from':'2026-09-28', 'date_to':'2026-10-25'}
    response = client.post('/api/rules', json={**body, 'id':rid, 'coaches':['Impossible']}, headers=headers)
    assert response.json()['needs_confirm']
    assert client.get('/api/rules').json()['rules'][0]['coaches'] == ['Nir']
    from app.rule_monitor import monday
    start = monday(date.today()).isoformat()
    assert client.post(f'/api/rules/{rid}/skip-period', json={'period_start':start}, headers=headers).json()['ok']
    assert client.post(f'/api/rules/{rid}/skip-period', json={'period_start':'2000-01-01'}, headers=headers).status_code == 422


async def test_unknown_schedule_warns_before_long_registration_window(monitor):
    monitor.snapshot.return_value.update(sessions=[], verified=False, published_through=None)
    await monitor.e.store.set_meta(monitor.key(1)+':validation', {'lead_days':16})
    await monitor.check(TODAY)
    text = monitor.e.notifier.send.await_args.args[0]
    assert '2026-10-12' in text
    assert 'טרם ניתן לאמת' in text


async def test_creation_detects_incorrect_weekly_cadence(monitor):
    monitor.snapshot.return_value.update(sessions=[session('2026-09-28', coach='Other'),
        session('2026-10-05'), session('2026-10-12', coach='Other'), session('2026-10-19')],
        published_through='2026-10-25', date_from='2026-09-28', date_to='2026-11-29')
    result = await monitor.validate(RULE, TODAY)
    assert result['state'] == 'partial'
    result = await monitor.validate({**RULE, 'recurrence_weeks':2}, TODAY)
    assert result['state'] == 'verified'
