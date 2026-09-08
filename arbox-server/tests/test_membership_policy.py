import asyncio
import time
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import app.rules as rules_module
from app.arbox_client import ArboxError
from app.membership_policy import MembershipPolicy, fingerprint, eligible, parse_limits
from app.quota_planner import plan_quota
from app.rules import RulesEngine, PlanningBlocked
from app.settings import Settings
from app.store import Store
from test_api import client, api_key


async def seed_policies(engine, limits=None):
    """Explicit evidence fixture; never enable plan-name inference in tests."""
    cats = await engine.membership_policy.catalog()
    for m in await engine.store.get_meta('memberships') or []:
        key = engine.membership_policy.key(m['id'])
        await engine.store.set_meta(key, {
            'source': 'manual', 'fingerprint': fingerprint(m), 'category_ids': [c['id'] for c in cats],
            'categories_known': True, 'limits': limits if limits is not None else [
                {'count': m.get('plan_quota') or 5, 'period': 'month'}], 'checked_at': time.time(),
        })
        await engine.store.set_meta(key + ':history', {'ok': True, 'checked_at': time.time()})


def member(mid, cats, quota=5, card=False):
    return {'id': mid, 'membership_type_id': mid * 100, 'active': True,
            'start': '2026-09-01', 'end': '2026-10-31', 'plan': f'Membership {mid}',
            'sessions_on_purchase': quota if card else None, 'sessions_left': quota if card else None,
            'policy': {'state': 'ready', 'categories_known': True, 'quota_known': True,
                       'category_ids': cats, 'limits': [{'count': quota, 'period': 'card' if card else 'month'}]}}


def plan(sid, cat=1, day='2026-09-20', **kw):
    return {'schedule_id': sid, 'category_id': cat, 'date': day, 'planning_source': 'scheduled', **kw}


def raw(sid=77, cat=1, day='2026-09-24', **kw):
    return {'id': sid, 'date': day, 'time': '10:00', 'end_time': '11:00',
            'box_fk': 73, 'box_categories': {'id': cat, 'name': f'Class {cat}'},
            'coach': {}, 'series': {}, 'enable_registration_time': 168,
            'booking_option': 'insertScheduleUser', 'user_booked': None, 'user_in_standby': None, **kw}


@pytest_asyncio.fixture
async def engine(tmp_path, monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls): return cls(2026, 9, 8)
    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026, 9, 8, 12)
    monkeypatch.setattr(rules_module, 'date', FixedDate)
    monkeypatch.setattr(rules_module, 'datetime', FixedTime)
    store = Store(str(tmp_path/'test.db')); await store.open(); store.active_box_id = 73
    await store.upsert_sessions([raw(), raw(78, 2), raw(79, 3)], box_id=73)
    members = [member(10, [1, 2, 3]), member(20, [1], card=True)]
    await store.set_meta('memberships', members)
    settings = Settings(str(tmp_path)); settings.select_studio(73)
    notifier = SimpleNamespace(settings=settings, send=AsyncMock(return_value=True), on_callback=None, on_message=None)
    client = SimpleNamespace(email='personal@example.test', user_id=7,
        book=AsyncMock(return_value=raw(user_booked=88)), join_standby=AsyncMock(),
        membership_details=AsyncMock(side_effect=ArboxError('not available',status=500)), membership_schedules=AsyncMock(return_value={'past':[], 'future':[], 'lateCancellation':[]}))
    syncer = SimpleNamespace(ensure_identity=AsyncMock(), refresh_membership=AsyncMock(),
                             box_id=73, location_id=42, membership_user_id=10)
    e = RulesEngine(store, client, syncer, notifier)
    for m in members:
        key = e.membership_policy.key(m['id'])
        await store.set_meta(key, {**m['policy'], 'source': 'manual', 'checked_at': time.time(),
                                  'fingerprint': fingerprint(m)})
        await store.set_meta(key+':history', {'ok': True, 'checked_at': time.time()})
    yield e
    await store.close()


