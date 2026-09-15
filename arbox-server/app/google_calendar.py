"""Opt-in Google calendar projection, isolated by Arbox account and studio.

Secrets are kept outside SQLite in a mode-0600 atomic file. Google IO never
performs Arbox writes. Event ids are deterministic so uncertain inserts can be
retried without duplicate events. Only this connection's future events change.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from .ical import calendar_description

SCOPE = 'https://www.googleapis.com/auth/calendar.app.created'
CALLBACK = '/api/calendar/google/callback'
API = 'https://www.googleapis.com/calendar/v3'
TOKEN = 'https://oauth2.googleapis.com/token'
AUTH = 'https://accounts.google.com/o/oauth2/v2/auth'
LABELS = {'scheduled': 'מתוזמן', 'automation': 'אוטומציה', 'booked': 'רשום',
          'standby': 'רשימת המתנה', 'review': 'דורש בדיקה'}
DEFAULTS = {k: {'enabled': True, 'color': c, 'reminders': [60, 30] if k == 'booked' else [],
                'busy': k == 'booked'} for k, c in zip(LABELS, ('9', '6', '10', '5', '11'))}


class CalendarError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def validate_credentials(data, redirect, server_host):
    """Uploaded URLs never determine token/API destinations."""
    web = data.get('web') if isinstance(data, dict) else None
    if not isinstance(web, dict):
        raise ValueError('צריך קובץ JSON של Web application שהורד מ־Google')
    for k in ('client_id', 'client_secret', 'project_id'):
        if not isinstance(web.get(k), str) or not 1 <= len(web[k]) <= 1024:
            raise ValueError('בקובץ חסרים פרטי חיבור תקינים')
    if not web['client_id'].endswith('.apps.googleusercontent.com'):
        raise ValueError('מזהה הלקוח אינו של Google')
    if not isinstance(redirect, str) or not isinstance(web.get('redirect_uris'), list):
        raise ValueError('כתובת החזרה אינה תקינה')
    u = urlsplit(redirect)
    if (u.scheme != 'https' or u.hostname != server_host or u.username or u.password
            or u.path != CALLBACK or u.query or u.fragment
            or redirect not in web.get('redirect_uris', [])):
        raise ValueError('כתובת החזרה חייבת להיות HTTPS של השרת הזה, עם הנתיב המוצג באשף, ולהופיע בקובץ')
    return {k: web[k] for k in ('client_id', 'client_secret', 'project_id')} | {'redirect_uri': redirect}


LEGACY_COLORS = dict(zip(map(str, range(1, 12)), (
    '#7986cb', '#33b679', '#8e24aa', '#e67c73', '#f6bf26', '#f4511e',
    '#039be5', '#616161', '#3f51b5', '#0b8043', '#d50000')))


def validate_preferences(data, palette=()):
    if not isinstance(data, dict) or set(data) != set(LABELS):
        raise ValueError('חסרות הגדרות למצבי האימון')
    out = {}
    for kind, p in data.items():
        if not isinstance(p, dict) or type(p.get('enabled')) is not bool or type(p.get('busy')) is not bool:
            raise ValueError('הגדרת מצב לא תקינה')
        if str(p.get('color')) not in set(LEGACY_COLORS) | {c['id'] for c in palette}:
            raise ValueError('יש לבחור צבע מתוך צבעי Google')
        reminders = p.get('reminders')
        if not isinstance(reminders, list) or len(reminders) > 5 or any(
                type(m) is not int or not 0 <= m <= 40320 for m in reminders):
            raise ValueError('אפשר לבחור עד חמש תזכורות, בין מועד האימון ל־28 ימים לפניו')
        out[kind] = {'enabled': p['enabled'], 'busy': p['busy'], 'color': str(p['color']),
                     'reminders': sorted(set(reminders), reverse=True)}
    return out


def event_body(session, kind, prefs, tz, location, owner):
    start = datetime.fromisoformat(f"{session['date']}T{session['start_time']}").replace(tzinfo=ZoneInfo(tz))
    end = datetime.fromisoformat(f"{session['date']}T{session.get('end_time') or session['start_time']}").replace(tzinfo=ZoneInfo(tz))
    if end <= start:
        end = start + timedelta(hours=1)
    title = session.get('category_name') or 'אימון'
    if session.get('coach_name'):
        title += ' · ' + session['coach_name']
    return {'summary': f"{LABELS[kind]} · {title}", 'location': location,
            'description': calendar_description(session, standby=kind == 'standby') + '\n\n'
                + 'מנוהל על ידי Arbox Companion. שינוי הרשמה נעשה באפליקציית Arbox Companion.',
            'start': {'dateTime': start.isoformat(), 'timeZone': tz},
            'end': {'dateTime': end.isoformat(), 'timeZone': tz},
            **({'colorId': prefs['color']} if prefs['color'] in LEGACY_COLORS else {'eventLabelId': prefs['color']}), 'transparency': 'opaque' if prefs['busy'] else 'transparent',
            'reminders': {'useDefault': False, 'overrides': [
                {'method': 'popup', 'minutes': m} for m in prefs['reminders']]},
            'extendedProperties': {'private': {'arbox_owner': owner, 'schedule_id': str(session['schedule_id'])}}}


class GoogleCalendar:
    def __init__(self, data_dir, engine):
        self.path = Path(data_dir) / 'google-calendar.json'
        self.engine = engine
        self.lock = asyncio.Lock()
        self.pending = {}
        self.http = None
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {'profiles': {}}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(self.data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def context(self):
        e = self.engine
        account = e.client.email or str(e.client.user_id or '')
        studio = e.store.active_box_id
        if not account or not studio:
            raise ValueError('יש להתחבר ל־Arbox ולבחור סטודיו לפני חיבור היומן')
        return hashlib.sha256(f'{account}:{studio}'.encode()).hexdigest()

    def profile(self):
        return self.data['profiles'].setdefault(self.context(), {
            'preferences': copy.deepcopy(DEFAULTS), 'enabled': False, 'events': {},
            'owner': secrets.token_hex(16)})

    def status(self):
        try:
            p = self.profile()
        except ValueError:
            return {'available': False, 'preferences': copy.deepcopy(DEFAULTS)}
        return {'available': True, 'uploaded': bool(p.get('credentials')), 'connected': bool(p.get('refresh_token')),
                'enabled': p['enabled'], 'preferences': p['preferences'], 'palette': p.get('palette', []),
                'project_id': p.get('credentials', {}).get('project_id'),
                'suggested_redirect': os.environ.get('GOOGLE_CALENDAR_REDIRECT_URI') or (self.engine.settings.base_url.rstrip('/') + CALLBACK if self.engine.settings.base_url.startswith('https://') else ''),
                'redirect_uri': p.get('credentials', {}).get('redirect_uri'),
                'email': p.get('email'), 'calendar_id': p.get('calendar_id'),
                'last_sync': p.get('last_sync'), 'error': p.get('error'),
                'event_count': len(p['events']), 'creation_pending': bool(p.get('creation_pending'))}

    def monitoring_status(self):
        status = self.status()
        state = ('unavailable' if not status.get('available') else
                 'disconnected' if not status.get('connected') else
                 'error' if status.get('error') else
                 'active' if status.get('enabled') else 'paused')
        return {'state': state, 'connected': bool(status.get('connected')),
                'enabled': bool(status.get('enabled')), 'last_sync': status.get('last_sync'),
                'error': status.get('error'), 'event_count': status.get('event_count', 0),
                'settings_url': self.engine.settings.base_url.rstrip('/') + '/settings?settings=calendar'}

    async def request(self, method, url, **kwargs):
        if self.http is None:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        try:
            async with self.http.request(method, url, allow_redirects=False, **kwargs) as r:
                data = await r.json(content_type=None) if r.status != 204 else {}
                if not 200 <= r.status < 300:
                    messages = {401: 'החיבור ל־Google פג. יש להתחבר מחדש',
                                403: 'Google דחה את הגישה. בדקו שה־API וההרשאה הופעלו',
                                404: 'היומן או האירוע לא נמצאו ב־Google',
                                429: 'Google מבקש להמתין. הסנכרון ינסה שוב בהמשך'}
                    raise CalendarError(messages.get(r.status, 'Google לא השלים את הבקשה. אפשר לנסות שוב'), r.status)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise CalendarError('לא ניתן להתחבר ל־Google כרגע. הסנכרון ינסה שוב') from err

    async def access(self, p):
        if p.get('access_token') and p.get('expires_at', 0) > time.time() + 60:
            return p['access_token']
        c = p['credentials']
        try:
            d = await self.request('POST', TOKEN, data={'client_id': c['client_id'], 'client_secret': c['client_secret'],
                'refresh_token': p['refresh_token'], 'grant_type': 'refresh_token'})
        except CalendarError as err:
            if err.status in (400, 401):
                raise CalendarError('החיבור ל־Google פג. יש להתחבר מחדש', err.status) from err
            raise
        if not isinstance(d, dict) or not d.get('access_token'):
            raise CalendarError('החיבור ל־Google פג. יש להתחבר מחדש')
        p.update(access_token=d['access_token'], expires_at=time.time() + d.get('expires_in', 3600))
        self.save()
        return p['access_token']

    async def google(self, p, method, path, **kwargs):
        token = await self.access(p)
        return await self.request(method, API + path, headers={'Authorization': 'Bearer ' + token}, **kwargs)

    async def refresh_palette(self, p):
        if not p.get('calendar_id') or not p.get('refresh_token'):
            return
        if p.get('palette_checked_at', 0) > time.time() - 900:
            return
        d = await self.google(p, 'GET', '/calendars/' + quote(p['calendar_id'], safe=''))
        p['palette'] = [dict(id=c['id'], color=c['backgroundColor'].lower(), name=c.get('name', ''))
                        for c in d.get('labelProperties', {}).get('eventLabels', [])
                        if isinstance(c.get('id'), str) and re.fullmatch(r'#[0-9a-fA-F]{6}', c.get('backgroundColor', ''))]
        p['palette_checked_at'] = time.time()
        self.save()

    def begin(self, return_url):
        p = self.profile()
        if not p.get('credentials'):
            raise ValueError('יש להעלות קודם את קובץ החיבור')
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v['expires'] > now}
        ticket = secrets.token_urlsafe(32)
        self.pending[ticket] = {'context': self.context(), 'expires': now + 600,
                                'return': return_url, 'stage': 'start'}
        base = p['credentials']['redirect_uri'].removesuffix(CALLBACK)
        return base + '/api/calendar/google/start?ticket=' + ticket

    def start(self, ticket):
        item = self.pending.pop(ticket, None)
        if not item or item['expires'] < time.time() or item['stage'] != 'start' or item['context'] != self.context():
            raise ValueError('קישור החיבור פג. חזרו להגדרות והתחברו שוב')
        state, cookie, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        c = self.profile()['credentials']
        item.update(stage='callback', cookie=hashlib.sha256(cookie.encode()).hexdigest(), verifier=verifier,
                    client_id=c['client_id'])
        self.pending[state] = item
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        url = AUTH + '?' + urlencode({'client_id': c['client_id'], 'redirect_uri': c['redirect_uri'],
            'response_type': 'code', 'scope': SCOPE + ' openid email', 'access_type': 'offline',
            'prompt': 'consent', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})
        return url, cookie

    async def callback(self, state, code, cookie):
        item = self.pending.pop(state, None)
        if (not item or item['expires'] < time.time() or item['stage'] != 'callback'
                or item['context'] != self.context() or not secrets.compare_digest(
                    item['cookie'], hashlib.sha256(cookie.encode()).hexdigest())):
            raise ValueError('לא ניתן לאמת את החיבור. התחילו שוב מתוך הגדרות היומן')
        p = self.profile()
        c = p['credentials']
        if c['client_id'] != item['client_id']:
            raise ValueError('קובץ החיבור השתנה. התחילו שוב')
        d = await self.request('POST', TOKEN, data={'client_id': c['client_id'], 'client_secret': c['client_secret'],
            'redirect_uri': c['redirect_uri'], 'grant_type': 'authorization_code', 'code': code,
            'code_verifier': item['verifier']})
        if not isinstance(d, dict) or not d.get('access_token') or SCOPE not in d.get('scope', '').split() or not d.get('refresh_token'):
            raise ValueError('נדרשת הרשאה ליומן וגישה מתמשכת. התחברו שוב ואשרו את ההרשאה')
        who = await self.request('GET', 'https://openidconnect.googleapis.com/v1/userinfo',
                                 headers={'Authorization': 'Bearer ' + d['access_token']})
        if not who.get('sub'):
            raise ValueError('לא ניתן לאמת את חשבון Google')
        if p.get('google_sub') and p['google_sub'] != who['sub']:
            # A different Google account must never reuse event/calendar mappings.
            p.pop('palette', None)
            p.pop('palette_checked_at', None)
            p.pop('calendar_id', None)
            p.pop('creation_pending', None)
            p['events'] = {}
            p['owner'] = secrets.token_hex(16)
        p.update(google_sub=who['sub'], email=who.get('email'), refresh_token=d['refresh_token'],
                 access_token=d['access_token'], expires_at=time.time()+d.get('expires_in', 3600), error=None,
                 enabled=False)
        self.save()
        return item['return']

    async def enable(self):
        p = self.profile()
        if not p.get('refresh_token'):
            raise ValueError('יש להתחבר ל־Google קודם')
        if not p.get('calendar_id'):
            if p.get('creation_pending'):
                raise ValueError('תוצאת יצירת היומן אינה ידועה. בדקו ב־Google והשתמשו באפשרות חיבור ליומן שנוצר')
            p['creation_pending'] = True
            self.save()
            try:
                d = await self.google(p, 'POST', '/calendars', json={
                    'summary': 'אימוני Arbox · ' + (self.engine.syncer.studio_name or 'הסטודיו שלי'),
                    'description': 'Arbox Companion connection ' + p['owner'],
                    'timeZone': self.engine.settings.timezone})
            except CalendarError as err:
                if err.status in (400, 401, 403, 429):
                    p['creation_pending'] = False
                    self.save()
                raise
            p['calendar_id'] = d['id']
            p['creation_pending'] = False
        p.update(enabled=True, error=None)
        self.save()

    async def recover_calendar(self, calendar_id):
        p = self.profile()
        if not p.get('creation_pending') or not p.get('refresh_token'):
            raise ValueError('אין יצירת יומן שממתינה לבירור')
        if not isinstance(calendar_id, str) or not 1 <= len(calendar_id) <= 512:
            raise ValueError('מזהה היומן לא תקין')
        d = await self.google(p, 'GET', '/calendars/' + quote(calendar_id, safe=''))
        if d.get('description') != 'Arbox Companion connection ' + p['owner']:
            raise ValueError('זה אינו היומן שנוצר בחיבור הזה')
        p.update(calendar_id=d['id'], creation_pending=False, enabled=True, error=None)
        self.save()

    async def desired(self, p):
        e = self.engine
        today = datetime.now(ZoneInfo(e.settings.timezone)).date()
        end = today + timedelta(days=30)
        rows = await e.store.get_sessions(date_from=today.isoformat(), date_to=end.isoformat())
        plans = await e._planned_sessions(today.isoformat(), end.isoformat())
        quota = await e.quota_status() or {}
        states = quota.get('plan_states', {})
        indexed = {r['schedule_id']: (r, 'booked' if r.get('user_booked') is not None else 'standby')
                   for r in rows if r.get('user_booked') is not None or r.get('user_in_standby') is not None}
        for plan in plans:
            sid = plan['schedule_id']
            if sid in indexed:
                continue
            row = await e.store.get_session(sid) or plan
            kind = 'automation' if plan['planning_source'] == 'autobook' else 'scheduled'
            if plan.get('intent_change') or states.get(str(sid), {}).get('state') not in (None, 'ready'):
                kind = 'review'
            indexed[sid] = (row, kind)
        identity = await e.store.get_meta('identity') or {}
        location = ', '.join(v for v in (identity.get('studio_name'), identity.get('address')) if v)
        desired = {}
        now = datetime.now(ZoneInfo(e.settings.timezone))
        for sid, (row, kind) in indexed.items():
            prefs = p['preferences'][kind]
            if not prefs['enabled']:
                continue
            prefs = dict(prefs)
            if prefs['color'] in LEGACY_COLORS:
                matching = next((c for c in p.get('palette', []) if c['color'] == LEGACY_COLORS[prefs['color']]), None)
                if matching:
                    prefs['color'] = matching['id']
            elif prefs['color'] not in {c['id'] for c in p.get('palette', [])}:
                raise CalendarError('צבע שנבחר הוסר מהיומן. בחרו צבע חדש בהגדרות לוח השנה')
            body = event_body(row, kind, prefs, e.settings.timezone, location, p['owner'])
            if datetime.fromisoformat(body['end']['dateTime']) <= now:
                continue
            generation = p.get('generations', {}).get(str(sid), 0)
            eid = 'ab' + hashlib.sha256(f"{p['owner']}:{sid}:{generation}".encode()).hexdigest()[:40]
            desired[eid] = body
        return desired

    async def sync(self):
        # Acquire the studio lock first everywhere: no lock inversion with API routes.
        async with self.engine.syncer.exclusive():
            async with self.lock:
                try:
                    p = self.profile()
                except ValueError:
                    return
                if not p.get('enabled') or not p.get('refresh_token'):
                    return
                try:
                    await self.refresh_palette(p)
                    desired = await self.desired(p)
                    base = '/calendars/' + quote(p['calendar_id'], safe='') + '/events'
                    for eid, body in desired.items():
                        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
                        saved = p['events'].get(eid, {})
                        if saved.get('digest') == digest and saved.get('checked_at', 0) > time.time() - 900:
                            continue
                        # Persist ownership before IO so a lost response is recoverable.
                        p['events'][eid] = {'end': body['end']['dateTime'], 'digest': None,
                                            'schedule_id': body['extendedProperties']['private']['schedule_id']}
                        self.save()
                        try:
                            if saved.get('digest'):
                                raise CalendarError('existing', 409)
                            await self.google(p, 'POST', base, json={'id': eid, **body},
                                              **({'params': {'eventLabelVersion': '1'}} if 'eventLabelId' in body else {}))
                        except CalendarError as err:
                            if err.status != 409:
                                raise
                            try:
                                existing = await self.google(p, 'GET', base + '/' + eid)
                            except CalendarError as missing:
                                if missing.status not in (404, 410):
                                    raise
                                existing = {'status': 'cancelled'}
                            if existing.get('status') == 'cancelled':
                                self.retire_event(p, eid)
                                continue  # Google tombstones cannot reuse IDs; next tick recreates it.
                            if existing.get('extendedProperties', {}).get('private', {}).get('arbox_owner') != p['owner']:
                                raise CalendarError('האירוע ביומן אינו שייך לחיבור הזה')
                            await self.google(p, 'PATCH', base + '/' + eid, json=body,
                                              **({'params': {'eventLabelVersion': '1'}} if 'eventLabelId' in body else {}))
                        p['events'][eid].update(digest=digest, checked_at=time.time())
                        self.save()
                    now = datetime.now(ZoneInfo(self.engine.settings.timezone))
                    for eid, saved in list(p['events'].items()):
                        if eid in desired or datetime.fromisoformat(saved['end']) <= now:
                            continue
                        try:
                            existing = await self.google(p, 'GET', base + '/' + eid)
                            if existing.get('status') != 'cancelled':
                                if existing.get('extendedProperties', {}).get('private', {}).get('arbox_owner') != p['owner']:
                                    raise CalendarError('האירוע ביומן אינו שייך לחיבור הזה')
                                await self.google(p, 'DELETE', base + '/' + eid)
                        except CalendarError as err:
                            if err.status not in (404, 410):
                                raise
                        self.retire_event(p, eid)
                    p.update(last_sync=now.isoformat(), error=None)
                except (CalendarError, ValueError, KeyError) as err:
                    p['error'] = str(err) if isinstance(err, CalendarError) else 'לא ניתן להשלים את הסנכרון. בדקו את הגדרות היומן'
                self.save()

    def retire_event(self, p, eid):
        saved = p['events'].pop(eid)
        sid = saved['schedule_id']
        generations = p.setdefault('generations', {})
        generations[sid] = generations.get(sid, 0) + 1
        self.save()

    async def close(self):
        if self.http:
            await self.http.close()
