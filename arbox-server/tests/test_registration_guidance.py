from datetime import datetime,timedelta
from unittest.mock import AsyncMock
from fastapi import FastAPI
import httpx2 as httpx
import pytest
from app.api import router
from app.registration_guidance import guidance,save_override,cohort_key
from test_registration_learning import prepared

async def test_guidance_preserves_master_switch_and_pin_priority(prepared):
    learning,e,raw=prepared;e.registration_learning=learning
    rule=await e.store.save_rule({'name':'Movement','mode':'autobook','enabled':True})
    save_override(e.settings,73,'rule',rule,'immediate')
    e.settings.update({'registration_timing':{'enabled':True}})
    normal=await guidance(e)
    pin=await guidance(e,as_pin=True)
    assert normal['sessions']['1']['policy']['mode']=='immediate'
    assert pin['sessions']['1']['policy']['mode']=='wait'
    assert normal['sessions']['1']['history']['openings']==0
    save_override(e.settings,73,'session',1,'immediate')
    assert (await guidance(e,as_pin=True))['sessions']['1']['policy']['mode']=='immediate'
    save_override(e.settings,73,'session',1,'inherit')
    assert (await guidance(e,as_pin=True))['sessions']['1']['policy']['mode']=='wait'
    e.settings.update({'registration_timing':{'enabled':False}})
    assert (await guidance(e,as_pin=True))['sessions']['1']['policy']['mode']=='immediate'
    e.autobook_tick.assert_not_awaited();e.watchlist_tick.assert_not_awaited()

async def test_guidance_reuses_cohort_history_for_future_occurrence(prepared):
    learning,e,raw=prepared;e.registration_learning=learning
    e.syncer.client.schedule_between.return_value=[{**raw,'registered':12}]
    await learning.tick()
    future={**raw,'id':2,'date':(datetime.fromisoformat(raw['date'])+timedelta(days=7)).date().isoformat(),'user_booked':None}
    await e.store.upsert_sessions([future],box_id=73)
    rule=await e.store.save_rule({'name':'Movement','mode':'autobook','enabled':True})
    data=await guidance(e)
    assert data['sessions']['2']['history']['openings']==1
    assert data['rules'][str(rule)]['history']['openings']==1 # two occurrences must not double-count
    assert data['sessions']['2']['status']=='before_open'
    assert data['sessions']['1']['status']=='booked'
    assert data['sessions']['2']['history']['cohorts']==[cohort_key(await e.store.get_session(1))]

async def test_guidance_matches_live_waiting_without_writing_decisions(prepared):
    learning,e,raw=prepared;e.registration_learning=learning
    await e.store.upsert_sessions([{**raw,'user_booked':None}],box_id=73)
    e.settings.update({'registration_timing':{'enabled':True}})
    data=await guidance(e,as_pin=True)
    assert data['sessions']['1']['status']=='waiting_occupancy'
    assert data['sessions']['1']['deadline_at']
    assert not await (await e.store.db.execute('SELECT * FROM registration_decisions')).fetchall()

async def test_scoped_override_api_authentication_and_no_booking(prepared):
    learning,e,raw=prepared;e.registration_learning=learning
    # These endpoints use the same reentrant context lock as all studio mutations.
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def exclusive():yield
    e.syncer.exclusive=exclusive
    app=FastAPI();app.include_router(router)
    app.state.settings=e.settings;app.state.store=e.store;app.state.syncer=e.syncer;app.state.rules_engine=e
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        path='/api/registration-policy/session/1'
        assert (await client.put(path,json={'mode':'immediate'})).status_code==401
        headers={'X-Api-Key':e.settings.api_key,'X-Arbox-Studio-Id':'73'}
        assert (await client.put(path,json={'mode':'immediate'},headers=headers)).status_code==200
        assert e.settings._data['registration_timing']['overrides']=={'session:73:1':{'mode':'immediate'}}
        assert (await client.put(path,json={'mode':'wait'},headers=headers)).status_code==422
        assert (await client.put(path,json={'mode':'inherit'},headers={**headers,'X-Arbox-Studio-Id':'74'})).status_code==409
        assert (await client.put('/api/registration-policy/session/999',json={'mode':'immediate'},headers=headers)).status_code==404
        assert (await client.get('/api/registration-guidance',headers=headers)).status_code==200
        assert (await client.put(path,json={'mode':'inherit'},headers=headers)).status_code==200
        assert not e.settings._data['registration_timing']['overrides']
    e.autobook_tick.assert_not_awaited();e.watchlist_tick.assert_not_awaited()

async def test_pin_preflight_does_not_save_preference_until_confirmed(prepared):
    learning,e,raw=prepared;e.registration_learning=learning
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def exclusive():yield
    e.syncer.exclusive=exclusive;e._blocked=lambda session:False
    e.store.vacation_blocks=AsyncMock(return_value=True)
    e.review_plans=AsyncMock();e.schedule_openings=AsyncMock();e.reconcile_planned_quota=AsyncMock();e.quota_status=AsyncMock(return_value={})
    async def watch_tick():
        assert e.settings._data['registration_timing']['overrides']['session:73:1']['mode']=='immediate'
    e.watchlist_tick=AsyncMock(side_effect=watch_tick)
    app=FastAPI();app.include_router(router)
    app.state.settings=e.settings;app.state.store=e.store;app.state.syncer=e.syncer;app.state.rules_engine=e
    headers={'X-Api-Key':e.settings.api_key,'X-Arbox-Studio-Id':'73'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        body={'schedule_id':1,'timing_mode':'immediate'}
        response=await client.post('/api/watchlist',json=body,headers=headers)
        assert response.json()['needs_confirm']
        assert not e.settings._data['registration_timing']['overrides']
        assert not await e.store.list_watchlist()
        response=await client.post('/api/watchlist',json={**body,'ignore_vacation':True},headers=headers)
        assert response.status_code==200
        e.watchlist_tick.assert_awaited_once()
