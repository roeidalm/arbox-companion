from unittest.mock import AsyncMock

import httpx2 as httpx
import pytest
from fastapi import FastAPI

from app.api import router
from test_membership_policy import engine, raw


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
