"""Discord interaction gateway inside the existing application process."""
import asyncio
import hashlib
import hmac

import discord

from .notification_reply import NotificationReply


def custom_id(data, key, text_input=False):
    prefix = 'at' if text_input else 'ab'
    mac = hmac.new(key.encode(), (prefix + ':' + data).encode(), hashlib.sha256).hexdigest()[:16]
    result = f'{prefix}:{mac}:{data}'
    if len(result) > 100:
        raise ValueError('Discord button identifier exceeds 100 characters')
    return result


def decode_id(value, key):
    try:
        prefix, mac, data = value.split(':', 2)
        if prefix not in ('ab', 'at') or not key:
            return None
        expected = custom_id(data, key, prefix == 'at')
        return (data, prefix == 'at') if hmac.compare_digest(value, expected) else None
    except (ValueError, AttributeError):
        return None


def render_bot(text, buttons, key):
    # Keep readable Markdown; block all mentions at the API level.
    text = text or 'Arbox Companion'
    parts = []
    while text:
        end = min(1800, len(text))
        while len(text[:end].encode('utf-16-le')) // 2 > 1800:
            end -= 1
        parts.append({'content': text[:end], 'allowed_mentions': {'parse': []}, 'components': []})
        text = text[end:]
    rows = []
    for row in buttons or []:
        current = []
        for b in row:
            if b.get('ha_only'):
                continue
            item = {'type': 2, 'label': str(b['text'])[:80]}
            url = b.get('uri') or b.get('url')
            if url and url.startswith(('https://', 'http://')):
                item.update(style=5, url=url)
            elif b.get('data'):
                data = b['data']
                item.update(style=1, custom_id=custom_id(data, key,
                    bool(b.get('text_input')) or data.startswith(('journal_notes:', 'preview_notes:', 'reason_other:'))))
            else:
                continue
            current.append(item)
            if len(current) == 5:
                rows.append({'type': 1, 'components': current}); current = []
        if current:
            rows.append({'type': 1, 'components': current})
    # Telegram reason menus use one choice per row. Discord permits only five
    # action rows per message: compact these menus before splitting messages.
    # Eight reasons fit in four mobile-friendly rows, with the prompt attached.
    if len(rows) > 5 and all(len(row['components']) == 1 for row in rows):
        choices = [row['components'][0] for row in rows]
        width = 2 if len(choices) <= 10 else 5
        rows = [{'type': 1, 'components': choices[i:i+width]}
                for i in range(0, len(choices), width)]
    for i in range(0, len(rows), 5):
        if i:
            parts.append({'content': 'אפשרויות נוספות', 'allowed_mentions': {'parse': []}, 'components': []})
        parts[-1]['components'] = rows[i:i+5]
    return parts


