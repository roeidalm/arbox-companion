import json
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.registration_learning import DEFAULTS,effective,should_wait,validate,RegistrationLearning,opening
from app.settings import Settings
from app.store import Store,flatten_session


def session(now):
    start=now+timedelta(days=7)
    return {'schedule_id':1,'box_id':73,'date':start.date().isoformat(),'start_time':start.strftime('%H:%M'), 'advance_hours':168}

def test_defaults_master_switch_and_scoped_overrides():
    s=session(datetime.now());c={**DEFAULTS,'overrides':{'session:73:1':{'mode':'wait','threshold_percent':40}}}
    assert effective(c,s)['mode']=='immediate'
    c['enabled']=True
    assert effective(c,s)['threshold_percent']==40
    assert effective(c,{**s,'box_id':74})['threshold_percent']==30
    c['overrides']['rule:73:5']={'mode':'immediate'}
    assert effective(c,{**s,'schedule_id':2},[{'id':5}])['mode']=='immediate'
    assert effective(c,s,[{'id':5}])['mode']=='wait'

@pytest.mark.parametrize('registered,seconds,wait',[(0,20,True),(3,20,True),(4,20,False),(9,20,False),(0,600,False)])
def test_threshold_and_timeout(registered,seconds,wait):
    now=datetime(2026,9,22,18,0)+timedelta(seconds=seconds)
    s=session(datetime(2026,9,22,18,0));c=effective({**DEFAULTS,'enabled':True},s)
    sample={'observed_at':now.isoformat(),'capacity':12,'registered':registered}
    assert should_wait(c,s,now,sample)==wait
    assert not should_wait(c,s,now,None)
    assert not should_wait(c,s,now,{**sample,'observed_at':(now-timedelta(seconds=90)).isoformat()})

@pytest.mark.parametrize('patch',[{'threshold_percent':0},{'threshold_percent':51},{'timeout_minutes':11},{'enabled':1},{'overrides':{'bad':{}}}])
def test_invalid_settings_rejected(tmp_path,patch):
    s=Settings(str(tmp_path))
    with pytest.raises(ValueError):s.update({'registration_timing':patch})
    assert s._data['registration_timing']==DEFAULTS

@pytest.fixture
async def prepared(tmp_path):
    store=Store(str(tmp_path/'data.db'));await store.open();store.active_box_id=73
    now=datetime.now().replace(second=0,microsecond=0);start=now+timedelta(days=7)
    raw={'id':1,'date':start.date().isoformat(),'time':start.strftime('%H:%M'),'enable_registration_time':168,
         'max_users':12,'registered':3,'user_booked':777,'box_categories':{'id':8,'name':'Movement'},'series':{'id':9}}
    await store.upsert_sessions([raw],box_id=73)
    settings=Settings(str(tmp_path));settings.update({'registration_timing':{'learning_enabled':True,'learning_window_minutes':10}})
    client=SimpleNamespace(schedule_between=AsyncMock(return_value=[raw]))
    sync=SimpleNamespace(box_id=73,location_id=1,client=client)
    engine=SimpleNamespace(store=store,settings=settings,syncer=sync,watchlist_tick=AsyncMock(),autobook_tick=AsyncMock())
    learning=RegistrationLearning(engine)
    yield learning,engine,raw
    await store.close()

async def test_learning_is_read_only_and_records_own_booking(prepared):
    learning,e,raw=prepared
    before=await e.store.get_session(1)
    await learning.tick()
    e.autobook_tick.assert_not_awaited();e.watchlist_tick.assert_not_awaited()
    assert await e.store.get_session(1)==before
    report=await learning.report();assert report['courses'][0]['openings']==1
    assert report['courses'][0]['observations'][0]['points'][0]['own_booking']
    assert not report['courses'][0]['observations'][0]['complete']
    assert report['recent_runs'][0]['status']=='ok'

async def test_disabled_learning_only_tracks_targets(prepared):
    learning,e,raw=prepared;e.settings.update({'registration_timing':{'learning_enabled':False}})
    await learning.tick();e.syncer.client.schedule_between.assert_not_awaited()
    await e.store.save_rule({'name':'Movement','mode':'autobook','enabled':True})
    await learning.tick();e.syncer.client.schedule_between.assert_awaited_once()
    e.autobook_tick.assert_not_awaited()

