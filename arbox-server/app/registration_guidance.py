"""Read-only context for registration decisions in the normal user flows."""
import json
from datetime import datetime, timedelta, timezone
from .registration_learning import effective, opening, should_wait, fill_risk
from .rules import rule_matches


def cohort_key(session):
    return json.dumps([session.get('box_id'),session.get('series_id'),session.get('category_id'),
                      session.get('coach_id'),datetime.fromisoformat(session['date']).weekday(),
                      session['start_time']],separators=(',',':'))


def history_summary(courses, threshold=None):
    windows = {(w['schedule_id'],w['opens_at']):w for c in courses for w in c['observations']}
    full = [w['first_observed_threshold_seconds']['100'] for w in windows.values()
            if w['first_observed_threshold_seconds']['100'] is not None]
    risks = [fill_risk([{'elapsed':p['seconds'],'capacity':p['capacity'],'registered':p['registered']} for p in w['points']],
                       {'threshold_percent':threshold}) if threshold is not None else w.get('fill_risk') for w in windows.values()]
    return {'openings':len(windows),'risky_openings':sum(bool(r and r['recommend_immediate']) for r in risks),
            'fastest_full_seconds':min(full) if full else None,'full_openings':len(full),
            'cohorts':[c['cohort'] for c in courses]}


def save_override(settings, box, kind, target, mode):
    if mode is None:
        return
    if mode not in ('inherit','immediate') or kind not in ('session','rule'):
        raise ValueError('בחרו ברירת מחדל או הרשמה מיידית')
    overrides = dict(settings._data['registration_timing']['overrides'])
    key = f'{kind}:{box}:{target}'
    if mode == 'inherit':
        overrides.pop(key,None)
    else:
        overrides[key] = {'mode':'immediate'}
    settings.update({'registration_timing':{'overrides':overrides}})


async def guidance(engine, as_pin=False):
    learner = engine.registration_learning
    report = await learner.report()
    courses = {c['cohort']:c for c in report['courses']}
    sessions = await engine.store.get_sessions(date_from=datetime.now().date().isoformat())
    rules = await engine.store.list_rules()
    pins = {p['schedule_id'] for p in await engine.store.list_watchlist(pending_only=True)}
    config = learner.config
    now = datetime.now()
    result = {'box_id':engine.store.active_box_id,'config':config,'sessions':{},'rules':{}}
    for session in sessions:
        matched = [r for r in rules if r['enabled'] and r['mode']=='autobook' and rule_matches(r,session)]
        policy = effective(config,session,() if as_pin or session['schedule_id'] in pins else matched)
        course = courses.get(cohort_key(session))
        history = history_summary([course] if course else [],policy['threshold_percent'])
        opens = opening(session)
        deadline = opens+timedelta(minutes=policy['timeout_minutes']) if opens else None
        sample = None
        if course and opens:
            window = next((w for w in course['observations'] if w['schedule_id']==session['schedule_id'] and w['opens_at']==opens.isoformat()),None)
            if window and window['points']:
                point=window['points'][-1]
                sample={'observed_at':point['observed_at'],'registered':point['registered'],'capacity':point['capacity']}
        if sample is None:
            try:
                if 0 <= (datetime.now(timezone.utc).replace(tzinfo=None)-datetime.fromisoformat(session['updated_at'])).total_seconds() <= 60:
                    sample={'observed_at':now.isoformat(),'registered':session['registered'],'capacity':session['max_users']}
            except (KeyError,TypeError,ValueError):
                pass
        if sample and (type(sample['capacity']) is not int or type(sample['registered']) is not int):
            sample=None
        if session.get('user_booked') is not None:
            status='booked'
        elif session.get('user_in_standby') is not None:
            status='standby'
        elif opens and now < opens:
            status='before_open'
        elif policy['mode']=='immediate':
            status='immediate'
        elif should_wait(policy,session,now,sample):
            status='waiting_occupancy'
        else:
            status='ready_for_check'
        result['sessions'][str(session['schedule_id'])]={
            'policy':{k:policy[k] for k in ('mode','threshold_percent','timeout_minutes')},
            'override':config['overrides'].get(f"session:{session.get('box_id')}:{session['schedule_id']}"),
            'history':history,'status':status,'opens_at':opens.isoformat() if opens else None,
            'deadline_at':deadline.isoformat() if deadline else None,
            'session':{k:session.get(k) for k in ('schedule_id','category_name','coach_name','date','start_time')},
        }
    for rule in rules:
        targets=[s for s in sessions if rule_matches(rule,s)]
        keys={cohort_key(s) for s in targets}
        policy=effective(config,{'box_id':engine.store.active_box_id,'schedule_id':0},[rule])
        result['rules'][str(rule['id'])]={
            'policy':{k:policy[k] for k in ('mode','threshold_percent','timeout_minutes')},
            'override':config['overrides'].get(f"rule:{engine.store.active_box_id}:{rule['id']}"),
            'history':history_summary([courses[k] for k in keys if k in courses],policy['threshold_percent']),
            'status':'disabled' if not rule['enabled'] else 'notify' if rule['mode']!='autobook' else 'rule',
        }
    return result
