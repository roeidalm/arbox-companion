from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from app.arbox_client import ArboxError
from app.sync import Syncer
from app.studio_cancellations import confirm_removed, reconcile_recent
from app.external_cancellations import notify_pending, callback
from test_membership_policy import engine, raw
from test_google_calendar import calendar


async def seed(engine):
    store = engine.store
    day = (date.today() + timedelta(days=1)).isoformat()
    await store.upsert_sessions([raw(day=day, user_booked=123, _selected_membership_id=20)], box_id=73)
    await store.record_booking_success(77, 'book', 'membership_sync', 20)
    session = await store.get_session(77)
    await store.remember_intent(session, 'autobook', 'test-rule')
    await store.add_prompt('old-cancel', 77, 'external_cancel')
    await store.add_prompt('old-attendance', 77, 'attendance')
    other = raw(sid=88, day=day, locations_box_fk=42)
    engine.client.schedule_between = AsyncMock(return_value=[other])
    sync = Syncer(engine.client, store)
    sync.box_id = 73; sync.location_id = 42; sync.ensure_identity = AsyncMock()
    engine.syncer = sync
    return session, other


@pytest.mark.asyncio
async def test_removed_class_closes_only_occurrence_without_warning_or_rebooking(engine):
    session, other = await seed(engine)
    await engine.store.save_rule({'name':'Class 1 weekly', 'categories':['Class 1'], 'mode':'autobook'})
    rules_before = await engine.store.list_rules()
    await engine.syncer._pull_range(session['date'], session['date'])
    outcome = await engine.store.get_training_outcome(77)
    assert outcome['status'] == 'cancelled_studio'
    assert outcome['reason_code'] == 'studio_removed'
    assert 'סיבת הביטול לא נמסרה' in outcome['reason_text']
    assert await engine.store.get_intent(77) is None
    assert (await engine.store.peek_prompt('old-cancel'))['answered_at']
    assert (await engine.store.peek_prompt('old-attendance'))['answered_at']
    assert 'טופלה' in await callback(engine, 'xc_yes', 'old-cancel')
    assert await engine.store.quota_commitments('2000-01-01', '2100-01-01', '2100-01-01 00:00') == []
    assert 77 not in [r['schedule_id'] for r in await engine.store.get_sessions()]
    assert 77 not in [r['schedule_id'] for r in await engine._planned_sessions('2000-01-01', '2100-01-01')]
    assert await engine.store.list_rules() == rules_before
    events = await engine.store.training_events()
    assert [e['event_type'] for e in events if e['schedule_id'] == 77] == ['booked', 'cancelled_studio']
    assert events[-1]['counts_entry'] == 0
    await notify_pending(engine); await notify_pending(engine)
    engine.notifier.send.assert_awaited_once()
    assert engine.notifier.send.await_args.kwargs['kind'] == 'studio'
    assert 'האם אתה ביטלת' not in engine.notifier.send.await_args.args[0]
    assert len(engine.notifier.send.await_args.args) == 1
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['empty_day','returns_on_second_read','wrong_studio','wrong_location',
                                 'malformed_day','read_error','still_booked','late_charge','bad_membership','missing_group'])
async def test_ambiguous_disappearance_never_becomes_studio_cancellation(engine, case):
    session, other = await seed(engine)
    if case == 'empty_day': engine.client.schedule_between.return_value = []
    if case == 'returns_on_second_read': engine.client.schedule_between.return_value = [other, raw(day=session['date'])]
    if case == 'wrong_studio': engine.client.schedule_between.return_value = [{**other, 'box_fk':999}]
    if case == 'wrong_location': engine.client.schedule_between.return_value = [{**other, 'locations_box_fk':999}]
    if case == 'malformed_day': engine.client.schedule_between.return_value = {'data':[]}
    if case == 'read_error': engine.client.schedule_between.side_effect = ArboxError('offline')
    if case in ('still_booked','late_charge'):
        group = 'future' if case == 'still_booked' else 'lateCancellation'
        engine.client.membership_schedules.side_effect = lambda mid: {
            'past':[], 'future':[], 'lateCancellation':[],
            group:[raw(day=session['date'], membership_user_fk=mid, user_booked=123)]}
    if case == 'bad_membership': engine.client.membership_schedules.return_value = {
        'past':[], 'future':[raw(sid=999, membership_user_fk=999)], 'lateCancellation':[]}
    if case == 'missing_group': engine.client.membership_schedules.return_value = {'future':[]}
    assert not await confirm_removed(engine.syncer, session, [other])
    assert await engine.store.get_training_outcome(77) is None
    assert (await engine.store.get_session(77))['user_booked'] == 123


