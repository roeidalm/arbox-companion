"""Authenticated setup endpoints plus browser-bound OAuth callbacks."""
import json
import logging
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .api import require_key
from .google_calendar import CALLBACK, CalendarError, validate_credentials, validate_preferences

router = APIRouter(prefix='/api/calendar/google')
COOKIE = 'arbox_google_oauth'


class HideOAuthQuery(logging.Filter):
    def filter(self, record):
        # Uvicorn otherwise logs authorization codes and one-use tickets.
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if '/api/calendar/google/' in str(args[2]):
                args[2] = str(args[2]).split('?')[0]
                record.args = tuple(args)
        return True


async def limited_json(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 32768:
            raise HTTPException(413, 'הקובץ גדול מדי. העלו את קובץ הלקוח שהורד מ־Google')
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise HTTPException(422, 'הקובץ אינו JSON תקין')


def protected(request):
    require_key(request, request.headers.get('X-Api-Key'))
    return request.app.state.google_calendar


@router.get('/status')
async def status(request: Request):
    g = protected(request)
    async with g.engine.syncer.exclusive(), g.lock:
        return g.status()


@router.post('/credentials')
async def upload(request: Request):
    g = protected(request)
    data = await limited_json(request)
    try:
        host = urlsplit(g.engine.settings.base_url).hostname or request.url.hostname
        credentials = validate_credentials(data.get('credentials'), data.get('redirect_uri', ''), host)
        async with g.engine.syncer.exclusive(), g.lock:
            p = g.profile()
            if p.get('refresh_token'):
                raise ValueError('יש לנתק את החיבור הקיים לפני החלפת הקובץ')
            if p.get('credentials', {}).get('client_id') != credentials['client_id']:
                for key in ('calendar_id', 'creation_pending', 'google_sub', 'email', 'last_sync', 'error'):
                    p.pop(key, None)
                p.update(events={}, generations={}, owner=secrets.token_hex(16))
            p['credentials'] = credentials
            g.save()
            return g.status()
    except (ValueError, AttributeError) as err:
        raise HTTPException(422, str(err))


@router.post('/preferences')
async def preferences(request: Request):
    g = protected(request)
    try:
        prefs = validate_preferences(await limited_json(request))
        async with g.engine.syncer.exclusive(), g.lock:
            g.profile()['preferences'] = prefs
            g.save()
            return g.status()
    except ValueError as err:
        raise HTTPException(422, str(err))


@router.post('/connect')
async def connect(request: Request):
    g = protected(request)
    try:
        async with g.engine.syncer.exclusive(), g.lock:
            # Preserve the browser's origin (including local hostname aliases),
            # otherwise its localStorage API key disappears after OAuth. Only
            # accept an Origin matching the Host already validated by middleware.
            origin = g.engine.settings.base_url or str(request.base_url).rstrip('/')
            browser_origin = request.headers.get('origin', '')
            parsed = urlsplit(browser_origin)
            if (parsed.scheme in ('http', 'https') and parsed.netloc.lower() == request.url.netloc.lower()
                    and not parsed.username and not parsed.password
                    and not parsed.path and not parsed.query and not parsed.fragment):
                origin = browser_origin
            return {'url': g.begin(origin.rstrip('/') + '/settings?google_calendar=connected')}
    except ValueError as err:
        raise HTTPException(409, str(err))


@router.get('/start')
async def start(request: Request, ticket: str = ''):
    g = request.app.state.google_calendar
    try:
        async with g.engine.syncer.exclusive(), g.lock:
            url, cookie = g.start(ticket)
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(COOKIE, cookie, httponly=True, secure=True, samesite='lax',
                            max_age=600, path=CALLBACK)
        return response
    except ValueError:
        return HTMLResponse('<html lang="he" dir="rtl"><h1>קישור החיבור פג</h1><p>חזרו להגדרות לוח השנה והתחברו שוב.</p></html>', status_code=400)


@router.get('/callback')
async def callback(request: Request, state: str = '', code: str = '', error: str = ''):
    g = request.app.state.google_calendar
    try:
        async with g.engine.syncer.exclusive(), g.lock:
            if error or not code:
                g.pending.pop(state, None)
                raise ValueError('החיבור לא אושר. חזרו להגדרות כדי לנסות שוב')
            target = await g.callback(state, code, request.cookies.get(COOKIE, ''))
        response = RedirectResponse(target, status_code=303)
    except (ValueError, CalendarError):
        response = HTMLResponse('<html lang="he" dir="rtl"><meta name="viewport" content="width=device-width">'
            '<h1>החיבור לא הושלם</h1><p>חזרו להגדרות לוח השנה ובחרו חיבור ל־Google מחדש. '
            'ודאו שהחשבון נוסף כמשתמש בדיקה ושאישרתם את הרשאת היומן.</p></html>', status_code=400)
    response.delete_cookie(COOKIE, path=CALLBACK, secure=True, httponly=True, samesite='lax')
    return response


@router.post('/enable')
async def enable(request: Request):
    g = protected(request)
    try:
        async with g.engine.syncer.exclusive(), g.lock:
            await g.enable()
        await g.sync()
        return g.status()
    except (ValueError, CalendarError) as err:
        raise HTTPException(409, str(err))


@router.post('/recover')
async def recover(request: Request):
    g = protected(request)
    body = await limited_json(request)
    try:
        async with g.engine.syncer.exclusive(), g.lock:
            await g.recover_calendar(body.get('calendar_id'))
        await g.sync()
        return g.status()
    except (ValueError, CalendarError) as err:
        raise HTTPException(409, str(err))


@router.post('/pause')
async def pause(request: Request):
    g = protected(request)
    async with g.engine.syncer.exclusive(), g.lock:
        g.profile()['enabled'] = False
        g.save()
        return g.status()


@router.post('/disconnect')
async def disconnect(request: Request):
    g = protected(request)
    async with g.engine.syncer.exclusive(), g.lock:
        p = g.profile()
        p['enabled'] = False
        for k in ('refresh_token', 'access_token', 'expires_at'):
            p.pop(k, None)
        g.pending.clear()
        g.save()
        return g.status()


@router.post('/sync')
async def sync(request: Request):
    g = protected(request)
    await g.sync()
    return g.status()
