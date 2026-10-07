import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.planning_intent import snapshot
from app.rules import RulesEngine
from app.settings import Settings
from app.store import Store
from app.studio_runtime import StudioRuntime
from app.sync import Syncer


def session(box):
    return {'id': box * 100, 'box_fk': box, 'date': date.today().isoformat(),
            'time': '23:58', 'end_time': '23:59', 'user_booked': None,
            'box_categories': {'id': box, 'name': f'Class {box}'},
            'coach': {}, 'series': {}, 'enable_registration_time': 168}


@pytest_asyncio.fixture
async def engine(tmp_path):
    store = Store(str(tmp_path / 'studio-callbacks.db'))
    await store.open()
    await store.set_meta('identity', {'box_id': 10})
    await store.enable_studio_metadata()
    await store.set_meta('studios', [
        {'id': box, 'location_id': box + 1, 'name': f'Studio {box}'} for box in (10, 20)])
    for box in (10, 20):
        store.active_box_id = box
        member = {'id': box * 10, 'active': True, 'plan': f'Membership {box}'}
        await store.set_meta('identity', {'box_id': box, 'location_id': box + 1,
                                         'membership_user_id': member['id']})
        await store.set_meta('membership', member)
        await store.set_meta('memberships', [member])
        await store.set_meta('profile', {'studio': {'name': f'Studio {box}'}})
        await store.upsert_sessions([session(box)], box_id=box)
    store.active_box_id = 10
    settings = Settings(str(tmp_path))
    settings.select_studio(10)
    client = SimpleNamespace(email='same@example.test', user_id=9)
    syncer = Syncer(client, store, settings=settings)
    syncer._apply_studio({'id': 10, 'location_id': 11, 'name': 'Studio 10'})
    syncer.membership_user_id = 100
    syncer.memberships = await store.get_meta('memberships')
    notifier = SimpleNamespace(settings=settings, send=AsyncMock(return_value=True))
    engine = RulesEngine(store, client, syncer, notifier)
    engine.studio_runtime = StudioRuntime(syncer, engine, settings, store)
    yield engine
    await store.close()


async def prompt(engine, box, cid, action, **kwargs):
    async def add():
        await engine.store.add_prompt(cid, box * 100, action, **kwargs)
    await engine.studio_runtime.run_for(box, add)


@pytest.mark.asyncio
async def test_attendance_button_routes_to_prompt_studio_and_restores_selection(engine):
    await prompt(engine, 20, 'second', 'attendance')
    result = await engine.handle_callback('attend_yes:second', source_channel='telegram')
    assert 'סימנתי שהגעת' in result
    assert engine.store.active_box_id == engine.syncer.box_id == 10
    assert await engine.store.get_training_outcome(1000) is None
    outcome = await engine.studio_runtime.run_for(20, engine.store.get_training_outcome, 2000)
    assert outcome['status'] == 'attended'
    assert (await engine.store.peek_prompt('second'))['answered_at']


@pytest.mark.asyncio
async def test_booking_button_uses_prompt_studio_membership_not_selected_panel(engine):
    row = await engine.studio_runtime.run_for(20, engine.store.get_session, 2000)
    await prompt(engine, 20, 'book-second', 'book', payload=json.dumps({'identity': snapshot(row)}))
    calls = []

    async def perform(row, action):
        calls.append((engine.store.active_box_id, engine.syncer.box_id,
                      engine.syncer.membership_user_id, row['schedule_id'], action))
        return {**session(20), 'user_booked': 555}, 200

    engine.perform_membership_action = perform
    engine.send_calendar_file = AsyncMock()
    result = await engine.handle_callback('book:book-second', source_channel='discord')
    assert 'נקבע' in result
    assert calls == [(20, 20, 200, 2000, 'book')]
    assert engine.store.active_box_id == engine.syncer.box_id == 10
    row = await engine.studio_runtime.run_for(20, engine.store.get_session, 2000)
    assert row['user_booked'] == 555