@pytest.mark.asyncio
async def test_disappeared_personal_booking_on_existing_class_remains_unknown(engine):
    session, other = await seed(engine)
    engine.client.schedule_between.return_value = [other, raw(day=session['date'])]
    await engine.syncer._pull_range(session['date'], session['date'])
    assert (await engine.store.get_training_outcome(77))['status'] == 'cancelled_unknown'
    engine.client.membership_schedules.assert_not_awaited()


@pytest.mark.asyncio
async def test_recent_unknown_is_resolved_and_stale_membership_cycle_cannot_revive_it(engine):
    session, other = await seed(engine)
    await engine.store.record_external_cancellation(77, 123)
    await engine.store.set_meta(engine.membership_policy.key(20)+':history', {
        'charges':[{'schedule_id':77,'date':session['date'],'membership_user_id':20,'commitment':'reserved'}]})
    await reconcile_recent(engine)
    assert (await engine.store.get_training_outcome(77))['status'] == 'cancelled_studio'
    await reconcile_recent(engine)
    assert len([e for e in await engine.store.training_events() if e['event_type']=='cancelled_studio']) == 1
    assert 77 not in [r['schedule_id'] for r in await engine._quota_commitments(await engine.store.get_meta('memberships'))]
    groups = {'past':[], 'future':[raw(day=session['date'],user_booked=123,membership_user_fk=20)], 'lateCancellation':[]}
    await engine.store.reconcile_membership_history(20, groups)
    assert (await engine.store.get_session(77))['user_booked'] is None
    # A distinct confirmed registration is a new lifecycle, not a stale response.
    groups['future'][0]['user_booked'] = 456
    await engine.store.reconcile_membership_history(20, groups)
    assert await engine.store.get_training_outcome(77) is None
    assert (await engine.store.get_session(77))['user_booked'] == 456


@pytest.mark.asyncio
async def test_old_evidence_cannot_cancel_a_new_booking_or_other_studio(engine):
    session, other = await seed(engine)
    await engine.store.upsert_sessions([raw(day=session['date'],user_booked=456,membership_user_fk=20)],box_id=73)
    assert not await confirm_removed(engine.syncer, session, [other])
    assert (await engine.store.get_session(77))['user_booked'] == 456
    engine.store.active_box_id=999
    assert not await engine.store.record_studio_cancellation(77,456)


@pytest.mark.asyncio
async def test_calendar_projects_studio_cancellation_without_review_or_reminders(calendar):
    row={**calendar.engine.store.get_session.return_value, 'date':(date.today()-timedelta(days=1)).isoformat(),
         'status':'cancelled_studio','reason_code':'studio_cancelled','reason_text':'מיעוט נרשמים — לפי הודעת המשתמש'}
    calendar.engine.store.training_history.return_value=[row]
    body=next(iter((await calendar.desired(calendar.profile())).values()))
    assert body['summary'].startswith('בוטל על ידי הסטודיו · ') and 'דורש בדיקה' not in body['summary']
    assert 'מיעוט נרשמים' in body['description'] and 'החיוב בבירור' not in body['description']
    assert body['transparency']=='transparent' and body['reminders']['overrides']==[]
