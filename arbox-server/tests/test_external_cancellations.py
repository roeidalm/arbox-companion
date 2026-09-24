from datetime import date, timedelta
from unittest.mock import AsyncMock
import pytest
from test_membership_policy import engine, raw
from test_google_calendar import calendar
from test_api import client, api_key
from app.external_cancellations import notify_pending, callback, save_reason


@pytest.mark.asyncio
async def test_late_cancel_import_preserves_reason_and_counts_once(engine):
    s = engine.store
    await s.upsert_sessions([raw(user_booked=123, membership_user_fk=20)], box_id=73)
    await s.record_booking_success(77, 'book', 'pin', 20)
    await s.upsert_sessions([raw(membership_user_fk=20)], box_id=73)
    groups = {'past': [], 'future': [], 'lateCancellation':[raw(membership_user_fk=20)]}
    await s.reconcile_membership_history(20, groups)
    assert (await s.get_training_outcome(77))['status'] == 'cancelled_late'
    await save_reason(engine, 77, 'other', 'לא התחשק לי')
    await s.reconcile_membership_history(20, groups)
    out = await s.get_training_outcome(77)
    assert out['reason_text'] == 'לא התחשק לי'
    events = await s.training_events()
    assert len([e for e in events if e['event_type']=='cancelled_late']) == 1
    assert [e for e in events if e['event_type']=='cancelled_late'][0]['booking_id']==123
    await s.set_meta(engine.membership_policy.key(20)+':history', {'charges':[{'schedule_id':77, 'date':raw()['date'], 'membership_user_id':20, 'commitment':'used'}]})
    ledger = await engine._quota_commitments(await s.get_meta('memberships'))
    assert len([r for r in ledger if r['schedule_id']==77]) == 1
    assert next(r for r in ledger if r['schedule_id']==77)['commitment']=='used'


@pytest.mark.asyncio
async def test_disappearance_is_uncertain_then_authoritative_and_notifies_once(engine):
    s=engine.store
    await s.upsert_sessions([raw(user_booked=123, membership_user_fk=20)],box_id=73)
    await s.record_booking_success(77,'book','pin',20)
    await s.upsert_sessions([raw(membership_user_fk=20)],box_id=73)
    await s.record_external_cancellation(77,123)
    assert (await s.get_training_outcome(77))['status']=='cancelled_unknown'
    rows=await s.quota_commitments('2000-01-01','2100-01-01','2026-09-24 12:00')
    assert rows[0]['commitment']=='uncertain'
    await notify_pending(engine); await notify_pending(engine)
    engine.notifier.send.assert_awaited_once()
    button=engine.notifier.send.await_args.args[1][0][0]['data']
    cid=button.split(':')[1]
    reply=await callback(engine,'xc_yes',cid)
    assert 'סיבת' in reply.text
    await callback(engine,'xc_reason_other',cid,'לא התחשק לי')
    await s.record_external_cancellation(77,123,confirmed_late=True)
    assert (await s.get_training_outcome(77))['reason_text']=='לא התחשק לי'
    assert (await s.get_training_outcome(77))['status']=='cancelled_late'


@pytest.mark.asyncio
async def test_old_cancel_does_not_overwrite_rebooking(engine):
    s=engine.store
    await s.upsert_sessions([raw(user_booked=456)],box_id=73)
    await s.record_external_cancellation(77,123,confirmed_late=True)
    assert await s.get_training_outcome(77) is None
    assert (await s.get_session(77))['user_booked']==456


@pytest.mark.asyncio
async def test_google_projects_yesterdays_cancellation_with_reason(calendar):
    g=calendar
    row={**g.engine.store.get_session.return_value, 'date':(date.today()-timedelta(days=1)).isoformat(),
         'status':'cancelled_late','reason_code':'other','reason_text':'לא התחשק לי'}
    g.engine.store.training_history.return_value=[row]
    body=next(iter((await g.desired(g.profile())).values()))
    assert body['summary'].startswith('בוטל')
    assert body['colorId']=='11'
    assert 'כניסה נוצלה' in body['description'] and 'לא התחשק לי' in body['description']
    assert body['transparency']=='transparent' and body['reminders']['overrides']==[]


def test_reason_endpoint_is_local_authenticated_and_studio_scoped(client):
    s=client.app.state.store
    client.portal.call(s.upsert_sessions,[raw()])
    client.portal.call(s.set_training_outcome,77,'cancelled_late','external_sync')
    path='/api/history/77/cancellation-reason'
    body={'status':'cancelled_late','reason_code':'other','reason_text':'לא התחשק לי'}
    assert client.put(path,json=body).status_code==401
    assert client.put(path,json=body,headers={'X-Api-Key':api_key(client)}).status_code==200
    assert client.portal.call(s.get_training_outcome,77)['reason_text']=='לא התחשק לי'

@pytest.mark.asyncio
async def test_sync_detects_and_preserves_removed_future_booking(engine):
    from app.sync import Syncer
    s=engine.store
    day=(date.today()+timedelta(days=1)).isoformat()
    await s.upsert_sessions([raw(day=day,user_booked=123,membership_user_fk=20)],box_id=73)
    await s.record_booking_success(77,'book','pin',20)
    sync=Syncer(engine.client,s)
    sync.box_id=73;sync.location_id=42
    sync.ensure_identity=AsyncMock()
    engine.client.schedule_between=AsyncMock(return_value=[])
    await sync._pull_range(day,day)
    assert (await s.get_session(77))['user_booked'] is None
    assert (await s.get_training_outcome(77))['status']=='cancelled_unknown'
    await sync._pull_range(day,day)
    assert len([e for e in await s.training_events() if e['event_type']=='cancelled_unknown'])==1

@pytest.mark.asyncio
async def test_old_notification_cannot_change_new_cancellation_cycle(engine):
    s=engine.store
    await s.record_external_cancellation(77,123,confirmed_late=True)
    await notify_pending(engine)
    cid=engine.notifier.send.await_args.args[1][0][0]['data'].split(':')[1]
    await s.upsert_sessions([raw(user_booked=456)],box_id=73)
    await s.record_booking_success(77,'book','manual',20)
    await s.record_external_cancellation(77,456,confirmed_late=True)
    answer=await callback(engine,'xc_reason_none',cid)
    assert 'השתנה' in answer
    assert (await s.get_training_outcome(77))['reason_code'] is None