async def test_failed_reads_do_not_invent_samples(prepared):
    learning,e,raw=prepared;e.syncer.client.schedule_between.side_effect=RuntimeError('network')
    await learning.tick();report=await learning.report()
    assert not report['courses'] and report['recent_runs'][0]['status']=='failed'

async def test_changed_opening_is_not_recorded_under_old_time(prepared):
    learning,e,raw=prepared;e.syncer.client.schedule_between.return_value=[{**raw,'enable_registration_time':24}]
    await learning.tick();assert not (await learning.report())['courses']

async def test_waiting_tick_is_separate_and_can_be_enabled(prepared):
    learning,e,raw=prepared;e.settings.update({'registration_timing':{'enabled':True}})
    await e.store.save_rule({'name':'Movement','mode':'autobook','enabled':True})
    await learning.tick();e.autobook_tick.assert_awaited_once()
    s=await e.store.get_session(1)
    assert await learning.defer(s)
    e.settings.update({'registration_timing':{'enabled':False}})
    assert not await learning.defer(s)

async def test_pinned_target_is_sampled_without_broad_learning(prepared):
    learning,e,raw=prepared
    e.settings.update({'registration_timing':{'learning_enabled':False}})
    await e.store.watch(1)
    await learning.tick()
    e.syncer.client.schedule_between.assert_awaited_once()
    e.watchlist_tick.assert_not_awaited()

