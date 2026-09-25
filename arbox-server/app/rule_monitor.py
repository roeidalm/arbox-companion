"""Validate recurring expectations independently of upstream occurrence IDs."""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import date, datetime, timedelta

from .store import flatten_session

_LOGGER = logging.getLogger(__name__)
HORIZON_DAYS = 62


def days(start, end):
    while start <= end:
        yield start
        start += timedelta(days=1)


def monday(day):
    return day - timedelta(days=day.weekday())


def signature(rule):
    fields = ('coaches', 'categories', 'weekdays', 'time_from', 'time_to', 'mode',
              'recurrence_weeks', 'recurrence_anchor')
    return hashlib.sha256(json.dumps({k: rule.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()[:20]


def evaluate(rule, sessions, today, published_through, *, excluded=(), verified=True):
    """One matching class per anchored N-week period; only inspect published days.

    Empty days inside a successfully fetched published horizon are closed days.
    An empty entire response cannot prove that the studio closed for two months.
    This cadence monitors coverage; it does not restrict existing booking filters.
    """
    from .rules import rule_matches
    length = 7 * rule.get('recurrence_weeks', 1)
    anchor = monday(date.fromisoformat(rule.get('recurrence_anchor') or today.isoformat()))
    start = anchor + timedelta(days=((today - anchor).days // length) * length)
    horizon = today + timedelta(days=HORIZON_DAYS)
    matches = [s for s in sessions if rule_matches(rule, s)]
    occupied = {s['date'] for s in sessions}
    periods = []
    while start <= horizon:
        end = start + timedelta(days=length - 1)
        expected = [d for d in days(start, end) if not rule.get('weekdays') or d.weekday() in rule['weekdays']]
        future = [d for d in expected if today <= d <= horizon]
        if not future or end > horizon:
            start = end + timedelta(days=1)
            continue
        covered = [s for s in matches if start.isoformat() <= s['date'] <= end.isoformat()]
        relevant = [d for d in future if d.isoformat() not in excluded]
        if not relevant:
            state = 'skipped'
        elif not verified:
            state = 'unverified'
        elif covered:
            state = 'matched'
        elif not published_through or any(d.isoformat() > published_through for d in relevant):
            state = 'unpublished'
        elif not any(d.isoformat() in occupied for d in relevant):
            state = 'closed'
        else:
            state = 'missing'
        periods.append({'start': start.isoformat(), 'end': end.isoformat(), 'state': state,
                        'next_expected': relevant[0].isoformat() if relevant else future[0].isoformat(),
                        'matches': len(covered)})
        start = end + timedelta(days=1)
    return periods


class RuleMonitor:
    def __init__(self, engine):
        self.e = engine

    def key(self, rule_id):
        return f'rule_monitor:{self.e.store.active_box_id}:{rule_id}'

    async def snapshot(self, today):
        """Fresh read only: validation must not cause upstream bookings."""
        end = today + timedelta(days=HORIZON_DAYS)
        try:
            await self.e.syncer.ensure_identity()
            rows = await self.e.client.schedule_between(self.e.syncer.box_id,
                self.e.syncer.location_id, today.isoformat(), end.isoformat())
            members = await self.e.store.get_meta('memberships') or []
            bonus = max((int(m.get('extra_advance_hours') or 0) for m in members), default=0)
            sessions = [flatten_session(r, extra_advance_hours=bonus, box_id=self.e.store.active_box_id) for r in rows]
            sessions = [s for s in sessions if today.isoformat() <= s['date'] <= end.isoformat()]
            # A past occurrence can satisfy the current multi-week period.
            past = await self.e.store.get_sessions(date_from=(today-timedelta(days=56)).isoformat(),
                                                  date_to=(today-timedelta(days=1)).isoformat())
            return {'sessions': [*past, *sessions], 'verified': bool(sessions),
                    'published_through': max((s['date'] for s in sessions), default=None),
                    'date_from': today.isoformat(), 'date_to': end.isoformat()}
        except Exception:
            _LOGGER.warning('Could not verify recurring rules against the studio schedule', exc_info=True)
            return {'sessions': [], 'verified': False, 'published_through': None,
                    'date_from': today.isoformat(), 'date_to': end.isoformat()}

    async def validate(self, rule, today=None):
        from .rules import rule_matches
        today = today or date.today()
        snapshot = await self.snapshot(today)
        matches = [s for s in snapshot['sessions'] if s['date'] >= today.isoformat()
                   and datetime.fromisoformat(f"{s['date']}T{s['start_time']}") > datetime.now()
                   and rule_matches(rule, s)] if snapshot['verified'] else []
        first = min((s['date'] for s in matches), default=None)
        proposed = {**rule, 'recurrence_anchor': rule.get('recurrence_anchor') or first or today.isoformat()}
        periods = evaluate(proposed, snapshot['sessions'], today, snapshot['published_through'], verified=snapshot['verified'])
        missing = [p for p in periods if p['state'] == 'missing']
        state = 'partial' if matches and missing else 'verified' if matches else 'no_match' if snapshot['verified'] else 'unverified'
        message = ('נמצאו אימונים, אבל יש תקופות ללא התאמה למחזוריות שבחרת' if state == 'partial' else
                   'נמצאו אימונים מתאימים בלוח הסטודיו' if matches else
                   'הבקשה אינה תואמת שום אימון בעתיד הנראה לעין' if snapshot['verified'] else
                   'לא ניתן לאמת את הבקשה: הסנכרון נכשל או שהלוח עדיין לא פורסם')
        if missing and matches:
            message += '\n' + '\n'.join(f"{p['start']}–{p['end']}" for p in missing)
        return {'state': state, 'match_count': len(matches), 'first_match': first,
                'date_from': snapshot['date_from'], 'date_to': snapshot['published_through'] or snapshot['date_to'],
                'periods': periods, 'lead_days': max((math.ceil(s.get('advance_hours', 0) / 24) + 1 for s in matches if s.get('advance_hours') is not None), default=8), 'message': message}

    async def exclusions(self, rule, today):
        from .rules import rule_matches
        excluded = set()
        skips = await self.e.store.get_meta(self.key(rule['id']) + ':skips') or []
        for period in skips:
            if period.get('signature') == signature(rule):
                excluded.update(d.isoformat() for d in days(date.fromisoformat(period['start']), date.fromisoformat(period['end'])))
        for v in await self.e.store.list_vacations():
            if v.get('block_autobook' if rule['mode'] == 'autobook' else 'block_notify'):
                lo = max(today, date.fromisoformat(v['date_from']))
                hi = min(today+timedelta(days=HORIZON_DAYS), date.fromisoformat(v['date_to']))
                excluded.update(d.isoformat() for d in days(lo, hi))
        # Explicitly accepted replacements and dismissed occurrences fulfil the
        # user's decision for that date without rewriting the recurring rule.
        cur = await self.e.store.db.execute(
            'SELECT snapshot FROM planning_intents i JOIN automation_skips k '
            'ON i.schedule_id=k.schedule_id AND i.box_id=k.box_id WHERE i.box_id=?',
            (self.e.store.active_box_id,))
        originals = [json.loads(r['snapshot']) for r in await cur.fetchall()]
        cur = await self.e.store.db.execute(
            "SELECT change_json FROM training_events WHERE box_id=? AND date>=? "
            "AND event_type IN ('planning_change_accepted','planning_change_cancelled') AND change_json IS NOT NULL",
            (self.e.store.active_box_id, today.isoformat()))
        originals += [json.loads(r['change_json'])['before'] for r in await cur.fetchall()]
        excluded.update(s['date'] for s in originals if rule_matches(rule, s))
        return excluded

    async def check(self, today=None):
        today = today or date.today()
        rules = [r for r in await self.e.store.list_rules() if r['enabled']]
        if not rules:
            return
        snapshot = await self.snapshot(today)
        for rule in rules:
            key = self.key(rule['id'])
            prior = await self.e.store.get_meta(key) or {}
            if not rule.get('recurrence_anchor'):
                # Fix legacy baselines once. A rolling anchor could mask a gap.
                rule['recurrence_anchor'] = prior.get('anchor') or monday(today).isoformat()
            periods = evaluate(rule, snapshot['sessions'], today, snapshot['published_through'],
                               excluded=await self.exclusions(rule, today), verified=snapshot['verified'])
            sig = signature(rule)
            notices = prior.get('notices', {}) if prior.get('signature') == sig else {}
            notices = {k:v for k,v in notices.items() if k >= monday(today).isoformat()}
            from .rules import rule_matches
            validation = await self.e.store.get_meta(key + ':validation') or {}
            observed_lead = max((math.ceil(s.get('advance_hours', 0) / 24) + 1 for s in snapshot['sessions']
                                 if s.get('advance_hours') is not None and rule_matches(rule, s)), default=0)
            lead_days = max(observed_lead, prior.get('lead_days', 0), validation.get('lead_days', 0), 8)
            due = [p for p in periods if p['state'] == 'missing' or
                   (p['state'] in ('unverified', 'unpublished') and
                    date.fromisoformat(p['next_expected']) <= today+timedelta(days=lead_days))]
            for period in periods:
                period['attention'] = period in due
            fresh = [p for p in due if notices.get(p['start']) != p['state']]
            if fresh:
                lines = [f"⚠️ בדיקת אוטומציה · {rule['name']}"]
                for p in fresh:
                    why = ('לא נמצא אימון שתואם את הבקשה' if p['state'] == 'missing' else
                           'טרם ניתן לאמת את הלוח וההרשמה אינה מובטחת')
                    lines.append(f"{p['start']}–{p['end']}: {why}")
                lines.append('בדקו את הכלל בלשונית האוטומציות; אפשר לעדכן את המחזוריות או להגדיר חופשה.')
                delivered = await self.e.notifier.send('\n'.join(lines), kind=rule['mode'] if rule['mode']=='autobook' else 'digest')
                if delivered:
                    notices.update({p['start']: p['state'] for p in fresh})
            # Re-arm resolved periods if they disappear again later.
            for p in periods:
                if p['state'] in ('matched', 'closed', 'skipped'):
                    notices.pop(p['start'], None)
            await self.e.store.set_meta(key, {'anchor': rule['recurrence_anchor'], 'signature': sig,
                'checked_at': datetime.now().isoformat(), 'periods': periods, 'notices': notices,
                'published_through': snapshot['published_through'], 'lead_days': lead_days})
