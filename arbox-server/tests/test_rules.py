import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

import app.rules as rules_module
from app.rules import (
    RulesEngine,
    _arrived_after,
    _vac_key,
    registration_open,
    rule_matches,
)
from app.sync import Syncer
from app.store import Store
from test_membership_policy import engine as prepared_engine, seed_policies, raw


BASE_SESSION = {
    "date": "2026-09-07",  # Monday
    "start_time": "18:00",
    "coach_name": "Dana",
    "category_name": "Pilates",
    "advance_hours": 168,
    "block_hours": 1,
}


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({}, True),
        ({"coaches": ["Dana"]}, True),
        ({"coaches": ["Other"]}, False),
        ({"categories": ["Pilates"]}, True),
        ({"categories": ["Yoga"]}, False),
        ({"weekdays": [0]}, True),
        ({"weekdays": [1]}, False),
        ({"time_from": "19:00"}, False),
        ({"time_to": "17:00"}, False),
    ],
)
def test_rule_matching_filters(patch, expected):
    assert rule_matches(patch, BASE_SESSION) is expected


def test_registration_rejects_unknown_or_invalid_start():
    assert registration_open({"advance_hours": None}, datetime.now())[0] is False
    assert registration_open({"advance_hours": 1, "date": "bad"}, datetime.now())[0] is False


def test_registration_rejects_a_class_that_already_started():
    session = {**BASE_SESSION, "advance_hours": 0}
    allowed, reason = registration_open(session, datetime(2026, 9, 7, 18, 0))
    assert allowed is False
    assert "כבר התחיל" in reason


def test_registration_obeys_both_sides_of_the_window():
    session = {**BASE_SESSION, "advance_hours": 24, "block_hours": 1}
    assert registration_open(session, datetime(2026, 9, 6, 17, 59))[0] is False
    assert registration_open(session, datetime(2026, 9, 6, 18, 0))[0] is True
    assert registration_open(session, datetime(2026, 9, 7, 17, 30))[0] is False


def test_late_session_or_rule_is_not_treated_as_old_backlog():
    opening = datetime(2026, 9, 1, 12, 0)
    old = "2026-09-01T11:59:59"
    new = "2026-09-01T12:00:01"

    assert _arrived_after({"first_seen": new}, [], opening) is True
    assert _arrived_after({"first_seen": old}, [{"created_at": new}], opening) is True
    assert _arrived_after({"first_seen": old}, [{"created_at": old}], opening) is False


@pytest.mark.parametrize(
    ("name", "quota"),
    [
        ("מנוי 5 בחודש", 5),
        ("2 לשבוע", 8),
        ("Unlimited", None),
        (None, None),
    ],
)
def test_membership_quota_parsing(name, quota):
    assert Syncer._parse_plan_quota(name) == quota


@pytest.mark.asyncio
async def test_sync_stores_every_active_membership_and_keeps_legacy_primary():
    class FakeStore:
        def __init__(self):
            self.meta = {"identity": {"membership_user_id": 10}}

        async def get_meta(self, key, default=None):
            return self.meta.get(key, default)

        async def set_meta(self, key, value):
            self.meta[key] = value

    client = SimpleNamespace(memberships=AsyncMock(return_value=[
        {"id": 10, "active": 1, "start": "2026-08-25", "end": None,
         "membership_types": {"id": 1, "name": "5 בחודש",
                              "is_recurring_payment": 1}},
        {"id": 20, "active": 1, "start": "2026-09-02", "end": "2026-10-01",
         "sessions_left": 5, "sessions_on_purchase": 5,
         "membership_types": {"id": 2, "name": "כרטיסיית מובמנט",
                              "is_recurring_payment": 0}},
    ]))
    settings = SimpleNamespace(preferred_membership_id=None)
    store = FakeStore()
    syncer = Syncer(client, store, settings=settings)
    syncer.box_id = 1
    syncer.membership_user_id = 10

    selected = await syncer._store_membership()

    assert selected["id"] == 10
    assert [m["id"] for m in store.meta["memberships"]] == [10, 20]
    assert store.meta["memberships"][1]["sessions_left"] == 5
    assert store.meta["memberships"][1]["end"] == "2026-10-01"


