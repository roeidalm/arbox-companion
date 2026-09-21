from unittest.mock import AsyncMock
from test_api import client, api_key


def setup_view(client):
    client.app.state.syncer.box_id=73
    client.app.state.store.active_box_id=73
    admin={'X-Api-Key':api_key(client)}
    response=client.post('/api/view-access',headers=admin)
    assert response.status_code==200
    return {'X-Api-Key':response.json()['key']},admin


def test_view_credentials_never_authorize_admin_or_actions(client):
    key,admin=setup_view(client)
    assert client.get('/api/view/summary').status_code==401
    assert client.get('/api/view/summary',headers=key).status_code==200
    assert client.get('/api/settings',headers=key).status_code==401
    assert client.post('/api/refresh',headers=key).status_code==401
    assert client.post('/api/book',headers=key,json={'schedule_id':1}).status_code==401
    assert client.post('/api/view-access',headers=key).status_code==401
    assert client.delete('/api/view-access',headers=key).status_code==401
    assert client.post('/api/view/summary',headers=key).status_code==405
    assert client.get('/api/view/summary?refresh=true',headers=key).status_code==400
    client.post('/api/view-access',headers=admin)
    assert client.get('/api/view/summary',headers=key).status_code==401
    client.delete('/api/view-access',headers=admin)
    assert not client.app.state.settings.view_key


def test_all_views_are_read_only_and_hide_free_text(client):
    key,admin=setup_view(client)
    s=client.app.state
    s.syncer.refresh_membership=AsyncMock(side_effect=AssertionError('network'))
    s.store.get_sessions=AsyncMock(return_value=[])
    s.store.workout_journals=AsyncMock(return_value=[{'schedule_id':1,'date':'2026-09-21','coach_feedback':'positive','notes':'SECRET'}])
    s.rules_engine.quota_status=AsyncMock(return_value={'memberships':[{'id':1,'name':'card','email':'SECRET','policy':{'evidence':'SECRET','state':'verified'}}]})
    s.rules_engine._planned_sessions=AsyncMock(return_value=[])
    before=s.store.db.total_changes
    for section in ('summary','sessions','memberships','automations','calendar','history','reviews','events','status','settings','studio'):
        r=client.get('/api/view/'+section,headers=key)
        assert r.status_code==200,(section,r.text)
        assert 'SECRET' not in r.text
        assert api_key(client) not in r.text
        assert key['X-Api-Key'] not in r.text
        assert r.headers['cache-control']=='no-store'
    assert s.store.db.total_changes==before
    s.rules_engine.quota_status.assert_awaited_with(read_only=True)
    assert client.get('/api/view/events?limit=501',headers=key).status_code==422
    assert client.get('/api/view/sessions?date_from=2026-09-22&date_to=2026-09-21',headers=key).status_code==422
    assert client.get('/api/view/summary',headers={**key,'X-Arbox-Studio-Id':'74'}).status_code==409


def test_view_page_and_schema(client):
    key,_=setup_view(client)
    assert client.get('/view').status_code==200
    assert 'events' in client.get('/api/view',headers=key).json()['sections']
    assert client.get('/api/view/unknown',headers=key).status_code==404


def test_event_cursor_redacts_free_text_and_preserves_order(client):
    key,_=setup_view(client)
    async def seed():
        await client.app.state.store.db.execute("INSERT INTO events(level,source,message,detail) VALUES ('error','sync','secret@example.com','Bearer SECRET')")
        await client.app.state.store.db.execute("INSERT INTO events(level,source,message,detail) VALUES ('warn','PRIVATE-TOKEN','secret','secret')")
        await client.app.state.store.db.commit()
    client.portal.call(seed)
    first=client.get('/api/view/events?limit=1',headers=key).json()['data']
    assert first['has_more']
    second=client.get('/api/view/events',params={'after_id':first['next_after_id']},headers=key).json()['data']
    assert second['events'][0]['id']>first['events'][0]['id']
    assert all('message' not in r and 'detail' not in r for r in first['events']+second['events'])
    assert 'SECRET' not in str(first)+str(second)
    assert second['events'][0]['source']=='other'
