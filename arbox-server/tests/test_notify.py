import pytest

from app.notify import _telegram_button


def test_telegram_button_supports_navigation_links_and_callbacks():
    assert _telegram_button({
        "text": "View details",
        "uri": "https://arbox.example/mine",
    }) == {
        "text": "View details",
        "url": "https://arbox.example/mine",
    }
    assert _telegram_button({
        "text": "Book",
        "data": "book:opaque-id",
    }) == {
        "text": "Book",
        "callback_data": "book:opaque-id",
    }


def test_telegram_button_rejects_an_unusable_shape():
    with pytest.raises(ValueError, match="needs data, uri, or url"):
        _telegram_button({"text": "Broken"})


async def test_notice_formatting_uses_utf16_entities_without_parsing_class_names(tmp_path):
    from unittest.mock import AsyncMock
    from app.notify import Notifier
    from app.settings import Settings

    settings = Settings(str(tmp_path))
    settings.update({'telegram': {'enabled': True, 'bot_token': 'test', 'chat_id': 'test'}})
    sent = []
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self, **kwargs): return {'ok': True}
    class Transport:
        def post(self, url, json):
            sent.append(json)
            return Response()
    notifier = Notifier(settings)
    notifier._http = AsyncMock(return_value=Transport())
    heading, when = '🎟️ המכסה מלאה', 'חמישי 24.9 · 10:00'
    text = f'{heading}\n\n{when}\nHS & <Movement>\n\n\nנשמרו בתכנון · ממתינים למכסה'
    await notifier._send_telegram(text, None, bold_lines=[heading, when])
    payload = sent[0]
    assert payload['text'] == text
    assert 'parse_mode' not in payload
    encoded = text.encode('utf-16-le')
    rendered = [encoded[e['offset']*2:(e['offset']+e['length'])*2].decode('utf-16-le') for e in payload['entities']]
    assert rendered == [heading, when]


async def test_journal_uses_one_selected_channel_without_escalation(tmp_path):
    from unittest.mock import AsyncMock
    from app.notify import Notifier
    from app.settings import Settings

    settings = Settings(str(tmp_path))
    settings.update({"ha": {"enabled": True, "webhook_url": "http://ha/api/webhook/private", "kinds": ["journal"]},
                     "telegram": {"enabled": True, "bot_token": "test", "chat_id": "test", "kinds": ["journal"]},
                     "notify": {"order": ["ha", "telegram"], "escalation_minutes": 2}})
    notifier = Notifier(settings)
    notifier._send_to = AsyncMock(return_value=True)
    selected = notifier.journal_form_channel()
    assert selected == "ha"
    buttons = [[{"text": "משוב", "uri": "/arbox-feedback#opaque"}]]
    assert await notifier.send_journal_form("Workout", buttons, channel=selected)
    notifier._send_to.assert_awaited_once_with(["ha"], "Workout", buttons)
    assert not notifier._esc_tasks
    settings.update({"ha": {"enabled": False}, "telegram": {"enabled": False}})
    assert notifier.journal_form_channel() is None
