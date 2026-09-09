import asyncio

import pytest

from app.membership_policy import eligible
from app.notification_reply import NotificationReply
from test_membership_policy import engine, raw


async def press(engine, button):
    return await engine.handle_callback(button['data'], source_channel='telegram')


def button(reply, text):
    return next(b for row in reply.buttons for b in row if text in b['text'])


async def notice(engine, sid=77):
    await engine.store.watch(sid)
    plan = next(p for p in await engine._planned_sessions('2026-09-01', '9999-12-31') if p['schedule_id'] == sid)
    return await engine.planning_actions.notice_buttons([(plan, {}, {})])


@pytest.mark.asyncio
async def test_ignore_releases_pending_capacity_preserves_bookings_and_deduplicates(engine):
    await engine.store.upsert_sessions([raw(90, user_booked=123)], box_id=73)
    buttons = await notice(engine)
    before = await engine.quota_status(target_date='2026-09-24')
    assert '77' in before['plan_states']
    preview = await press(engine, buttons[0][0])
    assert not engine.settings.blocked_categories
    yes = button(preview, 'כן,')
    results = await asyncio.gather(press(engine, yes), press(engine, yes))
    assert engine.settings.blocked_categories == ['Class 1']
    assert '77' not in (await engine.quota_status(target_date='2026-09-24'))['plan_states']
    assert (await engine.store.get_session(90))['user_booked'] == 123
    assert sum('נוסף להתעלמות' in r.text for r in results) == 1
    engine.client.book.assert_not_awaited()


@pytest.mark.asyncio
async def test_notice_actions_reject_other_studio_changed_class_and_changed_selection(engine):
    buttons = await notice(engine)
    engine.syncer.box_id = 74
    assert 'לא שייכת' in (await press(engine, buttons[0][0])).text
    engine.syncer.box_id = 73
    await engine.store.upsert_sessions([raw(box_categories={'id': 5, 'name': 'Different class'})], box_id=73)
    assert 'פרטי האימון השתנו' in (await press(engine, buttons[0][0])).text
    assert not engine.settings.blocked_categories


@pytest.mark.asyncio
async def test_assigns_only_the_occurrence_and_checks_capacity_again(engine):
    buttons = await notice(engine)
    choices = await press(engine, buttons[0][1])
    assert await engine.store.any_answered([buttons[0][1]['data'].split(':',1)[1]])
    confirmation = await press(engine, button(choices, 'Membership 20'))
    result = await press(engine, button(confirmation, 'מאשר ומשייך'))
    assert isinstance(result, NotificationReply) and 'שויך' in result.text
    assert (await engine.store.list_watchlist(pending_only=True))[0]['membership_user_id'] == 20
    assert engine.syncer.membership_user_id == 10
    engine.client.book.assert_not_awaited()
    assert 'כבר טופלה' in (await press(engine, button(confirmation, 'מאשר ומשייך'))).text


@pytest.mark.asyncio
async def test_confirmation_rechecks_newly_used_capacity(engine):
    buttons = await notice(engine)
    choices = await press(engine, buttons[0][1])
    confirmation = await press(engine, button(choices, 'Membership 20'))
    members = await engine.store.get_meta('memberships')
    members[1]['sessions_left'] = 0
    await engine.store.set_meta('memberships', members)
    result = await press(engine, button(confirmation, 'מאשר ומשייך'))
    assert 'לא ניתן לשייך' in result.text
    assert (await engine.store.list_watchlist(pending_only=True))[0]['membership_user_id'] is None


@pytest.mark.asyncio
async def test_manual_confirmation_only_allows_selected_category(engine):
    member = (await engine.store.get_meta('memberships'))[1]
    await engine.store.set_meta(engine.membership_policy.key(20), {})
    buttons = await notice(engine, 78)
    choices = await press(engine, buttons[0][1])
    confirmation = await press(engine, button(choices, 'Membership 20'))
    assert 'נדרש אישורך' in confirmation.text
    policy = await engine.membership_policy.get(member)
    assert not eligible({**member, 'policy': policy}, {'category_id': 2})
    result = await press(engine, button(confirmation, 'מאשר ומשייך'))
    assert 'שויך' in result.text
    policy = await engine.membership_policy.get(member)
    assert eligible({**member, 'policy': policy}, {'category_id': 2})
    assert not eligible({**member, 'policy': policy}, {'category_id': 3})
    assert policy['categories_known'] is False
    # Fresh explicit studio restrictions take precedence over a partial confirmation.
    assert not eligible({**member, 'policy': {**policy, 'categories_known':True, 'category_ids':[1]}}, {'category_id':2})


@pytest.mark.asyncio
async def test_manual_ignore_already_excluded_before_next_tick(engine):
    await engine.store.watch(77)
    engine.settings.block_category('Class 1')
    assert not await engine._planned_sessions('2026-09-01', '2026-10-31')


@pytest.mark.asyncio
async def test_multiple_workouts_and_members_never_exceed_ha_action_limit(engine):
    for sid in [77,78,79]: await engine.store.watch(sid)
    plans = await engine._planned_sessions('2026-09-01', '2026-10-31')
    buttons = await engine.planning_actions.notice_buttons([(p,{}, {}) for p in plans])
    reply = await press(engine, buttons[0][0])
    assert sum(map(len, reply.buttons)) <= 3
    for row in reply.buttons:
        for b in row: assert len(b['data'].encode()) <= 64


@pytest.mark.asyncio
async def test_unknown_membership_cannot_bypass_denial_or_stale_history(engine):
    member = (await engine.store.get_meta('memberships'))[1]
    key = engine.membership_policy.key(member['id'])
    await engine.store.set_meta(key, {})
    session = await engine.store.get_session(78)
    await engine.store.watch(78)
    await engine.store.set_meta(key + ':history', {'ok':False})
    option = await engine.planning_actions.member_option({}, session, member)
    assert not option['available']
    from app.membership_policy import fingerprint
    await engine.store.set_meta(key, {'fingerprint':fingerprint(member), 'denied_category_ids':[2]})
    option = await engine.planning_actions.member_option({}, session, member)
    assert not option['available']


@pytest.mark.asyncio
async def test_membership_revision_change_and_other_selection_reject_old_confirmation(engine):
    buttons = await notice(engine)
    choices = await press(engine, buttons[0][1])
    confirmation = await press(engine, button(choices, 'Membership 20'))
    members = await engine.store.get_meta('memberships')
    members[1]['membership_type_id'] = 999
    await engine.store.set_meta('memberships', members)
    assert 'פרטי המנוי השתנו' in (await press(engine, button(confirmation, 'מאשר ומשייך'))).text
    await engine.store.set_watch_membership(77,10)
    assert 'בחירת המנוי כבר השתנתה' in (await press(engine, buttons[0][0])).text