@pytest.mark.asyncio
async def test_free_text_remembers_origin_after_callback_restores_other_selected_studio(engine):
    await prompt(engine, 20, 'other', 'attendance_reason')
    await engine.handle_callback('reason_other:other')
    pending = await engine.store.get_meta('attendance_other_input')
    assert pending['box_id'] == 20
    assert engine.store.active_box_id == 10
    result = await engine.handle_message('missed the train')
    assert result == 'תודה, הסיבה נשמרה'
    outcome = await engine.studio_runtime.run_for(20, engine.store.get_training_outcome, 2000)
    assert outcome['reason_text'] == 'missed the train'
    assert await engine.store.get_meta('attendance_other_input') is None
    assert engine.store.active_box_id == 10


@pytest.mark.asyncio
async def test_unavailable_prompt_studio_does_not_consume_button(engine):
    await prompt(engine, 20, 'ignored', 'attendance')
    engine.settings.set_ignored_studios([20])
    result = await engine.handle_callback('attend_yes:ignored')
    assert 'לא בוצע שינוי' in result
    assert (await engine.store.peek_prompt('ignored'))['answered_at'] is None
    assert engine.store.active_box_id == 10


@pytest.mark.asyncio
async def test_detached_task_captures_origin_before_runtime_restores_selection(engine):
    calls = []

    async def work():
        calls.append((engine.store.active_box_id, engine.syncer.membership_user_id))

    async def spawn():
        return engine.spawn_in_studio(work)

    task = await engine.studio_runtime.run_for(20, spawn)
    assert engine.store.active_box_id == 10
    await task
    assert calls == [(20, 200)]
    assert engine.store.active_box_id == 10


@pytest.mark.asyncio
async def test_callback_waits_for_background_context_to_finish(engine):
    await prompt(engine, 10, 'first', 'attendance')
    entered, release = asyncio.Event(), asyncio.Event()

    async def background():
        entered.set()
        await release.wait()

    bg = asyncio.create_task(engine.studio_runtime.run_for(20, background))
    await entered.wait()
    action = asyncio.create_task(engine.handle_callback('attend_yes:first'))
    await asyncio.sleep(0)
    assert not action.done()
    assert (await engine.store.peek_prompt('first'))['answered_at'] is None
    release.set()
    await bg
    assert 'סימנתי שהגעת' in await action
    assert engine.store.active_box_id == 10


@pytest.mark.asyncio
async def test_opening_jobs_preserve_other_studios_and_execute_in_captured_studio(engine):
    starts = datetime.now() + timedelta(hours=32)
    jobs = {
        'open_10_1000': SimpleNamespace(id='open_10_1000'),
        'open_20_9999': SimpleNamespace(id='open_20_9999'),
    }
    added = []

    def add_job(function, trigger, **kwargs):
        added.append((function, kwargs))
        jobs[kwargs['id']] = SimpleNamespace(id=kwargs['id'], next_run_time=kwargs['run_date'])

    engine.scheduler = SimpleNamespace(get_jobs=lambda: list(jobs.values()),
                                       remove_job=lambda name: jobs.pop(name), add_job=add_job)
    engine.reconcile_planned_quota = AsyncMock()
    executed = []

    async def opening(sid):
        executed.append((engine.store.active_box_id, engine.syncer.membership_user_id, sid))

    engine._opening_fired = opening

    async def arm():
        row = {**session(20), 'date': starts.date().isoformat(),
               'time': starts.strftime('%H:%M'), 'enable_registration_time': 24}
        await engine.store.upsert_sessions([row], box_id=20)
        await engine.store.watch(2000)
        await engine.schedule_openings()

    await engine.studio_runtime.run_for(20, arm)
    assert set(jobs) == {'open_10_1000', 'open_20_2000'}
    assert len(added) == 1
    function, kwargs = added[0]
    assert kwargs['args'][0] == 20
    assert kwargs['args'][2] == 2000
    assert engine.store.active_box_id == 10
    await function(*kwargs['args'])
    assert executed == [(20, 200, 2000)]
    assert engine.store.active_box_id == 10
