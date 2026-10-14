"""Corroborate a removed class separately from a missing personal booking.

A nonempty scoped day is read twice and every known membership is checked.
Disappearance never supplies a cancellation reason or an exact cancellation time.
"""
import logging
import time
from datetime import date, timedelta

from .arbox_client import ArboxError

_LOGGER = logging.getLogger(__name__)


def _missing_from_day(rows, session, studio, location):
    if not isinstance(rows, list) or not rows:
        return False  # An empty day alone could be an upstream availability fault.
    if any(not isinstance(r, dict) or type(r.get('id')) is not int
           or r.get('box_fk') != studio or r.get('date') != session['date']
           or r.get('locations_box_fk', location) != location for r in rows):
        return False
    return all(r['id'] != session['schedule_id'] for r in rows)


async def confirm_removed(syncer, session, observed_rows=None):
    """Read-only evidence; True only after Store records the same booking cycle."""
    store, client = syncer.store, syncer.client
    studio, location = syncer.box_id, syncer.location_id
    sid, day = session['schedule_id'], session['date']
    if session.get('box_id') != studio or store.active_box_id != studio or not location:
        return False
    mid = session.get('membership_user_id')
    if type(mid) is not int or mid <= 0:
        return False
    prior = await store.get_training_outcome(sid)
    if prior and prior['status'] != 'cancelled_unknown':
        return False
    try:
        first = observed_rows if observed_rows is not None else await client.schedule_between(studio, location, day, day)
        if not _missing_from_day(first, session, studio, location):
            return False
        second = await client.schedule_between(studio, location, day, day)
        if not _missing_from_day(second, session, studio, location):
            return False
        members = await store.get_meta('memberships') or []
        mids = {mid, *(m['id'] for m in members if type(m.get('id')) is int and m['id'] > 0)}
        for membership_id in sorted(mids):
            groups = await client.membership_schedules(membership_id)
            if not isinstance(groups, dict):
                return False
            for group in ('past', 'future', 'lateCancellation'):
                rows = groups.get(group)
                if not isinstance(rows, list):
                    return False
                for row in rows:
                    if (not isinstance(row, dict) or type(row.get('id')) is not int
                            or not row.get('date') or row.get('box_fk') != studio
                            or row.get('membership_user_fk') != membership_id):
                        return False
                    if row['id'] == sid:
                        return False  # Still booked or charged: never infer a free cancellation.
        if store.active_box_id != studio:
            return False
        recorded = await store.record_studio_cancellation(sid, session.get('user_booked'))
        if recorded:
            await store.set_meta(f'studio_cancellation_evidence:{sid}', {
                'studio_id': studio, 'date': day, 'checked_at': time.time(),
                'source': 'two_day_reads_and_memberships', 'membership_ids': sorted(mids)})
        return recorded
    except (ArboxError, TypeError, ValueError, KeyError) as err:
        _LOGGER.warning('Could not verify removed class %s: %s', sid, type(err).__name__)
        return False


async def reconcile_recent(engine):
    """Resolve delayed/previous-version detections, including yesterday's class."""
    store = engine.store
    for outcome in await store.training_history():
        if (outcome['status'] != 'cancelled_unknown' or outcome.get('source') != 'external_sync'
                or outcome['date'] < (date.today() - timedelta(days=7)).isoformat()):
            continue
        sid = outcome['schedule_id']
        key = f'studio_cancellation_check:{sid}'
        last = await store.get_meta(key) or 0
        if time.time() - last < 1800:
            continue
        await store.set_meta(key, time.time())
        session = await store.get_session(sid)
        if session:
            await confirm_removed(engine.syncer, session)