@pytest.mark.asyncio
async def test_multi_membership_quota_separates_used_and_reserved(tmp_path, monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 2)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 2, 12, 0)

    monkeypatch.setattr(rules_module, "date", FixedDate)
    monkeypatch.setattr(rules_module, "datetime", FixedDateTime)
    store = Store(str(tmp_path / "quota.db"))
    await store.open()
    try:
        await store.set_meta("memberships", [
            {"id": 10, "active": True, "start": "2026-08-25", "end": None,
             "plan": "5 בחודש", "plan_quota": 5,
             "sessions_on_purchase": None, "sessions_left": None},
            {"id": 20, "active": True, "start": "2026-09-02", "end": "2026-10-01",
             "plan": "כרטיסיית מובמנט", "plan_quota": None,
             "sessions_on_purchase": 5, "sessions_left": 5},
        ])
        base = {
            "coach": {}, "box_categories": {"id": 2, "name": "Movement"}, "series": {},
            "end_time": "09:30", "user_in_standby": None,
            "booking_option": "cancelScheduleUser",
        }
        await store.upsert_sessions([
            {**base, "id": 1, "date": "2026-09-01", "time": "08:30",
             "user_booked": 101, "_selected_membership_id": 10},
            {**base, "id": 2, "date": "2026-09-03", "time": "08:30",
             "user_booked": 102, "_selected_membership_id": 10},
        ])
        settings = SimpleNamespace(
            preferred_membership_id=10,
            quota_for_month=lambda _month: 0,
        )
        notifier = SimpleNamespace(settings=settings, on_callback=None, on_message=None)
        syncer = SimpleNamespace(membership_user_id=10, memberships=[])
        engine = RulesEngine(store, object(), syncer, notifier)
        await seed_policies(engine)
        status = await engine.quota_status(True)
        assert status["quota"] == 10
        assert status["used"] == 1
        assert status["reserved"] == 1
        assert status["remaining"] == 8

        october = await RulesEngine(
            store, object(), syncer, notifier).quota_status(
                True, target_date="2026-10-03")
        assert october["quota"] == 10
        assert october["used"] == 0
        assert october["reserved"] == 0
        assert october["remaining"] == 10
        assert [m["id"] for m in october["memberships"]] == [10,20]

        expired_card = {
            "id": 20, "active": True, "start": "2026-09-02",
            "end": "2026-10-01", "plan": "כרטיסיית מובמנט",
            "sessions_on_purchase": 5, "sessions_left": 5,
        }
        await store.set_meta("memberships", [expired_card])
        await store.set_meta("membership", expired_card)
        remaining = await RulesEngine(store, object(), syncer, notifier).quota_status(True, target_date="2026-10-03")
        assert remaining["memberships"][0]["end"] == "2026-10-01"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_membership_selector_falls_back_only_on_explicit_restriction(prepared_engine):
    engine = prepared_engine
    restriction = rules_module.ArboxError('not in plan', status=425, body={'error': {'messageToUser': [
        {'name':'classTypeRestricts','value':{'class':'Class 1','allowedText':'Class 2'}}]}})
    engine.client.book.side_effect = [restriction, raw(user_booked=88)]
    _, mid = await engine.perform_membership_action(await engine.store.get_session(77), 'book')
    assert mid == 10
    assert engine.client.book.await_args_list == [call(77,20),call(77,10)]
    assert engine.settings.blocked_categories == []


@pytest.mark.asyncio
async def test_explicit_membership_override_never_silently_falls_back(prepared_engine):
    engine = prepared_engine
    engine.client.book.side_effect = rules_module.ArboxError('not in plan', status=425, body={'error': {'messageToUser': [
        {'name':'classTypeRestricts','value':{'class':'Class 1','allowedText':'Class 2'}}]}})
    with pytest.raises(rules_module.PlanningBlocked):
        await engine.perform_membership_action(await engine.store.get_session(77), 'book', 20)
    engine.client.book.assert_awaited_once_with(77,20)


@pytest.mark.asyncio
async def test_default_selector_uses_membership_valid_on_class_date(prepared_engine):
    engine = prepared_engine
    members = await engine.store.get_meta('memberships')
    members[1]['end'] = '2026-09-30'
    await engine.store.set_meta('memberships',members)
    await engine.store.upsert_sessions([raw(day='2026-10-03')],box_id=73)
    engine.client.book.return_value = raw(day='2026-10-03',user_booked=88)
    _, mid = await engine.perform_membership_action(await engine.store.get_session(77),'book')
    assert mid == 10
    engine.client.book.assert_awaited_once_with(77,10)


