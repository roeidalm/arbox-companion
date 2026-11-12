from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.api import router
from app.arbox_client import ArboxClient, ArboxError
from app.external_cancellations import notify_pending
from test_membership_policy import engine, raw


def late_refusal():
    return ArboxError('late cancellation', status=513, body={
        'statusCode': 513, 'error': {'code': 513, 'message': 'Schedule Exception',
        'messageToUser': "it's too late to cancel 12 before the class :("}, 'data': None})


@pytest_asyncio.fixture
async def flow(engine, tmp_path):
    day = (date.today() + timedelta(days=1)).isoformat()
    booked = raw(day=day, user_booked=123, membership_user_fk=20,
                 booking_option='cancelScheduleUser', disable_cancellation_time=12)
    await engine.store.upsert_sessions([booked], box_id=73)
    await engine.store.record_booking_success(77, 'book', 'rule', 20)
    client = ArboxClient(str(tmp_path))
    async def request(method, path, body):
        if path.endswith('checkLateCancel'):
            raise late_refusal()
        assert path.endswith('/delete')
        return {'data': {**booked, 'user_booked': None, 'booking_option': 'insertScheduleUser'}}
    client._request = AsyncMock(side_effect=request)
    app = FastAPI(); app.include_router(router)
    app.state.settings = engine.settings; app.state.syncer = engine.syncer
    app.state.store = engine.store; app.state.rules_engine = engine; app.state.client = client
    engine.syncer.exclusive = lambda: engine._tick_lock
    headers = {'X-Api-Key': engine.settings.api_key, 'X-Arbox-Studio-Id': '73'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        yield SimpleNamespace(http=http, client=client, engine=engine, headers=headers, booked=booked)
    await client.close()


async def test_verified_513_is_late_window_and_preview_never_cancels_or_records_consent(flow):
    r = await flow.http.get('/api/schedule/77/cancellation', headers=flow.headers)
    assert r.status_code == 200 and r.json() == {
        'studio_id': 73, 'schedule_id': 77, 'booking_id': 123, 'late_cancel': True, 'standby': False}
    assert r.headers['Cache-Control'] == 'no-store'
    assert (await flow.engine.store.get_session(77))['user_booked'] == 123
    assert not await flow.engine.store.get_meta('cancel_request:73:77:123')
    assert flow.client._request.await_count == 1


async def test_single_acknowledgement_cancels_late_and_records_reason_without_another_check(flow):
    await flow.http.get('/api/schedule/77/cancellation', headers=flow.headers)
    result = await flow.http.post('/api/cancel', headers=flow.headers, json={
        'schedule_id': 77, 'booking_id': 123, 'late_cancel': True, 'reason_code': 'illness'})
    assert result.status_code == 200 and result.json()['ok']
    assert flow.client._request.await_count == 2
    assert flow.client._request.await_args.args == ('POST', '/api/v2/scheduleUser/delete', {
        'schedule_user_id': 123, 'schedule_id': 77, 'late_cancel': True})
    outcome = await flow.engine.store.get_training_outcome(77)
    assert outcome['status'] == 'cancelled_late'
    assert (await flow.engine.store.training_events())[-1]['counts_entry'] == 1
    assert outcome['reason_code'] == 'illness'
    await notify_pending(flow.engine)
    flow.engine.notifier.send.assert_not_awaited()


@pytest.mark.parametrize('error', [ArboxError('offline', status=503),
    ArboxError('different exception', status=513, body={'error': {'code': 513, 'messageToUser': 'no permission'}}),
    ArboxError('malformed', status=513, body=None)])
async def test_other_errors_are_not_treated_as_permission_to_cancel(flow, error):
    flow.client._request.side_effect = error
    r = await flow.http.get('/api/schedule/77/cancellation', headers=flow.headers)
    assert r.status_code == 502
    assert (await flow.engine.store.get_session(77))['user_booked'] == 123
    assert not await flow.engine.store.get_meta('cancel_request:73:77:123')


async def test_preview_and_cancel_reject_wrong_studio_or_booking_before_upstream(flow):
    assert (await flow.http.get('/api/schedule/77/cancellation')).status_code == 401
    assert (await flow.http.get('/api/schedule/77/cancellation', headers={
        **flow.headers, 'X-Arbox-Studio-Id': '74'})).status_code == 409
    r = await flow.http.post('/api/cancel', headers=flow.headers, json={
        'schedule_id': 77, 'booking_id': 456, 'late_cancel': True})
    assert r.status_code == 409
    flow.client._request.assert_not_awaited()
    assert not await flow.engine.store.get_meta('cancel_request:73:77:123')


@pytest.mark.parametrize('late_ack', [False, True])
async def test_failed_confirmed_request_is_reconciled_without_asking_the_actor_again(flow, late_ack):
    if late_ack:
        flow.client._request.side_effect = ArboxError('connection lost', transient=True)
    r = await flow.http.post('/api/cancel', headers=flow.headers, json={
        'schedule_id': 77, 'booking_id': 123, 'late_cancel': late_ack, 'reason_code': 'work'})
    assert r.status_code == (502 if late_ack else 409)
    store = flow.engine.store
    assert (await store.get_session(77))['user_booked'] == 123
    assert not await store.get_training_outcome(77)
    assert await store.get_meta('cancel_request:73:77:123')
    # The native app later cancels the same booking. Only Arbox proves the fee.
    await store.upsert_sessions([{**flow.booked, 'user_booked': None}], box_id=73)
    await store.record_external_cancellation(77, 123)
    assert (await store.training_events())[-1]['counts_entry'] is None
    await notify_pending(flow.engine)
    await store.record_external_cancellation(77, 123, confirmed_late=True)
    await notify_pending(flow.engine)
    outcome = await store.get_training_outcome(77)
    assert outcome['source'] == 'external_requested' and outcome['reason_code'] == 'work'
    assert outcome['status'] == 'cancelled_late'
    assert (await store.training_events())[-1]['counts_entry'] == 1
    flow.engine.notifier.send.assert_not_awaited()


@pytest.mark.parametrize('different_booking', [False, True])
async def test_expired_or_different_booking_consent_does_not_suppress_unrelated_cancellation(flow, different_booking):
    store = flow.engine.store
    await store.remember_cancellation_request(77, 123, 'work')
    if not different_booking:
        await store.set_meta('cancel_request:73:77:123', {
            'requested_at': (datetime.now() - timedelta(hours=25)).isoformat(), 'reason_code': 'work'})
    booking = 456 if different_booking else 123
    await store.upsert_sessions([{**flow.booked, 'user_booked': booking}], box_id=73)
    await store.record_booking_success(77, 'book', 'manual', 20)
    await store.upsert_sessions([{**flow.booked, 'user_booked': None}], box_id=73)
    await store.record_external_cancellation(77, booking, confirmed_late=True)
    assert (await store.get_training_outcome(77))['source'] == 'external_sync'
    await notify_pending(flow.engine)
    assert flow.engine.notifier.send.await_count == 1


async def test_standby_preview_does_not_check_late_cancellation(flow):
    await flow.engine.store.upsert_sessions([{**flow.booked, 'user_booked': None,
        'user_in_standby': 789, 'booking_option': 'cancelWaitList'}], box_id=73)
    r = await flow.http.get('/api/schedule/77/cancellation', headers=flow.headers)
    assert r.status_code == 200 and r.json()['booking_id'] == 789
    assert r.json()['late_cancel'] is False and r.json()['standby'] is True
    flow.client._request.assert_not_awaited()


async def test_later_fee_confirmation_preserves_already_observed_user_intent(flow):
    store = flow.engine.store
    await store.remember_cancellation_request(77, 123, 'work')
    await store.upsert_sessions([{**flow.booked, 'user_booked': None}], box_id=73)
    await store.record_external_cancellation(77, 123)
    await store.set_meta('cancel_request:73:77:123', {
        'requested_at': (datetime.now() - timedelta(hours=25)).isoformat(), 'reason_code': 'work'})
    await store.record_external_cancellation(77, 123, confirmed_late=True)
    assert (await store.get_training_outcome(77))['source'] == 'external_requested'
    await notify_pending(flow.engine)
    flow.engine.notifier.send.assert_not_awaited()


@pytest.mark.parametrize('crosses_deadline', [False, True])
async def test_free_preview_cannot_silently_authorize_a_late_cancellation(flow, crosses_deadline):
    flow.client._request.side_effect = [
        'OK', late_refusal() if crosses_deadline else 'OK',
        {'data': {**flow.booked, 'user_booked': None, 'booking_option': 'insertScheduleUser'}}]
    preview = await flow.http.get('/api/schedule/77/cancellation', headers=flow.headers)
    assert preview.status_code == 200 and preview.json()['late_cancel'] is False
    r = await flow.http.post('/api/cancel', headers=flow.headers, json={
        'schedule_id': 77, 'booking_id': 123, 'late_cancel': False, 'reason_code': 'work'})
    assert r.status_code == (409 if crosses_deadline else 200)
    if crosses_deadline:
        assert 'late_cancel_required' in r.json()['detail']
        assert (await flow.engine.store.get_session(77))['user_booked'] == 123
        assert flow.client._request.await_count == 2  # read only; never delete
    else:
        assert flow.client._request.await_count == 3
