import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.arbox_client import ArboxError
from app.planning_intent import token
from app.rules import PlanningBlocked, RulesEngine
from app.sync import Syncer
from test_membership_policy import engine, raw
from test_planning_actions import press, button


@pytest.mark.asyncio
@pytest.mark.parametrize('patch', [
    {'box_categories': {'id': 100, 'name': 'Modern dance'}},
    {'coach': {'id': 4, 'first_name': 'New coach'}},
    {'date': '2026-09-25'}, {'time': '11:00'}, {'end_time': '12:00'},
])
async def test_changed_workout_is_held_without_background_notifications(engine, patch):
    await engine.store.watch(77)
    await engine.store.upsert_sessions([raw(**patch)], box_id=73)
    for _ in range(2):
        q = await engine.quota_status(target_date='2026-09-24')
        assert q['plan_states']['77']['state'] == 'session_changed'
        assert '77' not in q['plan_allocations']
        await engine._watchlist_tick()
        await engine.preflight_plans()
        await engine.reconcile_planned_quota()
    engine.client.book.assert_not_awaited()
    engine.notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_free_places_are_not_a_material_change_but_revert_still_needs_ack(engine):
    await engine.store.watch(77)
    await engine.store.upsert_sessions([raw(free=0, registered=12)], box_id=73)
    assert not await engine.store.intent_change(await engine.store.get_session(77))
    await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
    await engine.store.upsert_sessions([raw()], box_id=73)
    assert await engine.store.intent_change(await engine.store.get_session(77))
    # Selecting a card / reposting a pin must not acknowledge changed details.
    await engine.store.watch(77, membership_user_id=20)
    assert await engine.store.intent_change(await engine.store.get_session(77))
    result = await engine.confirm_plan_change(77, token(await engine.store.get_session(77)))
    assert result['state'] == 'ready'
    assert not await engine.store.intent_change(await engine.store.get_session(77))
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_automatic_occurrence_remains_visible_after_losing_rule_match(engine):
    await engine.store.save_rule({'name':'Class one', 'mode':'autobook', 'categories':['Class 1']})
    await engine._planned_sessions('2026-09-01', '2026-10-31')
    await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
    plans = await engine._planned_sessions('2026-09-01', '2026-10-31')
    assert len(plans) == 1 and plans[0]['intent_change']['state'] == 'session_changed'
    # A membership override cannot reset the saved identity.
    q = await engine.quota_status(target_date='2026-09-24', extra_plans=[{**await engine.store.get_session(77), 'membership_user_id':10}])
    assert q['plan_states']['77']['state'] == 'session_changed'
    await engine.confirm_plan_change(77, token(await engine.store.get_session(77)))
    assert (await engine.store.list_watchlist(pending_only=True))[0]['schedule_id'] == 77


@pytest.mark.asyncio
async def test_last_second_replacement_or_disconnect_never_books(engine):
    await engine.store.watch(77)
    original = await engine.store.get_session(77)
    async def replace(_):
        await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
        return {77}
    engine.syncer.refresh_selected.side_effect = replace
    with pytest.raises(PlanningBlocked, match='השתנו'):
        await engine.perform_membership_action(original, 'book')
    engine.client.book.assert_not_awaited()
    engine.syncer.refresh_selected.side_effect = ArboxError('offline', transient=True)
    with pytest.raises(PlanningBlocked, match='לא ניתן לאמת'):
        await engine.perform_membership_action(original, 'book')
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_confirmation_cannot_accept_a_second_replacement(engine):
    await engine.store.watch(77)
    await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
    expected = token(await engine.store.get_session(77))
    async def replace(_):
        await engine.store.upsert_sessions([raw(cat=3)], box_id=73)
        return {77}
    engine.syncer.refresh_selected.side_effect = replace
    with pytest.raises(PlanningBlocked):
        await engine.confirm_plan_change(77, expected)
    assert await engine.store.intent_change(await engine.store.get_session(77))


@pytest.mark.asyncio
async def test_daily_review_precedes_one_digest_and_survives_restart(engine):
    await engine.store.watch(77)
    await engine.store.upsert_sessions([raw(90, user_booked=900)], box_id=73)
    async def replace(rows):
        assert {s['schedule_id'] for s in rows} == {77,90} # no unrelated 78/79
        await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
        return {77,90}
    engine.syncer.refresh_selected.side_effect = replace
    await asyncio.gather(engine.nightly_digest(), engine.nightly_digest())
    engine.syncer.refresh_selected.assert_awaited_once()
    engine.notifier.send.assert_awaited_once()
    args, kw = engine.notifier.send.call_args
    assert 'Class 1' in args[0] and 'Class 2' in args[0] and 'התכנון מושהה' in args[0]
    assert kw['kind'] == 'digest'
    assert len(args[1][0]) == 1
    restarted = RulesEngine(engine.store, engine.client, engine.syncer, engine.notifier)
    await restarted.nightly_digest()
    engine.notifier.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_selected_refresh_is_read_only_and_does_not_run_sync_hooks(engine):
    await engine.store.watch(77)
    syncer = Syncer(engine.client, engine.store, settings=engine.settings)
    syncer.box_id, syncer.location_id = 73, 42
    syncer.ensure_identity = AsyncMock()
    syncer.on_sync = AsyncMock()
    engine.client.schedule_between = AsyncMock(return_value=[raw(cat=2), raw(78,cat=99)])
    assert await syncer.refresh_selected([await engine.store.get_session(77)]) == {77}
    assert (await engine.store.get_session(78))['category_id'] == 2
    syncer.on_sync.assert_not_awaited()
    engine.client.book.assert_not_awaited()
    assert (await engine.store.intent_change(await engine.store.get_session(77)))['state'] == 'session_changed'