@pytest.mark.asyncio
async def test_saved_invalid_override_stays_pending_until_user_repairs_it(prepared_engine):
    engine = prepared_engine
    await engine.store.watch(78,membership_user_id=20)
    selected,ready = await engine._validate_watch_membership(
        {'schedule_id':78,'membership_user_id':20},await engine.store.get_session(78))
    assert (selected,ready) == (20,False)
    assert (await engine.store.list_watchlist())[0]['membership_user_id'] == 20
    engine.client.book.assert_not_awaited()
    engine.notifier.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_plans_claim_capacity_in_opening_order_and_reconcile_once(
    tmp_path, monkeypatch,
):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 2)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 2, 12, 0)

    monkeypatch.setattr(rules_module, "date", FixedDate)
    monkeypatch.setattr(rules_module, "datetime", FixedDateTime)
    store = Store(str(tmp_path / "planned.db"))
    await store.open()
    try:
        regular = {
            "id": 10, "active": True, "start": "2026-08-25", "end": None,
            "plan": "המנוי הרגיל", "plan_quota": 5,
            "sessions_on_purchase": None, "sessions_left": None,
        }
        await store.set_meta("memberships", [regular])
        await store.set_meta("membership", regular)
        base = {
            "time": "10:00", "end_time": "11:00", "coach": {},
            "box_categories": {"id": 2, "name": "Movement"}, "series": {},
            "enable_registration_time": 168, "booking_option": "insertScheduleUser",
            "user_booked": None, "user_in_standby": None,
        }
        for offset in range(10):
            sid = 100 + offset
            await store.upsert_sessions([{
                **base, "id": sid, "date": f"2026-10-{3 + offset:02d}",
            }])
            await store.watch(sid)

        settings = SimpleNamespace(
            preferred_membership_id=20,
            quota_for_month=lambda _month: 0,
            is_blocked=lambda _category: False,
        )
        notifier = SimpleNamespace(
            settings=settings, on_callback=None, on_message=None,
            send=AsyncMock(return_value=True))
        syncer = SimpleNamespace(membership_user_id=20, memberships=[])
        engine = RulesEngine(store, object(), syncer, notifier)
        await seed_policies(engine)

        status = await engine.quota_status(True, target_date="2026-10-03")
        assert status["quota"] == 5
        assert status["planned"] == 5
        assert status["available_after_planned"] == 0
        assert status["plan_allocations"] == {
            str(sid): 10 for sid in range(100, 105)
        }
        assert status["uncovered_plans"] == list(range(105, 110))

        await engine.reconcile_planned_quota()
        await engine.reconcile_planned_quota()
        watches = await store.list_watchlist(pending_only=True)
        assert all(w['membership_user_id'] is None for w in watches)
        notifier.send.assert_awaited_once()
        assert 'ללא כיסוי' in notifier.send.await_args.args[0]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_quota_plans_autobook_rules_but_excludes_vacation(
    tmp_path, monkeypatch,
):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 3)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 3, 12, 0)

    monkeypatch.setattr(rules_module, "date", FixedDate)
    monkeypatch.setattr(rules_module, "datetime", FixedDateTime)
    store = Store(str(tmp_path / "autobook-planned.db"))
    await store.open()
    try:
        membership = {
            "id": 10, "active": True, "start": "2026-09-01", "end": None,
            "plan": "מנוי", "plan_quota": 1,
            "sessions_on_purchase": None, "sessions_left": None,
        }
        await store.set_meta("memberships", [membership])
        await store.set_meta("membership", membership)
        base = {
            "time": "09:30", "end_time": "10:30",
            "coach": {"first_name": "ליאור", "last_name": "מרפי"},
            "box_categories": {"id": 3, "name": "Flex- Back\\Arches"}, "series": {},
            "enable_registration_time": 168,
            "booking_option": "insertScheduleUser",
            "user_booked": None, "user_in_standby": None,
        }
        for sid, day in ((1, "2026-09-07"), (2, "2026-09-14"),
                         (3, "2026-09-21")):
            await store.upsert_sessions([{**base, "id": sid, "date": day}])
        await store.save_rule({
            "name": "ליאור ביום שני", "mode": "autobook", "weekdays": [0],
        })
        await store.add_vacation(
            "2026-09-05", "2026-09-10", False, True)

        settings = SimpleNamespace(
            preferred_membership_id=10,
            quota_for_month=lambda _month: 0,
            is_blocked=lambda _category: False,
        )
        notifier = SimpleNamespace(
            settings=settings, on_callback=None, on_message=None,
            send=AsyncMock(return_value=True))
        syncer = SimpleNamespace(
            membership_user_id=10, memberships=[], box_id=7315)
        engine = RulesEngine(store, object(), syncer, notifier)
        await seed_policies(engine)

        status = await engine.quota_status(True)
        assert status["planned_autobook"] == 2
        assert status["planned_scheduled"] == 0
        assert status["planned_total"] == 2
        assert status["planned"] == 1
        assert status["plan_allocations"] == {"2": 10}
        assert status["uncovered_plans"] == [3]

        await engine.reconcile_planned_quota()
        await engine.reconcile_planned_quota()
        notifier.send.assert_awaited_once()
        assert "21.9" in notifier.send.await_args.args[0]

        await store.set_automation_skip(3, True)
        status = await engine.quota_status(True)
        assert status["planned_autobook"] == 1
        assert status["planned_total"] == 1
        assert status["uncovered_plans"] == []
        await store.set_automation_skip(3, False)
        status = await engine.quota_status(True)
        assert status["planned_autobook"] == 2
        assert status["uncovered_plans"] == [3]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_standby_promotion_refreshes_membership_balance():
    store = SimpleNamespace(
        log_event=AsyncMock(),
        set_meta=AsyncMock(),
    )
    syncer = SimpleNamespace(refresh_membership=AsyncMock(return_value=[]))
    notifier = SimpleNamespace(
        settings=SimpleNamespace(), on_callback=None, on_message=None,
        send=AsyncMock(return_value=True),
    )
    engine = RulesEngine(store, object(), syncer, notifier)

    await engine.on_standby_promoted({
        "schedule_id": 42, "date": "2026-09-03",
        "start_time": "10:00", "end_time": "11:00",
        "category_name": "Movement",
        "coach_name": "Dana",
    })
    await asyncio.sleep(0)

    syncer.refresh_membership.assert_awaited_once()
    store.set_meta.assert_awaited_once_with("quota_cache", None)


