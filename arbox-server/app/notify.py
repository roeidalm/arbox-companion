"""Notification channels, driven entirely by settings the user edits in the UI.

- telegram_direct: talks straight to the Telegram Bot API. Buttons are an
  inline keyboard; answers arrive via a getUpdates long-poll task running
  inside the server, so no public webhook and no HA dependency.
- ha_notify: POSTs to an HA webhook (no token, least privilege) whose
  automation forwards to notify.mobile_app_*; the pressed action comes back
  through a second HA automation that POSTs to /api/ha/callback.

Action-button callback data format (both channels):
"<action>:<callback_id>", where the callback_id maps to a pending_prompts
row. Link buttons carry uri/url instead. Buttons may carry channel hints:
tg_only (Telegram inline keyboards fit many buttons) and ha_only (the
companion app caps 3 actions, so HA gets one batch-info action).

Orchestration: every message has a kind (digest/autobook/standby/studio/
system). Channels opt in per kind, and an actionable message can escalate —
first channel only, then the rest if nothing was answered in time.
"""

from __future__ import annotations

import asyncio
import html
import re
import json
import logging
import time
from typing import Awaitable, Callable

import aiohttp

from .notification_context import notification_kind, health_notice, routed
from .settings import Settings
from .discord_notify import DiscordDelivery, DiscordDeliveryError, CHANNELS
from .discord_bot import DiscordBot
from .notification_reply import NotificationReply

_LOGGER = logging.getLogger(__name__)

TG_API = "https://api.telegram.org"

# handler(callback_id) -> answer text to show the user
CallbackHandler = Callable[..., Awaitable[str | NotificationReply]]
MessageHandler = Callable[..., Awaitable[str | None]]
EventLogger = Callable[..., Awaitable[None]]


_CHANNEL_HE = {"telegram": "טלגרם", "ha": "Home Assistant", "discord": "Discord"}


def _ha_action(b: dict) -> dict:
    """Companion-app action. A URI button opens a link; anything else posts
    its callback id back through the webhook automation."""
    if b.get("uri"):
        return {"action": "URI", "title": b["text"], "uri": b["uri"]}
    out = {"action": f"ARBOX_{b['data']}", "title": b["text"]}
    if b.get('authentication_required'):
        out['authenticationRequired'] = True
    if b.get("text_input"):
        out.update({"behavior": "textInput",
                    "textInputPlaceholder": b.get("placeholder") or "כתבו כאן…"})
    return out


