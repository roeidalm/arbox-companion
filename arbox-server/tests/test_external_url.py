from test_api import client, api_key
from test_google_calendar import CREDS, REDIRECT

def test_external_origin_preserves_internal_and_rejects_unknown_hosts(client):
    headers={'X-Api-Key':api_key(client)}
    s=client.app.state.settings
    s.update({'base_url':'http://internal.example:8177','external_url':'https://public.example'})
    assert s.base_url=='http://internal.example:8177'
    assert s.browser_url=='https://public.example'
    for host in ('internal.example','public.example','arbox-server'):
        assert client.get('/api/settings',headers={**headers,'Host':host}).status_code==200
    assert client.get('/api/settings',headers={**headers,'Host':'evil.example'}).status_code==400
    s.update({'external_url':''})
    assert s.browser_url==s.base_url

def test_redirect_migration_preserves_calendar_and_tokens(client):
    state=client.app.state
    state.settings.update({'base_url':'http://internal.example:8177','external_url':'https://public.example'})
    state.store.active_box_id=73
    state.client.email='test@example.com'
    g=state.google_calendar
    p=g.profile()
    p.update(credentials={'client_id':'123.apps.googleusercontent.com','redirect_uri':REDIRECT},
             refresh_token='existing',calendar_id='same-calendar',events={'42':'same-event'})
    headers={'X-Api-Key':api_key(client)}
    path='/api/calendar/google/redirect'
    assert client.post(path,json={'redirect_uri':'https://evil.example/api/calendar/google/callback'},headers=headers).status_code==422
    response=client.post(path,json={'redirect_uri':'https://public.example/api/calendar/google/callback'},headers=headers)
    assert response.status_code==200
    assert p['refresh_token']=='existing' and p['calendar_id']=='same-calendar' and p['events']=={'42':'same-event'}
    assert client.post(path,json={}).status_code==401