class DiscordBot:
    def __init__(self, notifier):
        self.notifier = notifier
        self.settings = notifier.settings
        self.client = None
        self.task = None
        self.connected = False
        self.lock = asyncio.Lock()

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.client:
            await self.client.close()
        self.connected = False

    async def run(self):
        while True:
            if not self.settings.discord.get('enabled') or not self.settings.discord_bot_configured:
                await asyncio.sleep(5)
                continue
            token = self.settings.discord_bot_token
            client = discord.Client(intents=discord.Intents.none(), allowed_mentions=discord.AllowedMentions.none())
            self.client = client
            @client.event
            async def on_ready():
                self.connected = True
            @client.event
            async def on_disconnect():
                self.connected = False
            @client.event
            async def on_interaction(interaction):
                try:
                    await self.interaction(interaction)
                except Exception:
                    await self.notifier._log('error', 'הטיפול בכפתור Discord נכשל', channel='discord')
            runner = asyncio.create_task(client.start(token))
            try:
                while not runner.done():
                    await asyncio.sleep(5)
                    if not self.settings.discord.get('enabled') or token != self.settings.discord_bot_token:
                        break
                if runner.done():
                    await runner
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.notifier._log('error', 'חיבור בוט Discord נכשל — בדקו טוקן והרשאות', channel='discord')
            finally:
                await client.close()
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
                self.connected = False
            await asyncio.sleep(30)

    def authorized(self, interaction):
        s = self.settings.discord
        return (s.get('enabled') and self.settings.discord_bot_configured
                and str(interaction.user.id) == s.get('allowed_user_id')
                and str(interaction.channel_id) == s.get('channel_id')
                and str(interaction.guild_id) == s.get('guild_id'))

    async def interaction(self, interaction):
        if interaction.type != discord.InteractionType.component:
            return
        decoded = decode_id((interaction.data or {}).get('custom_id', ''), self.settings.discord_bot_token)
        if not self.authorized(interaction) or not decoded:
            await interaction.response.send_message('הכפתור אינו זמין למשתמש הזה או שפג תוקפו.', ephemeral=True)
            return
        data, text_input = decoded
        if not interaction.message or str(interaction.message.author.id) != str(interaction.application_id):
            await interaction.response.send_message('מקור ההודעה אינו תקין.', ephemeral=True)
            return
        if text_input:
            owner = self
            source = interaction.message
            class InputModal(discord.ui.Modal, title='עדכון האימון'):
                answer = discord.ui.TextInput(label='העדכון שלך', style=discord.TextStyle.paragraph, max_length=4000)
                async def on_submit(self, submitted):
                    if not owner.authorized(submitted):
                        await submitted.response.send_message('אין הרשאה.', ephemeral=True)
                        return
                    await owner.execute(submitted, data, source, str(self.answer))
            modal = InputModal(timeout=300)
            if data.startswith(('reason_other:', 'xc_reason_other:')):
                modal.answer.max_length = 500
            await interaction.response.send_modal(modal)
            return
        await self.execute(interaction, data, interaction.message)

    async def execute(self, interaction, data, source, reply_text=None):
        # ACK before Arbox calls; Discord requires this within three seconds.
        await interaction.response.defer(thinking=True, ephemeral=True)
        async with self.lock:
            if data == 'discord_test:ping':
                answer = NotificationReply('✅ הכפתור עובד. לא בוצעה פעולה באימון.')
            else:
                try:
                    answer = await self.notifier.on_callback(data, source_channel='discord', reply_text=reply_text)
                except Exception:
                    await interaction.edit_original_response(content='הפעולה נכשלה. בדקו את מצב האימון לפני ניסיון נוסף.')
                    await self.notifier._log('error', 'פעולת Discord נכשלה', channel='discord')
                    return
            # Information must not remove the still-valid booking buttons.
            if data.startswith(('info:', 'infob:')):
                await interaction.edit_original_response(content=str(answer)[:1900])
                return
            if isinstance(answer, NotificationReply):
                text, buttons = answer.text, answer.buttons
            else:
                text, buttons = str(answer), []
                # A digest can offer several workouts. Consume only the buttons
                # for this prompt; keep the other workouts usable.
                cid = data.partition(':')[2]
                for row in getattr(source, 'components', []):
                    remaining = []
                    for button in row.children:
                        decoded = decode_id(getattr(button, 'custom_id', None), self.settings.discord_bot_token)
                        if decoded and decoded[0].partition(':')[2] != cid:
                            remaining.append({'text': button.label, 'data': decoded[0], 'text_input': decoded[1]})
                        elif getattr(button, 'url', None):
                            remaining.append({'text': button.label, 'url': button.url})
                    if remaining:
                        buttons.append(remaining)
                if buttons:
                    text = (getattr(source, 'content', '') + '\n\n' + text).strip()
            payloads = render_bot(text, buttons, self.settings.discord_bot_token)
            first = payloads[0]
            def make_view(payload):
                view = discord.ui.View(timeout=None)
                for index, row in enumerate(payload['components']):
                    for b in row['components']:
                        view.add_item(discord.ui.Button(label=b['label'], style=discord.ButtonStyle(b['style']),
                            custom_id=b.get('custom_id'), url=b.get('url'), row=index))
                return view
            view = make_view(first)
            try:
                await source.edit(content=first['content'], view=view, allowed_mentions=discord.AllowedMentions.none())
                for extra in payloads[1:]:
                    await source.channel.send(extra['content'], view=make_view(extra), allowed_mentions=discord.AllowedMentions.none())
                # The edited source is already the acknowledgement. Remove the
                # temporary thinking reply instead of leaving a detached echo.
                try:
                    await interaction.delete_original_response()
                except Exception:
                    await interaction.edit_original_response(content='עודכן ✓')
            except Exception:
                await interaction.edit_original_response(content='הפעולה טופלה, אך עדכון ההודעה נכשל. בדקו את מצב האימון באפליקציה.')
                await self.notifier._log('warn', 'פעולת Discord טופלה אך עדכון ההודעה נכשל', channel='discord')