def _telegram_button(b: dict) -> dict:
    """Translate the shared button shape to Telegram's inline-keyboard shape.

    Callback buttons use callback_data; navigation buttons use url.  Treating
    every shared button as a callback used to raise KeyError("data") for the
    late-cancel warning's "view details" URI, dropping the whole message.
    """
    out = {"text": b["text"]}
    link = b.get("uri") or b.get("url")
    if link:
        out["url"] = link
    elif b.get("data"):
        out["callback_data"] = b["data"]
    else:
        raise ValueError("Telegram button needs data, uri, or url")
    return out


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.audit = None
        self.discord_delivery = DiscordDelivery(settings, self._log)
        self.discord_delivery.audit = self._audit
        self.discord_bot = DiscordBot(self)
        self._session: aiohttp.ClientSession | None = None
        self._tg_task: asyncio.Task | None = None
        self._tg_offset = 0
        self.telegram_connected = None
        self._preview_telegram_until = 0.0
        self._tg_wakeup = asyncio.Event()
        self._esc_tasks: set[asyncio.Task] = set()
        # button presses run off the poll loop; held so they are not collected
        # mid-flight and are cancelled cleanly on shutdown
        self._cb_tasks: set[asyncio.Task] = set()
        self.on_callback: CallbackHandler | None = None
        # Free text is ignored unless the rules engine has explicitly armed
        # an "other reason" input. This keeps ordinary bot messages from
        # accidentally becoming personal attendance data.
        self.on_message: MessageHandler | None = None
        # Set by the app once the store exists. A delivery failure is the one
        # event that cannot be reported by sending a notification, so it has
        # to be written somewhere the user pulls from instead.
        self.log_event: EventLogger | None = None
        # Optional active-studio context. Kept out of Settings because studio
        # discovery is live Arbox state, not user-entered configuration.
        self.studio_context: Callable[[], tuple[str | None, int]] | None = None

    def _with_studio(self, text: str) -> str:
        if not self.studio_context:
            return text
        name, count = self.studio_context()
        if count > 1 and name:
            return f"📍 {name}\n{text}"
        return text

    async def _log(self, level: str, message: str, detail: str = "",
                   channel: str | None = None, source: str = "notify") -> None:
        if self.log_event:
            await self.log_event(level, source, message, detail or None,
                                 tag=channel)

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=70)
            )
        return self._session

    async def close(self) -> None:
        await self.discord_bot.close()
        await self.discord_delivery.close()
        if self._tg_task:
            self._tg_task.cancel()
        for t in (*self._esc_tasks, *self._cb_tasks):
            t.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------- sending

    def _configured(self, name: str) -> bool:
        if name == "telegram":
            tg = self.settings.telegram
            return bool(tg.get("bot_token") and tg.get("chat_id"))
        if name == "discord": return self.settings.discord_bot_configured or bool(self.settings.discord_webhook)
        return bool(self.settings.ha.get("webhook_url"))

    def _eligible(self, kind: str) -> list[str]:
        order = [c for c in (self.settings.notify.get("order") or [])
                 if c in CHANNELS]
        for c in CHANNELS:
            if c not in order:
                order.append(c)
        out = []
        for name in order:
            ch = getattr(self.settings, name)
            if not ch.get("enabled") or not self._configured(name):
                continue
            if kind != "system" and kind not in (ch.get("kinds") or []):
                continue
            out.append(name)
        return out

    @routed
    async def send(
        self,
        text: str,
        buttons: list[list[dict]] | None = None,
        kind: str = "system",
        is_answered=None,
        telegram_bold: list[str] | None = None,
    ) -> bool:
        """Send by kind through the channel orchestration.

        buttons: rows of {text, data, tg_only?, ha_only?}. is_answered: async
        callable -> bool; with it, buttons, and escalation_minutes > 0, only
        the first eligible channel gets the message now — the rest follow if
        nothing was answered in time.
        """
        text = self._with_studio(text)
        eligible = self._eligible(kind)
        if not eligible:
            await self._log("info", "התראה סוננה לפי ההגדרות", f"type={kind}; לא נבחר ערוץ פעיל ומוגדר")
            return False
        try:
            esc = int(self.settings.notify.get("escalation_minutes") or 0)
        except (TypeError, ValueError):
            esc = 0
        if self.settings.notify.get("delivery_spacing_minutes", 0) > 0 or not buttons or esc <= 0 or len(eligible) < 2 or is_answered is None:
            return await self._send_to(eligible, text, buttons, telegram_bold=telegram_bold)
        delivered = False
        rest = []
        for i, channel in enumerate(eligible):
            delivered = await self._send_to([channel], text, buttons, telegram_bold=telegram_bold)
            if delivered:
                rest = eligible[i + 1:]
                break
        if delivered and rest:
            task = asyncio.create_task(self._escalate(rest, text, buttons, esc, is_answered, telegram_bold))
            self._esc_tasks.add(task)
            task.add_done_callback(self._esc_tasks.discard)
        return delivered

    LEVEL_ICON = {"warn": "⚠️", "error": "❌"}
    LEVEL_HE = {"warn": "אזהרה", "error": "שגיאה"}

    async def push_event(self, level: str, source: str, message: str,
                         detail: str | None = None) -> None:
        """Announce one event-log entry, per channel, per severity.

        Not routed through send(): the "log" kind is filtered a second time by
        each channel's own minimum level, so one channel can take warnings
        while another takes only errors.
        """
        if level not in ("warn", "error"):
            return
        names = [n for n in self._eligible("log")
                 if self.settings.wants_log(n, level)]
        if not names:
            await self._log("info", "אירוע מערכת סונן לפי ההגדרות", f"level={level}; source={source}")
            return
        text = (f"{self.LEVEL_ICON.get(level, '•')} {self.LEVEL_HE.get(level, level)}"
                f" · {message}")
        if detail:
            text += f"\n{detail}"
        text = self._with_studio(text)
        token = notification_kind.set('log')
        try:
            return await self._send_to(names, text, None, immediate=True)
        finally:
            notification_kind.reset(token)

    @routed
    async def send_document(
        self,
        filename: str,
        content: bytes,
        caption: str = "",
        kind: str = "system",
        mime: str = "text/calendar",
        link: str = "",
        link_title: str = "",
        buttons: list[list[dict]] | None = None,
    ) -> None:
        """Deliver a file. Telegram uploads it; HA (no file channel) gets a
        notification with a URI action pointing at `link`, when one is given."""
        caption = self._with_studio(caption) if caption else caption
        names = self._eligible(kind)
        spacing = self.settings.notify.get("delivery_spacing_minutes", 0)
        if spacing and len(names) > 1:
            await self._send_document_channels(names[:1], filename, content, caption, mime, link, link_title, buttons, 0)
            async def later():
                await asyncio.sleep(spacing * 60)
                await self._send_document_channels(names[1:], filename, content, caption, mime, link, link_title, buttons, spacing)
            task = asyncio.create_task(later())
            self._esc_tasks.add(task)
            task.add_done_callback(self._esc_tasks.discard)
        else:
            await self._send_document_channels(names, filename, content, caption, mime, link, link_title, buttons, 0)

    async def _send_document_channels(self, names, filename, content, caption, mime, link, link_title, buttons, spacing):
        for index, name in enumerate(names):
            if index and spacing:
                await asyncio.sleep(spacing * 60)
            if not getattr(self.settings, name).get("enabled"):
                continue
            for target in self.settings.notification_targets(name, notification_kind.get()):
                extra = {'target': target} if target else {}
                label = self.target_label(name, target)
                try:
                    if name == "telegram":
                        await self._tg_document(filename, content, caption, mime, buttons, **extra)
                    elif name == "discord":
                        await self.discord_delivery.deliver(html.unescape(re.sub(r"</?b>", "**", caption)) if caption else filename,
                            [[{"text": link_title or filename, "uri": link}]] if link else buttons, **extra)
                    elif link:
                        await self._send_ha(caption or filename, [[{"text": link_title or "📅 הוסף ליומן", "uri": link}]], **extra)
                    else:
                        continue
                    if name != 'discord':
                        await self._audit(name,label,notification_kind.get(),'sent')
                except Exception as err:
                    if name != 'discord':
                        await self._audit(name,label,notification_kind.get(),'failed',type(err).__name__)
                    await self._log('error', f'שליחת קובץ ל-{_CHANNEL_HE.get(name,name)} נכשלה', type(err).__name__,channel=name)

    async def _tg_document(
        self, filename: str, content: bytes, caption: str, mime: str,
        buttons: list[list[dict]] | None = None, *, target: str = "",
    ) -> None:
        tg = self.settings.telegram
        form = aiohttp.FormData()
        form.add_field("chat_id", str(target or tg["chat_id"]))
        if len(caption.encode('utf-16-le')) // 2 > 1024:
            # Never truncate HTML in the middle of a tag/entity. Preserve the
            # complete caption as separate plain-text messages before the file.
            plain = html.unescape(re.sub(r'<[^>]+>', '', caption))
            for start in range(0, len(plain), 1800):
                await self._send_telegram(plain[start:start+1800], None, target=target)
            caption = ''
        if caption:
            # HTML so a long URL can hide behind a word instead of filling
            # half the screen; callers escape anything user-supplied
            form.add_field("caption", caption)
            form.add_field("parse_mode", "HTML")
        if buttons:
            # a link button beats a link in the text: nothing to read, nothing
            # to mis-tap, and the caption stays one clean line
            rows = [[{"text": b["text"], "url": b["url"]} for b in row
                     if b.get("url")] for row in buttons]
            rows = [r for r in rows if r]
            if rows:
                form.add_field("reply_markup",
                               json.dumps({"inline_keyboard": rows}))
        form.add_field("document", content, filename=filename, content_type=mime)
        session = await self._http()
        async with session.post(
            f"{TG_API}/bot{tg['bot_token']}/sendDocument", data=form
        ) as resp:
            body = await resp.json(content_type=None)
            if not body.get("ok"):
                raise RuntimeError(f"Telegram sendDocument: HTTP {resp.status}")

    async def _audit(self, channel, target, kind, state, error=None):
        if self.audit and not health_notice.get():
            try:
                await self.audit(channel, target, kind, state, error)
            except Exception:
                _LOGGER.exception('Could not record notification receipt')

    async def _deliver_channel(self, name, text, buttons, telegram_bold=None, *, require_all=False):
        kind = notification_kind.get()
        failures = []
        accepted = 0
        for target in self.settings.notification_targets(name, kind):
            extra = {'target': target} if target else {}
            label = self.target_label(name, target)
            try:
                if name == 'telegram':
                    await self._send_telegram(text, buttons, **extra,
                        **({'bold_lines': telegram_bold} if telegram_bold else {}))
                elif name == 'discord':
                    await self.discord_delivery.deliver(text, buttons, **extra)
                else:
                    await self._send_ha(text, buttons, **extra)
                accepted += 1
                if name != 'discord':
                    await self._audit(name, label, kind, 'sent')
            except Exception as err:
                if not isinstance(err, DiscordDeliveryError):
                    code = re.search(r'(?:HTTP |: |-> )(\d{3})\b', str(err))
                    await self._audit(name, label, kind, 'failed',
                                      'http_' + code[1] if code else type(err).__name__)
                failures.append(err)
        if failures and (not accepted or require_all):
            raise failures[0]

    def target_label(self, channel, target=''):
        import hashlib
        config = getattr(self.settings, channel)
        value = target or (config.get('channel_id') if channel == 'discord' else
                           config.get('chat_id') if channel == 'telegram' else config.get('webhook_url')) or ''
        if channel == 'discord' and (target.startswith('https://') or not self.settings.discord_bot_configured):
            try:
                return 'webhook:' + self.discord_delivery.destination(target)[1][:12]
            except (ValueError, RuntimeError):
                return 'primary'
        if '://' in value:
            return 'webhook:' + hashlib.sha256(value.encode()).hexdigest()[:12]
        return str(value) or 'primary'

    async def _send_to(
        self, names: list[str], text: str, buttons: list[list[dict]] | None,
        *, telegram_bold: list[str] | None = None, immediate: bool = False,
    ) -> bool:
        """Deliver to each channel; True if at least one took it.

        The return value is what lets a caller re-arm a prompt it already
        consumed: a flow that hands the user a button and then fails to send
        the message carrying it is dead with no way back.
        """
        spacing = 0 if immediate else self.settings.notify.get("delivery_spacing_minutes", 0)
        if spacing > 0 and len(names) > 1:
            await self._send_to(names[:1], text, buttons, telegram_bold=telegram_bold)
            task = asyncio.create_task(self._send_spaced(names[1:], text, buttons, spacing, telegram_bold))
            self._esc_tasks.add(task)
            task.add_done_callback(self._esc_tasks.discard)
            return True  # Later channels have accepted scheduled delivery.
        coros = [self._deliver_channel(n, text, buttons, telegram_bold) for n in names]
        results = await asyncio.gather(*coros, return_exceptions=True)
        failed = [n for n, r in zip(names, results) if isinstance(r, Exception)]
        for name, r in zip(names, results):
            if isinstance(r, Exception):
                _LOGGER.error("Notify via %s failed (%s)", name, type(r).__name__)
                # if another channel carried it, this is a warning; if every
                # channel failed, the message reached nobody
                await self._log(
                    "error" if len(failed) == len(names) else "warn",
                    f"שליחת התראה ל-{_CHANNEL_HE.get(name, name)} נכשלה",
                    type(r).__name__ + ("" if len(failed) == len(names)
                              else " · ההודעה נשלחה בערוץ אחר"),
                    channel=name,
                )
        return len(failed) < len(names)

    async def _send_spaced(self, names, text, buttons, minutes, telegram_bold=None):
        """Broadcast in priority order; answering does not cancel later copies."""
        for name in names:
            await asyncio.sleep(minutes * 60)
            if not getattr(self.settings, name).get("enabled"):
                continue
            if await self._send_to([name], text, buttons, telegram_bold=telegram_bold):
                await self._log("info", "התראה נשלחה לפי סדר הערוצים", channel=name)

    async def _escalate(
        self, rest: list[str], text: str, buttons, minutes: int, is_answered, telegram_bold=None
    ) -> None:
        await asyncio.sleep(minutes * 60)
        try:
            if await is_answered():
                _LOGGER.info("Escalation skipped — already answered")
                return
        except Exception as err:  # noqa: BLE001 — escalate anyway
            _LOGGER.error("is_answered check failed: %s", err)
        _LOGGER.info("Escalating unanswered message to: %s", rest)
        await self._send_to(rest, f"⏰ תזכורת — טרם נענה:\n{text}", buttons, telegram_bold=telegram_bold)

    async def send_test(self, channel: str, kind: str | None = None) -> str:
        text = "🧪 Arbox Companion — הודעת בדיקה בלבד. לא בוצעה פעולה באימון."
        if kind is not None:
            names = self._eligible(kind)
            if channel != 'routing':
                names = [n for n in names if n == channel]
            if not names:
                raise ValueError('סוג ההתראה אינו מופעל בערוץ שנבחר')
            token = notification_kind.set(kind)
            try:
                for name in names:
                    await self._deliver_channel(name, text + '\nסוג: ' + kind, None, require_all=True)
                if 'discord' in names:
                    status = await self.discord_delivery.status()
                    if status['queued']:
                        return 'pending'
                return 'sent'
            finally:
                notification_kind.reset(token)
        if channel == "routing":
            if not await self.send(text):
                raise RuntimeError("שליחת הבדיקה לערוץ הראשון נכשלה")
        elif channel == "discord":
            await self.discord_delivery.deliver(text, [[{"text": "בדיקת כפתור ✓", "data": "discord_test:ping"}]] if self.settings.discord_bot_configured else None, force=True)
        elif channel == "telegram":
            await self._send_telegram(text, None, force=True)
        elif channel == "ha":
            await self._send_ha(text, None, force=True)
        else:
            raise ValueError(f"unknown channel {channel}")
        return "sent"

    def journal_form_channel(self) -> str | None:
        eligible = self._eligible("journal")
        return eligible[0] if eligible else None

    async def send_journal_form(self, text: str, buttons: list[list[dict]], *, channel=None) -> bool:
        """One prompt on the preferred enabled journal channel; no escalation."""
        selected = channel or self.journal_form_channel()
        token = notification_kind.set('journal')
        try:
            return await self._send_to([selected] if selected else [], text, buttons)
        finally:
            notification_kind.reset(token)

    async def send_preview(self, channel: str, text: str, buttons: list[list[dict]]) -> None:
        """Explicit rehearsal: one configured channel, independent of routing."""
        for target in self.settings.notification_targets(channel, 'journal'):
            extra = {'target': target} if target else {}
            if channel == "discord":
                await self.discord_delivery.deliver(text, buttons, force=True, **extra)
            elif channel == "telegram":
                await self._send_telegram(text, buttons, force=True, **extra)
                self._preview_telegram_until = time.monotonic() + 1800
                self._tg_wakeup.set()
                self.start_telegram_poller()
            elif channel == "ha":
                await self._send_ha(text, buttons, force=True, **extra)
            else:
                raise ValueError("unknown preview channel")

    async def _send_telegram(
        self, text: str, buttons: list[list[dict]] | None, force: bool = False,
        *, bold_lines: list[str] | None = None, message_id: int | None = None, target: str = "",
    ) -> None:
        tg = self.settings.telegram
        if not (tg.get("bot_token") and tg.get("chat_id")):
            if force:
                raise RuntimeError("Telegram not configured")
            return
        if not tg.get("enabled") and not force:
            return
        payload: dict = {"chat_id": target or tg["chat_id"], "text": text}
        if message_id is not None:
            payload['message_id'] = message_id
        if bold_lines:
            # Telegram offsets count UTF-16 units, including emoji surrogates.
            # Entities keep upstream class names as plain text, not markup.
            offset, entities = 0, []
            for line in text.splitlines(keepends=True):
                content = line.rstrip('\r\n')
                if content in bold_lines:
                    entities.append({'type': 'bold', 'offset': offset,
                                     'length': len(content.encode('utf-16-le')) // 2})
                offset += len(line.encode('utf-16-le')) // 2
            if entities:
                payload['entities'] = entities
        rows = [[_telegram_button(b) for b in row if not b.get("ha_only")]
                for row in (buttons or [])]
        rows = [r for r in rows if r]
        if rows or message_id is not None:
            payload["reply_markup"] = {"inline_keyboard": rows}
        session = await self._http()
        async with session.post(
            f"{TG_API}/bot{tg['bot_token']}/{'editMessageText' if message_id is not None else 'sendMessage'}", json=payload
        ) as resp:
            body = await resp.json(content_type=None)
            if not body.get("ok"):
                if message_id is not None and 'message is not modified' in body.get('description', ''):
                    return
                raise RuntimeError(f"Telegram sendMessage: {body.get('error_code', resp.status)}")

    async def _send_ha(
        self, text: str, buttons: list[list[dict]] | None, force: bool = False,
        *, tag: str | None = None, target: str = "",
    ) -> None:
        """POST to an HA webhook. The webhook id is the only secret needed —
        no long-lived token, no API access; it can only fire the automation
        attached to it (see dashboard/arbox-callback-automation.yaml)."""
        ha = self.settings.ha
        url = target or ha.get("webhook_url")
        if not url:
            if force:
                raise RuntimeError("HA webhook not configured")
            return
        if not ha.get("enabled") and not force:
            return
        # the companion app caps actionable notifications at 3 buttons —
        # chunk instead of silently dropping the 4th+ class. tg_only buttons
        # (per-class info) are skipped; ha_only extras (batch info) ride on
        # every chunk.
        flat = [b for row in (buttons or []) for b in row
                if not b.get("tg_only") and not b.get("ha_only")]
        extras = [b for row in (buttons or []) for b in row if b.get("ha_only")]
        per = max(1, 3 - len(extras))
        chunks = [flat[i:i + per] + extras for i in range(0, len(flat), per)] \
            or ([extras] if extras else [None])
        session = await self._http()
        tag = tag or next((b.get('notification_tag') for row in buttons or [] for b in row if b.get('notification_tag')), None)
        for i, chunk in enumerate(chunks):
            payload: dict = {
                "message": text if i == 0 else f"(המשך {i + 1}/{len(chunks)})",
                "title": "Arbox Companion",
                "actions": [_ha_action(b) for b in (chunk or [])],
            }
            if target:
                route_id = self.target_label('ha', target).split(':', 1)[-1]
                for action in payload['actions']:
                    if action['action'].startswith('ARBOX_'):
                        action['action'] = 'ARBOX_route:' + route_id + ':' + action['action'][6:]
            if tag:
                payload.update(tag=tag if i == 0 else f'{tag}-{i}', alert_once=True)
            async with session.post(url, json=payload) as resp:
                if resp.status not in (200, 201):
                    raise RuntimeError(
                        f"HA webhook -> {resp.status}"
                    )

    # ------------------------------------------------- telegram long-polling

    def start_telegram_poller(self) -> None:
        if self._tg_task is None or self._tg_task.done():
            self._tg_task = asyncio.create_task(self._tg_poll_loop())

    async def _tg_poll_loop(self) -> None:
        _LOGGER.info("Telegram poller started")
        while True:
            try:
                tg = self.settings.telegram
                if not (tg.get("bot_token") and (
                    tg.get("enabled") or time.monotonic() < self._preview_telegram_until
                )):
                    try:
                        await asyncio.wait_for(self._tg_wakeup.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        pass
                    self._tg_wakeup.clear()
                    continue
                session = await self._http()
                async with session.get(
                    f"{TG_API}/bot{tg['bot_token']}/getUpdates",
                    params={
                        "timeout": 50,
                        "offset": self._tg_offset,
                        "allowed_updates": '["callback_query","message"]',
                    },
                ) as resp:
                    body = await resp.json(content_type=None)
                if not body.get("ok"):
                    await self._telegram_connection(False)
                    _LOGGER.error("getUpdates failed (code=%s)", body.get('error_code'))
                    await asyncio.sleep(30)
                    continue
                await self._telegram_connection(True)
                for upd in body.get("result", []):
                    self._tg_offset = upd["update_id"] + 1
                    cq = upd.get("callback_query")
                    if not tg.get("enabled"):
                        if cq and not cq.get("data", "").startswith("preview_"):
                            continue
                    if cq:
                        await self._handle_tg_callback(cq)
                    elif upd.get("message"):
                        await self._handle_tg_message(upd["message"])
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - poller must survive
                await self._telegram_connection(False)
                _LOGGER.error("Telegram poll error (%s)", type(err).__name__)
                await asyncio.sleep(15)

    async def _telegram_connection(self, connected):
        if self.telegram_connected != connected:
            self.telegram_connected = connected
            await self._log('info', 'חיבור קבלת הפעולות מטלגרם פעיל' if connected else
                            'חיבור קבלת הפעולות מטלגרם נותק', channel='telegram_gateway')

    async def _handle_tg_callback(self, cq: dict) -> None:
        tg = self.settings.telegram
        want = str(tg.get("chat_id") or "")
        got = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
        if not want or not got or got not in self.settings.allowed_notification_targets("telegram"):
            # a press from a chat this bot was not configured for: the prompt
            # id is the only thing take_prompt checks, so without this anyone
            # who can see a forwarded button could book or cancel classes
            _LOGGER.warning("Ignoring callback from chat %s (configured: %s)",
                            got, want)
            await self._log("warn", "לחיצה מצ׳אט לא מוכר נדחתה",
                            f"chat_id {got}", channel="telegram")
            return
        if got.startswith("-"):
            # a group: Telegram gives every member the same buttons, and there
            # is nothing in the callback that distinguishes them. Said once, in
            # the log, rather than pretended away.
            await self._log("warn", "היעד בטלגרם הוא קבוצה",
                            "כל חבר/ת קבוצה יכול/ה ללחוץ על הכפתורים",
                            channel="telegram")

        session = await self._http()
        # Ack first, work second. answerCallbackQuery has to land within
        # Telegram's callback lifetime, and the handler below can take the
        # full 30s Arbox timeout — the ack then arrives after the query has
        # expired, the spinner never stops, and the user presses again.
        async with session.post(
            f"{TG_API}/bot{tg['bot_token']}/answerCallbackQuery",
            json={"callback_query_id": cq["id"], "text": "עובד על זה…"},
        ):
            pass
        # and off the poll loop, so a slow booking cannot stall getUpdates
        task = asyncio.create_task(self._run_callback(cq.get("data", ""), (cq.get('message') or {}).get('message_id'), target=got))
        self._cb_tasks.add(task)
        task.add_done_callback(self._cb_tasks.discard)

    async def _run_callback(self, data: str, message_id: int | None = None, target: str = "") -> None:
        answer = "🤷"
        if self.on_callback:
            try:
                answer = await self.on_callback(data, source_channel="telegram")
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Callback handler failed (%s)", type(err).__name__)
                await self._log('error', 'פעולה מטלגרם נכשלה', type(err).__name__, channel='telegram', source='system')
                answer = f"שגיאה: {err}"
        # the outcome goes to the chat, where it persists — the popup is gone
        # by the time a booking finishes anyway
        try:
            if isinstance(answer, NotificationReply):
                await self._send_telegram(answer.text, answer.buttons, force=True, message_id=message_id, **({"target": target} if target else {}))
            else:
                await self._send_telegram(answer, None, force=True, **({"target": target} if target else {}))
        except RuntimeError:
            pass

    async def _handle_tg_message(self, message: dict) -> None:
        """Accept text only from the configured chat and only when armed."""
        tg = self.settings.telegram
        want = str(tg.get("chat_id") or "")
        got = str((message.get("chat") or {}).get("id", ""))
        text = message.get("text")
        if not text or not self.on_message or (not want or got not in self.settings.allowed_notification_targets("telegram")):
            return
        try:
            answer = await self.on_message(text, preview_only=not tg.get("enabled"))
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Telegram message handler failed (%s)", type(err).__name__)
            await self._log('error', 'שמירת תשובה מטלגרם נכשלה', type(err).__name__, channel='telegram', source='system')
            answer = "שגיאה בשמירת הסיבה — אפשר לנסות שוב"
        if answer:
            try:
                await self._send_telegram(answer, None, force=True, target=got)
            except RuntimeError:
                pass
