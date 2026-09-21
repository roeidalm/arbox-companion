"""Versioned, allowlisted projections. GET never refreshes or writes state."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import secrets
from fastapi import APIRouter, Request, HTTPException, Query
from .api import require_key

router = APIRouter(prefix='/api/view')
admin = APIRouter(prefix='/api/view-access')
SESSION = 'schedule_id date start_time end_time coach_name category_name free registered max_users stand_by stand_by_position registration_opens'.split()
MEMBER = 'id name start end active sessions_left quota period period_start period_end used reserved standby planned uncertain available available_after_planned quota_source'.split()
SECTIONS = ('summary','sessions','memberships','automations','calendar','history','reviews','events','status','settings','studio','notifications')
SOURCES = {'sync','booking','autobook','watchlist','telegram','ha','system','calendar','google_calendar','membership','quota','journal','attendance','vacation','startup','planning','notify','auth','retention'}


def pick(row, keys):
    return {k: row.get(k) for k in keys}


def authorize(request):
    s=request.app.state.settings
    supplied=request.headers.get('X-Api-Key','')
    if not supplied or not any(k and secrets.compare_digest(supplied.encode(),k.encode()) for k in (s.api_key,s.view_key)):
        raise HTTPException(401,'Missing or invalid read credential')


@admin.get('')
async def access_status(request: Request):
    require_key(request,request.headers.get('X-Api-Key'))
    return {'enabled':bool(request.app.state.settings.view_key)}


@admin.post('')
async def rotate(request: Request):
    require_key(request,request.headers.get('X-Api-Key'))
    from fastapi.responses import JSONResponse
    return JSONResponse({'key':request.app.state.settings.rotate_view_key()},headers={'Cache-Control':'no-store'})


@admin.delete('')
async def revoke(request: Request):
    require_key(request,request.headers.get('X-Api-Key'))
    request.app.state.settings.revoke_view_key()
    return {'enabled':False}


def session_view(row):
    return {**pick(row,SESSION),'state':('booked' if row.get('user_booked') is not None else 'standby' if row.get('user_in_standby') is not None else 'planned' if row.get('watched') else 'available')}


async def metadata(s):
    now=datetime.now(ZoneInfo(s.settings.timezone))
    last=await s.store.get_meta('last_sync')
    age=None
    try:
        dt=datetime.fromisoformat(last)
        if dt.tzinfo is None: dt=dt.replace(tzinfo=now.tzinfo)
        age=max(0,int((now-dt).total_seconds()))
    except (ValueError,TypeError): pass
    return {'schema_version':1,'generated_at':now.isoformat(),'last_sync':last,'age_seconds':age,
            'stale':age is None or age>900,'timezone':s.settings.timezone,
            'studio_id':s.syncer.box_id,'app_url':s.settings.browser_url}


async def status(s):
    # Do not call Google.profile(): setdefault there can mutate connection state.
    g=s.google_calendar
    try: p=g.data.get('profiles',{}).get(g.context(),{})
    except ValueError: p={}
    return {'google_calendar':{'connected':bool(p.get('refresh_token')),'enabled':bool(p.get('enabled')),
            'state':'error' if p.get('error') else 'active' if p.get('enabled') else 'paused' if p.get('refresh_token') else 'disconnected',
            'last_sync':p.get('last_sync'),'event_count':len(p.get('events',{}))},
            'event_counts_7_days':await s.store.event_counts(days=7),
            'notifications':{k:{'enabled':bool(getattr(s.settings,k).get('enabled'))} for k in ('telegram','ha','discord')}}


@router.get('')
async def catalog(request:Request):
    authorize(request)
    return {'schema_version':1,'sections':list(SECTIONS),'read_only':True}


@router.get('/{section}')
async def view(request:Request, section:str, limit:int=Query(100,ge=1,le=500),
               offset:int=Query(0,ge=0,le=100000), after_id:int=Query(0,ge=0),
               date_from:date|None=None,date_to:date|None=None):
    authorize(request)
    if section not in SECTIONS: raise HTTPException(404,'Unknown view')
    if request.query_params.get('refresh') is not None: raise HTTPException(400,'Views do not support refresh')
    s=request.app.state
    async with s.syncer.exclusive():
        if s.syncer.box_id is None: raise HTTPException(409,'Select a studio first')
        expected=request.headers.get('X-Arbox-Studio-Id')
        if expected is not None and expected!=str(s.syncer.box_id): raise HTTPException(409,'Studio changed')
        meta=await metadata(s)
        today=datetime.now(ZoneInfo(s.settings.timezone)).date()
        start=date_from or (today-timedelta(days=30) if section in ('history','reviews') else today)
        end=date_to or (today if section in ('history','reviews') else today+timedelta(days=30))
        if end<start or (end-start).days>366: raise HTTPException(422,'Date range must be ordered and at most 366 days')
        data={}
        if section in ('sessions','calendar','history','summary'):
            rows=await s.store.get_sessions(date_from=start.isoformat(),date_to=end.isoformat())
            plans=await s.rules_engine._planned_sessions(start.isoformat(),end.isoformat(),read_only=True)
            planned={r['schedule_id']:r for r in plans}
            data['sessions']=[{**session_view(r),**({'state':'planned','planning_source':planned[r['schedule_id']].get('planning_source')} if r['schedule_id'] in planned and r.get('user_booked') is None and r.get('user_in_standby') is None else {})} for r in rows]
            if section in ('calendar','history'):data['sessions']=[r for r in data['sessions'] if r['state']!='available']
            if section=='history':
                cur=await s.store.db.execute('SELECT schedule_id,date,start_time,end_time,category_name,coach_name,status,updated_at FROM training_outcomes WHERE box_id=? AND date>=? AND date<=? ORDER BY date DESC,start_time DESC',(s.syncer.box_id,start.isoformat(),end.isoformat()))
                data={'history':[dict(r) for r in await cur.fetchall()]}
        if section in ('memberships','summary'):
            q=await s.rules_engine.quota_status(read_only=True) or {}
            data['memberships']=[{**pick(m,MEMBER),'verification_state':m.get('policy',{}).get('state','unknown')} for m in q.get('memberships',[])]
            states=q.get('plan_states',{})
            data['attention']={'waiting_for_sync':sum(x.get('state')=='sync_pending' for x in states.values()),
                'action_required':sum(x.get('state') in ('needs_review','unattributed','uncertain','session_changed','no_capacity','no_membership') for x in states.values())}
        if section in ('automations','summary'):
            data['automations']=[pick(r,'id enabled coaches categories weekdays time_from time_to mode'.split()) for r in await s.store.list_rules()]
        if section in ('status','summary','notifications'):data.update(await status(s))
        if section=='summary':
            mine=[r for r in data.pop('sessions') if r['state']!='available']
            now=datetime.now(ZoneInfo(s.settings.timezone)).isoformat()[:19]
            future=[r for r in mine if r['date']+'T'+r['start_time']>now]
            data['next_class']=next((r for r in future if r['state']=='booked'),None)
            week=[r for r in future if r['date']<(today+timedelta(days=7)).isoformat()]
            data['next_7_days']={k:sum(r['state']==k for r in week) for k in ('booked','planned','standby')}
            data['automations']={'enabled':sum(bool(r['enabled']) for r in data['automations'])}
        if section=='reviews':
            rows=await s.store.workout_journals()
            data['reviews']=[pick(r,'schedule_id date start_time category_name coach_name coach_feedback class_feedback updated_at'.split()) for r in rows if start.isoformat()<=r.get('date','')<=end.isoformat()]
        if section=='events':
            # Events are instance-wide. No raw messages/detail: upstream errors may contain tokens or PII.
            cur=await s.store.db.execute('SELECT id,ts,level,source,schedule_id FROM events WHERE id>? ORDER BY id LIMIT ?',(after_id,limit+1))
            rows=[dict(r) for r in await cur.fetchall()]
            data['events']=[{**r,'source':r['source'] if r['source'] in SOURCES else 'other','level':r['level'] if r['level'] in ('info','warn','error') else 'info'} for r in rows[:limit]]
            data['scope']='instance';data['has_more']=len(rows)>limit
            data['next_after_id']=rows[min(len(rows),limit)-1]['id'] if rows else after_id
        if section=='settings':
            data={'timezone':s.settings.timezone,'calendar_alarms':s.settings.calendar_alarms,'journal_level':s.settings.journal.get('level'),'retention':s.settings.retention}
        if section=='studio':data={'id':s.syncer.box_id,'name':s.syncer.studio_name}
        # Paginate collections without hiding truncation. Summary keeps its small projections intact.
        if section not in ('summary','events'):
            for key,val in list(data.items()):
                if isinstance(val,list):
                    data['total']=len(val);data['offset']=offset;data['has_more']=len(val)>offset+limit;data[key]=val[offset:offset+limit]
        from fastapi.responses import JSONResponse
        return JSONResponse({'meta':meta,'data':data},headers={'Cache-Control':'no-store','X-Arbox-Studio-Id':str(s.syncer.box_id)})
