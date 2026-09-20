import time
import pytest
from unittest.mock import AsyncMock
from app.arbox_client import ArboxError
from app.rules import PlanningBlocked
from test_membership_policy import engine, raw

async def two_plans(engine):
    await engine.store.upsert_sessions([raw(78, cat=1)], box_id=73)
    for sid in (77, 78):
        await engine.store.watch(sid, membership_user_id=20)

@pytest.mark.asyncio
async def test_success_refreshes_history_and_keeps_other_plan_ready(engine):
    await two_plans(engine)
    await engine.perform_membership_action(await engine.store.get_session(77), 'book', 20)
    result = await engine.quota_status()
    assert '77' not in result['plan_states']
    assert result['plan_states']['78']['state'] == 'ready'
    assert engine.client.membership_schedules.await_count == 4  # before and after, two cards
    assert len([r for r in await engine._quota_commitments(await engine.store.get_meta('memberships')) if r['schedule_id'] == 77]) == 1
    await engine.reconcile_planned_quota()
    engine.notifier.send.assert_not_awaited()

@pytest.mark.asyncio
async def test_failed_refresh_preserves_charges_blocks_write_and_recovers_without_booking(engine):
    await two_plans(engine)
    groups = {'past': [], 'future': [], 'lateCancellation': []}
    charge = raw(90, day='2026-09-02', membership_user_fk=20)
    async def history(mid):
        if engine.client.book.await_count:
            raise ArboxError('offline', status=503)
        return {**groups, 'lateCancellation': [charge] if mid == 20 else []}
    engine.client.membership_schedules.side_effect = history
    await engine.perform_membership_action(await engine.store.get_session(77), 'book', 20)
    key = engine.membership_policy.key(20) + ':history'
    evidence = await engine.store.get_meta(key)
    assert not evidence['ok'] and evidence['charges'][0]['schedule_id'] == 90
    assert (await engine.quota_status())['plan_states']['78']['state'] == 'sync_pending'
    await engine.reconcile_planned_quota()
    engine.notifier.send.assert_not_awaited()
    with pytest.raises(PlanningBlocked):
        await engine.perform_membership_action(await engine.store.get_session(78), 'book', 20)
    engine.client.book.assert_awaited_once()
    # Persistent failure is visible and notified once; no manual eligibility buttons.
    for mid in (10,20):
        k=engine.membership_policy.key(mid)+':history'
        e=await engine.store.get_meta(k)
        await engine.store.set_meta(k,{**e,'pending_since':time.time()-901,'checked_at':0})
    await engine.reconcile_planned_quota(); await engine.reconcile_planned_quota()
    engine.notifier.send.assert_awaited_once()
    assert 'אימות המכסה' in engine.notifier.send.await_args.args[0]
    assert engine.notifier.send.await_args.args[1] == []
    engine.client.membership_schedules.side_effect = None
    engine.client.membership_schedules.return_value = groups
    await engine.refresh_pending_evidence()
    assert (await engine.quota_status())['plan_states']['78']['state'] == 'ready'
    engine.client.book.assert_awaited_once()
