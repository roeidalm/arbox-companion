"""Observe opening occupancy without changing booking or eligibility state."""
import asyncio
import json
from collections import defaultdict
from datetime import datetime
from .store import flatten_session

DEFAULTS = {'learning_enabled': False, 'enabled': False, 'threshold_percent': 30,
            'timeout_minutes': 10, 'learning_window_minutes': 30, 'overrides': {}}


def validate(config):
    for key in ('learning_enabled', 'enabled'):
        if key in config and type(config[key]) is not bool:
            raise ValueError('מצב הרשמה ולמידה חייב להיות פעיל או כבוי')
    def values(obj):
        for key, low, high in [('threshold_percent', 1, 50), ('timeout_minutes', 1, 10), ('learning_window_minutes', 1, 120)]:
            if key in obj and (type(obj[key]) is not int or not low <= obj[key] <= high):
                raise ValueError('אחוז התפוסה חייב להיות 1–50 וזמן ההמתנה 1–10 דקות וחלון הלמידה 1–120 דקות')
    values(config)
    if 'overrides' in config:
        if not isinstance(config['overrides'], dict) or len(config['overrides']) > 500:
            raise ValueError('רשימת החריגות אינה תקינה')
        for key, override in config['overrides'].items():
            parts = key.split(':')
            if len(parts) != 3 or parts[0] not in ('session', 'rule') or not all(p.isdigit() for p in parts[1:]):
                raise ValueError('מזהה החריגה אינו תקין')
            if not isinstance(override, dict) or override.get('mode') not in ('immediate', 'wait'):
                raise ValueError('בחרו הרשמה מיידית או המתנה לחריגה')
            values(override)


def opening(s):
    from .rules import opening_moment
    try:
        if not s.get('advance_hours'):
            return None
        return opening_moment(datetime.fromisoformat(f"{s['date']}T{s['start_time']}"), s['advance_hours'])
    except (ValueError, KeyError, TypeError):
        return None


def effective(config, session, rules=()):
    result = {**DEFAULTS, **config}
    box = session.get('box_id') or 0
    # Global switch is a master switch. Off always preserves today's behavior.
    if not result['enabled']:
        return {**result, 'mode': 'immediate'}
    overrides = result['overrides']
    candidates = [overrides.get(f"rule:{box}:{r['id']}") for r in sorted(rules, key=lambda r:r['id'])]
    chosen = next((o for o in candidates if o), None)
    chosen = overrides.get(f"session:{box}:{session['schedule_id']}", chosen)
    return {**result, 'mode': 'wait', **(chosen or {})}


def should_wait(config, session, now, sample=None):
    opens = opening(session)
    if config['mode'] != 'wait' or opens is None:
        return False
    age = (now - opens).total_seconds()
    if age < 0 or age >= config['timeout_minutes'] * 60:
        return False
    # Missing/stale occupancy must never hold up a reservation indefinitely.
    if not sample or (now - datetime.fromisoformat(sample['observed_at'])).total_seconds() > 60:
        return False
    capacity, registered = sample['capacity'], sample['registered']
    if capacity <= 0:
        return False
    occupied = 100 * registered / capacity
    free = 100 * max(0, capacity-registered) / capacity
    return occupied < config['threshold_percent'] and free > config['threshold_percent']


def observation_outcome(points, policy, now, opens):
    """Replay the saved policy against observations; never claim a real booking."""
    if policy is None:
        return {'reason': 'unknown_policy', 'seconds': None}
    threshold, deadline = policy['threshold_percent'], policy['timeout_minutes'] * 60
    before = [p for p in points if p['elapsed'] <= deadline]
    crossing = next((p for p in before if 100 * p['registered'] / p['capacity'] >= threshold
                     or 100 * max(0, p['capacity']-p['registered']) / p['capacity'] <= threshold), None)
    base = {'threshold_percent': threshold, 'timeout_minutes': policy['timeout_minutes'],
            'simulation': True}
    if crossing:
        return {**base, 'reason': 'threshold', 'seconds': round(crossing['elapsed']),
                'occupancy_percent': round(100 * crossing['registered'] / crossing['capacity'], 1)}
    after = next((p for p in points if p['elapsed'] >= deadline), None)
    if after:
        used = [p for p in points if p['elapsed'] <= after['elapsed']]
        covered = used[0]['elapsed'] <= 55 and max(
            (b['elapsed']-a['elapsed'] for a,b in zip(used,used[1:])), default=0) <= 75
        # A threshold first seen beyond the deadline may have crossed before it.
        ambiguous = after['elapsed'] > deadline and after['registered']/after['capacity']*100 >= threshold
        return {**base, 'reason': 'timeout' if covered and not ambiguous else 'insufficient_data',
                'seconds': deadline, 'observed_seconds': round(after['elapsed']),
                'occupancy_percent': round(100 * after['registered'] / after['capacity'], 1)}
    return {**base, 'reason': 'pending' if (now-opens).total_seconds() <= deadline+55 else 'insufficient_data',
            'seconds': None}