@pytest.mark.asyncio
async def test_late_cancel_warning_retries_until_delivery(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 1, 7, 15, 19)

    class FakeStore:
        def __init__(self):
            self.meta = {}
            self.events = []

        async def my_sessions(self, date_from=None):
            return [{
                "schedule_id": 53332639,
                "date": "2026-09-01",
                "start_time": "20:15",
                "category_name": "HS, Mobility & Strength",
                "coach_name": "Dana",
                "user_booked": 183699898,
                "cancel_hours": 12,
            }]

        async def get_meta(self, key, default=None):
            return self.meta.get(key, default)

        async def set_meta(self, key, value):
            self.meta[key] = value

        async def log_event(self, *args, **kwargs):
            self.events.append((args, kwargs))

    class FakeNotifier:
        def __init__(self):
            self.settings = SimpleNamespace(
                late_cancel_warning_minutes=60,
                base_url="http://arbox.example",
            )
            self.on_callback = None
            self.results = [False, True]
            self.sent = []

        async def send(self, text, buttons=None, kind="system", **kwargs):
            self.sent.append((text, buttons, kind))
            return self.results.pop(0)

    monkeypatch.setattr(rules_module, "datetime", FixedDateTime)
    store = FakeStore()
    notifier = FakeNotifier()
    engine = RulesEngine(store, object(), object(), notifier)

    await engine.late_cancel_tick()
    assert "late_cancel_warned" not in store.meta
    assert store.events == []

    await engine.late_cancel_tick()
    assert store.meta["late_cancel_warned"] == [53332639]
    assert len(store.events) == 1
    assert len(notifier.sent) == 2

    await engine.late_cancel_tick()
    assert len(notifier.sent) == 2


class DigestStore:
    def __init__(self, sessions=None, rules=None, blocked=None, plans=None):
        self.sessions = sessions or {}
        self.rules = rules or []
        self.blocked = blocked or set()
        self.prompts = []
        self.plans = plans or []
        self.active_box_id = 73
        self.meta = {}
        self.intents = {}

    async def get_meta(self, key, default=None): return self.meta.get(key, default)
    async def set_meta(self, key, value): self.meta[key] = value
    async def list_auto_intents(self): return []
    async def get_intent(self, sid): return self.intents.get(sid)
    async def remember_intent(self, session, source, signature=None):
        self.intents[session['schedule_id']] = {'source':source, 'rule_signature':signature, 'snapshot':json.dumps(session)}
    async def intent_change(self, session): return None
    async def my_sessions(self, **kwargs): return await self.get_sessions(mine=True)

    async def get_sessions(self, date_from=None, date_to=None, mine=False, **kwargs):
        rows = [
            session
            for day, sessions in self.sessions.items()
            if (date_from is None or day >= date_from)
            and (date_to is None or day <= date_to)
            for session in sessions
        ]
        if mine:
            rows = [
                s for s in rows
                if s.get("user_booked") is not None
                or s.get("user_in_standby") is not None
            ]
        return rows

    async def automation_skip_ids(self): return set()
    async def get_session(self, schedule_id):
        return next((s for s in await self.get_sessions() if s['schedule_id'] == schedule_id), None)
    async def planned_sessions(self, start, end):
        return [p for p in self.plans if start <= p['date'] <= end]
    async def autobook_attempted(self, sid): return False

    async def list_rules(self):
        return self.rules

    async def vacation_blocks(self, day, kind):
        return (day, kind) in self.blocked

    async def add_prompt(self, *args, **kwargs):
        self.prompts.append((args, kwargs))

    async def any_answered(self, cids):
        return False


