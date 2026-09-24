import copy
import json
import re
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock
import pytest
from app.google_calendar import GoogleCalendar, CalendarError, DEFAULTS, CALLBACK, SCOPE, validate_credentials, validate_preferences
from app.sync import ReentrantAsyncLock
from test_api import client, api_key

REDIRECT = 'https://server.example:8446' + CALLBACK
CREDS = {'web': {'client_id': '123.apps.googleusercontent.com', 'client_secret': 'test-secret',
    'project_id': 'test-project', 'redirect_uris': [REDIRECT], 'token_uri': 'https://evil.invalid'}}

@pytest.fixture
def calendar(tmp_path):
    lock = ReentrantAsyncLock()
    row = dict(schedule_id=42, date=(datetime.now()+timedelta(days=1)).date().isoformat(),
        start_time='08:00', end_time='09:00', category_name='Movement', coach_name='Coach', user_booked=None, user_in_standby=None)
    e = SimpleNamespace(client=SimpleNamespace(email='arbox@example.test', user_id=1),
        settings=SimpleNamespace(timezone='Asia/Jerusalem', base_url='http://server.example:8177'),
        syncer=SimpleNamespace(exclusive=lambda: lock, studio_name='Studio'),
        store=SimpleNamespace(active_box_id=73, get_sessions=AsyncMock(return_value=[row]),
            get_session=AsyncMock(return_value=row), training_history=AsyncMock(return_value=[]), get_meta=AsyncMock(return_value={})),
        _planned_sessions=AsyncMock(return_value=[{**row, 'planning_source':'scheduled'}]),
        quota_status=AsyncMock(return_value={'plan_states': {'42': {'state':'ready'}}}))
    g=GoogleCalendar(tmp_path,e)
    g.profile().update(credentials=validate_credentials(CREDS,REDIRECT,'server.example'), refresh_token='test-refresh',calendar_id='test-calendar',enabled=True, palette_checked_at=time.time())
    return g

def test_validation():
    assert 'token_uri' not in validate_credentials(CREDS,REDIRECT,'server.example')
    for u in [None, REDIRECT+'?x=1', 'http://server.example'+CALLBACK, 'https://evil.invalid'+CALLBACK]:
        with pytest.raises(ValueError): validate_credentials(CREDS,u,'server.example')
    with pytest.raises(ValueError): validate_credentials({'installed':CREDS['web']},REDIRECT,'server.example')
    assert validate_preferences(DEFAULTS)['booked']['reminders']==[60,30]
    for value in [[True],[1.5],[-1],[40321],list(range(6))]:
        prefs=copy.deepcopy(DEFAULTS);prefs['booked']['reminders']=value
        with pytest.raises(ValueError):validate_preferences(prefs)

def test_status_storage_isolation(calendar):
    calendar.save()
    assert calendar.path.stat().st_mode&0o777==0o600
    result=json.dumps(calendar.status())
    assert 'test-secret' not in result and 'test-refresh' not in result
    assert GoogleCalendar(calendar.path.parent,calendar.engine).profile()['calendar_id']=='test-calendar'
    calendar.engine.store.active_box_id=74
    assert not calendar.status()['connected']
    calendar.engine.store.active_box_id=73;calendar.engine.client.email='other@example.test'
    assert not calendar.status()['connected']

def begin(g):
    ticket=parse_qs(urlsplit(g.begin('http://server.example:8177/settings')).query)['ticket'][0]
    url,cookie=g.start(ticket)
    return parse_qs(urlsplit(url).query),cookie

@pytest.mark.asyncio
async def test_oauth_binding_replay_and_context(calendar):
    q,cookie=begin(calendar)
    assert q['code_challenge_method']==['S256'] and q['access_type']==['offline']
    calendar.request=AsyncMock()
    with pytest.raises(ValueError):await calendar.callback(q['state'][0],'code','wrong')
    with pytest.raises(ValueError):await calendar.callback(q['state'][0],'code',cookie)
    q,cookie=begin(calendar);calendar.engine.store.active_box_id=74
    with pytest.raises(ValueError):await calendar.callback(q['state'][0],'code',cookie)
    calendar.request.assert_not_awaited()

