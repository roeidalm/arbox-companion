"""Persist delivery receipts and report incidents through independent channels."""
import time
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from .notification_context import notification_kind, health_notice


class NotificationHealth:
    def __init__(self, store, notifier):
        self.store, self.notifier = store, notifier
        self._locks = defaultdict(asyncio.Lock)

    async def start(self):
        await self.store.db.execute('''CREATE TABLE IF NOT EXISTS notification_status (
            channel TEXT, target TEXT, state TEXT, kind TEXT, error TEXT,
            last_success TEXT, last_failure TEXT, updated TEXT,
            PRIMARY KEY(channel,target))''')
        await self.store.db.commit()
        self.notifier.audit = self.record

    async def record(self, channel, target, kind, state, error=None):
        now = datetime.now().isoformat(timespec='seconds')
        success = now if state == 'sent' else None
        failure = now if state in ('failed', 'unknown') else None
        await self.store.db.execute('''INSERT INTO notification_status
            (channel,target,state,kind,error,last_success,last_failure,updated) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(channel,target) DO UPDATE SET state=excluded.state,kind=excluded.kind,
            error=excluded.error,last_success=COALESCE(excluded.last_success,last_success),
            last_failure=COALESCE(excluded.last_failure,last_failure),updated=excluded.updated''',
            (channel,target,state,kind,error,success,failure,now))
        await self.store.db.commit()
        await self.store.log_event('error' if failure else 'info', 'notify',
            {'sent':'השירות אישר קבלת התראה', 'pending':'התראה ממתינה לשליחה',
             'failed':'שליחת התראה נכשלה', 'unknown':'מסירת התראה לא אומתה'}.get(state, state),
            f"ערוץ: {channel} · סוג: {kind} · יעד: {target}" + (f" · סיבה: {error}" if error else ""), tag=channel)

    async def snapshot(self):
        rows = [dict(r) for r in await (await self.store.db.execute('SELECT * FROM notification_status')).fetchall()]
        output = {}
        for channel in ('telegram','ha','discord'):
            config = getattr(self.notifier.settings, channel)
            # Removed routes must not leave the channel permanently failing.
            labels = {self.notifier.target_label(channel, target)
                      for kind in ('system', *config.get('kinds', []))
                      for target in self.notifier.settings.notification_targets(channel, kind)}
            current = [r for r in rows if r['channel'] == channel and r['target'] in labels]
            recent = (datetime.now()-timedelta(hours=24)).isoformat(timespec='seconds')
            failures = [r for r in current if r['state'] in ('failed','unknown') and r['updated'] >= recent]
            queued = sum(r['state'] == 'pending' for r in current)
            state = ('off' if not config.get('enabled') else 'unconfigured' if not self.notifier._configured(channel)
                     else 'failing' if failures else 'queued' if queued else 'ok' if current and all(r['state'] == 'sent' for r in current) else 'unknown')
            cur = await self.store.db.execute("SELECT COUNT(*) FROM events WHERE source='notify' AND tag=? "
                "AND message IN ('שליחת התראה נכשלה','מסירת התראה לא אומתה') "
                "AND ts >= datetime('now','localtime','-1 day')", (channel,))
            failures_24h = (await cur.fetchone())[0]
            output[channel] = dict(state=state,failures=len(failures),failures_24h=failures_24h,queued=queued,targets=current,
                last_success=max((r['last_success'] for r in current if r['last_success']),default=None),
                last_failure=max((r['last_failure'] for r in current if r['last_failure']),default=None))
            if channel == 'discord':
                output[channel]['gateway'] = ('connected' if self.notifier.discord_bot.connected else 'disconnected') if self.notifier.settings.discord_bot_configured else 'unused'
                output[channel]['queue'] = await self.notifier.discord_delivery.status()
        return output

    async def incident(self, key, failing, message, *, excluded=(), grace=0):
        async with self._locks[key]:
            await self._incident(key, failing, message, excluded=excluded, grace=grace)

    async def _incident(self, key, failing, message, *, excluded=(), grace=0):
        meta = 'notification_incident:' + key
        incident = await self.store.get_meta(meta) or {}
        now = time.time()
        if failing:
            if not incident:
                incident = {'since': now, 'last_notice': 0, 'announced': False}
            if now - incident['since'] < grace:
                await self.store.set_meta(meta, incident)
                return
            if now - incident['last_notice'] < (6*3600 if incident.get('announced') else 300):
                return
            text = '⚠️ ' + message
        elif incident.get('announced'):
            if now - incident.get('recovery_attempt', 0) < 300:
                return
            incident['recovery_attempt'] = now
            await self.store.set_meta(meta, incident)
            text = '✅ התקלה הסתיימה: ' + message
        else:
            if incident:
                await self.store.set_meta(meta, {})
            return
        token = notification_kind.set('log')
        guard = health_notice.set(True)
        try:
            names = [n for n in self.notifier._eligible('log') if n not in excluded]
            # Independent transports, no spacing: don't schedule an alert as "sent".
            results = []
            for name in names:
                try:
                    await self.notifier._deliver_channel(name, text, None)
                    if name != 'discord' or not (await self.notifier.discord_delivery.status())['queued']:
                        results.append(name)
                except Exception:
                    pass
            await self.store.log_event('error' if failing else 'info', 'notify', text,
                'דווח דרך: ' + ', '.join(results) if results else 'לא ניתן למסור התראה בערוץ חלופי', tag='health')
            if failing:
                incident.update(last_notice=now,announced=incident.get('announced',False) or bool(results))
                await self.store.set_meta(meta, incident)
            elif results:
                await self.store.set_meta(meta, {})
        finally:
            notification_kind.reset(token)
            health_notice.reset(guard)

    async def tick(self):
        snapshot = await self.snapshot()
        for channel, status in snapshot.items():
            if status['state'] not in ('ok', 'failing'):
                continue
            await self.incident('delivery:'+channel, status['state'] == 'failing',
                                f'שליחת התראות דרך {channel}', excluded=(channel,))
        if self.notifier.settings.telegram.get('enabled') and self.notifier.telegram_connected is not None:
            await self.incident('telegram_gateway', not self.notifier.telegram_connected,
                                'חיבור קבלת הפעולות מטלגרם', excluded=('telegram',), grace=120)
        bot = self.notifier.discord_bot
        active = self.notifier.settings.discord.get('enabled') and self.notifier.settings.discord_bot_configured
        if active:
            await self.incident('discord_gateway', not bot.connected,
                                'חיבור בוט Discord', excluded=('discord',), grace=120)
        last_sync = await self.store.get_meta('last_sync')
        if last_sync:
            try:
                stale = (datetime.now() - datetime.fromisoformat(last_sync)).total_seconds() > 7200
                await self.incident('sync_stale', stale, 'הסנכרון עם Arbox לא עודכן מעל שעתיים')
            except (ValueError, TypeError):
                pass