class DigestNotifier:
    def __init__(self, results=None):
        self.settings = SimpleNamespace(
            is_blocked=lambda category: False,
            base_url="http://arbox.example",
        )
        self.on_callback = None
        self.results = list(results or [True])
        self.sent = []

    async def send(self, text, buttons=None, kind="system", **kwargs):
        self.sent.append((text, buttons, kind, kwargs))
        return self.results.pop(0)


class DigestSyncer:
    def __init__(self):
        self.calls = []

    async def sync_range(self, date_from, date_to):
        self.calls.append((date_from, date_to))

    async def refresh_selected(self, sessions):
        self.calls.append({s['schedule_id'] for s in sessions})
        return {s['schedule_id'] for s in sessions}


def digest_session(schedule_id, day, start, category, coach="Dana", **patch):
    session = {
        "schedule_id": schedule_id,
        "date": day,
        "start_time": start,
        "end_time": "21:15",
        "category_name": category,
        "coach_name": coach,
        "booking_option": "insertScheduleUser",
        "user_booked": None,
        "user_in_standby": None,
        "stand_by_position": None,
        "advance_hours": 168,
        "block_hours": 1,
        "free": 4,
        **patch,
    }
    advance = session.get("advance_hours")
    if advance:
        begins = datetime.fromisoformat(f"{day}T{start}")
        session["registration_opens"] = (
            begins - timedelta(hours=advance)).isoformat()
    return session


def fixed_digest_clock(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 1)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 1, 20, 0)

    monkeypatch.setattr(rules_module, "date", FixedDate)
    monkeypatch.setattr(rules_module, "datetime", FixedDateTime)


@pytest.mark.asyncio
async def test_digest_shows_pinned_morning_class_alongside_evening_suggestions(monkeypatch):
    fixed_digest_clock(monkeypatch)
    morning = digest_session(40, '2026-09-09', '09:30', 'Movement', coach='Noa', advance_hours=72)
    evening = digest_session(41, '2026-09-09', '20:15', 'HS', coach='Noa')
    rule = {'enabled':True, 'mode':'notify', 'categories':[], 'coaches':[], 'weekdays':[]}
    store = DigestStore({'2026-09-09':[morning, evening]}, [rule], plans=[morning])
    notifier = DigestNotifier()
    engine = RulesEngine(store, object(), DigestSyncer(), notifier)
    engine.quota_status = AsyncMock(return_value=None)
    await engine.nightly_digest()
    text = notifier.sent[0][0]
    assert 'כבר מתוכנן לך באותו יום (טרם הוזמן)' in text
    assert '09:30' in text and 'Movement' in text and 'Noa' in text
    assert [p[0][1] for p in store.prompts] == [41]


@pytest.mark.asyncio
async def test_nightly_message_combines_tomorrow_and_future_sections(monkeypatch):
    fixed_digest_clock(monkeypatch)
    notify_rule = {
        "enabled": 1, "mode": "notify", "coaches": [], "categories": [],
        "weekdays": [], "time_from": None, "time_to": None,
    }
    auto_rule = {
        **notify_rule, "mode": "autobook", "categories": ["Auto class"],
    }
    tomorrow = [
        digest_session(1, "2026-09-02", "20:15", "Booked", user_booked=11),
        digest_session(
            2, "2026-09-02", "18:00", "Waitlisted",
            user_in_standby=22, stand_by_position=3,
        ),
    ]
    future = [
        digest_session(3, "2026-09-09", "09:30", "Auto class"),
        digest_session(4, "2026-09-09", "18:30", "Manual class"),
    ]
    store = DigestStore(
        {"2026-09-02": tomorrow, "2026-09-09": future},
        [notify_rule, auto_rule],
    )
    notifier, syncer = DigestNotifier(), DigestSyncer()
    engine = RulesEngine(store, object(), syncer, notifier)
    engine.quota_status = AsyncMock(return_value=None)

    await engine.nightly_digest()

    assert syncer.calls == [{1, 2, 3}]
    assert len(notifier.sent) == 1
    text, buttons, kind, _ = notifier.sent[0]
    assert kind == "digest"
    assert "📌 מחר\n• 20:15 · Booked · Dana" in text
    assert "⏳ עדיין בהמתנה\n• 18:00 · Waitlisted · Dana · מקום 3" in text
    assert "🗓️ ההרשמה נפתחת מחר ליום רביעי, 9.9" in text
    assert "🤖 מתוכנן להזמנה אוטומטית — בכפוף לכיסוי המנוי והמכסה" in text
    assert "🔔 תזכורת להזמנה — השיעור הזה לא יוזמן אוטומטית" in text
    assert "כדי לתזמן הזמנה, בחר שיעור:" in text
    assert len(buttons) == 2  # one manual action row + HA batch-info row
    assert [p[0][1] for p in store.prompts] == [4]