def test_nine_september_commitments_do_not_borrow_movement_capacity():
    members = [member(10, [1, 2]), member(20, [1], card=True)]
    members[1]['sessions_left'] = 4
    actual = [plan(1, 2, commitment='used', membership_user_id=10),
              plan(2, 2, commitment='used', membership_user_id=10),
              plan(3, 2, commitment='reserved', membership_user_id=10),
              plan(4, 2, commitment='reserved', membership_user_id=10),
              plan(5, 1, commitment='reserved', membership_user_id=20)]
    future = [plan(6, 1), plan(7, 2), plan(8, 2), plan(9, 2, planning_source='autobook')]
    q = plan_quota(members, actual, future, '2026-09')
    assert q['used'] + q['reserved'] + q['planned_total'] == 9
    assert q['plan_allocations'] == {'6': 20, '7': 10}
    assert q['uncovered_plans'] == [8, 9]
    assert q['memberships'][1]['available_after_planned'] == 3


def test_card_capacity_spans_months_and_prior_usage():
    m = member(1, [1], quota=2, card=True); m['sessions_left'] = 1
    q = plan_quota([m], [plan(1, day='2026-09-03', commitment='used', membership_user_id=1)],
                   [plan(2), plan(3, day='2026-10-05')], '2026-10')
    assert q['plan_allocations'] == {'2': 1}
    assert q['uncovered_plans'] == [3]
    assert q['memberships'][0]['used'] == 1


def test_unknown_attribution_is_visible_and_cannot_use_preferred_bucket():
    q = plan_quota([member(1,[1])], [plan(1, commitment='used', membership_user_id=None)], [plan(2)], '2026-09')
    assert q['unattributed_sessions'] == [1]
    assert q['memberships'][0]['used'] == 0
    assert q['plan_states']['2']['state'] == 'unattributed'


def test_explicit_wrong_membership_does_not_change_silently():
    q = plan_quota([member(10,[1,2]), member(20,[1],card=True)], [], [plan(1,2,membership_user_id=20)], '2026-09')
    assert not q['plan_allocations']
    assert q['plan_states']['1']['state'] == 'no_membership'


def test_daily_weekly_and_monthly_limits_share_the_same_ledger():
    m = member(1,[1]); m['policy']['limits'] += [{'count':1,'period':'day'}, {'count':2,'period':'week','week_start':6}]
    q = plan_quota([m], [], [plan(1,day='2026-09-13'), plan(2,day='2026-09-13'),
                             plan(3,day='2026-09-14'),plan(4,day='2026-09-15'),plan(5,day='2026-09-20')], '2026-09')
    assert set(q['plan_allocations']) == {'1','3','5'}
    assert q['uncovered_plans'] == [2,4]
    assert parse_limits([{'header':'Frequency Limitations','values':['Up to 2 sessions per week']}])[1]


@pytest.mark.asyncio
async def test_all_errors_are_parsed_and_restriction_is_scoped(engine):
    err = ArboxError('425',status=425,body={'error': {'messageToUser': [
        {'name':'registerScheduleDisabled','value':{'hours':168}},
        {'name':'classTypeRestricts','value':{'allowedText':'Class 1\n','class':'Class 2'}}]}})
    m = (await engine.store.get_meta('memberships'))[1]
    await engine.store.set_meta(engine.membership_policy.key(20), {})
    assert await engine.membership_policy.learn_rejection(m, {'category_id':2}, err)
    policy = await engine.membership_policy.get(m)
    assert eligible({**m, 'policy':policy}, {'category_id':1})
    assert not eligible({**m, 'policy':policy}, {'category_id':2})
    assert engine.settings.blocked_categories == []
    engine.store.active_box_id = 74
    assert (await engine.membership_policy.get(m))['categories_known'] is False


@pytest.mark.asyncio
async def test_ambiguous_names_and_new_membership_require_review(engine):
    m = (await engine.store.get_meta('memberships'))[0]
    await engine.store.upsert_sessions([raw(90,3,box_categories={'id':3,'name':'Class 1'})], box_id=73)
    await engine.store.set_meta(engine.membership_policy.key(10), {'source':'shop','fingerprint':fingerprint(m),
        'verified_at':time.time(),'categories_known':True,'category_names':['Class 1'],'limits':[{'count':5,'period':'month'}]})
    p = await engine.membership_policy.get(m)
    assert p['unmatched'] == ['Class 1']
    assert not eligible({**m,'policy':p}, {'category_id':1})
    assert (await engine.membership_policy.get({**m,'end':'2027-01-01'}))['state'] == 'needs_review'