class RegistrationLearning:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.settings = engine.settings
        self.lock = asyncio.Lock()
        self.ready = False

    @property
    def config(self):
        return {**DEFAULTS, **getattr(self.settings, '_data', {}).get('registration_timing', {})}

    async def open(self):
        if self.ready:
            return
        await self.store.db.executescript('''
        CREATE TABLE IF NOT EXISTS registration_samples (
          box_id INTEGER NOT NULL, schedule_id INTEGER NOT NULL, opens_at TEXT NOT NULL,
          observed_at TEXT NOT NULL, elapsed REAL NOT NULL, capacity INTEGER NOT NULL,
          registered INTEGER NOT NULL, own_booking INTEGER NOT NULL, cohort TEXT NOT NULL,
          label TEXT NOT NULL, PRIMARY KEY(box_id,schedule_id,opens_at,observed_at));
        CREATE INDEX IF NOT EXISTS registration_samples_time ON registration_samples(observed_at);
        CREATE TABLE IF NOT EXISTS registration_windows (
          box_id INTEGER NOT NULL, schedule_id INTEGER NOT NULL, opens_at TEXT NOT NULL,
          policy_json TEXT NOT NULL, PRIMARY KEY(box_id,schedule_id,opens_at));
        CREATE TABLE IF NOT EXISTS registration_decisions (
          box_id INTEGER NOT NULL, schedule_id INTEGER NOT NULL, opens_at TEXT NOT NULL,
          decided_at TEXT NOT NULL, reason TEXT NOT NULL, policy_json TEXT NOT NULL,
          PRIMARY KEY(box_id,schedule_id,opens_at,decided_at));
        CREATE TABLE IF NOT EXISTS registration_sample_runs (
          observed_at TEXT NOT NULL, box_id INTEGER NOT NULL, expected INTEGER NOT NULL,
          found INTEGER NOT NULL, status TEXT NOT NULL);
        ''')
        await self.store.db.commit()
        self.ready = True

    async def wanted(self, sessions):
        from .rules import rule_matches
        rules = [r for r in await self.store.list_rules() if r['enabled'] and r['mode']=='autobook']
        pins = {w['schedule_id'] for w in await self.store.list_watchlist(pending_only=True)}
        skipped = await self.store.automation_skip_ids()
        return {s['schedule_id'] for s in sessions if s['schedule_id'] in pins or
                (s['schedule_id'] not in skipped and any(rule_matches(r,s) for r in rules))}

    async def tick(self):
        if self.lock.locked():
            return
        async with self.lock:
            await self.open()
            now = datetime.now()
            sessions = await self.store.get_sessions(date_from=now.date().isoformat())
            wanted = await self.wanted(sessions)
            saved = {(r['schedule_id'],r['opens_at']):json.loads(r['policy_json']) for r in
                     await (await self.store.db.execute('SELECT * FROM registration_windows WHERE box_id=?',
                                                        (self.store.active_box_id,))).fetchall()}
            latest = {(r['schedule_id'],r['opens_at']):r['elapsed'] for r in
                      await (await self.store.db.execute(
                          'SELECT schedule_id,opens_at,MAX(elapsed) AS elapsed FROM registration_samples WHERE box_id=? GROUP BY schedule_id,opens_at',
                          (self.store.active_box_id,))).fetchall()}
            candidates = []
            for session in sessions:
                opens = opening(session)
                if opens is None:
                    continue
                policy = saved.get((session['schedule_id'],opens.isoformat()), self.config)
                if not (self.config['learning_enabled'] or session['schedule_id'] in wanted or policy.get('targeted')):
                    continue
                window = policy.get('learning_window_minutes',10)*60
                if self.config['enabled'] and session['schedule_id'] in wanted:
                    window = max(window, 600)  # cover all supported per-target waiting timeouts
                if latest.get((session['schedule_id'],opens.isoformat()), -1) >= window:
                    continue
                if 0 <= (now-opens).total_seconds() <= window+40:
                    candidates.append(session)
            if not candidates:
                return
            await self.sample(candidates)
            # Learning alone never invokes booking ticks or modifies their cadence.
            if self.config['enabled'] and any(s['schedule_id'] in wanted for s in candidates):
                await self.engine.watchlist_tick()
                await self.engine.autobook_tick()

    async def sample(self, sessions):
        await self.open()
        syncer = self.engine.syncer
        box, location = syncer.box_id, syncer.location_id
        if box is None or location is None:
            return
        stamp = datetime.now()
        found = 0
        status = 'ok'
        try:
            from .rules import rule_matches
            rules = [r for r in await self.store.list_rules() if r['enabled'] and r['mode']=='autobook']
            targets = await self.wanted(sessions)
            stored = await self.store.get_sessions_by_ids([s['schedule_id'] for s in sessions])
            days = [s['date'] for s in sessions]
            raw = await asyncio.wait_for(syncer.client.schedule_between(box,location,min(days),max(days)), timeout=15)
            if syncer.box_id != box or syncer.location_id != location:
                return
            by_id = {s['id']:s for s in raw}
            observed = datetime.now()
            for original in sessions:
                item = by_id.get(original['schedule_id'])
                if not item:
                    continue
                try:
                    old_raw = json.loads(stored.get(original['schedule_id'], {}).get('raw_json') or '{}')
                    bonus = max(0, int(original.get('advance_hours') or 0)-int(old_raw.get('enable_registration_time') or 0))
                except (ValueError, TypeError):
                    bonus = 0
                flat = flatten_session(item, extra_advance_hours=bonus, box_id=box)
                if opening(flat) != opening(original):
                    continue
                # Use the same effective opening (including membership advance) as booking.
                opens = opening(original)
                cap, registered = flat.get('max_users'), flat.get('registered')
                if not opens or type(cap) is not int or type(registered) is not int or cap<=0 or registered<0:
                    continue
                elapsed = (observed-opens).total_seconds()
                matched = [r for r in rules if rule_matches(r, original)]
                policy = effective({**self.config, 'enabled': True}, original, matched)
                policy = {k: policy[k] for k in ('threshold_percent', 'timeout_minutes', 'mode')}
                policy['waiting_enabled'] = self.config['enabled']
                policy['learning_window_minutes'] = self.config['learning_window_minutes']
                policy['targeted'] = original['schedule_id'] in targets
                existing = await (await self.store.db.execute(
                    'SELECT policy_json FROM registration_windows WHERE box_id=? AND schedule_id=? AND opens_at=?',
                    (box,original['schedule_id'],opens.isoformat()))).fetchone()
                if existing:
                    policy = json.loads(existing['policy_json'])
                window = policy.get('learning_window_minutes',10)*60
                if self.config['enabled'] and original['schedule_id'] in targets:
                    window = max(window,600)
                if not 0 <= elapsed <= window+55:
                    continue
                await self.store.db.execute('INSERT OR IGNORE INTO registration_windows VALUES(?,?,?,?)',
                    (box, original['schedule_id'], opens.isoformat(), json.dumps(policy)))
                cohort = json.dumps([box,flat.get('series_id'),flat.get('category_id'),flat.get('coach_id'),
                                     datetime.fromisoformat(original['date']).weekday(),original['start_time']], separators=(',',':'))
                label = f"{original.get('category_name') or ''} · {original.get('coach_name') or ''} · {original['start_time']}"
                await self.store.db.execute('INSERT OR IGNORE INTO registration_samples VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (box,original['schedule_id'],opens.isoformat(),observed.isoformat(),elapsed,cap,registered,
                     int(flat.get('user_booked') is not None),cohort,label))
                found += 1
            if found != len(sessions):
                status = 'partial'
        except asyncio.CancelledError:
            raise
        except Exception:
            status = 'failed'
        await self.store.db.execute('INSERT INTO registration_sample_runs VALUES(?,?,?,?,?)',
                                    (stamp.isoformat(),box,len(sessions),found,status))
        await self.store.db.commit()

    async def defer(self, session, rules=()):
        config = effective(self.config,session,rules)
        if config['mode'] != 'wait':
            return False
        await self.open()
        # At an opening the existing sync already refreshed this row. Reuse it;
        # subsequent randomized ticks use the separate observation stream.
        sample = None
        row = await (await self.store.db.execute(
            'SELECT * FROM registration_samples WHERE box_id=? AND schedule_id=? AND opens_at=? ORDER BY observed_at DESC LIMIT 1',
            (session.get('box_id') or 0,session['schedule_id'],opening(session).isoformat() if opening(session) else ''))).fetchone()
        if row:
            sample = dict(row)
        else:
            # Existing stored timestamps are UTC; only accept genuinely fresh data.
            try:
                updated = datetime.fromisoformat(session['updated_at'])
                if (datetime.utcnow()-updated).total_seconds() <= 60:
                    sample = {'observed_at':datetime.now().isoformat(),'capacity':session['max_users'],'registered':session['registered']}
            except (KeyError, ValueError, TypeError):
                pass
        if sample and (type(sample['capacity']) is not int or type(sample['registered']) is not int):
            sample = None
        now = datetime.now()
        waiting = should_wait(config,session,now,sample)
        opens = opening(session)
        if not waiting and opens is not None and now >= opens:
            age = (now-opens).total_seconds()
            reason = 'timeout' if age >= config['timeout_minutes']*60 else (
                'threshold' if sample and (now-datetime.fromisoformat(sample['observed_at'])).total_seconds() <= 60
                and sample['capacity'] > 0 else 'missing_data')
            await self.store.db.execute('INSERT OR IGNORE INTO registration_decisions VALUES(?,?,?,?,?,?)',
                (session.get('box_id') or 0, session['schedule_id'], opens.isoformat(), now.isoformat(), reason,
                 json.dumps({k:config[k] for k in ('threshold_percent','timeout_minutes')})))
            await self.store.db.commit()
        return waiting

    async def report(self):
        await self.open()
        box = self.store.active_box_id
        rows = [dict(r) for r in await (await self.store.db.execute(
            'SELECT * FROM registration_samples WHERE box_id=? ORDER BY observed_at',(box,))).fetchall()]
        policies = {(r['schedule_id'],r['opens_at']):json.loads(r['policy_json']) for r in
                    await (await self.store.db.execute('SELECT * FROM registration_windows WHERE box_id=?',(box,))).fetchall()}
        decisions = defaultdict(list)
        for row in await (await self.store.db.execute('SELECT * FROM registration_decisions WHERE box_id=? ORDER BY decided_at',(box,))).fetchall():
            decisions[(row['schedule_id'],row['opens_at'])].append(
                {'decided_at':row['decided_at'],'reason':row['reason'],'policy':json.loads(row['policy_json'])})
        groups = defaultdict(lambda:defaultdict(list))
        for row in rows:
            groups[row['cohort']][(row['schedule_id'],row['opens_at'])].append(row)
        courses = []
        for cohort, windows in groups.items():
            observations = []
            for (sid,opens), points in windows.items():
                gaps = [b['elapsed']-a['elapsed'] for a,b in zip(points,points[1:])]
                policy = policies.get((sid,opens))
                window_seconds = (policy or {}).get('learning_window_minutes', 10)*60
                complete = points[0]['elapsed']<=55 and points[-1]['elapsed']>=window_seconds and max(gaps,default=0)<=75
                crossings = {str(p):next((round(x['elapsed']) for x in points if x['registered']/x['capacity']*100>=p),None) for p in (30,70,100)}
                policy = policies.get((sid,opens))
                outcome = observation_outcome(points, policy, datetime.now(), datetime.fromisoformat(opens))
                observations.append({'policy':policy,'outcome':outcome,'decisions':decisions[(sid,opens)],'schedule_id':sid,'opens_at':opens,'samples':len(points),'complete':complete,
                    'first_seconds':round(points[0]['elapsed']),'last_seconds':round(points[-1]['elapsed']),
                    'max_gap_seconds':round(max(gaps,default=0)),'first_observed_threshold_seconds':crossings,
                    'own_booking_first_observed_seconds':next((round(p['elapsed']) for p in points if p['own_booking']),None),
                    'points':[{'observed_at':p['observed_at'],'occupancy_percent':round(100*p['registered']/p['capacity'],1),'seconds':round(p['elapsed']),'registered':p['registered'],'capacity':p['capacity'],'own_booking':bool(p['own_booking'])} for p in points]})
            courses.append({'cohort':cohort,'label':points[0]['label'],'openings':len(windows),'complete_openings':sum(w['complete'] for w in observations),'observations':observations})
        runs = [dict(r) for r in await (await self.store.db.execute('SELECT * FROM registration_sample_runs WHERE box_id=? ORDER BY observed_at DESC LIMIT 20',(box,))).fetchall()]
        return {'config':self.config,'sample_seconds_min':20,'sample_seconds_max':40,'window_minutes':self.config['learning_window_minutes'],'courses':courses,'recent_runs':runs,
                'warning':'מומלץ ללמוד שבועיים עם הרשמה מיידית. זמני הספים הם זמן הזיהוי הראשון, לא זמן ההרשמה המדויק. אין תחזית כשאין מספיק פתיחות שנמדדו.'}
