import asyncio
import json
from unittest.mock import AsyncMock
import aiohttp
import pytest
from app.settings import Settings
from app.notify import Notifier
from app.discord_notify import DiscordDelivery, render, webhook_url

URL='https://discord.com/api/webhooks/123/test-token'

class Response:
    def __init__(self,status=200,body=None,headers=None,error=None):
        self.status=status;self.body=body if body is not None else {'id':'1551675712849715363'};self.headers=headers or {};self.error=error
    async def __aenter__(self):
        if self.error:raise self.error
        return self
    async def __aexit__(self,*args):pass
    async def json(self):return self.body

class Transport:
    def __init__(self,*responses):self.responses=list(responses);self.calls=[]
    def post(self,url,**kwargs):
        self.calls.append((url,kwargs));return self.responses.pop(0)
    async def close(self):pass

@pytest.fixture
def settings(tmp_path):
    s=Settings(str(tmp_path));s.update({'discord':{'enabled':True,'webhook_url':URL},'external_url':'https://arbox.example.com'})
    return s

@pytest.mark.parametrize('url',['http://discord.com/api/webhooks/1/x','https://evil.test/api/webhooks/1/x','https://discord.com.evil.test/api/webhooks/1/x','https://discord.com/api/webhooks/1/x/messages/2','https://discord.com@evil.test/api/webhooks/1/x'])
def test_rejects_invalid_destinations(url):
    with pytest.raises(ValueError):webhook_url(url)

def test_render_hebrew_mentions_markdown_and_long_text():
    parts=render('אימון **חשוב** @everyone <@123>\n'*200,[[{'text':'book','data':'book:secret'}]],'https://app.example')
    assert len(parts)>1
    assert all(len(p['content'])<=2000 and p['allowed_mentions']=={'parse':[]} for p in parts)
    text=''.join(p['content'] for p in parts)
    assert '\\*\\*חשוב' in text and '@everyone' not in text and 'book:secret' not in text
    assert 'https://app.example/mine' in text

def test_wait_query_preserved():
    url=webhook_url(URL+'?thread_id=321&wait=false')
    assert 'thread_id=321' in url and 'wait=true' in url

def test_secret_file_masking_and_order(settings,tmp_path,monkeypatch):
    secret=tmp_path/'secret';secret.write_text(URL)
    monkeypatch.setenv('ARBOX_DISCORD_WEBHOOK_URL_FILE',str(secret))
    public=settings.public_view()['discord']
    assert public['webhook_url']=='***' and public['managed_secret']
    assert 'test-token' not in json.dumps(settings.public_view())
    settings.update({'notify':{'order':['discord','ha','telegram']}})
    assert settings.notify['order']==['discord','ha','telegram']
    with pytest.raises(ValueError):settings.update({'notify':{'order':['ha','ha']}})

@pytest.mark.asyncio
async def test_delivered_once_id_as_string(settings):
    d=DiscordDelivery(settings,AsyncMock());d.http=Transport(Response())
    try:
        await d.deliver('שלום',event_id='event-1');await d.deliver('שלום',event_id='event-1')
        assert len(d.http.calls)==1
        row=await (await d.db.execute('SELECT * FROM delivery')).fetchone()
        assert row['state']=='sent' and row['message_id']=='1551675712849715363' and row['payload']=='{}'
        assert d.http.calls[0][1]['allow_redirects'] is False
    finally:await d.close()

@pytest.mark.asyncio
async def test_rate_limit_is_durable_and_respects_retry_after(settings):
    d=DiscordDelivery(settings,AsyncMock());d.http=Transport(Response(429,{'retry_after':90}),Response())
    try:
        await d.deliver('queued',event_id='event-2')
        row=await (await d.db.execute('SELECT * FROM delivery')).fetchone()
        assert row['state']=='pending' and row['next_attempt']-row['created']>=90
        await d.drain();assert len(d.http.calls)==1
        await d.db.execute('UPDATE delivery SET next_attempt=0');await d.db.commit();await d.drain()
        assert (await d.status())['queued']==0
    finally:await d.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('status',[400,401,403,404])
