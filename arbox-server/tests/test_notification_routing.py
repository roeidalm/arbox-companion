import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from app.settings import Settings
from app.notify import Notifier
from app.store import Store
from app.notification_health import NotificationHealth
from app.notification_context import notification_kind


@pytest.fixture
async def system(tmp_path):
    settings = Settings(str(tmp_path))
    settings.update({'telegram': {'enabled': True, 'bot_token': 'secret', 'chat_id': '1', 'log_level': 'warn'},
                     'discord': {'enabled': True,'bot_token':'secret','channel_id':'2','guild_id':'3','allowed_user_id':'4','log_level':'warn'}})
    store = Store(str(tmp_path/'audit.db')); await store.open()
    notifier = Notifier(settings)
    notifier.log_event = store.log_event
    health = NotificationHealth(store, notifier); await health.start()
    store.on_event = notifier.push_event
    notifier._send_telegram = AsyncMock()
    notifier.discord_delivery.deliver = AsyncMock()
    notifier.discord_bot.connected = True
    yield settings, notifier, store, health
    await notifier.close(); await store.close()


async def test_empty_route_falls_back_and_different_kinds_stay_separate(system):
    settings,n,store,health = system
    settings.update({'telegram':{'routes':{'log':{'target':'9'},'digest':{'target':'   ','copy':True}}}})
    await n.send('night',kind='digest')
    n._send_telegram.assert_awaited_with('night',None)
    await n.push_event('warn','system','failure')
    assert n._send_telegram.call_args.kwargs == {'target':'9'}
    assert settings.notification_targets('telegram','attendance') == ['']
    settings.update({'telegram':{'routes':{'log':{'target':None}}}})
    assert settings.notification_targets('telegram','log') == ['']


async def test_copy_and_per_destination_failure_are_independent(system):
    settings,n,store,health = system
    settings.update({'telegram':{'routes':{'log':{'target':'9','copy':True}}}})
    n._send_telegram.side_effect = [None, RuntimeError('failed')]
    await n.push_event('warn','system','failure')
    status=(await health.snapshot())['telegram']
    assert status['state']=='failing'
    assert {x['target']:x['state'] for x in status['targets']} == {'1':'sent','9':'failed'}
    await health.record('telegram','1','digest','sent')
    assert (await health.snapshot())['telegram']['state']=='failing'
    await health.record('telegram','9','log','sent')
    assert (await health.snapshot())['telegram']['state']=='ok'


async def test_warn_filter_and_disabled_kind_are_respected(system):
    settings,n,store,health=system
    settings.update({'discord':{'log_level':'error'},'telegram':{'kinds':['digest']}})
    await n.push_event('warn','system','test')
    n._send_telegram.assert_not_awaited(); n.discord_delivery.deliver.assert_not_awaited()
    await n.push_event('error','system','test')
    n.discord_delivery.deliver.assert_awaited_once()
    assert any('סונן' in r['message'] for r in await store.list_events(source='notify'))


async def test_incidents_persist_dedupe_and_recover_without_recursion(system):
    settings,n,store,health=system
    n._send_telegram.side_effect=RuntimeError('offline')
    await health.record('discord','2','log','failed','http_403')
    await health.tick()
    assert n._send_telegram.await_count==1
    # Failed alternate reporting must not create a second channel incident.
    assert (await health.snapshot())['telegram']['state']=='unknown'
    await health.tick(); assert n._send_telegram.await_count==1
    n._send_telegram.side_effect=None
    incident=await store.get_meta('notification_incident:delivery:discord')
    incident['last_notice']=0; await store.set_meta('notification_incident:delivery:discord',incident)
    await health.tick(); assert n._send_telegram.await_count==2
    replacement=NotificationHealth(store,n)
    await replacement.tick(); assert n._send_telegram.await_count==2
    await health.record('discord','2','log','sent')
    await replacement.tick(); assert n._send_telegram.await_count==3
    assert 'הסתיימה' in n._send_telegram.call_args.args[0]
    await replacement.tick(); assert n._send_telegram.await_count==3


async def test_gateway_grace_and_recovery(system,monkeypatch):
    settings,n,store,health=system
    clock=[100000.0];monkeypatch.setattr('app.notification_health.time.time',lambda:clock[0])
    n.discord_bot.connected=False
    await health.tick();n._send_telegram.assert_not_awaited()
    clock[0]+=119;await health.tick();n._send_telegram.assert_not_awaited()
    clock[0]+=2;await health.tick();assert n._send_telegram.await_count==1
    n.discord_bot.connected=True;await health.tick();assert n._send_telegram.await_count==2


async def test_old_failure_is_history_and_does_not_fake_recovery(system):
    settings,n,store,health=system
    await health.record('telegram','1','log','failed')
    await store.db.execute("UPDATE notification_status SET updated='2020-01-01T00:00:00'")
    await store.db.commit()
    assert (await health.snapshot())['telegram']['state']=='unknown'
    await health.tick();n.discord_delivery.deliver.assert_not_awaited()


async def test_secret_routes_masked_preserved_removed_and_validated(system):
    settings,*_=system
    url='https://discord.com/api/webhooks/123/secret'
    settings.update({'discord':{'routes':{'log':{'target':url,'copy':False}}}})
    assert settings.public_view()['discord']['routes']['log']['target']=='***'
    settings.update({'discord':{'routes':{'log':{'target':'***','copy':True}}}})
    assert settings.notification_targets('discord','log')==['',url]
    for invalid in [{'x':{'target':'123'}},{'log':{'target':'https://evil.test/hook'}},{'log':{'target':'3','copy':'yes'}}]:
        with pytest.raises(ValueError):settings.update({'discord':{'routes':invalid}})
    settings.update({'discord':{'routes':{'log':{'target':''}}}})
    assert settings.notification_targets('discord','log')==['']


async def test_event_range_counts_pagination_and_boundaries(system):
    settings,n,store,health=system
    for ts,level in [('2026-08-31 23:59:59','error'),('2026-09-01 00:00:00','warn'),('2026-09-30 23:59:59','error'),('2026-10-01 00:00:00','info')]:
        await store.db.execute('INSERT INTO events(ts,level,source,message) VALUES(?,?,?,?)',(ts,level,'system',ts))
    await store.db.commit()
    filters=dict(date_from='2026-09-01',date_to='2026-09-30',source='system')
    rows=await store.list_events(**filters,limit=1)
    assert rows[0]['ts']=='2026-09-30 23:59:59'
    page2=await store.list_events(**filters,before_id=rows[0]['id'])
    assert [r['ts'] for r in page2]==['2026-09-01 00:00:00']
    assert await store.event_counts(days=None,**filters)=={'info':0,'warn':1,'error':1}
    assert len(await store.list_events())==4


async def test_telegram_callback_returns_to_alternate_source(system):
    settings,n,*_=system
    settings.update({'telegram':{'routes':{'log':{'target':'9'}}}})
    n.on_callback=AsyncMock(return_value='done')
    await n._run_callback('test',target='9')
    n._send_telegram.assert_awaited_once_with('done',None,force=True,target='9')