@pytest.mark.asyncio
async def test_vacation_hides_future_section_but_not_tomorrow_reminder(monkeypatch):
    fixed_digest_clock(monkeypatch)
    booked = digest_session(
        1, "2026-09-02", "20:00", "Evening class", user_booked=11)
    store = DigestStore(
        {"2026-09-02": [booked]},
        blocked={("2026-09-09", "notify")},
    )
    notifier, syncer = DigestNotifier(), DigestSyncer()
    engine = RulesEngine(store, object(), syncer, notifier)

    await engine.nightly_digest()

    assert syncer.calls == [{1}]
    assert len(notifier.sent) == 1
    text, buttons, kind, _ = notifier.sent[0]
    assert text == "📌 מחר\n• 20:00 · Evening class · Dana"
    assert buttons is None
    assert kind == "digest"


@pytest.mark.asyncio
async def test_digest_maps_different_class_dates_to_their_real_opening_day(
    monkeypatch,
):
    fixed_digest_clock(monkeypatch)
    notify_rule = {
        "enabled": 1, "mode": "notify", "coaches": [], "categories": [],
        "weekdays": [], "time_from": None, "time_to": None,
    }
    sessions = {
        "2026-09-05": [
            digest_session(31, "2026-09-05", "09:30", "Three-day class",
                           advance_hours=72),
        ],
        "2026-09-09": [
            digest_session(32, "2026-09-09", "18:30", "Seven-day class",
                           advance_hours=168),
        ],
    }
    store = DigestStore(sessions, [notify_rule])
    notifier, syncer = DigestNotifier(), DigestSyncer()
    engine = RulesEngine(store, object(), syncer, notifier)
    engine.quota_status = AsyncMock(return_value=None)

    await engine.nightly_digest()

    text, buttons, kind, _ = notifier.sent[0]
    assert kind == "digest"
    assert "🗓️ ההרשמה נפתחת מחר\n" in text
    assert "יום שבת, 5.9 · 09:30–21:15 · Three-day class" in text
    assert "יום רביעי, 9.9 · 18:30–21:15 · Seven-day class" in text
    assert [row[0]["text"] for row in buttons[:-1]] == [
        "🎯 5.9 09:30 Dana", "🎯 9.9 18:30 Dana",
    ]


@pytest.mark.asyncio
async def test_future_only_all_automatic_needs_no_buttons(monkeypatch):
    fixed_digest_clock(monkeypatch)
    notify_rule = {
        "enabled": 1, "mode": "notify", "coaches": [], "categories": [],
        "weekdays": [], "time_from": None, "time_to": None,
    }
    auto_rule = {**notify_rule, "mode": "autobook"}
    future = digest_session(3, "2026-09-09", "09:30", "Auto class")
    store = DigestStore(
        {"2026-09-09": [future]}, [notify_rule, auto_rule])
    notifier, syncer = DigestNotifier(), DigestSyncer()
    engine = RulesEngine(store, object(), syncer, notifier)

    await engine.nightly_digest()

    assert len(notifier.sent) == 1
    text, buttons, kind, _ = notifier.sent[0]
    assert "📌 מחר" not in text
    assert "🤖 מתוכנן להזמנה אוטומטית — בכפוף לכיסוי המנוי והמכסה" in text
    assert "🔔 תזכורת להזמנה" not in text
    assert buttons is None
    assert kind == "digest"


@pytest.mark.asyncio
async def test_nightly_message_stays_silent_when_both_sections_are_empty(monkeypatch):
    fixed_digest_clock(monkeypatch)
    store = DigestStore(blocked={("2026-09-09", "notify")})
    notifier, syncer = DigestNotifier(), DigestSyncer()
    engine = RulesEngine(store, object(), syncer, notifier)

    await engine.nightly_digest()

    assert notifier.sent == []


