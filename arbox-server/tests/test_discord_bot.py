from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import discord
import pytest
from app.settings import Settings
from app.notify import Notifier
from app.discord_bot import custom_id, decode_id, render_bot
from app.notification_reply import NotificationReply

@pytest.fixture
def notifier(tmp_path):
    s=Settings(str(tmp_path));s.update({'discord':{'enabled':True,'bot_token':'test-secret','guild_id':'1','channel_id':'2','allowed_user_id':'3'}})
    n=Notifier(s);n.on_callback=AsyncMock(return_value=NotificationReply('נשמר',[[{'text':'הבא','data':'plan:next'}]]))
    n._log=AsyncMock()
    return n

def interaction(data='plan:one',user=3,channel=2,guild=1):
    return SimpleNamespace(type=discord.InteractionType.component,application_id=4,
        user=SimpleNamespace(id=user),channel_id=channel,guild_id=guild,
        data={'custom_id':custom_id(data,'test-secret')},
        message=SimpleNamespace(author=SimpleNamespace(id=4),edit=AsyncMock(),channel=SimpleNamespace(send=AsyncMock())),
        response=SimpleNamespace(defer=AsyncMock(),send_message=AsyncMock(),send_modal=AsyncMock()),
        edit_original_response=AsyncMock())

def test_render_readable_buttons_and_limits():
    buttons=[[{'text':f'בחירה {i}','data':f'plan:{i}'}] for i in range(31)]
    parts=render_bot('אימון **מחר** 🏋️'*300,buttons,'key')
    controls=[b for p in parts for row in p['components'] for b in row['components']]
    assert len(controls)==31 and all(len(p['components'])<=5 for p in parts)
    assert all(len(p['content'].encode('utf-16-le'))//2<=1800 for p in parts)
    assert '**מחר**' in parts[0]['content']
    assert all(p['allowed_mentions']=={'parse':[]} for p in parts)
    assert decode_id(controls[0]['custom_id'],'key')==('plan:0',False)
    assert decode_id(controls[0]['custom_id']+'x','key') is None
    assert decode_id(controls[0]['custom_id'],'rotated') is None

@pytest.mark.parametrize('change',[{'user':9},{'channel':9},{'guild':9}])
async def test_unauthorized_never_executes(notifier,change):
    i=interaction(**change);await notifier.discord_bot.interaction(i)
    notifier.on_callback.assert_not_awaited();i.response.send_message.assert_awaited_once()

async def test_bad_signature_never_executes(notifier):
    i=interaction();i.data['custom_id']='ab:bad:plan:one'
    await notifier.discord_bot.interaction(i);notifier.on_callback.assert_not_awaited()

async def test_callback_ack_then_existing_handler_and_edit(notifier):
    i=interaction()
    async def handler(*args,**kwargs):
        i.response.defer.assert_awaited_once()
        return NotificationReply('השלב הבא',[[{'text':'המשך','data':'plan:next'}]])
    notifier.on_callback.side_effect=handler
    await notifier.discord_bot.interaction(i)
    notifier.on_callback.assert_awaited_once_with('plan:one',source_channel='discord',reply_text=None)
    assert i.message.edit.call_args.kwargs['content']=='השלב הבא'
    assert i.message.edit.call_args.kwargs['view'].children[0].label=='המשך'

async def test_info_preserves_action_buttons(notifier):
    i=interaction('info:one');notifier.on_callback.return_value='תיאור'
    await notifier.discord_bot.interaction(i)
    i.message.edit.assert_not_awaited()
    i.edit_original_response.assert_awaited_once_with(content='תיאור')

async def test_text_input_opens_modal_without_action(notifier):
    i=interaction();i.data['custom_id']=custom_id('journal_notes:one','test-secret',True)
    await notifier.discord_bot.interaction(i)
    notifier.on_callback.assert_not_awaited();i.response.send_modal.assert_awaited_once()

async def test_failed_edit_never_reexecutes(notifier):
    i=interaction();i.message.edit.side_effect=RuntimeError('network')
    await notifier.discord_bot.interaction(i)
    assert notifier.on_callback.await_count==1
    assert 'טופלה' in i.edit_original_response.call_args.kwargs['content']

def test_secret_masking_and_file_priority(notifier,tmp_path,monkeypatch):
    s=notifier.settings;file=tmp_path/'token';file.write_text('file-secret')
    monkeypatch.setenv('ARBOX_DISCORD_BOT_TOKEN_FILE',str(file))
    assert s.discord_bot_token=='file-secret'
    d=s.public_view()['discord'];assert d['bot_token']=='***' and d['managed_bot_secret'] and d['bot_configured']
    s.update({'discord':{'bot_token':'***'}});assert s.discord['bot_token']=='test-secret'

async def test_bot_test_button_never_calls_rules(notifier):
    i=interaction('discord_test:ping');await notifier.discord_bot.interaction(i)
    notifier.on_callback.assert_not_awaited();assert 'לא בוצעה פעולה' in i.message.edit.call_args.kwargs['content']

async def test_modal_submits_text_to_same_handler(notifier):
    i=interaction();i.data['custom_id']=custom_id('reason_other:one','test-secret',True)
    await notifier.discord_bot.interaction(i)
    modal=i.response.send_modal.call_args.args[0]
    assert modal.answer.max_length==500
    modal.answer._value='לא הרגשתי טוב'
    submitted=interaction()
    await modal.on_submit(submitted)
    notifier.on_callback.assert_awaited_once_with('reason_other:one',source_channel='discord',reply_text='לא הרגשתי טוב')

async def test_duplicate_shared_prompt_claim_is_atomic(tmp_path):
    from app.store import Store
    s=Store(str(tmp_path/'test.db'));await s.open()
    try:
        await s.add_prompt('one',1,'book')
        results=await asyncio.gather(s.take_prompt('one'),s.take_prompt('one'))
        assert sum(x is not None for x in results)==1
    finally: await s.close()

async def test_bot_delivery_uses_bot_endpoint_and_native_components(notifier):
    from test_discord_notify import Transport,Response
    d=notifier.discord_delivery;d.http=Transport(Response())
    try:
        await d.deliver('test',[[{'text':'בדיקה','data':'discord_test:ping'}]])
        url,opts=d.http.calls[0]
        assert url=='https://discord.com/api/v10/channels/2/messages'
        assert opts['headers']['Authorization']=='Bot test-secret'
        assert opts['json']['components'][0]['components'][0]['label']=='בדיקה'
        assert (await d.status())['queued']==0
    finally: await d.close()
