"""Upgrading removes only learning data and keeps opening-time booking."""
import json
from unittest.mock import AsyncMock

import pytest

from app.settings import Settings
from app.store import Store
from test_api import client, api_key
from test_membership_policy import engine, raw

TABLES = ('registration_samples', 'registration_windows',
          'registration_decisions', 'registration_sample_runs')


def test_legacy_waiting_settings_are_removed_without_changing_other_preferences(tmp_path):
    original = Settings(str(tmp_path))
    original.update({'monthly_quota': 14, 'calendar_alarms': [120, 60],
                     'telegram': {'chat_id': '123'}})
    path = tmp_path / 'settings.json'
    saved = json.loads(path.read_text())
    path.write_text(json.dumps({**saved, 'registration_timing': {
        'enabled': True, 'learning_enabled': True,
        'overrides': {'session:73:1': {'mode': 'wait'}}}}))
    upgraded = Settings(str(tmp_path))
    assert json.loads(path.read_text()) == saved
    assert 'registration_timing' not in upgraded.public_view()
    with pytest.raises(ValueError, match='הוסרו'):
        upgraded.update({'registration_timing': {'enabled': True}})
    assert json.loads(path.read_text()) == saved


@pytest.mark.asyncio
async def test_cleanup_is_idempotent_and_preserves_bookings_rules_and_journal(tmp_path):
    store = Store(str(tmp_path / 'upgrade.db'))
    await store.open()
    await store.upsert_sessions([raw(user_booked=88)], box_id=73)
    await store.db.executescript('''
        INSERT INTO rules(id, box_id, name, mode) VALUES(1, 73, 'Weekly', 'autobook');
        INSERT INTO watchlist(schedule_id) VALUES(77);
        INSERT INTO autobook_done(schedule_id, result) VALUES(77, 'booked');
        INSERT INTO training_outcomes(schedule_id, box_id, status, source) VALUES(77, 73, 'attended', 'manual');
        INSERT INTO training_events(schedule_id, box_id, event_type, source) VALUES(77, 73, 'booked', 'manual');
        INSERT INTO workout_journals(schedule_id, box_id, notes) VALUES(77, 73, 'Keep my notes');
        INSERT INTO workout_exercises(schedule_id, box_id, name) VALUES(77, 73, 'Keep my exercise');
    ''')
    for table in TABLES:
        await store.db.execute(f'CREATE TABLE {table}(box_id INTEGER, payload TEXT)')
        await store.db.executemany(f'INSERT INTO {table} VALUES(?,?)', [(73, 'old sample'), (99, 'other studio')])
    kept = ('sessions', 'rules', 'watchlist', 'autobook_done', 'training_outcomes',
            'training_events', 'workout_journals', 'workout_exercises')
    async def snapshot():
        return {t: [dict(r) for r in await (await store.db.execute(f'SELECT * FROM {t}')).fetchall()] for t in kept}
    before = await snapshot()
    await store.db.commit()
    await store.close()
    for _ in range(2):
        await store.open()
        names = {r[0] for r in await (await store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
        assert not names.intersection(TABLES)
        assert await snapshot() == before
        await store.close()


def test_retired_api_and_background_sampler_are_unavailable(client):
    headers = {'X-Api-Key': api_key(client)}
    for path in ('registration-learning', 'registration-guidance'):
        assert client.get('/api/' + path, headers=headers).status_code == 404
    assert client.put('/api/registration-policy/session/1', headers=headers,
                      json={'mode': 'immediate'}).status_code == 404
    assert client.app.state.scheduler.get_job('registration_learning') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['pin', 'rule'])
async def test_open_sessions_book_immediately_despite_legacy_wait_preferences(engine, kind):
    e = engine
    # The fixture clock is 2026-09-08 12:00; registration opens right now.
    await e.store.upsert_sessions([raw(1, day='2026-09-15', time='12:00',
                                      registered=0, max_users=16)], box_id=73)
    e.settings._data['registration_timing'] = {'enabled': True, 'threshold_percent': 30, 'timeout_minutes': 10}
    if kind == 'pin':
        await e.store.watch(1, True)
        e._validate_watch_membership = AsyncMock(return_value=(10, True))
        e._grab_watched = AsyncMock()
        await e.watchlist_tick()
        assert e._grab_watched.await_args.args[0]['schedule_id'] == 1
    else:
        await e.store.save_rule({'name': 'Weekly', 'mode': 'autobook', 'enabled': True})
        e._planned_sessions = AsyncMock(return_value=[])
        e._try_autobook = AsyncMock()
        await e.autobook_tick()
        e._try_autobook.assert_awaited_once()
        assert e._try_autobook.await_args.args[0]['schedule_id'] == 1