@pytest.mark.asyncio
async def test_vacation_announcement_retries_and_mentions_reminders(monkeypatch):
    vacation = {
        "id": 2,
        "date_from": "2026-09-05",
        "date_to": "2026-09-10",
        "block_notify": 1,
        "block_autobook": 1,
        "created_at": "2026-09-01 19:33:21",
    }

    class VacationStore(DigestStore):
        def __init__(self):
            super().__init__()
            self.meta = {}
            self.events = []

        async def vacations_covering(self, day):
            return [vacation] if day == "2026-09-09" else []

        async def get_meta(self, key, default=None):
            return self.meta.get(key, default)

        async def set_meta(self, key, value):
            self.meta[key] = value

        async def log_event(self, *args, **kwargs):
            self.events.append((args, kwargs))

    store = VacationStore()
    notifier = DigestNotifier(results=[False, True])
    engine = RulesEngine(store, object(), object(), notifier)

    await engine._announce_vacation_start("2026-09-09", "2026-09-01")
    assert "vacation_open_announced" not in store.meta
    assert store.events == []

    await engine._announce_vacation_start("2026-09-09", "2026-09-01")
    assert store.meta["vacation_open_announced"] == [_vac_key(vacation)]
    assert len(store.events) == 1
    text, buttons, kind, _ = notifier.sent[-1]
    assert "🏖️ החופשה מתחילה להשפיע מהערב" in text
    assert "תזכורות על אימונים שכבר הזמנת ימשיכו להגיע כרגיל" in text
    assert buttons[0][0]["text"] == "🗑️ בטל את החופשה"
    assert kind == "vacation"


@pytest.mark.asyncio
async def test_vacation_tick_maps_each_class_to_its_real_opening_day(monkeypatch):
    fixed_digest_clock(monkeypatch)
    sessions = {
        "2026-09-05": [
            digest_session(31, "2026-09-05", "09:30", "Three-day class",
                           advance_hours=72),
        ],
        "2026-09-09": [
            digest_session(32, "2026-09-09", "18:30", "Seven-day class",
                           advance_hours=168),
        ],
    }
    engine = RulesEngine(
        DigestStore(sessions), object(), object(), DigestNotifier())
    engine._announce_vacation_start = AsyncMock()
    engine._announce_vacation_end = AsyncMock()

    await engine.vacation_announce_tick()

    assert engine._announce_vacation_start.await_args_list == [
        call("2026-09-05", "2026-09-01"),
        call("2026-09-09", "2026-09-01"),
    ]
    engine._announce_vacation_end.assert_awaited_once_with(
        rules_module.date(2026, 9, 1))


@pytest.mark.asyncio
async def test_opening_job_moves_when_refreshed_schedule_changes(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 1)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 1, 12, 0)

    class OpeningStore:
        async def list_watchlist(self, pending_only=False):
            return [{"schedule_id": 77}]

        async def list_rules(self):
            return []

        async def get_session(self, schedule_id):
            assert schedule_id == 77
            return {
                "schedule_id": 77,
                "date": "2026-09-02",
                "start_time": "20:00",
                "advance_hours": 24,
            }

    class FakeScheduler:
        def __init__(self):
            self.job = SimpleNamespace(
                id="open_77",
                next_run_time=FixedDateTime(2026, 9, 1, 18, 0),
            )
            self.removed = []
            self.added = []

        def get_jobs(self):
            return [self.job]

        def remove_job(self, job_id):
            self.removed.append(job_id)

        def add_job(self, *args, **kwargs):
            self.added.append((args, kwargs))

    monkeypatch.setattr(rules_module, "date", FixedDate)
    monkeypatch.setattr(rules_module, "datetime", FixedDateTime)
    monkeypatch.setattr(rules_module.random, "uniform", lambda *_args: 5)
    engine = RulesEngine(
        OpeningStore(), object(), object(), DigestNotifier())
    engine.scheduler = FakeScheduler()
    engine.reconcile_planned_quota = AsyncMock()

    await engine.schedule_openings()

    assert engine.scheduler.removed == ["open_77"]
    assert len(engine.scheduler.added) == 1
    args, kwargs = engine.scheduler.added[0]
    assert args[1] == "date"
    assert kwargs["id"] == "open_77"
    assert kwargs["run_date"] == FixedDateTime(2026, 9, 1, 20, 0, 5)
    engine.reconcile_planned_quota.assert_awaited_once()


def test_vacation_dedupe_distinguishes_recreated_identical_range():
    base = {
        "id": 2,
        "date_from": "2026-09-05",
        "date_to": "2026-09-10",
        "block_notify": 1,
        "block_autobook": 1,
    }
    old = {**base, "created_at": "2026-08-30 19:30:00"}
    recreated = {**base, "created_at": "2026-09-01 19:30:00"}

    assert _vac_key(old) != _vac_key(recreated)