@pytest.mark.asyncio
async def test_deleted_class_remains_held_and_cannot_be_accepted(engine):
    await engine.store.watch(77)
    await engine.store.delete_sessions_in_range_not_in('2026-09-01','2026-09-30',[78,79])
    assert await engine.store.get_session(77)
    plan = (await engine._planned_sessions('2026-09-01','2026-09-30'))[0]
    assert 'אינו מופיע' in plan['intent_change']['reason']
    engine.syncer.refresh_selected.return_value = set()
    engine.syncer.refresh_selected.side_effect = None
    with pytest.raises(PlanningBlocked):
        await engine.confirm_plan_change(77, token(await engine.store.get_session(77)))


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['scheduled','autobook'])
async def test_change_notice_can_cancel_only_that_occurrence(engine, source):
    if source == 'scheduled': await engine.store.watch(77)
    else:
        await engine.store.save_rule({'name':'Class one', 'mode':'autobook', 'categories':['Class 1']})
        await engine._planned_sessions('2026-09-01','2026-09-30')
    await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
    plans = await engine._planned_sessions('2026-09-01','2026-09-30')
    buttons = await engine.planning_actions.notice_buttons([(plans[0], plans[0]['intent_change'], {})])
    result = await press(engine, buttons[0][1])
    assert 'בוטל' in result.text
    assert not await engine._planned_sessions('2026-09-01','2026-09-30')
    assert not engine.settings.blocked_categories
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_upgrade_seeds_old_pin_before_first_new_sync(engine):
    await engine.store.watch(77)
    await engine.store.set_meta('identity', {'box_id':73})
    await engine.store.db.execute('DELETE FROM planning_intents')
    await engine.store.db.commit()
    await engine.store.close()
    await engine.store.open()
    await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
    assert json.loads((await engine.store.get_intent(77))['snapshot'])['category_id'] == 1
    assert await engine.store.intent_change(await engine.store.get_session(77))


@pytest.mark.asyncio
@pytest.mark.parametrize('change_when', ['before_click', 'during_click', 'legacy'])
async def test_old_digest_cannot_schedule_a_replacement(engine, change_when):
    session = await engine.store.get_session(77)
    if change_when == 'legacy':
        await engine.store.add_prompt('old', 77, 'watch')
        data = 'watch:old'
    else:
        buttons, _ = await engine._digest_buttons([session])
        data = buttons[0][0]['data']
    if change_when == 'before_click':
        await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
    elif change_when == 'during_click':
        async def replace(_):
            await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
            return {77}
        engine.syncer.refresh_selected.side_effect = replace
    response = await engine.handle_callback(data)
    assert 'פרטי האימון' in response
    assert not await engine.store.list_watchlist(pending_only=True)
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_confirmation_checks_end_time_and_is_single_use(engine):
    await engine.store.watch(77)
    await engine.store.upsert_sessions([raw(cat=2)], box_id=73)
    plan = (await engine._planned_sessions('2026-09-01', '2026-09-30'))[0]
    buttons = await engine.planning_actions.notice_buttons([(plan, plan['intent_change'], {})])
    await engine.store.upsert_sessions([raw(cat=2, end_time='12:00')], box_id=73)
    assert 'פרטי האימון השתנו' in (await press(engine, buttons[0][0])).text
    buttons = await engine.planning_actions.notice_buttons([(plan, plan['intent_change'], {})])
    assert 'אושר' in (await press(engine, buttons[0][0])).text
    assert 'כבר טופלה' in (await press(engine, buttons[0][0])).text
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_changes_and_many_offers_still_send_one_ha_notification(engine):
    from app.notify import Notifier
    await engine.store.watch(77)
    await engine.store.upsert_sessions([raw(cat=2), *[raw(i, day='2026-09-16') for i in (90,91,92)]], box_id=73)
    await engine.store.save_rule({'name':'Offers', 'mode':'notify', 'categories':['Class 1']})
    await engine.nightly_digest()
    text, buttons = engine.notifier.send.call_args.args
    # All offers remain directly actionable on Telegram.
    assert sum(b.get('data', '').startswith('watch:') for row in buttons for b in row) == 3
    engine.settings.update({'ha': {'enabled':True, 'webhook_url':'http://ha.test/webhook'}})
    sent = []
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Transport:
        def post(self, url, json):
            sent.append(json)
            return Response()
    notifier = Notifier(engine.settings)
    notifier._http = AsyncMock(return_value=Transport())
    await notifier._send_ha(text, buttons)
    assert len(sent) == 1
    assert len(sent[0]['actions']) == 2
    assert sent[0]['actions'][0]['uri'] == '/arbox#schedule'
    assert sent[0]['actions'][1]['title'] == 'בדיקת השינויים באימונים'


@pytest.mark.asyncio
async def test_daily_review_includes_promotion_without_a_second_push(engine):
    await engine.store.upsert_sessions([raw(90, user_in_standby=999)], box_id=73)
    async def promote(rows):
        await engine.store.upsert_sessions([raw(90, user_booked=123)], box_id=73)
        return {90}
    engine.syncer.refresh_selected.side_effect = promote
    await engine.nightly_digest()
    engine.notifier.send.assert_awaited_once()
    assert 'עלית מרשימת ההמתנה' in engine.notifier.send.call_args.args[0]
    engine.syncer.refresh_membership.assert_awaited_once()