@pytest.mark.asyncio
async def test_oauth_account_change(calendar):
    q,cookie=begin(calendar);calendar.profile()['google_sub']='old'
    calendar.request=AsyncMock(side_effect=[{'access_token':'access','refresh_token':'new','scope':SCOPE},{'sub':'new','email':'google@example.test'}])
    assert await calendar.callback(q['state'][0],'code',cookie)=='http://server.example:8177/settings'
    p=calendar.profile()
    assert 'calendar_id' not in p and not p['enabled'] and p['refresh_token']=='new'
    assert calendar.request.call_args_list[0].kwargs['data']['code_verifier']

@pytest.mark.asyncio
async def test_projection_precedence(calendar):
    p=calendar.profile();desired=await calendar.desired(p);eid,body=next(iter(desired.items()))
    assert re.fullmatch('[0-9a-v]{5,1024}',eid) and body['colorId']=='9'
    calendar.engine._planned_sessions.return_value[0]['planning_source']='autobook'
    assert (await calendar.desired(p))[eid]['colorId']=='6'
    calendar.engine.quota_status.return_value['plan_states']['42']['state']='needs_review'
    assert (await calendar.desired(p))[eid]['colorId']=='11'
    calendar.engine.store.get_sessions.return_value[0]['user_booked']=12
    booked=await calendar.desired(p)
    assert booked[eid]['colorId']=='10'
    assert booked[eid]['start']['dateTime'].endswith(('+03:00','+02:00'))
    assert booked[eid]['reminders']['overrides']==[{'method':'popup','minutes':60},{'method':'popup','minutes':30}]
    p['preferences']['booked']['enabled']=False
    assert await calendar.desired(p)=={}

@pytest.mark.asyncio
async def test_disabled(calendar):
    calendar.profile()['enabled']=False;calendar.google=AsyncMock()
    await calendar.sync()
    calendar.google.assert_not_awaited();calendar.engine._planned_sessions.assert_not_awaited()

@pytest.mark.asyncio
async def test_retry_update_cancel_replan(calendar):
    remote={};calls=[]
    async def google(p,method,path,**kw):
        calls.append(method);eid=path.rsplit('/',1)[-1]
        if method=='POST':
            eid=kw['json']['id']
            if eid in remote:raise CalendarError('conflict',409)
            remote[eid]=copy.deepcopy(kw['json'])
            if len(remote)==1 and calls.count('POST')==1:raise CalendarError('timeout')
            return remote[eid]
        if method=='GET':return remote[eid]
        if method=='PATCH':remote[eid].update(kw['json']);return remote[eid]
        if method=='DELETE':remote[eid]={'status':'cancelled'};return {}
    calendar.google=google
    await calendar.sync();assert calendar.profile()['error']
    await calendar.sync();assert not calendar.profile()['error'] and len(remote)==1
    first=next(iter(remote));assert calls==['POST','POST','GET','PATCH']
    await calendar.sync();assert calls==['POST','POST','GET','PATCH']
    calendar.engine.store.get_sessions.return_value[0]['user_booked']=12
    await calendar.sync();assert len(remote)==1 and remote[first]['colorId']=='10'
    calendar.engine.store.get_sessions.return_value[0]['user_booked']=None
    plans=calendar.engine._planned_sessions.return_value;calendar.engine._planned_sessions.return_value=[]
    await calendar.sync();assert remote[first]['status']=='cancelled'
    calendar.engine._planned_sessions.return_value=plans
    await calendar.sync();assert len(remote)==2 and len(calendar.profile()['events'])==1 and not calendar.profile()['error']

