from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.balance_reminder import BalanceReminder
from app.settings import Settings
from app.store import Store
from app.quota_planner import plan_quota
from test_membership_policy import member, plan


@pytest.fixture
async def reminder(tmp_path):
    store = Store(str(tmp_path/'db')); await store.open(); store.active_box_id = 73
    settings = Settings(str(tmp_path)); settings.select_studio(73)
    settings.update({'balance_reminder': {'enabled':True, 'days_before':14, 'min_entries':5}})
    m = member(1, [1], quota=12)
    # Three booked/reserved entries and three planned leave SIX free, not nine.
    q = plan_quota([m], [plan(i, commitment='reserved', membership_user_id=1) for i in range(1,4)],
                   [plan(i) for i in range(4,7)], '2026-09', anchor='2026-09-16')
    engine = SimpleNamespace(store=store, settings=settings, notifier=SimpleNamespace(send=AsyncMock(return_value=True)),
        syncer=SimpleNamespace(refresh_membership=AsyncMock()), refresh_planning_evidence=AsyncMock(), quota_status=AsyncMock(return_value=q))
    yield BalanceReminder(engine)
    await store.close()


async def test_reminder_subtracts_plans_and_bookings_and_sends_once_per_period(reminder):
    await reminder.check(date(2026, 9, 15))
    reminder.e.notifier.send.assert_not_awaited()
    await reminder.check(date(2026, 9, 16))
    await reminder.check(date(2026, 9, 17))
    reminder.e.notifier.send.assert_awaited_once()
    assert '6 כניסות פנויות' in reminder.e.notifier.send.await_args.args[0]
    assert reminder.e.notifier.send.await_args.kwargs['kind'] == 'membership'


async def test_threshold_is_inclusive_and_failed_delivery_retries(reminder):
    reminder.e.settings.update({'balance_reminder': {'min_entries':6}})
    reminder.e.notifier.send.side_effect = [False, True]
    await reminder.check(date(2026, 9, 16))
    await reminder.check(date(2026, 9, 17))
    await reminder.check(date(2026, 9, 18))
    assert reminder.e.notifier.send.await_count == 2


async def test_below_threshold_uncertain_or_stale_balance_does_not_mislead(reminder):
    reminder.e.settings.update({'balance_reminder': {'min_entries':7}})
    await reminder.check(date(2026, 9, 16))
    reminder.e.notifier.send.assert_not_awaited()
    reminder.e.settings.update({'balance_reminder': {'min_entries':5}})
    reminder.e.quota_status.return_value['memberships'][0]['policy']['state'] = 'sync_pending'
    await reminder.check(date(2026, 9, 16))
    reminder.e.notifier.send.assert_not_awaited()


async def test_card_uses_expiration_not_end_of_month(reminder):
    m = reminder.e.quota_status.return_value['memberships'][0]
    m.update(period='card', period_end='2026-10-05', end='2026-10-05')
    await reminder.check(date(2026, 9, 16))
    reminder.e.notifier.send.assert_not_awaited()
    await reminder.check(date(2026, 9, 21))
    assert '2026-10-05' in reminder.e.notifier.send.await_args.args[0]


def test_settings_validation_and_studio_isolation(tmp_path):
    s = Settings(str(tmp_path)); s.select_studio(73)
    assert s.balance_reminder == {'enabled':False, 'days_before':14, 'min_entries':5}
    s.update({'balance_reminder': {'enabled':True, 'days_before':10, 'min_entries':3}})
    s.select_studio(74)
    assert s.balance_reminder['enabled'] is False
    s.select_studio(73)
    assert s.balance_reminder['min_entries'] == 3
    for value in ({'days_before':0}, {'min_entries':-1}, {'enabled':'yes'}, {'days_before':True}):
        with pytest.raises(ValueError): s.update({'balance_reminder':value})
    assert s.balance_reminder['days_before'] == 10


async def test_card_balance_does_not_subtract_existing_bookings_twice(reminder):
    m = member(1, [1], quota=10, card=True)
    m.update(sessions_left=7, end='2026-09-30')
    q = plan_quota([m], [plan(i, commitment='reserved', membership_user_id=1) for i in range(1,4)],
                   [plan(i) for i in range(4,6)], '2026-09', anchor='2026-09-16')
    assert q['memberships'][0]['available_after_planned'] == 5
    reminder.e.quota_status.return_value = q
    await reminder.check(date(2026, 9, 16))
    assert '5 כניסות פנויות' in reminder.e.notifier.send.await_args.args[0]

async def test_balance_is_not_acknowledged_before_combined_delivery(reminder):
    receipts = []
    await reminder.check(date(2026, 9, 16), deferred=receipts)
    reminder.e.notifier.send.assert_not_awaited()
    assert len(receipts) == 1
    assert not await reminder.e.store.get_meta(receipts[0]['key'])
    await reminder.e.store.set_meta(receipts[0]['key'], receipts[0]['value'])
    retry = []
    await reminder.check(date(2026, 9, 17), deferred=retry)
    assert retry == []
