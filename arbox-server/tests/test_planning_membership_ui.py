import hashlib
import json
from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from fastapi import FastAPI

from app.api import router
from app.membership_policy import fingerprint
from test_membership_policy import engine, raw


async def shop_inventory(engine, *, manual_confirmation=False):
    """Use real inventory/policy/history refresh, mocking only upstream reads.

    A zero advance window prevents booking probes; these tests exercise an
    explicit membership selection without making or simulating a registration.
    """
    sid, category = (78, 2) if manual_confirmation else (77, 1)
    await engine.store.upsert_sessions([raw(sid, category, enable_registration_time=0)], box_id=73)
    await engine.store.watch(sid)
    engine.syncer.refresh_profile = AsyncMock()
    sections = [{'header': 'Frequency Limitations', 'values': ['Up to 4 sessions per month']}]
    if not manual_confirmation:
        sections.append({'header': 'Available Classes', 'values': ['Class 1']})
    engine.client.membership_details = AsyncMock(return_value={'limitations': sections})
    member = next(m for m in await engine.store.get_meta('memberships') if m['id'] == 20)
    await engine.store.set_meta(engine.membership_policy.key(20), {
        'source': 'shop', 'fingerprint': fingerprint(member),
        # Existing evidence for another class must not imply category 2 access.
        'booked_categories': {'1': {'schedule_id': 1, 'booking_id': 2}},
    })
    choices = await engine.planning_actions.membership_options(sid)
    chosen = next(o for o in choices['options'] if o['id'] == 20)
    assert chosen['available'] and chosen['manual'] == manual_confirmation
    return sid, chosen, member


@pytest.mark.asyncio
async def test_real_manual_policy_refresh_only_changes_timestamp_and_still_confirms(engine):
    await engine.store.watch(77)
    engine.syncer.refresh_profile = AsyncMock()
    choices = await engine.planning_actions.membership_options(77, refresh=False)
    chosen = next(o for o in choices['options'] if o['id'] == 20)
    member = next(m for m in await engine.store.get_meta('memberships') if m['id'] == 20)
    before = await engine.membership_policy.get(member)
    result = await engine.planning_actions.submit_membership(77, chosen['token'])
    after = await engine.membership_policy.get(member)
    assert {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)} == {'checked_at'}
    assert result['ok'] and result['membership_user_id'] == 20
    engine.syncer.refresh_profile.assert_awaited_once()
    engine.client.book.assert_not_awaited()
    engine.notifier.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('manual_confirmation', [False, True])
async def test_real_shop_refresh_keeps_eligibility_and_manual_confirmation(engine, manual_confirmation):
    sid, chosen, member = await shop_inventory(engine, manual_confirmation=manual_confirmation)
    before = await engine.membership_policy.get(member)
    if manual_confirmation:
        with pytest.raises(ValueError, match='אישור מפורש'):
            await engine.planning_actions.submit_membership(sid, chosen['token'])
    result = await engine.planning_actions.submit_membership(
        sid, chosen['token'], confirm_category=manual_confirmation)
    after = await engine.membership_policy.get(member)
    assert before['checked_at'] != after['checked_at']
    assert before['verified_at'] != after['verified_at']
    assert result['ok'] and result['membership_user_id'] == 20
    if manual_confirmation:
        assert after['confirmed_category_ids'] == [2]
        assert not after['categories_known']
    assert (await engine.store.list_watchlist(pending_only=True))[0]['membership_user_id'] == 20
    assert engine.syncer.refresh_profile.await_count == 2
    assert engine.client.membership_details.await_count == 2
    engine.client.book.assert_not_awaited()
    engine.notifier.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['expiry', 'category', 'limit', 'balance'])