@pytest.mark.asyncio
async def test_uncertain_calendar_creation(calendar):
    p=calendar.profile();p.pop('calendar_id');p['enabled']=False
    calendar.google=AsyncMock(side_effect=CalendarError('timeout'))
    with pytest.raises(CalendarError):await calendar.enable()
    with pytest.raises(ValueError):await calendar.enable()
    assert calendar.google.await_count==1 and p['creation_pending']
    calendar.google=AsyncMock(return_value={'id':'wrong','description':'foreign'})
    with pytest.raises(ValueError):await calendar.recover_calendar('wrong')
    calendar.google.return_value={'id':'right','description':'Arbox Companion connection '+p['owner']}
    await calendar.recover_calendar('right');assert p['enabled'] and p['calendar_id']=='right'

@pytest.mark.asyncio
async def test_invalid_grant(calendar):
    calendar.request=AsyncMock(side_effect=CalendarError('bad grant',400))
    with pytest.raises(CalendarError,match='מחדש'):await calendar.access(calendar.profile())

def test_api_auth_validation_secrets(client):
    g=client.app.state.google_calendar;g.engine.client.email='user@example.test';g.engine.store.active_box_id=73
    g.engine.settings.update({'base_url':'http://server.example:8177'})
    h={'X-Api-Key':api_key(client)}
    for route in ['credentials','preferences','connect','enable','pause','disconnect','recover','sync']:
        assert client.post('/api/calendar/google/'+route,json={}).status_code==401
    assert client.get('/api/calendar/google/status').status_code==401
    for value in [[],None,'bad']:
        assert client.post('/api/calendar/google/credentials',json=value,headers=h).status_code==422
    assert client.post('/api/calendar/google/credentials',content='x'*32769,headers=h).status_code==413
    r=client.post('/api/calendar/google/credentials',json={'credentials':CREDS,'redirect_uri':REDIRECT},headers=h)
    assert r.status_code==200 and r.json()['uploaded'] and 'test-secret' not in r.text
    assert r.headers['cache-control']=='no-store'
    r=client.post('/api/calendar/google/connect',headers=h)
    ticket=parse_qs(urlsplit(r.json()['url']).query)['ticket'][0]
    r=client.get('/api/calendar/google/start?ticket='+ticket,follow_redirects=False)
    assert r.status_code==303
    cookie=r.headers['set-cookie'];assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie
    assert client.get('/api/calendar/google/start?ticket='+ticket).status_code==400

@pytest.mark.asyncio
async def test_definite_creation_failure_can_retry(calendar):
    p=calendar.profile();p.pop('calendar_id');p['enabled']=False
    calendar.google=AsyncMock(side_effect=CalendarError('forbidden',403))
    with pytest.raises(CalendarError):await calendar.enable()
    assert not p['creation_pending']
    calendar.google=AsyncMock(return_value={'id':'new-calendar'})
    await calendar.enable();assert p['enabled'] and p['calendar_id']=='new-calendar'

@pytest.mark.asyncio
async def test_foreign_event_is_never_patched_or_deleted(calendar):
    calendar.google=AsyncMock(return_value={})
    await calendar.sync()
    calendar.engine._planned_sessions.return_value=[]
    calendar.google.reset_mock()
    calendar.google.return_value={'extendedProperties':{'private':{'arbox_owner':'foreign'}}}
    await calendar.sync()
    assert calendar.profile()['error']
    assert [c.args[1] for c in calendar.google.call_args_list]==['GET']

@pytest.mark.parametrize('origin,expected',[
    ('http://testserver','http://testserver/settings?google_calendar=connected'),
    ('https://evil.example','http://server.example:8177/settings?google_calendar=connected'),
    ('http://testserver/evil','http://server.example:8177/settings?google_calendar=connected'),
])
def test_oauth_returns_to_validated_browser_origin(client,origin,expected):
    g=client.app.state.google_calendar;g.engine.client.email='user@example.test';g.engine.store.active_box_id=73
    g.engine.settings.update({'base_url':'http://server.example:8177'})
    g.profile()['credentials']=validate_credentials(CREDS,REDIRECT,'server.example')
    r=client.post('/api/calendar/google/connect',headers={'X-Api-Key':api_key(client),'Origin':origin})
    assert r.status_code==200
    ticket=parse_qs(urlsplit(r.json()['url']).query)['ticket'][0]
    assert g.pending[ticket]['return']==expected