async def test_permanent_failure_sanitized(settings,status):
    log=AsyncMock();d=DiscordDelivery(settings,log);d.http=Transport(Response(status,{'secret':URL}))
    try:
        with pytest.raises(RuntimeError):await d.deliver('test')
        await d.drain();assert len(d.http.calls)==1
        assert 'test-token' not in str(log.call_args_list)
        assert (await d.status())['failed']==1
    finally:await d.close()

@pytest.mark.asyncio
async def test_timeout_is_uncertain_not_retried(settings):
    d=DiscordDelivery(settings,AsyncMock());d.http=Transport(Response(error=asyncio.TimeoutError(URL)))
    try:
        with pytest.raises(RuntimeError):await d.deliver('test')
        await d.drain();assert len(d.http.calls)==1 and (await d.status())['unknown']==1
    finally:await d.close()

@pytest.mark.asyncio
async def test_retry_bound(settings):
    d=DiscordDelivery(settings,AsyncMock());d.http=Transport(Response(503),Response(503),Response(503))
    try:
        await d.deliver('test')
        for _ in range(3):
            await d.db.execute('UPDATE delivery SET next_attempt=0');await d.db.commit();await d.drain()
        assert len(d.http.calls)==3 and (await d.status())['failed']==1
    finally:await d.close()

@pytest.mark.asyncio
async def test_restart_preserves_pending_and_marks_inflight_uncertain(settings):
    d=DiscordDelivery(settings,AsyncMock());d.http=Transport(Response(429,{'retry_after':60}))
    await d.deliver('test',event_id='event');await d.close()
    d=DiscordDelivery(settings,AsyncMock());await d.start()
    assert (await d.status())['queued']==1
    await d.db.execute("UPDATE delivery SET state='sending'");await d.db.commit();await d.close()
    d=DiscordDelivery(settings,AsyncMock());await d.start()
    assert (await d.status())['unknown']==1
    await d.close()

@pytest.mark.asyncio
async def test_all_channels_and_failed_first_escalation(settings):
    settings.update({'telegram':{'enabled':True,'bot_token':'x','chat_id':'1'},'ha':{'enabled':True,'webhook_url':'http://ha'},'notify':{'order':['discord','telegram','ha'],'escalation_minutes':1}})
    n=Notifier(settings);n.discord_delivery.deliver=AsyncMock(side_effect=RuntimeError('failed'))
    n._send_telegram=AsyncMock();n._send_ha=AsyncMock();n._escalate=AsyncMock()
    try:
        assert await n.send('test',[[{'text':'reply','data':'book:1'}]],is_answered=AsyncMock(return_value=False))
        await asyncio.sleep(0)
        n._send_telegram.assert_awaited_once();n._send_ha.assert_not_awaited()
        assert n._escalate.call_args.args[0]==['ha']
    finally:await n.close()


def test_emoji_parts_fit_discord_limit():
    assert all(len(p['content'].encode('utf-16-le'))//2<=1800 for p in render('🏋️'*2000,None,''))

@pytest.mark.asyncio
async def test_rotated_destination_cannot_receive_old_queue(settings):
    d=DiscordDelivery(settings,AsyncMock());d.http=Transport(Response(429,{'retry_after':1}))
    try:
        await d.deliver('private old message')
        settings.update({'discord':{'webhook_url':URL+'new'}})
        await d.db.execute('UPDATE delivery SET next_attempt=0');await d.db.commit();await d.drain()
        assert len(d.http.calls)==1 and (await d.status())['failed']==1
    finally:await d.close()

@pytest.mark.asyncio
async def test_disabled_channel_pauses_pending_delivery(settings):
    d=DiscordDelivery(settings,AsyncMock());d.http=Transport(Response(429,{'retry_after':1}))
    try:
        await d.deliver('test');settings.update({'discord':{'enabled':False}})
        await d.db.execute('UPDATE delivery SET next_attempt=0');await d.db.commit();await d.drain()
        assert len(d.http.calls)==1 and (await d.status())['queued']==1
    finally:await d.close()