@pytest.mark.asyncio
async def test_concurrent_requests_book_once_and_keep_membership(engine):
    session = await engine.store.get_session(77)
    results = await asyncio.gather(*(engine.perform_membership_action(session,'book') for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(r,PlanningBlocked) for r in results) == 1
    engine.client.book.assert_awaited_once_with(77,20)
    assert (await engine.store.get_session(77))['membership_user_id'] == 20


@pytest.mark.asyncio
async def test_unknown_write_is_not_retried_after_restart_and_reserves_capacity(engine):
    engine.client.book.side_effect = ArboxError('timeout', transient=True)
    s = await engine.store.get_session(77)
    with pytest.raises(PlanningBlocked): await engine.perform_membership_action(s,'book')
    restored = RulesEngine(engine.store,engine.client,engine.syncer,engine.notifier)
    with pytest.raises(PlanningBlocked): await restored.perform_membership_action(s,'book')
    engine.client.book.assert_awaited_once()
    await engine.store.watch(77)
    q = await restored.quota_status()
    assert q['plan_states']['77']['state'] == 'uncertain'
    assert q['memberships'][1]['uncertain'] == 1


@pytest.mark.asyncio
async def test_safe_preflight_learns_once_and_timing_alone_does_not_verify(engine):
    m = (await engine.store.get_meta('memberships'))[1]
    await engine.store.set_meta(engine.membership_policy.key(20), {'fingerprint':fingerprint(m),'checked_at':time.time()})
    await engine.store.watch(77,membership_user_id=20)
    engine.client.book.side_effect = ArboxError('not yet',status=425,body={'error':{'messageToUser':[
        {'name':'registerScheduleDisabled','value':{'hours':168}}]}})
    await engine.preflight_plans(); await engine.preflight_plans()
    engine.client.book.assert_awaited_once_with(77,20)
    assert (await engine.membership_policy.get(m))['state'] == 'needs_review'
    assert (await engine.quota_status())['unresolved_plans'] == [77]


@pytest.mark.asyncio
async def test_preflight_never_probes_an_open_or_unwanted_class(engine):
    m = (await engine.store.get_meta('memberships'))[1]
    await engine.store.set_meta(engine.membership_policy.key(20), {'fingerprint':fingerprint(m)})
    await engine.preflight_plans()
    engine.client.book.assert_not_awaited()
    await engine.store.upsert_sessions([raw(day='2026-09-09')],box_id=73)
    await engine.store.watch(77,membership_user_id=20)
    await engine.preflight_plans()
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_missed_window_is_one_event_across_repeated_ticks_and_restart(engine):
    s = await engine.store.get_session(77)
    for _ in range(5): await engine._mark_missed_window(s, datetime(2026,9,1))
    restored = RulesEngine(engine.store,engine.client,engine.syncer,engine.notifier)
    await restored._mark_missed_window(s,datetime(2026,9,1))
    cur = await engine.store.db.execute("SELECT COUNT(*) FROM events WHERE message LIKE 'לא נתפס%'")
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_policy_conflict_notice_is_one_state_transition(engine):
    await engine.store.watch(78,membership_user_id=20)
    for _ in range(5): await engine.reconcile_planned_quota()
    restored = RulesEngine(engine.store,engine.client,engine.syncer,engine.notifier)
    await restored.reconcile_planned_quota()
    engine.notifier.send.assert_awaited_once()
    assert '24.9' in engine.notifier.send.await_args.args[0]


@pytest.mark.asyncio
async def test_membership_history_restores_missing_booking_without_resetting_availability(engine):
    await engine.store.reconcile_membership_history(20, {'future':[raw(90, membership_user_fk=20,user_booked=101)],'past':[],'lateCancellation':[]})
    assert (await engine.store.get_session(90))['membership_user_id'] == 20
    assert any(s['schedule_id'] == 90 for s in await engine.store.my_sessions())
    with pytest.raises(ValueError):
        await engine.store.reconcile_membership_history(20, {'future':[raw(membership_user_fk=10,user_booked=101)],'past':[],'lateCancellation':[]})


def test_allocator_moves_flexible_plan_to_cover_a_later_specialist_class():
    q = plan_quota([member(1,[1,2],1),member(2,[1,3],1)], [], [plan(1,1),plan(2,2)], '2026-09')
    assert q['plan_allocations'] == {'1':2,'2':1}
    assert q['uncovered_plans'] == []


@pytest.mark.asyncio
async def test_duplicate_booking_receipt_is_one_history_event(engine):
    await engine.perform_membership_action(await engine.store.get_session(77),'book')
    await engine.store.record_booking_success(77,'book','manual',20)
    rows = await engine.store.training_events()
    assert len(rows) == 1
    assert rows[0]['source'] == 'manual'


@pytest.mark.asyncio
async def test_failed_shop_read_is_cached_and_manual_revision_is_not_overwritten(engine):
    m = (await engine.store.get_meta('memberships'))[1]
    key = engine.membership_policy.key(m['id'])
    await engine.store.set_meta(key, {})
    await engine.membership_policy.refresh([m]); await engine.membership_policy.refresh([m])
    engine.client.membership_details.assert_awaited_once()
    assert (await engine.membership_policy.get(m))['state'] == 'needs_review'
    await engine.membership_policy.save_manual(m,[1],[],fingerprint(m))
    changed = {**m,'end':'2027-01-01'}
    await engine.membership_policy.refresh([changed])
    assert (await engine.membership_policy.get(changed))['state'] == 'needs_review'
    assert (await engine.store.get_meta(key))['source'] == 'manual'


@pytest.mark.asyncio
async def test_preflight_unexpected_success_is_a_real_booking_not_a_test_record(engine):
    m = (await engine.store.get_meta('memberships'))[1]
    await engine.store.set_meta(engine.membership_policy.key(20), {'fingerprint':fingerprint(m)})
    await engine.store.watch(77,membership_user_id=20)
    await engine.preflight_plans(); await engine.preflight_plans()
    engine.client.book.assert_awaited_once()
    assert (await engine.store.get_session(77))['user_booked'] == 88
    assert (await engine.store.list_watchlist())[0]['result'] == 'booked'
    assert (await engine.store.training_events())[0]['source'] == 'preflight'


@pytest.mark.asyncio
async def test_unattributed_history_recovers_same_day_booking_without_erasing_window(engine):
    await engine.store.reconcile_membership_history(20, {'future':[raw(membership_user_fk=20,user_booked=101)],'past':[],'lateCancellation':[]})
    s = await engine.store.get_session(77)
    assert s['user_booked'] == 101 and s['membership_user_id'] == 20
    assert s['advance_hours'] == 168


@pytest.mark.asyncio
async def test_legacy_block_migration_preserves_manual_block_and_past_decision(engine):
    engine.settings.block_category('Class 1'); engine.settings.block_category('Class 2')
    await engine.store.log_event('warn','autobook','חסמתי קטגוריה · Class 1',schedule_id=77)
    await engine.store.mark_autobook(77,'blocked category')
    await engine.migrate_learned_blocks()
    assert engine.settings.blocked_categories == ['Class 2']
    assert await engine.store.autobook_attempted(77)


def test_policy_api_requires_key_and_valid_membership_revision(client):
    store = client.app.state.store
    engine = client.app.state.rules_engine
    m = member(20,[1],card=True)
    client.portal.call(store.set_meta,'memberships',[m])
    client.portal.call(store.upsert_sessions,[raw()])
    assert client.get('/api/membership-policies').status_code == 401
    assert client.get('/api/quota').status_code == 401
    headers = {'X-Api-Key':api_key(client)}
    data = client.get('/api/membership-policies',headers=headers).json()
    assert data['categories'] == [{'id':1,'name':'Class 1'}]
    body = {'category_ids':[1], 'limits':[], 'fingerprint':fingerprint(m)}
    assert client.put('/api/membership-policies/20',json=body).status_code == 401
    assert client.put('/api/membership-policies/20',headers=headers,json={**body,'fingerprint':'outdated'}).status_code == 409
    assert client.put('/api/membership-policies/20',headers=headers,json={**body,'category_ids':[999]}).status_code == 409
    assert client.put('/api/membership-policies/20',headers=headers,json=body).status_code == 200
    assert client.put('/api/membership-policies/999',headers=headers,json=body).status_code == 404


def test_missing_booking_is_not_resumed_without_explicit_confirmation(client):
    store = client.app.state.store
    engine = client.app.state.rules_engine
    client.portal.call(store.upsert_sessions,[raw()])
    key = engine.membership_policy.key(0)+':uncertain'
    client.portal.call(store.set_meta,key,{'77':{'membership_user_id':20}})
    client.app.state.syncer.sync_range = AsyncMock()
    engine.refresh_planning_evidence = AsyncMock()
    headers = {'X-Api-Key':api_key(client)}
    assert client.post('/api/planning/77/reconcile',json={}).status_code == 401
    assert client.post('/api/planning/77/reconcile',headers=headers,json={}).status_code == 200
    assert client.portal.call(store.get_meta,key)
    assert client.post('/api/planning/77/reconcile',headers=headers,json={'confirm_not_booked':True}).status_code == 200
    assert client.portal.call(store.get_meta,key) == {}


@pytest.mark.asyncio
async def test_preflight_counts_late_charges_absent_from_calendar(engine):
    m = (await engine.store.get_meta('memberships'))[0]
    key = engine.membership_policy.key(m['id'])
    await engine.store.set_meta(key, {'fingerprint': fingerprint(m), 'limits': [{'count':1, 'period':'month'}]})
    await engine.store.set_meta(key+':history', {'ok': True, 'checked_at': time.time(), 'charges': [
        {'schedule_id': 900, 'date': '2026-09-03', 'commitment': 'used', 'membership_user_id':m['id']}]})
    await engine.store.watch(77, membership_user_id=m['id'])
    await engine.preflight_plans()
    engine.client.book.assert_not_awaited()
    quota = await engine.quota_status()
    assert quota['used'] == 1
    assert quota['memberships'][0]['available'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('remove_calendar_row', [False, True])
async def test_stale_membership_history_does_not_restore_cancelled_booking(engine, remove_calendar_row):
    await engine.store._insert_training_event(77, 'cancelled_safe', 'manual', booking_id=101)
    if remove_calendar_row:
        await engine.store.db.execute('DELETE FROM sessions WHERE schedule_id=77')
    groups = {'past': [], 'lateCancellation': [], 'future': [raw(membership_user_fk=20, user_booked=101)]}
    await engine.store.reconcile_membership_history(20, groups)
    assert (await engine.store.get_session(77) or {}).get('user_booked') is None
    groups['future'][0]['user_booked'] = 102
    await engine.store.reconcile_membership_history(20, groups)
    assert (await engine.store.get_session(77))['user_booked'] == 102


@pytest.mark.asyncio
async def test_history_refresh_does_not_append_bookings_after_other_events(engine):
    groups = {'past': [], 'lateCancellation': [], 'future': [raw(membership_user_fk=20, user_booked=101)]}
    await engine.store.reconcile_membership_history(20, groups)
    await engine.store._insert_training_event(77, 'attended', 'manual', booking_id=101)
    await engine.store.reconcile_membership_history(20, groups)
    assert len(await engine.store.training_events()) == 2


def test_unpinned_interrupted_booking_is_visible_but_never_retried(client):
    store, engine = client.app.state.store, client.app.state.rules_engine
    client.portal.call(store.upsert_sessions, [raw(day='2026-01-01')])
    key = engine.membership_policy.key(0)+':uncertain'
    client.portal.call(store.set_meta, key, {'77':{'membership_user_id':20, 'action':'book'}})
    headers = {'X-Api-Key':api_key(client)}
    assert client.get('/api/me').json()['sessions'] == []
    rows = client.get('/api/me', headers=headers).json()['sessions']
    assert len(rows) == 1
    assert rows[0]['planning_source'] == 'uncertain'
    assert rows[0]['planning']['state'] == 'uncertain'
    assert client.portal.call(store.list_watchlist) == []
    assert 'raw_json' not in rows[0]


def test_weekly_display_uses_selected_date_instead_of_first_week_of_month():
    m = member(1,[1]); m['policy']['limits'] = [{'count':1,'period':'week','week_start':6}]
    q = plan_quota([m], [plan(1, day='2026-09-14', commitment='reserved', membership_user_id=1)],
                   [], '2026-09', anchor='2026-09-16')
    assert q['memberships'][0]['reserved'] == 1
    assert q['memberships'][0]['period_start'] == '2026-09-13'


@pytest.mark.asyncio
async def test_interrupted_write_keeps_capacity_when_studio_deletes_calendar_row(engine):
    engine.client.book.side_effect = ArboxError('timeout', transient=True)
    with pytest.raises(PlanningBlocked):
        await engine.perform_membership_action(await engine.store.get_session(77), 'book', 20)
    await engine.store.delete_sessions_in_range_not_in('2026-09-24','2026-09-24',[])
    await engine.refresh_planning_evidence(force_history=True)
    q = await engine.quota_status()
    assert q['plan_states']['77']['state'] == 'uncertain'
    assert q['memberships'][1]['uncertain'] == 1
    assert q['memberships'][1]['available_after_planned'] == 4
    snapshot = (await engine.uncertain_sessions())[0]
    assert snapshot['schedule_id'] == 77 and 'raw_json' not in snapshot
    await engine.preflight_plans()
    engine.client.book.assert_awaited_once()