async def test_report_distinguishes_full_coverage_and_keeps_history(prepared):
    learning,e,raw=prepared
    await learning.tick()
    row=dict(await (await e.store.db.execute('SELECT * FROM registration_samples')).fetchone())
    await e.store.db.execute('DELETE FROM registration_samples')
    opens=datetime.fromisoformat(row['opens_at'])
    for seconds in range(0,601,30):
        values=(row['box_id'],row['schedule_id'],row['opens_at'],(opens+timedelta(seconds=seconds)).isoformat(),
                seconds,12,min(12,seconds//30),seconds>=90,row['cohort'],row['label'])
        await e.store.db.execute('INSERT INTO registration_samples VALUES(?,?,?,?,?,?,?,?,?,?)',values)
    await e.store.db.commit()
    restarted=RegistrationLearning(e)
    report=await restarted.report()
    window=report['courses'][0]['observations'][0]
    assert window['complete'] and window['samples']==21
    assert window['first_observed_threshold_seconds']=={'30':120,'70':270,'100':360}
    assert window['own_booking_first_observed_seconds']==90
    await e.store.db.execute('DELETE FROM registration_samples WHERE elapsed BETWEEN 90 AND 180')
    await e.store.db.commit()
    assert not (await restarted.report())['courses'][0]['observations'][0]['complete']

def test_learning_settings_survive_reload_without_enabling_waiting(tmp_path):
    settings=Settings(str(tmp_path))
    settings.update({'registration_timing':{'learning_enabled':True,'threshold_percent':40}})
    reloaded=Settings(str(tmp_path))
    assert reloaded._data['registration_timing']['learning_enabled']
    assert reloaded._data['registration_timing']['threshold_percent']==40
    assert not reloaded._data['registration_timing']['enabled']

@pytest.mark.parametrize('counts,reason,seconds',[
    ([0,1,4,5], 'threshold',40),
    ([0,1,2,2], 'timeout',60),
    ([0,1,2,4], 'insufficient_data',60),
])
def test_saved_policy_outcome_distinguishes_deadline_and_threshold(counts,reason,seconds):
    from app.registration_learning import observation_outcome
    opens=datetime(2026,9,22,8)
    points=[{'elapsed':t,'capacity':10,'registered':n} for t,n in zip([0,20,40,65],counts)]
    result=observation_outcome(points,{'threshold_percent':30,'timeout_minutes':1},opens+timedelta(minutes=2),opens)
    assert result['reason']==reason and result['seconds']==seconds
    assert result['simulation']

async def test_policy_snapshot_and_all_sample_rows_survive_settings_changes(prepared):
    learning,e,raw=prepared
    await learning.tick()
    first=(await learning.report())['courses'][0]['observations'][0]
    assert first['policy']['learning_window_minutes']==10
    e.settings.update({'registration_timing':{'learning_window_minutes':30,'timeout_minutes':2,'threshold_percent':40}})
    await learning.tick()
    report=await learning.report();window=report['courses'][0]['observations'][0]
    assert window['samples']==2
    assert window['policy']==first['policy']
    assert all(p['occupancy_percent']==25 for p in window['points'])
    assert len({p['observed_at'] for p in window['points']})==2

async def test_actual_wait_release_is_recorded_separately(prepared):
    learning,e,raw=prepared
    e.settings.update({'registration_timing':{'enabled':True}})
    e.syncer.client.schedule_between.return_value=[{**raw,'registered':8}]
    await learning.sample(await e.store.get_sessions())
    assert not await learning.defer(await e.store.get_session(1))
    window=(await learning.report())['courses'][0]['observations'][0]
    assert window['decisions'][0]['reason']=='threshold'

def test_jittered_sampling_intervals_stay_between_twenty_and_forty_seconds():
    from apscheduler.triggers.interval import IntervalTrigger
    from datetime import timezone
    trigger=IntervalTrigger(seconds=20,jitter=20,timezone=timezone.utc)
    previous=datetime.now(timezone.utc)
    gaps=[]
    for _ in range(30):
        next_time=trigger.get_next_fire_time(previous,previous)
        gaps.append((next_time-previous).total_seconds());previous=next_time
    assert all(20<=gap<=40 for gap in gaps)
    assert len(set(gaps))>1

async def test_half_hour_learning_continues_after_ten_minutes_and_uses_saved_window(prepared):
    learning,e,raw=prepared
    start=datetime.now().replace(second=0,microsecond=0)+timedelta(days=7,minutes=-20)
    raw={**raw,'date':start.date().isoformat(),'time':start.strftime('%H:%M')}
    await e.store.upsert_sessions([raw],box_id=73)
    e.syncer.client.schedule_between.return_value=[raw]
    e.settings.update({'registration_timing':{'learning_window_minutes':30}})
    await learning.tick()
    window=(await learning.report())['courses'][0]['observations'][0]
    assert window['points'][0]['seconds']>=1200
    assert window['policy']['learning_window_minutes']==30
    e.settings.update({'registration_timing':{'learning_window_minutes':1}})
    await learning.tick()
    assert (await learning.report())['courses'][0]['observations'][0]['samples']==2
    e.autobook_tick.assert_not_awaited()
    e.watchlist_tick.assert_not_awaited()

async def test_full_class_stops_sampling_even_after_restart_or_cancellation(prepared):
    learning,e,raw=prepared
    e.syncer.client.schedule_between.return_value=[{**raw,'registered':12}]
    await learning.tick()
    assert e.syncer.client.schedule_between.await_count==1
    # A later cancellation does not restart this completed learning window.
    e.syncer.client.schedule_between.return_value=[{**raw,'registered':10}]
    await RegistrationLearning(e).tick()
    assert e.syncer.client.schedule_between.await_count==1
    report=await learning.report();window=report['courses'][0]['observations'][0]
    assert window['stop_reason']=='full' and window['samples']==1
    e.autobook_tick.assert_not_awaited();e.watchlist_tick.assert_not_awaited()

async def test_full_class_does_not_stop_observing_other_classes(prepared):
    learning,e,raw=prepared
    other={**raw,'id':2,'registered':1}
    await e.store.upsert_sessions([other],box_id=73)
    e.syncer.client.schedule_between.return_value=[{**raw,'registered':12},other]
    await learning.tick();await learning.tick()
    rows=await (await e.store.db.execute('SELECT schedule_id,COUNT(*) n FROM registration_samples GROUP BY schedule_id')).fetchall()
    assert {r['schedule_id']:r['n'] for r in rows}=={1:1,2:2}

@pytest.mark.parametrize('times,counts,risky,reason',[
    ([25],[12],True,'full_first_sample'),
    ([400],[12],False,'late_first_sample'),
    ([10,40,70],[0,4,12],True,'short_observed_margin'),
    ([10,40],[0,12],True,'short_observed_margin'),
    ([10,400],[0,12],False,'no_fast_fill_evidence'),
    ([10,40,70,100,130],[0,4,6,9,12],False,'no_fast_fill_evidence'),
])
def test_fast_fill_warning_requires_observed_evidence(times,counts,risky,reason):
    from app.registration_learning import fill_risk
    result=fill_risk([{'elapsed':t,'capacity':12,'registered':n} for t,n in zip(times,counts)],{'threshold_percent':30})
    assert result['recommend_immediate']==risky and result['reason']==reason