@pytest.mark.asyncio
async def test_description_and_color_changes_update_existing_event(calendar):
    from app.ical import calendar_description
    row=calendar.engine.store.get_sessions.return_value[0]
    row.update(category_bio='תיאור האימון\nלכל הרמות', registered=8, max_users=15, user_booked=12)
    p=calendar.profile();calendar.google=AsyncMock(return_value={})
    await calendar.sync()
    first=calendar.google.call_args_list[0].kwargs['json']
    assert first['description'].startswith(calendar_description(row)+'\n\n')
    assert 'תיאור האימון\nלכל הרמות\n8/15 רשומים' in first['description']
    p['preferences']['booked']['color']='3'
    calendar.google.reset_mock();calendar.google.return_value=first
    await calendar.sync()
    assert [c.args[1] for c in calendar.google.call_args_list]==['GET','PATCH']
    patch=calendar.google.call_args_list[-1]
    assert patch.args[2].endswith('/'+first['id'])
    assert patch.kwargs['json']['colorId']=='3'
    assert patch.kwargs['json']['description']==first['description']
    assert len(p['events'])==1


@pytest.mark.asyncio
async def test_calendar_palette_migrates_and_updates_same_event(calendar):
    p = calendar.profile()
    p.pop('palette_checked_at')
    blue, extra = 'blue-label-uuid', 'extra-label-uuid'
    labels = [{'id': blue, 'backgroundColor': '#3f51b5'},
              {'id': extra, 'backgroundColor': '#009688', 'name': 'My eucalyptus'}]
    remote = {}
    writes = []
    async def google(profile, method, path, **kw):
        if path == '/calendars/test-calendar':
            return {'labelProperties': {'eventLabels': labels}}
        eid = path.rsplit('/', 1)[-1]
        if method == 'POST':
            body = kw['json']; remote[body['id']] = copy.deepcopy(body)
        elif method == 'GET':
            return remote[eid]
        elif method == 'PATCH':
            remote[eid].update(kw['json'])
        if method in ('POST', 'PATCH'):
            assert kw['params'] == {'eventLabelVersion': '1'}
            assert 'colorId' not in kw['json']
            writes.append(method)
        return {}
    calendar.google = google
    await calendar.sync()
    assert not p['error']
    assert calendar.status()['palette'][1]['name'] == 'My eucalyptus'
    eid = next(iter(remote))
    assert remote[eid]['eventLabelId'] == blue
    prefs = copy.deepcopy(p['preferences']); prefs['scheduled']['color'] = extra
    with pytest.raises(ValueError): validate_preferences(prefs)
    p['preferences'] = validate_preferences(prefs, p['palette'])
    await calendar.sync()
    assert remote[eid]['eventLabelId'] == extra and len(remote) == 1
    assert writes == ['POST', 'PATCH']
    # A removed label must not silently reset an event's color.
    p['palette_checked_at'] = 0; labels.pop()
    await calendar.sync()
    assert p['error'] and writes == ['POST', 'PATCH']


def test_monitoring_status_has_no_credentials_or_account_email(calendar):
    p=calendar.profile();p['email']='private@example.test'
    public=calendar.monitoring_status()
    assert public['state']=='active'
    assert set(public)=={'state','connected','enabled','last_sync','error','event_count','settings_url'}
    p['error']='Sync failed'
    assert calendar.monitoring_status()['state']=='error'
    p['error']=None;p['enabled']=False
    assert calendar.monitoring_status()['state']=='paused'
    p.pop('refresh_token')
    assert calendar.monitoring_status()['state']=='disconnected'


def test_manual_sync_rejects_inactive_connection(client):
    g=client.app.state.google_calendar;g.engine.client.email='user@example.test';g.engine.store.active_box_id=73
    response=client.post('/api/calendar/google/sync',headers={'X-Api-Key':api_key(client)})
    assert response.status_code==409
