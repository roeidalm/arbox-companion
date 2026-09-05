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