class AttendanceStore:
    def __init__(self, sessions, tracking_since="2026-09-01"):
        self.sessions = sessions
        self.meta = {"attendance_tracking_since": tracking_since}
        self.outcomes = {}
        self.prompts = []

    async def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    async def set_meta(self, key, value):
        self.meta[key] = value

    async def my_sessions(self, date_from=None):
        return self.sessions

    async def get_training_outcome(self, sid):
        return self.outcomes.get(sid)

    async def set_training_outcome(self, sid, status, source, *reason):
        self.outcomes[sid] = {"status": status, "source": source,
                              "reason": reason}

    async def expire_prompts(self, sid, actions):
        self.expired = (sid, actions)

    async def live_prompt_for(self, sid, action):
        return False

    async def add_prompt(self, cid, sid, action, **kwargs):
        self.prompts.append((cid, sid, action))

    async def delete_prompt(self, cid):
        self.prompts = [p for p in self.prompts if p[0] != cid]

    async def any_answered(self, cids):
        return False

    async def log_event(self, *args, **kwargs):
        self.last_event = (args, kwargs)


@pytest.mark.asyncio
async def test_attendance_question_is_sent_five_minutes_before_start(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 1)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 1, 19, 56)

    monkeypatch.setattr(rules_module, "date", FixedDate)
    monkeypatch.setattr(rules_module, "datetime", FixedDateTime)
    session = digest_session(
        91, "2026-09-01", "20:00", "Movement", user_booked=44)
    store = AttendanceStore([session])
    notifier = DigestNotifier()
    engine = RulesEngine(store, object(), object(), notifier)

    await engine.attendance_tick()

    assert len(store.prompts) == 1
    text, buttons, kind, _ = notifier.sent[0]
    assert text == (
        "🏁 זמן לאימון\n"
        "Movement\n"
        "Dana\n"
        "🕒 20:00–21:15\n\n"
        "הגעת?"
    )
    assert "מקומות" not in text
    assert kind == "attendance"
    assert buttons[0][0]["data"].startswith("attend_yes:")
    assert buttons[0][1]["data"].startswith("attend_no:")


@pytest.mark.asyncio
async def test_attendance_timeout_defaults_to_attended_after_midnight(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 2)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 2, 0, 1)

    monkeypatch.setattr(rules_module, "date", FixedDate)
    monkeypatch.setattr(rules_module, "datetime", FixedDateTime)
    session = digest_session(
        92, "2026-09-01", "20:00", "Movement", user_booked=44)
    store = AttendanceStore([session])
    notifier = DigestNotifier()
    engine = RulesEngine(store, object(), object(), notifier)

    await engine.attendance_tick()

    assert store.outcomes[92] == {
        "status": "attended", "source": "timeout", "reason": (),
    }
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_no_answer_records_missed_then_opens_reason_choices():
    session = digest_session(
        93, date.today().isoformat(), "20:00", "Movement", user_booked=44)
    store = SimpleNamespace(
        peek_prompt=AsyncMock(return_value={
            "schedule_id": 93, "action": "attendance", "dry_run": 0}),
        take_prompt=AsyncMock(return_value={
            "schedule_id": 93, "action": "attendance", "dry_run": 0}),
        get_session=AsyncMock(return_value=session),
        get_training_outcome=AsyncMock(return_value=None),
        set_training_outcome=AsyncMock(),
        add_prompt=AsyncMock(),
    )
    notifier = DigestNotifier()
    engine = RulesEngine(store, object(), object(), notifier)

    result = await engine.handle_callback("attend_no:question")

    assert "בחר" in result
    store.set_training_outcome.assert_awaited_once_with(93, "missed", "manual")
    assert len(notifier.sent[0][1]) == len(engine.REASON_LABELS)
    assert notifier.sent[0][2] == "attendance"


@pytest.mark.asyncio
async def test_free_text_other_reason_is_saved_only_when_armed():
    session = digest_session(
        94, date.today().isoformat(), "20:00", "Movement", user_booked=44)
    store = SimpleNamespace(
        get_meta=AsyncMock(return_value={"schedule_id": 94}),
        get_training_outcome=AsyncMock(return_value={"status": "missed"}),
        set_training_outcome=AsyncMock(),
        set_meta=AsyncMock(),
        get_session=AsyncMock(return_value=session),
        log_event=AsyncMock(),
    )
    engine = RulesEngine(store, object(), object(), DigestNotifier())

    result = await engine.handle_message("  train delay  ")

    assert result == "תודה, הסיבה נשמרה"
    store.set_training_outcome.assert_awaited_once_with(
        94, "missed", "manual", "other", "train delay")
    store.set_meta.assert_awaited_once_with("attendance_other_input", None)
