"""The workout the user intended, independent of its mutable Arbox id."""
import hashlib
import json

FIELDS = ('schedule_id', 'box_id', 'category_id', 'category_name', 'coach_id',
          'coach_name', 'date', 'start_time', 'end_time')


def snapshot(session):
    return {key: session.get(key) for key in FIELDS}


def token(session):
    return hashlib.sha256(json.dumps(snapshot(session), sort_keys=True).encode()).hexdigest()


def description(old, new):
    changes = []
    for keys, label in ((('category_id', 'category_name'), 'סוג האימון'),
                        (('coach_id', 'coach_name'), 'מאמן/ת')):
        if any(old.get(k) != new.get(k) for k in keys):
            changes.append(f"{label}: {old.get(keys[1]) or 'לא ידוע'} ← {new.get(keys[1]) or 'לא ידוע'}")
    if old.get('date') != new.get('date'):
        changes.append(f"תאריך: {old.get('date')} ← {new.get('date')}")
    if any(old.get(k) != new.get(k) for k in ('start_time', 'end_time')):
        changes.append(f"שעות: {old.get('start_time')}–{old.get('end_time') or ''} ← {new.get('start_time')}–{new.get('end_time') or ''}")
    return ' · '.join(changes) or 'פרטי האימון השתנו מאז התכנון'


def change(record, session):
    if not record:
        return None
    original = json.loads(record['snapshot'])
    if not record.get('changed') and snapshot(session) == original:
        return None
    return {'state': 'session_changed', 'reason': ('האימון אינו מופיע בלוח המעודכן — נדרש בירור' if record.get('changed') == 2 else description(original, session)),
            'original': original, 'current': snapshot(session), 'token': token(session),
            'source': record['source']}
