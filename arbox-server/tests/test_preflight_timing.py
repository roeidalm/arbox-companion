import time
import pytest
from app.arbox_client import ArboxError
from app.membership_policy import fingerprint, eligible
from test_membership_policy import engine
from test_membership_policy import raw

TIME = {'name':'registerScheduleDisabled','value':{'hours':168}}

@pytest.mark.parametrize('messages', [[], [TIME, {'name':'classTypeRestricts'}],
    [TIME, {'name':'unknownRestriction'}], [TIME, {}], [TIME, 'unexpected'],
    [{'name':'registerScheduleDisabled','value':{'hours':0}}]])
def test_only_complete_recognized_time_response_passes(messages):
    assert not ArboxError('no',status=425,body={'error':{'messageToUser':messages}}).timing_only()

@pytest.mark.asyncio
async def test_cached_timing_response_is_reused_even_when_daily_budget_is_full(engine):
    m=(await engine.store.get_meta('memberships'))[1]
    await engine.store.set_meta(engine.membership_policy.key(20), {'fingerprint':fingerprint(m)})
    await engine.store.watch(77,membership_user_id=20)
    key=engine.membership_policy.key(0)+':probes'
    await engine.store.set_meta(key, {
        f"20:{fingerprint(m)}:1":{'at':time.time(),'schedule_id':77,'status':425,'messages':[TIME]},
        'other':{'at':time.time()},
    })
    await engine.preflight_plans(); await engine.preflight_plans()
    engine.client.book.assert_not_awaited()
    assert (await engine.quota_status())['plan_states']['77']['state']=='ready'
    policy=await engine.membership_policy.get(m)
    assert eligible({**m,'policy':policy},{'category_id':1})
    assert not eligible({**m,'policy':policy},{'category_id':2})
    assert not (await engine.membership_policy.get({**m,'end':'2027-01-01'}))['preflight_category_ids']

@pytest.mark.asyncio
async def test_expiry_and_explicit_denial_override_early_check(engine):
    m=(await engine.store.get_meta('memberships'))[1]
    await engine.store.set_meta(engine.membership_policy.key(20), {'fingerprint':fingerprint(m)})
    s=await engine.store.get_session(77)
    err=ArboxError('early',status=425,body={'error':{'messageToUser':[TIME]}})
    assert not await engine.membership_policy.learn_preflight(m,s,err,checked_at=time.time()-8*86400)
    assert await engine.membership_policy.learn_preflight(m,s,err)
    await engine.membership_policy.learn_rejection(m,s,ArboxError('denied',status=425,body={'error':{'messageToUser':[
        {'name':'classTypeRestricts','value':{'allowedText':'Class 2'}}]}}))
    assert not eligible({**m,'policy':await engine.membership_policy.get(m)},s)
    assert not await engine.membership_policy.learn_preflight(m,s,err)

@pytest.mark.asyncio
async def test_picker_accepts_early_check_without_manual_confirmation(engine):
    m=(await engine.store.get_meta('memberships'))[1]
    await engine.store.set_meta(engine.membership_policy.key(20), {'fingerprint':fingerprint(m)})
    await engine.store.watch(77,membership_user_id=20)
    s=await engine.store.get_session(77)
    await engine.membership_policy.learn_preflight(m,s,ArboxError('early',status=425,body={'error':{'messageToUser':[TIME]}}))
    choices=await engine.planning_actions.membership_options(77,refresh=False)
    choice=next(o for o in choices['options'] if o['id']==20)
    assert choice['available'] and not choice['manual']

@pytest.mark.asyncio
async def test_probe_projection_keeps_other_confirmed_classes_in_capacity(engine):
    members=await engine.store.get_meta('memberships')
    m=members[1]
    m['sessions_on_purchase']=1
    m['sessions_left']=1
    await engine.store.set_meta('memberships',[m])
    await engine.store.set_meta(engine.membership_policy.key(20), {
        'fingerprint':fingerprint(m),'confirmed_category_ids':[2]})
    await engine.store.upsert_sessions([raw(79,2,day='2026-09-23')],box_id=73)
    await engine.store.watch(79,membership_user_id=20)
    await engine.store.watch(77,membership_user_id=20)
    await engine.preflight_plans()
    engine.client.book.assert_not_awaited()
