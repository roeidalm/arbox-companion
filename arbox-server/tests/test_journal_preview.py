"""Rehearse real callback routes without involving an account or workout."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.journal_preview import JournalPreview


def last_action(notifier, prefix):
    buttons = notifier.send_preview.call_args.args[2]
    return next(b["data"] for row in buttons for b in row if b["data"].startswith(prefix))


async def press(preview, notifier, prefix, channel="ha", text=None):
    action, cid = last_action(notifier, prefix).split(":")
    return await preview.callback(action, cid, channel, text)


@pytest.mark.parametrize("channel", ["ha", "telegram"])
@pytest.mark.parametrize("level", ["quick", "feedback", "full"])
async def test_all_levels_complete_on_only_the_requested_channel(channel, level):
    notifier = SimpleNamespace(send_preview=AsyncMock())
    preview = JournalPreview(notifier)
    await preview.start(channel, level)
    if level == "full":
        await press(preview, notifier, "preview_feedback", channel)
    result = await press(preview, notifier, "preview_coach_pos", channel)
    if level != "quick":
        result = await press(preview, notifier, "preview_class_neutral", channel)
    if level == "full":
        if channel == "ha":
            result = await press(preview, notifier, "preview_notes", channel, "סקוואט 60 ק״ג 3x8")
        else:
            await press(preview, notifier, "preview_notes", channel)
            result = await preview.message("סקוואט 60 ק״ג 3x8")
        assert "סקוואט" in result
        assert "תרגילים/שורות שזוהו" in result
    assert "לא נשמר דבר" in result
    assert "מאמן/ת: חיובי" in result
    assert not preview.sessions
    assert all(c.args[0] == channel for c in notifier.send_preview.call_args_list)


async def test_duplicate_cross_channel_and_invalid_buttons_cannot_advance():
    notifier = SimpleNamespace(send_preview=AsyncMock())
    preview = JournalPreview(notifier)
    await preview.start("ha", "feedback")
    action, cid = last_action(notifier, "preview_coach_pos").split(":")
    await preview.callback(action, cid, "telegram")
    await preview.callback("preview_class_pos", cid, "ha")
    assert notifier.send_preview.await_count == 1
    await asyncio.gather(*(preview.callback(action, cid, "ha") for _ in range(2)))
    assert notifier.send_preview.await_count == 2


async def test_send_failure_leaves_current_step_retryable():
    notifier = SimpleNamespace(send_preview=AsyncMock())
    preview = JournalPreview(notifier)
    await preview.start("ha", "feedback")
    action, cid = last_action(notifier, "preview_coach_pos").split(":")
    notifier.send_preview.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError):
        await preview.callback(action, cid, "ha")
    assert preview.sessions[cid]["answers"] == {}
    notifier.send_preview.side_effect = None
    await preview.callback(action, cid, "ha")
    assert cid not in preview.sessions


async def test_skip_expiry_restart_and_replacement():
    notifier = SimpleNamespace(send_preview=AsyncMock())
    preview = JournalPreview(notifier)
    await preview.start("ha", "full")
    old_action, old_cid = last_action(notifier, "preview_feedback").split(":")
    await preview.start("telegram", "quick")
    await preview.start("ha", "feedback")
    assert len(preview.sessions) == 2
    assert "פגה" in await preview.callback(old_action, old_cid, "ha")
    assert "לדלג" in await press(preview, notifier, "preview_skip")
    for state in preview.sessions.values():
        state["expires"] = time.monotonic() - 1
    assert await preview.message("unarmed text") is None
    assert not preview.sessions
    assert "פגה" in await JournalPreview(notifier).callback(old_action, old_cid, "ha")


async def test_full_notes_require_explicit_input_and_ha_can_retry_missing_text():
    notifier = SimpleNamespace(send_preview=AsyncMock())
    preview = JournalPreview(notifier)
    await preview.start("ha", "full")
    assert await preview.message("not a Telegram rehearsal") is None
    assert "reply_text" in await press(preview, notifier, "preview_notes")
    assert "לא נשמר דבר" in await press(preview, notifier, "preview_notes", text="מתח 3x6")
    await preview.start("telegram", "full")
    assert await preview.message("not armed yet") is None
    await press(preview, notifier, "preview_notes", "telegram")
    assert "4,000" in await preview.message("x" * 4001)
    assert "לדלג" in await preview.message("/cancel")
    assert await preview.message("ordinary message") is None