async def test_real_refresh_rejects_changed_terms_and_rechecks_capacity(engine, change):
    sid, chosen, member = await shop_inventory(engine)
    if change in ('expiry', 'balance'):
        async def refresh_profile():
            members = await engine.store.get_meta('memberships')
            members[1].update({'end': '2026-09-23'} if change == 'expiry' else {'sessions_left': 0})
            await engine.store.set_meta('memberships', members)
        engine.syncer.refresh_profile.side_effect = refresh_profile
    else:
        sections = [{'header': 'Frequency Limitations', 'values': [
            'Up to 3 sessions per month' if change == 'limit' else 'Up to 4 sessions per month']},
            {'header': 'Available Classes', 'values': ['Class 3'] if change == 'category' else ['Class 1']}]
        engine.client.membership_details.return_value = {'limitations': sections}
        # Test explicit revocation without independent prior-booking evidence.
        if change == 'category':
            key = engine.membership_policy.key(20)
            saved = await engine.store.get_meta(key)
            saved.pop('booked_categories')
            await engine.store.set_meta(key, saved)
            choices = await engine.planning_actions.membership_options(sid, refresh=False)
            chosen = next(o for o in choices['options'] if o['id'] == 20)
    expected = ('פרטי המנוי השתנו' if change == 'expiry' else
                'המכסה' if change == 'balance' else 'תנאי הזכאות או מגבלות השימוש')
    with pytest.raises(ValueError, match=expected):
        await engine.planning_actions.submit_membership(sid, chosen['token'])
    assert (await engine.store.list_watchlist(pending_only=True))[0]['membership_user_id'] is None
    assert not (await engine.store.peek_prompt(chosen['token']))['answered_at']
    engine.client.book.assert_not_awaited()
    engine.notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_whole_record_hash_requires_reopening_without_refresh_or_assignment(engine):
    await engine.store.watch(77)
    choices = await engine.planning_actions.membership_options(77, refresh=False)
    member = next(m for m in await engine.store.get_meta('memberships') if m['id'] == 20)
    chosen = next(o for o in choices['options'] if o['id'] == 20)
    prompt = await engine.store.peek_prompt(chosen['token'])
    context = json.loads(prompt['payload'])
    context['policy_digest'] = hashlib.sha256(json.dumps(
        await engine.membership_policy.get(member), sort_keys=True).encode()).hexdigest()
    await engine.store.add_prompt('old-choice', 77, 'planning_action', payload=json.dumps(context))
    engine.syncer.refresh_profile = AsyncMock()
    with pytest.raises(ValueError, match='לפני עדכון המערכת'):
        await engine.planning_actions.submit_membership(77, 'old-choice')
    engine.syncer.refresh_profile.assert_not_awaited()
    result = await engine.planning_actions.callback('old-choice')
    assert 'לפני עדכון המערכת' in result.text and not result.result
    assert (await engine.store.list_watchlist(pending_only=True))[0]['membership_user_id'] is None
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_web_and_ha_api_saves_manual_category_after_real_refresh(engine):
    sid, _, member = await shop_inventory(engine, manual_confirmation=True)
    app = FastAPI(); app.include_router(router)
    app.state.settings = engine.settings; app.state.syncer = engine.syncer
    app.state.store = engine.store; app.state.rules_engine = engine
    engine.syncer.exclusive = lambda: engine._tick_lock
    headers = {'X-Api-Key': engine.settings.api_key, 'X-Arbox-Studio-Id': '73'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/api/planning/{sid}/membership-options', json={}, headers=headers)
        assert response.status_code == 200
        chosen = next(o for o in response.json()['options'] if o['id'] == member['id'])
        assert chosen['manual']
        endpoint = f'/api/planning/{sid}/membership'
        denied = await client.post(endpoint, json={'token': chosen['token']}, headers=headers)
        assert denied.status_code == 409 and 'אישור מפורש' in denied.json()['detail']
        accepted = await client.post(endpoint, json={'token': chosen['token'], 'confirm_category': True}, headers=headers)
        assert accepted.status_code == 200 and accepted.json()['ok']
        duplicate = await client.post(endpoint, json={'token': chosen['token'], 'confirm_category': True}, headers=headers)
        assert duplicate.status_code == 409 and 'כבר טופלה' in duplicate.json()['detail']
    assert (await engine.membership_policy.get(member))['confirmed_category_ids'] == [2]
    engine.client.book.assert_not_awaited()
    engine.notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_automatic_occurrence_can_choose_and_reset_membership(engine):
    await engine.store.save_rule({'name':'Class', 'enabled':True, 'mode':'autobook',
        'categories':['Class 1'], 'coaches':[], 'weekdays':[]})
    engine.refresh_membership_inventory = AsyncMock()
    choices = await engine.planning_actions.membership_options(77)
    chosen = next(o for o in choices['options'] if o['id'] == 20)
    assert chosen['available']
    result = await engine.planning_actions.submit_membership(77, chosen['token'])
    assert result['ok'] and result['membership_user_id'] == 20
    assert (await engine.store.list_watchlist(pending_only=True))[0]['membership_user_id'] == 20
    assert (await engine.store.list_rules())[0]['mode'] == 'autobook'
    engine.client.book.assert_not_awaited()
    engine.notifier.send.assert_not_awaited()
    with pytest.raises(ValueError, match='כבר טופלה'):
        await engine.planning_actions.submit_membership(77, chosen['token'])
    choices = await engine.planning_actions.membership_options(77, refresh=False)
    automatic = next(o for o in choices['options'] if o['id'] is None)
    await engine.planning_actions.submit_membership(77, automatic['token'])
    assert (await engine.store.list_watchlist(pending_only=True))[0]['membership_user_id'] is None


@pytest.mark.asyncio
async def test_unknown_card_requires_explicit_confirmation_of_only_that_category(engine):
    await engine.store.watch(78)
    await engine.store.set_meta(engine.membership_policy.key(20), {})
    engine.refresh_membership_inventory = AsyncMock()
    choices = await engine.planning_actions.membership_options(78)
    chosen = next(o for o in choices['options'] if o['id'] == 20)
    assert chosen['manual'] and chosen['available']
    with pytest.raises(ValueError, match='אישור מפורש'):
        await engine.planning_actions.submit_membership(78, chosen['token'])
    assert (await engine.store.list_watchlist(pending_only=True))[0]['membership_user_id'] is None
    await engine.planning_actions.submit_membership(78, chosen['token'], confirm_category=True)
    member = next(m for m in await engine.store.get_meta('memberships') if m['id'] == 20)
    policy = await engine.membership_policy.get(member)
    assert policy['confirmed_category_ids'] == [2]
    assert not policy['categories_known']


@pytest.mark.asyncio
@pytest.mark.parametrize('change,reason', [
    ({'start':'2026-10-01'}, 'אחרי האימון'),
    ({'end':'2026-09-23'}, 'לפני האימון'),
    ({'active':False}, 'אינו פעיל'),
])
async def test_invalid_membership_dates_are_disabled_without_action_token(engine, change, reason):
    await engine.store.watch(77)
    members = await engine.store.get_meta('memberships'); members[1].update(change)
    await engine.store.set_meta('memberships', members)
    choices = await engine.planning_actions.membership_options(77, refresh=False)
    chosen = next(o for o in choices['options'] if o['id'] == 20)
    assert not chosen['available'] and 'token' not in chosen and reason in chosen['reason']


@pytest.mark.asyncio
async def test_future_membership_is_valid_on_its_start_date_not_before(engine):
    await engine.store.upsert_sessions([raw(80, day='2026-10-01')], box_id=73)
    await engine.store.watch(77); await engine.store.watch(80)
    members = await engine.store.get_meta('memberships'); members[1]['start'] = '2026-10-01'
    await engine.store.set_meta('memberships', members)
    from app.membership_policy import fingerprint
    policy = await engine.store.get_meta(engine.membership_policy.key(20))
    policy['fingerprint'] = fingerprint(members[1])
    await engine.store.set_meta(engine.membership_policy.key(20), policy)
    sep = await engine.planning_actions.membership_options(77, refresh=False)
    oct = await engine.planning_actions.membership_options(80, refresh=False)
    assert not next(o for o in sep['options'] if o['id'] == 20)['available']
    assert next(o for o in oct['options'] if o['id'] == 20)['available']


@pytest.mark.asyncio
async def test_save_refreshes_inventory_and_rejects_new_expiry(engine):
    await engine.store.watch(77)
    choices = await engine.planning_actions.membership_options(77, refresh=False)
    chosen = next(o for o in choices['options'] if o['id'] == 20)
    async def expired(**kwargs):
        assert kwargs == {'force':True}
        members = await engine.store.get_meta('memberships'); members[1]['end'] = '2026-09-23'
        await engine.store.set_meta('memberships', members)
    engine.refresh_membership_inventory = AsyncMock(side_effect=expired)
    with pytest.raises(ValueError, match='פרטי המנוי השתנו'):
        await engine.planning_actions.submit_membership(77, chosen['token'])
    assert (await engine.store.list_watchlist(pending_only=True))[0]['membership_user_id'] is None
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_membership_refresh_is_quiet_and_debounced_but_save_can_force(engine):
    engine.syncer.refresh_profile = AsyncMock()
    engine.refresh_planning_evidence = AsyncMock()
    await engine.refresh_membership_inventory()
    await engine.refresh_membership_inventory()
    engine.syncer.refresh_profile.assert_awaited_once()
    engine.refresh_planning_evidence.assert_awaited_once_with(force_history=True, force_policy=True)
    await engine.refresh_membership_inventory(force=True)
    assert engine.syncer.refresh_profile.await_count == 2
    engine.notifier.send.assert_not_awaited()
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoints_require_key_and_current_studio(engine):
    app = FastAPI(); app.include_router(router)
    app.state.settings = engine.settings; app.state.syncer = engine.syncer
    app.state.store = engine.store; app.state.rules_engine = engine
    engine.syncer.exclusive = lambda: engine._tick_lock
    engine.refresh_membership_inventory = AsyncMock()
    await engine.store.watch(77)
    key = engine.settings.api_key
    headers = {'X-Api-Key':key, 'X-Arbox-Studio-Id':'73'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for endpoint,body in [('/api/memberships/refresh',{}),
                              ('/api/planning/77/membership-options',{}),
                              ('/api/planning/77/membership',{'token':'unknown'})]:
            assert (await client.post(endpoint,json=body)).status_code == 401
            assert (await client.post(endpoint,json=body,headers={**headers,'X-Arbox-Studio-Id':'74'})).status_code == 409
        response = await client.post('/api/planning/77/membership-options', json={}, headers=headers)
        assert response.status_code == 200
        token = next(o['token'] for o in response.json()['options'] if o['id'] == 20)
        assert (await client.post('/api/planning/78/membership', json={'token':token}, headers=headers)).status_code == 409
        result = await client.post('/api/planning/77/membership', json={'token':token}, headers=headers)
        assert result.status_code == 200 and result.json()['ok']
