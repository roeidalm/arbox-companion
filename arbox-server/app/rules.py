"""Rule evaluation: the nightly digest and the auto-book watcher.

A rule matches a session on coach / category / weekday / time-window.
mode=notify  -> the session appears in the nightly digest with a book button
mode=autobook -> the server books it itself the moment registration opens
                 (registration_opens = class start - the per-session
                 enable_registration_time supplied by Arbox)
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
from datetime import date, datetime, timedelta

from .arbox_client import ArboxClient, ArboxError
from .ical import build_calendar, google_calendar_url
from .journal import parse_exercise_text
from .journal_flow import feedback_buttons, initial_prompt, notes_prompt
from .journal_preview import JournalPreview
from .feedback_form import FeedbackForm
from .notify import Notifier
from .store import NO_SCHEDULE, Store
from .studio_context import ReentrantAsyncLock
from .sync import FULL_WINDOW_DAYS, Syncer
from .membership_policy import MembershipPolicy, eligible
from .quota_planner import plan_quota, REASONS
from .planning_actions import PlanningActions

_LOGGER = logging.getLogger(__name__)

# jitter applied after a registration window opens (seconds)
OPENING_JITTER_SECONDS = (5, 25)

# how long after a window opens autobook still treats it as a live trigger
CATCHUP_GRACE_HOURS = 6

# how long a quota reading stays good enough to serve without asking Arbox
QUOTA_TTL_SECONDS = 600


class PlanningBlocked(ArboxError):
    """A retained intent needs user input; it must not become a failed attempt."""
    def __init__(self, reason: str):
        super().__init__(reason, status=409)

# transient upstream failures tolerated before a booking is called off. Six
# five-minute ticks is half an hour of retrying a class that stays bookable
# for days — long enough to ride out a blip, short enough to stop.
TRANSIENT_MAX_ATTEMPTS = 6


def _newer_than(stamp: str | None, moment: datetime) -> bool:
    """Did this row appear after `moment`? An unknown stamp reads as older,
    which is the safe direction: it keeps the catch-up cap in force."""
    if not stamp:
        return False
    try:
        return datetime.fromisoformat(str(stamp)) > moment
    except (TypeError, ValueError):
        return False


def _arrived_after(session: dict, rules: list[dict], moment: datetime) -> bool:
    """True when the class or the rule that wants it is newer than `moment` —
    i.e. nobody could have booked it at the opening, so this is not a backlog."""
    if _newer_than(session.get("first_seen"), moment):
        return True
    return any(_newer_than(r.get("created_at"), moment) for r in rules)


def rule_matches(rule: dict, session: dict) -> bool:
    if rule.get("coaches") and session.get("coach_name") not in rule["coaches"]:
        return False
    if rule.get("categories") and session.get("category_name") not in rule["categories"]:
        return False
    if rule.get("weekdays"):
        wd = datetime.fromisoformat(session["date"]).weekday()  # 0=Mon
        if wd not in rule["weekdays"]:
            return False
    t = session.get("start_time") or ""
    if rule.get("time_from") and t < rule["time_from"]:
        return False
    if rule.get("time_to") and t > rule["time_to"]:
        return False
    return True


def opening_moment(start: datetime, advance: int) -> datetime:
    """When the registration window opens, erring early across DST.

    The window is a duration, and the two ways to subtract it disagree by an
    hour on the two weekends a year the clocks move: naive wall-clock
    arithmetic (start - 168h on the calendar) versus real elapsed time. Which
    one Arbox uses is not observable from here, so take whichever comes first.

    Being early is free — Arbox answers registerScheduleDisabled and both the
    pin path and the autobook path already treat that as "not yet, try again"
    without marking anything. Being late costs the class.
    """
    naive = start - timedelta(hours=advance)
    try:
        elapsed = (start.astimezone() - timedelta(hours=advance)) \
            .astimezone().replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return naive
    return min(naive, elapsed)


def registration_open(s: dict, now: datetime) -> tuple[bool, str]:
    """Can this class be booked right now?

    booking_option is not a gate — Arbox reports insertScheduleUser for
    classes still outside their window and only rejects at insert time with
    425 registerScheduleDisabled — so the window is evaluated here.
    """
    advance = s.get("advance_hours")
    if advance is None:
        return False, "אין מידע על חלון ההרשמה"

    try:
        start = datetime.fromisoformat(f"{s['date']}T{s['start_time']}")
    except (KeyError, TypeError, ValueError):
        return False, "שעת התחלה לא תקינה"

    # A class that has already begun is not bookable, whatever the window
    # says. Classes with no cancellation bound and advance_hours 0 ("always
    # open") otherwise read as open all day, and today's finished 08:00 class
    # would be booked in the evening — by a revived pin, or by a rule created
    # after it ended.
    if now >= start:
        return False, "השיעור כבר התחיל"

    block = s.get("block_hours")
    if block:
        # the near-side bound: too close to the start to book
        if now > start - timedelta(hours=block):
            return False, f"ההרשמה נסגרת {block} שעות לפני"

    if advance == 0:
        return True, "פתוח להרשמה (בלי הגבלת זמן מראש)"

    opens_at = opening_moment(start, advance)
    if now < opens_at:
        return False, f"נפתח ב-{opens_at:%d/%m %H:%M}"
    return True, "פתוח להרשמה"


WEEKDAYS_HE = ("שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון")

def _he_day(iso_date: str) -> str:
    try:
        return WEEKDAYS_HE[date.fromisoformat(iso_date).weekday()]
    except ValueError:
        return ""


def _he_short_date(iso_date: str) -> str:
    """"יום שני, 14.9" — compact enough for a Telegram section title."""
    try:
        d = date.fromisoformat(iso_date)
        return f"יום {WEEKDAYS_HE[d.weekday()]}, {d.day}.{d.month}"
    except ValueError:
        return iso_date


def _esc_html(s: str) -> str:
    """Telegram HTML parse_mode: a stray & or < in a class name would make
    the whole caption fail to send."""
    return (str(s or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def fmt_alarm(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return "יום לפני" if d == 1 else f"{d} ימים לפני"
    if minutes % 60 == 0:
        h = minutes // 60
        return "שעה לפני" if h == 1 else f"{h} שעות לפני"
    return f"{minutes} דק׳ לפני"


def fmt_when(s: dict) -> str:
    """"ראשון 30.8 · 08:00" — how the log names a class.

    The stored date is ISO; spelled out in a log line it wraps onto two rows
    on a phone and reads like a database, not like a class you go to.
    """
    day, time = s.get("date") or "", (s.get("start_time") or "")[:5]
    try:
        d = date.fromisoformat(day)
        stamp = f"{WEEKDAYS_HE[d.weekday()]} {d.day}.{d.month}"
    except ValueError:
        stamp = day
    return f"{stamp} · {time}" if time else stamp


def fmt_class(s: dict) -> str:
    """The full "when + what" label used across the event log."""
    bits = [fmt_when(s), s.get("category_name") or "שיעור"]
    if s.get("coach_name"):
        bits.append(s["coach_name"])
    return " · ".join(bits)


def planning_notice(problems: list[tuple[dict, dict, dict]]) -> tuple[str, list[str]]:
    """A short notice; show a quota fraction only for one matching monthly plan."""
    capacity = all(state.get('state') == 'no_capacity' for _, state, _ in problems)
    heading = '🎟️ אין יתרה במנוי לאימונים האלה' if capacity else '⏸️ אימונים דורשים בדיקה'
    breakdown = ''
    matches = {}
    if capacity and len({s['date'][:7] for s, _, _ in problems}) == 1:
        for session, _, quota in problems:
            for member in quota.get('memberships', []):
                if (member.get('active') and eligible(member, session)
                        and (not member.get('start') or member['start'] <= session['date'])
                        and (not member.get('end') or member['end'] >= session['date'])
                        and session.get('membership_user_id') in (None, member['id'])):
                    matches[member['id']] = member
        if len(matches) == 1:
            m = next(iter(matches.values()))
            committed = sum(m.get(k, 0) for k in ('used', 'reserved', 'planned', 'standby', 'uncertain'))
            if m.get('period') == 'month' and m.get('quota') is not None and committed >= m['quota']:
                heading = f"🎟️ המכסה החודשית מלאה — {committed}/{m['quota']}"
                breakdown = f"{m['used']} נוצלו · {m['reserved']} מוזמנים · {m['planned']} " + ('מתוכנן' if m['planned'] == 1 else 'מתוכננים')
                if m.get('standby'): breakdown += f" · {m['standby']} בהמתנה"
                if m.get('uncertain'): breakdown += f" · {m['uncertain']} בבירור"
    count = len(problems)
    subject = 'אימון אחד נשאר' if count == 1 else 'שני אימונים נשארו' if count == 2 else f'{count} אימונים נשארו'
    introduction = subject + ' ללא כיסוי:' if capacity else 'נדרשת בדיקה לפני ההרשמה:'
    lines = [heading] + ([breakdown] if breakdown else []) + ['', introduction]
    bold = [heading, introduction]
    for session, state, _ in problems:
        when = fmt_when(session)
        lines += ['', when, ' · '.join(filter(None, [session.get('category_name') or 'שיעור', session.get('coach_name')]))]
        bold.append(when)
        if not capacity:
            lines.append(state.get('reason') or 'יש לבדוק את פרטי המנוי')
    footer = 'נשמרו בתכנון · ממתינים למכסה' if capacity else 'נשמרו בתכנון · ההרשמה מושהית'
    return '\n'.join(lines) + '\n\n\n' + footer, bold


def fmt_vac_range(v: dict) -> str:
    """"ראשון 7.9 – שבת 13.9", or a single day when from == to."""
    try:
        a = date.fromisoformat(v["date_from"])
        b = date.fromisoformat(v["date_to"])
    except (KeyError, TypeError, ValueError):
        return f"{v.get('date_from')} – {v.get('date_to')}"
    one = lambda d: f"{WEEKDAYS_HE[d.weekday()]} {d.day}.{d.month}"
    return one(a) if a == b else f"{one(a)} – {one(b)}"


def _vac_scope(v: dict) -> str:
    """What this vacation actually silences. The API rejects one that blocks
    neither, so the branches are exhaustive."""
    if v["block_notify"] and v["block_autobook"]:
        return "לא אשלח הצעות הרשמה ולא אזמין אוטומטית"
    if v["block_notify"]:
        return "לא אשלח הצעות הרשמה (הזמנה אוטומטית ממשיכה כרגיל)"
    return "לא אזמין אוטומטית (ההודעה הלילית ממשיכה כרגיל)"


def _vac_key(v: dict) -> str:
    """Dedupe one concrete creation of a vacation.

    Older databases predate AUTOINCREMENT and may reuse an id after delete.
    created_at distinguishes a newly-created identical range from the one the
    user just cancelled, while range/flags still make in-place edits speak.
    """
    return (f"{v['id']}:{v['date_from']}:{v['date_to']}"
            f":{int(bool(v['block_notify']))}{int(bool(v['block_autobook']))}"
            f":{v.get('created_at') or ''}")


def _vac_redundant(v: dict, others: list[dict]) -> bool:
    """Is v wholly inside a range that announces earlier?

    Two overlapping vacations describe one silence; only the outer one should
    speak. Ties on date_from break by id so exactly one of them does.
    """
    for w in others:
        if w["id"] == v["id"]:
            continue
        if ((w["date_from"], w["id"]) < (v["date_from"], v["id"])
                and w["date_from"] <= v["date_from"]
                and w["date_to"] >= v["date_to"]):
            return True
    return False


def _fmt_session(s: dict) -> str:
    bits = [f"{s['start_time']}–{s['end_time'] or '?'}", s.get("category_name") or "שיעור"]
    if s.get("coach_name"):
        bits.append(s["coach_name"])
    free = s.get("free")
    if s.get("booking_option") == "insertStandby":
        bits.append("(מלא — המתנה)")
    elif free is not None:
        bits.append(f"({free} מקומות)")
    return " · ".join(bits)


def _fmt_attendance_question(s: dict) -> str:
    """Keep the arrival prompt focused on the class, not its availability."""
    name = s.get("category_name") or "שיעור"
    coach = s.get("coach_name")
    heading = "\n".join(part for part in (name, coach) if part)
    start = s.get("start_time") or "?"
    end = s.get("end_time") or "?"
    return f"🏁 זמן לאימון\n{heading}\n🕒 {start}–{end}\n\nהגעת?"


def _fmt_tomorrow_session(s: dict) -> str:
    """The short, commitment-focused row used by the nightly reminder."""
    bits = [(s.get("start_time") or "?")[:5],
            s.get("category_name") or "שיעור"]
    if s.get("coach_name"):
        bits.append(s["coach_name"])
    if s.get("user_in_standby") is not None and s.get("stand_by_position"):
        bits.append(f"מקום {s['stand_by_position']}")
    return " · ".join(bits)


def _registration_opens_on(s: dict, day: str) -> bool:
    """Whether this session's actual registration opening falls on ``day``."""
    value = s.get("registration_opens")
    if value:
        try:
            return datetime.fromisoformat(value).date().isoformat() == day
        except (TypeError, ValueError):
            pass
    advance = s.get("advance_hours")
    if not advance:
        return False
    try:
        start = datetime.fromisoformat(f"{s['date']}T{s['start_time']}")
        return opening_moment(start, int(advance)).date().isoformat() == day
    except (KeyError, TypeError, ValueError):
        return False


class RulesEngine:
    def __init__(
        self, store: Store, client: ArboxClient, syncer: Syncer, notifier: Notifier
    ) -> None:
        self.store = store
        self.client = client
        self.syncer = syncer
        self.notifier = notifier
        self.settings = notifier.settings
        self.membership_policy = MembershipPolicy(store, client, syncer)
        self.journal_preview = JournalPreview(notifier)
        self.feedback_form = FeedbackForm(store, notifier)
        self._journal_lock = asyncio.Lock()
        self.scheduler = None   # set at startup; enables exact-moment grabs
        # Both ticks are driven from five places — their own 5-minute jobs,
        # every one-shot opening job, the vacation callback, and two API
        # routes — and APScheduler's max_instances only guards a job against
        # itself. Two overlapping runs read the same pending row before either
        # writes, and book the same class twice.
        # A tick may refresh memberships, and an API booking may invoke a
        # tick. One task-reentrant operation lock avoids inverted lock orders
        # and keeps the selected studio stable throughout those operations.
        # Lightweight rule-engine adapters may omit sync serialization.
        exclusive = getattr(syncer, "exclusive", None)
        self._tick_lock = exclusive() if exclusive else ReentrantAsyncLock()
        self._membership_lock = self._tick_lock
        self.planning_actions = PlanningActions(self)
        notifier.on_callback = self.handle_callback
        notifier.on_message = self.handle_message

    ATTENDANCE_LEAD_MINUTES = 5
    REASON_LABELS = {
        "work": "💼 עבודה",
        "illness": "🤒 מחלה",
        "injury": "🩹 פציעה",
        "personal": "🏠 אילוץ אישי/משפחתי",
        "fatigue": "😴 עייפות/התאוששות",
        "plans_changed": "🔄 שינוי תוכניות",
        "other": "✍️ אחר",
        "none": "➖ ללא סיבה",
    }

    # ---------------------------------------------------------------- quota

    @staticmethod
    def _membership_valid_on(membership: dict, target: str) -> bool:
        return bool(membership.get("active")
                    and (not membership.get("start")
                         or membership["start"] <= target)
                    and (not membership.get("end")
                         or membership["end"] >= target))

    async def _active_memberships(self, on_date: str | date | None = None) -> list[dict]:
        target = (on_date.isoformat() if isinstance(on_date, date)
                  else on_date or date.today().isoformat())
        memberships = await self.store.get_meta("memberships") or []
        return [m for m in memberships if self._membership_valid_on(m, target)]

    async def _planned_sessions(
        self, start: str, end: str, extra_plans: list[dict] | None = None,
        *, include_skipped: bool = False,
    ) -> list[dict]:
        """All future booking intents, ordered by registration opening.

        A pin is explicit intent; an enabled autobook rule is intent too.  The
        old quota planner only saw pins, so recurring automation could silently
        overbook the month.  Vacations are evaluated here, before capacity is
        claimed, because a class we already know will be skipped is not a plan.
        """
        skipped = await self.store.automation_skip_ids()
        plans: list[dict] = []
        seen: set[int] = set()
        stored = await self.store.planned_sessions(start, end)
        for raw in [*(extra_plans or []), *stored]:
            plan = dict(raw)
            sid = int(plan["schedule_id"])
            if sid in seen or not start <= plan.get("date", "") <= end:
                continue
            current_session = await self.store.get_session(sid) or plan
            if self._blocked(current_session):
                continue
            if not plan.get("ignore_vacation") and await self.store.vacation_blocks(
                    plan["date"], "autobook"):
                continue
            plan["planning_source"] = "scheduled"
            plans.append(plan)
            seen.add(sid)

        rules = [r for r in await self.store.list_rules()
                 if r["enabled"] and r["mode"] == "autobook"]
        if rules:
            now = datetime.now()
            for session in await self.store.get_sessions(date_from=start, date_to=end):
                sid = int(session["schedule_id"])
                if sid in seen or session.get("user_booked") is not None \
                        or session.get("user_in_standby") is not None:
                    continue
                try:
                    starts = datetime.fromisoformat(
                        f"{session['date']}T{session['start_time']}")
                except (KeyError, TypeError, ValueError):
                    continue
                if starts <= now or not any(rule_matches(r, session) for r in rules):
                    continue
                if self._blocked(session) or await self.store.vacation_blocks(
                        session["date"], "autobook"):
                    continue
                if sid in skipped and not include_skipped:
                    continue
                if await self.store.autobook_attempted(sid):
                    continue
                plans.append({
                    **session,
                    "membership_user_id": None,
                    "created_at": min(
                        (r.get("created_at") or "" for r in rules
                         if rule_matches(r, session)), default=""),
                    "planning_source": "autobook",
                    "automation_skipped": sid in skipped,
                })
                seen.add(sid)

        plans.sort(key=lambda p: (
            p.get("registration_opens") or
            f"{p.get('date', '9999-12-31')}T{p.get('start_time') or '23:59'}",
            p.get("created_at") or "", int(p.get("schedule_id") or 0),
        ))
        return plans

    async def uncertain_sessions(self) -> list[dict]:
        """Interrupted writes remain visible, including unpinned manual actions.

        These are reconciliation records, not instructions to book again.
        Keep past sessions visible until their outcome is resolved too.
        """
        pending = await self.store.get_meta(self.membership_policy.key(0) + ':uncertain') or {}
        rows = []
        for sid, operation in pending.items():
            session = await self.store.get_session(int(sid)) or operation.get('session')
            if session and session.get('user_booked') is None and session.get('user_in_standby') is None:
                rows.append({**session, 'membership_user_id': operation['membership_user_id'],
                             'planning_source': 'uncertain'})
        return rows

    async def _quota_commitments(self, members: list[dict]) -> list[dict]:
        """The identical charge ledger for rendering, early probes and booking."""
        commitments = await self.store.quota_commitments(
            '0001-01-01', '9999-12-31', datetime.now().strftime('%Y-%m-%d %H:%M'))
        indexed = {r['schedule_id']: r for r in commitments}
        for member in members:
            history = await self.store.get_meta(self.membership_policy.key(member['id']) + ':history') or {}
            for row in history.get('charges', []):
                if row['schedule_id'] not in indexed:
                    indexed[row['schedule_id']] = row
                elif (indexed[row['schedule_id']].get('status') == 'cancelled_late'
                      and indexed[row['schedule_id']].get('membership_user_id') is None):
                    indexed[row['schedule_id']]['membership_user_id'] = member['id']
        for row in await self.uncertain_sessions():
            if row['schedule_id'] not in indexed:
                indexed[row['schedule_id']] = {**row, 'commitment': 'uncertain'}
        return list(indexed.values())

    async def quota_status(
        self, force: bool = False, target_date: str | date | None = None,
        extra_plans: list[dict] | None = None,
        policy_overrides: dict | None = None,
    ) -> dict | None:
        """Compute from one evidence-backed ledger. Rendering never calls Arbox."""
        anchor = target_date.isoformat() if isinstance(target_date, date) else target_date or date.today().isoformat()
        members = await self.store.get_meta("memberships") or []
        plans = await self._planned_sessions(date.today().isoformat(), "9999-12-31", extra_plans)
        pending_rows = await self.uncertain_sessions()
        if not members and not plans and not pending_rows:
            return None
        details = [{**m, "policy": (policy_overrides or {}).get(m['id']) or await self.membership_policy.get(m)} for m in members]
        import time
        for detail in details:
            history = await self.store.get_meta(self.membership_policy.key(detail["id"]) + ":history") or {}
            if not history.get("ok") or time.time() - history.get("checked_at", 0) > 86400:
                detail["policy"] = {**detail["policy"], "state": "needs_review",
                                    "reason": "היסטוריית המנוי טרם אומתה — נדרש סנכרון"}
        commitments = await self._quota_commitments(members)
        uncertain = await self.store.get_meta(self.membership_policy.key(0) + ":uncertain") or {}
        planned_ids = {p['schedule_id'] for p in plans}
        plans.extend(row for row in pending_rows if row['schedule_id'] not in planned_ids)
        return plan_quota(details, commitments, plans, anchor[:7], uncertain, anchor=anchor)

    async def refresh_planning_evidence(self, *, force_history: bool = False) -> None:
        """Read-only review; failures are cached, never retried on every render."""
        import time
        async with self._membership_lock:
            members = await self.store.get_meta("memberships") or []
            await self.membership_policy.refresh(members)
            for member in members:
                key = self.membership_policy.key(member["id"]) + ":history"
                previous = await self.store.get_meta(key) or {}
                ttl = 300 if member.get('active') else 86400
                if (not force_history or not member.get('active')) and time.time() - previous.get("checked_at", 0) < ttl:
                    continue
                evidence = {"checked_at": time.time(), "charges": previous.get('charges', [])}
                try:
                    groups = await self.client.membership_schedules(member["id"])
                    await self.store.reconcile_membership_history(member["id"], groups)
                    evidence["ok"] = True
                    evidence['charges'] = [
                        {'schedule_id': r['id'], 'date': r['date'], 'membership_user_id': member['id'],
                         'commitment':'used'} for r in groups['lateCancellation']]
                except (ArboxError, ValueError) as err:
                    evidence.update(ok=False, error=str(err))
                await self.store.set_meta(key, evidence)
            # A positive upstream result resolves an interrupted write. Absence
            # is not proof of rejection and never automatically permits a retry.
            key = self.membership_policy.key(0) + ":uncertain"
            pending = await self.store.get_meta(key) or {}
            for sid in list(pending):
                session = await self.store.get_session(int(sid)) or {}
                if session.get("user_booked") is not None or session.get("user_in_standby") is not None:
                    pending.pop(sid)
            await self.store.set_meta(key, pending)

    async def review_plans(self) -> None:
        async with self._membership_lock:
            await self.migrate_learned_blocks()
            await self.refresh_planning_evidence()
            await self.preflight_plans()
            await self.reconcile_planned_quota()

    async def migrate_learned_blocks(self) -> None:
        key = self.membership_policy.key(0) + ":legacy_blocks_migrated"
        if await self.store.get_meta(key):
            return
        cur = await self.store.db.execute(
            "SELECT DISTINCT s.category_name FROM events e JOIN sessions s "
            "ON s.schedule_id=e.schedule_id WHERE e.source='autobook' "
            "AND e.message='חסמתי קטגוריה · ' || s.category_name "
            "AND (? IS NULL OR s.box_id=?)",
            (self.store.active_box_id, self.store.active_box_id))
        learned = {r[0] for r in await cur.fetchall()}
        blocked = getattr(self.settings, "blocked_categories", [])
        if learned.intersection(blocked):
            self.settings.update({"blocked_categories": [c for c in blocked if c not in learned]})
        # Do not erase past decisions or revive old failed attempts on upgrade.
        await self.store.set_meta(key, True)

    async def preflight_plans(self) -> None:
        """One early attempt for an existing intent, never an arbitrary class.

        There is no dry-run endpoint. Even far outside the documented window a
        surprising success is a real booking: persist it and stop. Timing-only
        rejection is NOT proof that this membership permits the category.
        """
        from .membership_policy import fingerprint
        import time
        plans = await self._planned_sessions(date.today().isoformat(), "9999-12-31")
        members = await self.store.get_meta("memberships") or []
        history_key = self.membership_policy.key(0) + ":probes"
        probes = await self.store.get_meta(history_key) or {}
        recent = sum(time.time() - p.get("at", 0) < 86400 for p in probes.values())
        for plan in plans:
            if recent >= 2:
                break
            # Pin rows deliberately carry only cached presentation fields;
            # resolve the full session before evaluating the safety boundary.
            session = await self.store.get_session(plan["schedule_id"]) or {}
            advance = session.get("advance_hours")
            if not isinstance(advance, (int, float)) or advance <= 0 or not session.get("category_id"):
                continue
            start = datetime.fromisoformat(f"{session['date']}T{session['start_time']}")
            bonus = max((int(m.get("extra_advance_hours") or 0) for m in members), default=0)
            if opening_moment(start, advance + bonus) < datetime.now() + timedelta(hours=24):
                continue
            for member in members:
                if recent >= 2:
                    break
                if not self._membership_valid_on(member, session["date"]):
                    continue
                if plan.get("membership_user_id") not in (None, member["id"]):
                    continue
                policy = await self.membership_policy.get(member)
                # Explicit evidence/manual decisions are never probed again.
                if (policy.get("categories_known") or not policy.get("quota_known")
                        or session['category_id'] in policy.get('confirmed_category_ids', [])
                        or policy.get("contradiction") or policy.get("source") == "manual"):
                    continue
                probe_id = f"{member['id']}:{fingerprint(member)}:{session['category_id']}"
                if probe_id in probes:
                    continue
                verified = await self.store.get_meta(self.membership_policy.key(member["id"]) + ":history") or {}
                if not verified.get("ok") or time.time() - verified.get('checked_at', 0) > 86400:
                    continue
                # Test quota independently from the unknown eligibility. A
                # surprising real booking must still fit the actual ledger.
                details = [{**m, "policy": await self.membership_policy.get(m)} for m in members]
                for d in details:
                    if d["id"] == member["id"]:
                        d["policy"] = {**policy, "state": "ready", "categories_known": True, "category_ids": [session["category_id"]]}
                commitments = await self._quota_commitments(members)
                pending_key = self.membership_policy.key(0) + ":uncertain"
                pending = await self.store.get_meta(pending_key) or {}
                # Do not consume other capacity while any write needs reconciliation.
                if pending:
                    continue
                projected_plans = [{**p, "membership_user_id": member["id"]} if p["schedule_id"] == plan["schedule_id"] else p for p in plans]
                projected = plan_quota(details, commitments, projected_plans, session["date"][:7])
                if projected["plan_allocations"].get(str(session["schedule_id"])) != member["id"]:
                    continue
                probes[probe_id] = {"at": time.time(), "schedule_id": session["schedule_id"]}
                await self.store.set_meta(history_key, probes)
                recent += 1
                pending[str(session["schedule_id"])] = {
                    "membership_user_id": member["id"], "action": "preflight",
                    "session": {k: v for k, v in session.items() if k != 'raw_json'}}
                await self.store.set_meta(pending_key, pending)
                try:
                    updated = await self.client.book(session["schedule_id"], member["id"])
                except ArboxError as err:
                    if not err.transient:
                        pending.pop(str(session["schedule_id"]), None)
                        await self.store.set_meta(pending_key, pending)
                    await self.membership_policy.learn_rejection(member, session, err)
                    probes[probe_id]["messages"] = err.messages()
                    probes[probe_id]["status"] = err.status
                    await self.store.set_meta(history_key, probes)
                    continue
                if not updated or updated.get("id") != session['schedule_id'] or updated.get('user_booked') is None:
                    continue  # unknown outcome stays paused
                updated["_selected_membership_id"] = member["id"]
                await self.store.upsert_sessions([updated])
                await self.store.record_booking_success(session["schedule_id"], "book", "preflight", member["id"])
                await self.store.mark_watch(session["schedule_id"], "booked")
                await self.store.mark_autobook(session["schedule_id"], "booked")
                pending.pop(str(session["schedule_id"]), None)
                await self.store.set_meta(pending_key, pending)
                await self.notifier.send(f"🎯 האימון המתוכנן הוזמן כבר בבדיקה המוקדמת: {fmt_class(session)}", kind="autobook")
                return  # refresh balances before any further probe

    async def reconcile_planned_quota(self) -> None:
        """Persist state transitions, so ticks and restarts cannot repeat alerts."""
        plans = await self._planned_sessions(date.today().isoformat(), "9999-12-31")
        key = self.membership_policy.key(0) + ":plan_alerts"
        previous = await self.store.get_meta(key) or {}
        current, problems = {}, []
        held_key = self.membership_policy.key(0) + ':held_plans'
        held = await self.store.get_meta(held_key) or {}
        for month in sorted({p["date"][:7] for p in plans}):
            status = await self.quota_status(target_date=month + "-01") or {}
            for plan in (p for p in plans if p["date"][:7] == month):
                sid = str(plan["schedule_id"])
                state = (status.get("plan_states") or {}).get(sid, {})
                if state.get("state") not in (None, "ready"):
                    current[sid] = state.get("state")
                    held.setdefault(sid, plan['date'])
                    if previous.get(sid) != current[sid]:
                        problems.append((plan, state, status))
        # Save before sending: an uncertain delivery must not become a flood.
        await self.store.set_meta(key, current)
        await self.store.set_meta(held_key, {sid: day for sid, day in held.items() if day >= date.today().isoformat()})
        if problems:
            await self.store.log_event("warn", "quota", "תכנונים דורשים בדיקה",
                                       "\n".join(f"{fmt_class(p)} · {state.get('reason')}" for p, state, _ in problems), notified=True)
            buttons = await self.planning_actions.notice_buttons(problems)
            text, bold = planning_notice(problems)
            await self.notifier.send(text, buttons, kind="membership", telegram_bold=bold)

    async def _membership_candidates(
        self, target_date: str | date | None = None,
        schedule_id: int | None = None,
    ) -> list[dict]:
        status = await self.quota_status(target_date=target_date) or {}
        allocation = status.get("plan_allocations", {}).get(str(schedule_id))
        return [m for m in status.get("memberships", []) if m["id"] == allocation]

    async def perform_membership_action(
        self, session: dict, action: str, membership_override: int | None = None,
    ) -> tuple[dict, int]:
        async with self._membership_lock:
            return await self._perform_membership_action(
                session, action, membership_override)

    async def _perform_membership_action(
        self, session: dict, action: str, membership_override: int | None = None,
    ) -> tuple[dict, int]:
        """Serialize capacity checks and writes; never retry an unknown outcome."""
        await self.syncer.ensure_identity()
        await self.syncer.refresh_membership()
        await self.refresh_planning_evidence(force_history=True)
        sid = session["schedule_id"]
        fresh = await self.store.get_session(sid)
        if not fresh:
            raise PlanningBlocked('האימון אינו נמצא בסטודיו הפעיל. רעננו את התצוגה')
        if fresh.get("user_booked") is not None or fresh.get("user_in_standby") is not None:
            raise PlanningBlocked("כבר קיימת הרשמה או המתנה לאימון. רעננו את התצוגה")
        plan = {**fresh, "membership_user_id": membership_override,
                "ignore_vacation": True, "created_at": datetime.now().isoformat()}
        # Replace any existing intent for this class for the explicit selection.
        # The selected membership still has to fit all the other commitments.
        members = await self.store.get_meta("memberships") or []
        tried = set()
        while True:
            status = await self.quota_status(target_date=session["date"], extra_plans=[plan]) or {}
            state = status.get("plan_states", {}).get(str(sid), {})
            mid = status.get("plan_allocations", {}).get(str(sid))
            if mid is None or mid in tried:
                raise PlanningBlocked(state.get("reason") or REASONS["needs_review"])
            tried.add(mid)
            member = next(m for m in members if m["id"] == mid)
            key = self.membership_policy.key(0) + ":uncertain"
            pending = await self.store.get_meta(key) or {}
            # The marker survives process termination during an upstream write.
            pending[str(sid)] = {"membership_user_id": mid, "action": action,
                                 "at": datetime.now().isoformat(),
                                 "session": {k: v for k, v in fresh.items() if k != 'raw_json'}}
            await self.store.set_meta(key, pending)
            try:
                updated = (await self.client.book(sid, mid) if action == "book" else
                           await self.client.join_standby(sid, mid))
            except ArboxError as err:
                if err.transient:
                    raise PlanningBlocked(REASONS["uncertain"]) from err
                pending.pop(str(sid), None)
                await self.store.set_meta(key, pending)
                if await self.membership_policy.learn_rejection(member, fresh, err):
                    if membership_override is None:
                        continue
                    raise PlanningBlocked("המנוי שנבחר נדחה לסוג האימון. נדרשת בדיקת התאמה") from err
                raise
            if not updated or updated.get("id") != sid or updated.get(
                    "user_booked" if action == "book" else "user_in_standby") is None:
                raise PlanningBlocked(REASONS["uncertain"])
            updated["_selected_membership_id"] = mid
            await self.store.upsert_sessions([updated])
            await self.store.record_booking_success(sid, action, "membership", mid)
            pending.pop(str(sid), None)
            await self.store.set_meta(key, pending)
            await self.store.set_meta(self.membership_policy.key(mid) + ":history", None)
            await self.store.set_meta("quota_cache", None)
            return updated, mid

    async def _refresh_memberships_quietly(self) -> None:
        try:
            await self.syncer.refresh_membership()
            await self.store.set_meta("quota_cache", None)
        except Exception as err:  # noqa: BLE001 — refresh never undoes booking
            _LOGGER.warning("Membership refresh after action failed: %s", err)

    async def _validate_watch_membership(
        self, watch: dict, session: dict, *, notify: bool = True,
    ) -> tuple[int | None, bool]:
        chosen = watch.get("membership_user_id")
        status = await self.quota_status(target_date=session["date"]) or {}
        ready = status.get("plan_states", {}).get(str(session["schedule_id"]), {}).get("state") == "ready"
        if not ready and notify:
            await self.reconcile_planned_quota()
        return chosen, ready

    async def _reconcile_pending_memberships(self) -> None:
        await self.review_plans()

    @staticmethod
    def _membership_fingerprint(memberships: list[dict]) -> str:
        material = [{k: m.get(k) for k in (
            "id", "membership_type_id", "plan", "active", "start", "end",
            "sessions_on_purchase", "plan_quota")}
            for m in memberships]
        return json.dumps(sorted(material, key=lambda m: m.get("id") or 0),
                          ensure_ascii=False, sort_keys=True)

    async def membership_check(self) -> None:
        """Daily inventory refresh and change notification."""
        await self.syncer.refresh_membership()
        box_id = self.syncer.box_id
        fingerprint_key = (f"membership_notified_fingerprint:{box_id}"
                           if box_id else "membership_notified_fingerprint")
        previous = await self.store.get_meta(fingerprint_key)
        active = await self._active_memberships()
        fingerprint = self._membership_fingerprint(active)
        await self.review_plans()

        if len(active) == 1:
            if self.settings.preferred_membership_id != active[0].get("id"):
                self.settings.update({"preferred_membership_id": active[0].get("id")})
        elif not active:
            if previous != fingerprint:
                delivered = await self.notifier.send(
                    "⚠️ אין כרגע מנוי פעיל. עצרתי הזמנות עד שיופיע מנוי פעיל.",
                    kind="membership")
                if delivered:
                    await self.store.set_meta(
                        fingerprint_key, fingerprint)
            return

        if previous == fingerprint:
            return
        try:
            old_ids = {m.get("id") for m in json.loads(previous or "[]")}
        except (TypeError, json.JSONDecodeError):
            old_ids = set()
        added = [m for m in active if m.get("id") not in old_ids]
        def display_date(value: str | None) -> str:
            try:
                return datetime.fromisoformat(str(value)).strftime("%d.%m.%Y")
            except (TypeError, ValueError):
                return str(value or "")
        lines = ["🎁 נוסף לך מנוי חדש" if added else "🎟️ המנויים שלך עודכנו"]
        for m in (added or active):
            allowance = (m.get("sessions_on_purchase")
                         if m.get("sessions_on_purchase") is not None
                         else m.get("plan_quota"))
            kind = "מנוי מתחדש" if m.get("recurring") else "כרטיסייה חד־פעמית"
            summary = [kind]
            if allowance:
                left = m.get("sessions_left")
                summary.append(f"{left if left is not None else allowance} כניסות")
            if str(m.get("price")) in ("0", "0.0", "0.00"):
                summary.append("מתנה")
            elif m.get("price") is not None:
                summary.append(f"₪{m['price']}")
            lines += ["", f"🎟️ {m.get('plan') or m.get('id')}",
                      " · ".join(summary)]
            if m.get("start"):
                lines.append(f"📅 מתחיל: {display_date(m['start'])}")
            if m.get("end"):
                lines.append(f"⏳ בתוקף עד: {display_date(m['end'])}")
        existing = [m for m in active if m not in added]
        if added and existing:
            lines += ["", "המנוי הקיים נשאר פעיל:"]
            lines += [f"🔄 {m.get('plan') or m.get('id')}"
                      + (f" · {m.get('plan_quota')} כניסות בחודש"
                         if m.get('plan_quota') else "")
                      for m in existing]
        lines += ["", "המכסה נבדקת לכל מנוי ולסוגי השיעורים שהוא מכסה. הפירוט זמין ב־Arbox ← שלי."]
        buttons = []
        callback_ids = []
        if len(active) > 1:
            lines += ["אפשר לבחור מנוי אחר לכל אימון.", "",
                      "מאיזה מנוי להשתמש קודם?"]
            batch = secrets.token_urlsafe(8)
            for m in active:
                cid = secrets.token_urlsafe(8)
                callback_ids.append(cid)
                await self.store.add_prompt(
                    cid, NO_SCHEDULE, "membership_select", batch_id=batch,
                    payload=json.dumps({"membership_id": m["id"],
                                        "fingerprint": fingerprint}))
                icon = "🎁" if m in added else "🔄" if m.get("recurring") else "🎟️"
                suffix = f" — עד {display_date(m['end'])}" if m.get("end") else ""
                buttons.append([{"text": f"{icon} {m.get('plan') or m['id']}{suffix}",
                                 "data": f"membership_select:{cid}"}])
        delivered = await self.notifier.send(
            "\n".join(lines), buttons or None, kind="membership",
            is_answered=(lambda: self.store.any_answered(callback_ids))
            if callback_ids else None)
        if delivered:
            await self.store.set_meta(fingerprint_key, fingerprint)
        else:
            for cid in callback_ids:
                await self.store.delete_prompt(cid)

    # -------------------------------------------------------------- digest

    async def nightly_digest(self) -> None:
        """One evening message with independent tomorrow and booking sections.

        The first section is a reminder about commitments on the next day and
        is never silenced by a vacation. The second selects every known class
        whose per-session registration window opens tomorrow. Class dates may
        differ because studios can use different windows for different classes.
        """
        today = date.today()
        next_day = (today + timedelta(days=1)).isoformat()
        horizon = (today + timedelta(days=FULL_WINDOW_DAYS)).isoformat()

        # Existing commitments remain visible even when there are no notify
        # rules or future registration candidates are blocked by a vacation.
        try:
            # The 15–30 day tail was refreshed once at 19:30 and the first
            # 14 days are refreshed throughout the day. Only tomorrow needs
            # one last point refresh for attendance and standby changes.
            await self.syncer.sync_range(next_day, next_day)
        except ArboxError as err:
            _LOGGER.warning("Tomorrow reminder pre-sync failed, using cached: %s", err)
        tomorrow_sessions = await self.store.get_sessions(
            date_from=next_day, date_to=next_day, mine=True)
        booked_next_day = [
            s for s in tomorrow_sessions if s.get("user_booked") is not None
        ]
        standby_next_day = [
            s for s in tomorrow_sessions
            if s.get("user_booked") is None
            and s.get("user_in_standby") is not None
        ]

        sections: list[list[str]] = []
        if booked_next_day:
            sections.append(
                ["📌 מחר"]
                + ["• " + _fmt_tomorrow_session(s) for s in booked_next_day]
            )
        if standby_next_day:
            sections.append(
                ["⏳ עדיין בהמתנה"]
                + ["• " + _fmt_tomorrow_session(s) for s in standby_next_day]
            )

        all_rules = await self.store.list_rules()
        rules = [r for r in all_rules if r["enabled"] and r["mode"] == "notify"]
        auto_rules = [r for r in all_rules if r["enabled"] and r["mode"] == "autobook"]
        matches: list[dict] = []
        covered: list[dict] = []
        buttons: list[list[dict]] | None = None
        cids: list[str] = []

        if rules:
            sessions = await self.store.get_sessions(
                date_from=today.isoformat(), date_to=horizon)
            opening_sessions = [
                s for s in sessions if _registration_opens_on(s, next_day)
            ]
            candidates = []
            for session in opening_sessions:
                if not any(rule_matches(r, session) for r in rules):
                    continue
                if session.get("user_booked") is not None \
                        or session.get("user_in_standby") is not None:
                    continue
                if session.get("booking_option") not in (
                        "insertScheduleUser", "insertStandby"):
                    continue
                if self._blocked(session) or await self.store.vacation_blocks(
                        session["date"], "notify"):
                    continue
                candidates.append(session)

            auto_paused_ids = {
                s["schedule_id"] for s in candidates
                if await self.store.vacation_blocks(s["date"], "autobook")
            }
            planned = await self._planned_sessions(today.isoformat(), horizon)
            planned_ids = {p["schedule_id"] for p in planned}
            covered = [
                s for s in candidates
                if s["schedule_id"] not in auto_paused_ids
                and s["schedule_id"] in planned_ids
            ]
            matches = [s for s in candidates if s not in covered]

            if candidates:
                target_dates = sorted({s["date"] for s in candidates})
                show_dates = len(target_dates) > 1
                heading = (
                    f"🗓️ ההרשמה נפתחת מחר ל{_he_short_date(target_dates[0])}"
                    if not show_dates else "🗓️ ההרשמה נפתחת מחר"
                )

                def opening_label(session: dict) -> str:
                    prefix = (f"{_he_short_date(session['date'])} · "
                              if show_dates else "")
                    return prefix + _fmt_session(session)

                future = [heading]
                booked_on_target = [
                    s for s in sessions
                    if s["date"] in target_dates and (
                        s.get("user_booked") is not None
                        or s.get("user_in_standby") is not None)
                ]
                planned_on_target = [p for p in planned if p["date"] in target_dates
                                     and p["schedule_id"] not in {c["schedule_id"] for c in covered}]
                if planned_on_target:
                    future += ["", "כבר מתוכנן לך באותו יום (טרם הוזמן):"]
                    future += ["• " + opening_label(p) for p in planned_on_target]
                if booked_on_target:
                    future += ["", "כבר יש לך באותו יום:"]
                    future += ["• " + opening_label(s) for s in booked_on_target]

                if covered:
                    auto_label = (
                        "🤖 מתוכנן להזמנה אוטומטית — בכפוף לכיסוי המנוי והמכסה"
                        if len(covered) == 1 else
                        "🤖 מתוכננים להזמנה אוטומטית — בכפוף לכיסוי המנוי והמכסה"
                    )
                    future += ["", auto_label]
                    future += ["• " + opening_label(s) for s in covered]

                if matches:
                    months = {s["date"][:7] for s in matches}
                    q = (await self.quota_status(target_date=matches[0]["date"])
                         if len(months) == 1 else None)
                    if q:
                        future += ["", f"🎟️ נותרו לך {q['remaining']} מתוך "
                                   f"{q['quota']} כניסות החודש"]
                        if q["remaining"] == 0:
                            future.append("⚠️ ההצעות למטה יחרגו מהמכסה")
                        if q["overcommitted"]:
                            future.append(
                                "⚠️ יש תכנונים ללא מכסה מתאימה. בדקו את הפירוט לפי מנוי ב׳שלי׳")
                    if auto_paused_ids:
                        future += ["", "🏖️ ההזמנה האוטומטית מושבתת בתאריך הזה "
                                   "בגלל חופשה."]
                    manual_label = (
                        "🔔 תזכורת להזמנה — השיעור הזה לא יוזמן אוטומטית"
                        if len(matches) == 1 else
                        "🔔 תזכורת להזמנה — השיעורים האלה לא יוזמנו אוטומטית"
                    )
                    future += ["", manual_label]
                    now = datetime.now()
                    for s in matches:
                        is_open, why = registration_open(s, now)
                        future.append(
                            "• " + opening_label(s) if is_open
                            else f"• ⏳ {opening_label(s)} — {why}"
                        )
                    future += ["", "כדי לתזמן הזמנה, בחר שיעור:"]
                    buttons, cids = await self._digest_buttons(
                        matches, include_date=show_dates)
                sections.append(future)
            else:
                _LOGGER.info("Digest: no registrations open on %s", next_day)

        if not sections:
            _LOGGER.info("Nightly message: nothing relevant")
            return

        delivered = await self.notifier.send(
            "\n\n".join("\n".join(section) for section in sections),
            buttons, kind="digest",
            is_answered=(lambda: self.store.any_answered(cids)) if cids else None,
        )
        if delivered:
            _LOGGER.info(
                "Nightly message sent: %d tomorrow, %d openings on %s",
                len(booked_next_day) + len(standby_next_day),
                len(matches) + len(covered), next_day)
        else:
            _LOGGER.warning("Nightly message delivery failed")

    async def _digest_buttons(
        self, sessions: list[dict], dry_run: bool = False,
        include_date: bool = False,
    ) -> tuple[list[list[dict]], list[str]]:
        """Book/standby button per class + info affordances, batch-grouped.

        Telegram gets an ℹ️ button per class row (tg_only); HA gets one
        batch-info action per notification (ha_only) because the companion
        app caps actions at 3.
        """
        batch = secrets.token_urlsafe(6)
        buttons: list[list[dict]] = []
        cids: list[str] = []
        for s in sessions:
            cid = secrets.token_urlsafe(8)
            cids.append(cid)
            is_open, _ = registration_open(s, datetime.now())
            if not is_open:
                action = "watch"          # queue it for the opening moment
            elif s["booking_option"] == "insertScheduleUser":
                action = "book"
            else:
                action = "standby"
            await self.store.add_prompt(
                cid, s["schedule_id"], action, dry_run=dry_run, batch_id=batch
            )
            icon = {"book": "📖", "standby": "⏳", "watch": "🎯"}[action]
            label = f"{icon} {s['start_time']}"
            if include_date:
                try:
                    d = date.fromisoformat(s["date"])
                    label = f"{icon} {d.day}.{d.month} {s['start_time']}"
                except (KeyError, ValueError):
                    pass
            if s.get("coach_name"):
                label += f" {s['coach_name'].split()[0]}"
            buttons.append([
                {"text": label, "data": f"{action}:{cid}"},
                {"text": "ℹ️", "data": f"info:{cid}", "tg_only": True},
            ])
        buttons.append([{"text": "ℹ️ פרטים על השיעורים",
                         "data": f"infob:{batch}", "ha_only": True}])
        return buttons, cids

    async def send_test_digest(self) -> dict:
        """End-to-end rehearsal of the nightly digest.

        Uses the real digest builder, the real prompt store and the real
        notifier, on real upcoming sessions — so a misconfigured channel,
        a broken button payload or a dead callback route fails HERE rather
        than at 20:00 on the night it matters. The prompts are flagged
        dry_run, so pressing a button proves the round trip without booking.
        """
        today = date.today().isoformat()
        horizon = (date.today() + timedelta(days=14)).isoformat()
        sessions = [
            s for s in await self.store.get_sessions(date_from=today, date_to=horizon)
            if s.get("booking_option") in ("insertScheduleUser", "insertStandby")
        ][:3]
        if not sessions:
            raise RuntimeError("no bookable sessions in the window to test with")

        lines = [
            "🧪 בדיקה מקצה לקצה — כך ייראה חלק ההזמנה בהודעה הלילית:",
            "",
            "🔔 תזכורת להזמנה — השיעורים האלה לא יוזמנו אוטומטית",
        ]
        lines += ["• " + _fmt_session(s) for s in sessions]
        lines += [
            "",
            "כדי לתזמן הזמנה, בחר שיעור:",
            "🧪 זו בדיקה בלבד — לחיצה תריץ את המסלול ולא תזמין כלום.",
        ]
        buttons, cids = await self._digest_buttons(sessions, dry_run=True)
        await self.notifier.send(
            "\n".join(lines), buttons, kind="digest",
            is_answered=lambda: self.store.any_answered(cids),
        )
        return {"sessions": len(sessions),
                "labels": [b[0]["text"] for b in buttons[:-1]]}

    # ----------------------------------------------------------- callbacks

    async def handle_callback(self, data: str, reply_text: str | None = None,
                              source_channel: str | None = None) -> str:
        """A button was pressed (Telegram callback or HA action)."""
        try:
            action, cid = data.split(":", 1)
        except ValueError:
            return "כפתור לא מוכר"

        if action.startswith("preview_"):
            return await self.journal_preview.callback(action, cid, source_channel, reply_text)
        if action == 'plan':
            return await self.planning_actions.callback(cid)

        if action == "info":
            # never consumes the prompt — the booking button must stay live
            prompt = await self.store.peek_prompt(cid)
            if not prompt:
                return "הבקשה כבר לא קיימת"
            session = await self.store.get_session(prompt["schedule_id"])
            return self._describe(session)
        if action == "infob":
            prompts = await self.store.batch_prompts(cid)
            if not prompts:
                return "הבקשה כבר לא קיימת"
            seen_cats: set[str] = set()
            parts = []
            for pr in prompts:
                session = await self.store.get_session(pr["schedule_id"])
                if not session:
                    continue
                cat = session.get("category_name") or ""
                if cat in seen_cats:
                    continue
                seen_cats.add(cat)
                parts.append(self._describe(session))
            return "\n\n".join(parts) or "אין תיאור לשיעורים האלה"

        pending = await self.store.peek_prompt(cid)
        if pending and pending.get("action") == "feedback_form":
            return "את המשוב הזה ממלאים בטופס שנפתח מההתראה"
        prompt = await self.store.take_prompt(cid)
        if not prompt:
            return "הבקשה כבר טופלה או שפגה"

        # above the Arbox path on purpose: a vacation change is purely local,
        # and everything below sits inside the try/except that assumes a
        # booking write and calls restore_prompt on failure
        if action in ("vcancel", "vconfirm", "vkeep"):
            return await self._vacation_callback(action, prompt, cid)

        if action == "membership_select":
            if prompt.get("action") != "membership_select":
                return "הבחירה כבר לא תקפה"
            try:
                ref = json.loads(prompt.get("payload") or "{}")
                membership_id = int(ref["membership_id"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return "בחירת מנוי לא תקינה"
            active = await self._active_memberships()
            selected = next((m for m in active if m.get("id") == membership_id), None)
            if not selected:
                return "המנוי כבר אינו פעיל — לא שיניתי את ברירת המחדל"
            self.settings.update({"preferred_membership_id": membership_id})
            self.syncer.membership_user_id = membership_id
            await self.store.set_meta("membership", selected)
            await self.store.set_meta("quota_cache", None)
            await self.store.answer_batch(prompt.get("batch_id"))
            await self.store.log_event(
                "info", "quota", f"מנוי ברירת מחדל · {selected.get('plan')}",
                f"membership_user_id={membership_id}")
            return f"✅ נשמר. אשתמש קודם ב-{selected.get('plan')}"

        schedule_id = prompt["schedule_id"]
        session = await self.store.get_session(schedule_id)
        label = _fmt_session(session) if session else str(schedule_id)

        if action.startswith("journal_"):
            if not session or session.get("date") != date.today().isoformat():
                return "המשוב נסגר בחצות — אפשר לעדכן בכל עת דרך היומן"
            if prompt.get("action") not in ("journal", "journal_class"):
                return "הבקשה כבר לא תקפה"
            current = await self.store.workout_journal(schedule_id) or {}
            if action == "journal_skip":
                await self.store.dismiss_workout_journal(schedule_id)
                return "דילגנו ✓ אפשר למלא את היומן אחר כך"
            if action == "journal_notes":
                if reply_text and reply_text.strip():
                    value = reply_text.strip()[:4000]
                    outcome = await self.store.get_training_outcome(schedule_id)
                    if not outcome or outcome.get("status") != "attended":
                        await self.store.set_training_outcome(
                            schedule_id, "attended", "journal")
                    await self.store.save_workout_journal(
                        schedule_id, coach_feedback=current.get("coach_feedback"),
                        class_feedback=current.get("class_feedback"), notes=value,
                        exercises=parse_exercise_text(value))
                    return "נשמר ביומן ✓"
                await self.store.set_meta("journal_text_input", {
                    "kind": "journal", "schedule_id": schedule_id,
                    "date": date.today().isoformat(),
                })
                return ("✍️ אפשר לכתוב עכשיו מה עשית. למשל: "
                        "סקוואט 60 ק״ג 3x8; מתח 3x6")
            if action == "journal_feedback":
                next_cid = secrets.token_urlsafe(8)
                await self.store.add_prompt(next_cid, schedule_id, "journal_class")
                subject = (f"איך היה עם {session['coach_name']}?"
                           if session.get("coach_name") else "איך היה השיעור?")
                await self.notifier.send(subject, feedback_buttons(
                    next_cid, coach=bool(session.get("coach_name"))), kind="journal")
                return "בחר/י משוב קצר בהודעה הבאה"
            value = ("positive" if action.endswith("pos") else
                     "neutral" if action.endswith("neutral") else
                     "not_applicable" if action.endswith("na") else "negative")
            outcome = await self.store.get_training_outcome(schedule_id)
            if not outcome or outcome.get("status") != "attended":
                await self.store.set_training_outcome(
                    schedule_id, "attended", "journal")
            is_class = action.startswith("journal_class_")
            await self.store.save_workout_journal(
                schedule_id,
                coach_feedback=(current.get("coach_feedback") if is_class else value),
                class_feedback=(value if is_class else current.get("class_feedback")),
                notes=current.get("notes"), exercises=current.get("exercises"),
            )
            level = self.settings.journal["level"]
            if not is_class and level in ("feedback", "full"):
                next_cid = secrets.token_urlsafe(8)
                await self.store.add_prompt(next_cid, schedule_id, "journal_class")
                await self.notifier.send(
                    f"ואיך היה השיעור עצמו?\n{session.get('category_name') or 'שיעור'}",
                    feedback_buttons(next_cid, coach=False), kind="journal")
                return "נשמר ✓ יש עוד שאלה קצרה אחת"
            if is_class and level == "full":
                next_cid = secrets.token_urlsafe(8)
                await self.store.add_prompt(next_cid, schedule_id, "journal")
                title, buttons = notes_prompt(next_cid)
                await self.notifier.send(title, buttons, kind="journal")
                return "המשוב נשמר ✓ אפשר להוסיף תרגילים או לדלג"
            return "תודה — נשמר ביומן ✓"

        if action in ("attend_yes", "attend_no"):
            if prompt.get("action") != "attendance" or not session:
                return "הבקשה כבר לא תקפה"
            existing = await self.store.get_training_outcome(schedule_id)
            if existing and (existing.get("status") or "").startswith("cancelled"):
                return "האימון כבר בוטל; שאלת ההגעה נסגרה"
            if existing and existing.get("status") == "standby_cancelled":
                return "כבר יצאת מרשימת ההמתנה; שאלת ההגעה נסגרה"
            if session.get("date") != date.today().isoformat():
                if not await self.store.get_training_outcome(schedule_id):
                    await self.store.set_training_outcome(
                        schedule_id, "attended", "timeout")
                return "הכפתור פג בחצות; האימון סומן אוטומטית כהגעה"
            if action == "attend_yes":
                await self.store.set_training_outcome(
                    schedule_id, "attended", "manual")
                await self.store.log_event(
                    "info", "booking", f"הגעה אושרה · {fmt_class(session)}",
                    "דווח ידנית מההתראה", schedule_id)
                return "✅ תודה על העדכון — סימנתי שהגעת"
            await self.store.set_training_outcome(
                schedule_id, "missed", "manual")
            reason_cid = secrets.token_urlsafe(8)
            await self.store.add_prompt(
                reason_cid, schedule_id, "attendance_reason")
            buttons = [[{
                "text": text, "data": f"reason_{code}:{reason_cid}",
            }] for code, text in self.REASON_LABELS.items()]
            await self.notifier.send(
                f"❌ רשמתי שלא הגעת ל-{label}. מה הסיבה?",
                buttons, kind="attendance")
            return "בחר/י סיבה בהודעה הבאה"

        if action.startswith("reason_"):
            if prompt.get("action") != "attendance_reason" or not session:
                return "הבקשה כבר לא תקפה"
            code = action.removeprefix("reason_")
            if code not in self.REASON_LABELS:
                return "סיבה לא מוכרת"
            if code == "other":
                await self.store.set_meta("attendance_other_input", {
                    "schedule_id": schedule_id,
                    "date": date.today().isoformat(),
                })
                return ("✍️ כתוב/י עכשיו את הסיבה בהודעה חופשית בטלגרם, "
                        "או עדכן/י אותה בהיסטוריה באפליקציה")
            await self.store.set_training_outcome(
                schedule_id, "missed", "manual", code)
            await self.store.log_event(
                "info", "booking", f"אי-הגעה עודכנה · {fmt_class(session)}",
                self.REASON_LABELS[code], schedule_id)
            return f"תודה, הסיבה נשמרה: {self.REASON_LABELS[code]}"

        if prompt["dry_run"]:
            # end-to-end test: the whole chain ran for real (notification ->
            # button -> transport -> server -> prompt lookup); only the Arbox
            # write is skipped, so nothing is actually booked
            _LOGGER.info("Dry-run callback OK: %s %s", action, schedule_id)
            verb = {"book": "היה נרשם", "standby": "היה נכנס להמתנה",
                    "watch": "היה מתזמן"}.get(action, "היה פועל")
            return (
                f"🧪 בדיקה הצליחה! הכל עובד מקצה לקצה.\n"
                f"באמת {verb} ל: {label}\n"
                f"(זו בדיקה — לא בוצעה שום הזמנה)"
            )

        try:
            await self.syncer.ensure_identity()
            if action == "book":
                updated, membership_id = await self.perform_membership_action(
                    session, "book")
                result = f"✅ נקבע! {label}"
            elif action == "standby":
                updated, membership_id = await self.perform_membership_action(
                    session, "standby")
                result = f"⏳ נכנסת להמתנה: {label}"
            elif action == "watch":
                # nothing to book yet; queue it so the one-shot job takes it
                # the instant registration opens
                await self.store.watch(schedule_id, allow_standby=True)
                await self.review_plans()
                await self.schedule_openings()
                _, why = registration_open(session or {}, datetime.now())
                await self.store.log_event(
                    "info", "watchlist", f"תוזמן מההודעה הלילית · {label}",
                    why, schedule_id)
                q = await self.quota_status(target_date=session['date']) or {}
                state = q.get('plan_states', {}).get(str(schedule_id), {})
                return f"🎯 התכנון נשמר — {state.get('reason', why)}"
            else:
                return "פעולה לא מוכרת"
        except PlanningBlocked as err:
            await self.store.restore_prompt(cid)
            return f"⚠️ {err}. לתיקון: Arbox ← שלי ← פירוט המנויים"
        except ArboxError as err:
            # Explicit upstream refusal can be retried by the user; unknown
            # outcomes are handled by PlanningBlocked and its persistent marker.
            # press can retry instead of hitting "already handled"
            await self.store.restore_prompt(cid)
            _LOGGER.error("Callback %s failed: %s", data, err)
            return f"❌ נכשל: {err} — אפשר ללחוץ שוב לניסיון נוסף"
        # the write returns the updated session — store it so UI/HA see it now
        if updated and updated.get("id"):
            await self.store.upsert_sessions([updated])
        if action in ("book", "standby"):
            await self.store.record_booking_success(
                schedule_id, action, "notification", membership_id)
        if action == "book":
            # booking is the moment the class becomes calendar-worthy;
            # standby isn't confirmed yet, so it gets nothing
            await self.send_calendar_file(schedule_id)
        return result

    async def handle_message(self, text: str, preview_only: bool = False) -> str | None:
        """Consume free text only after an explicit reason/journal button."""
        preview = await self.journal_preview.message(text)
        if preview is not None or preview_only:
            return preview
        journal_pending = await self.store.get_meta("journal_text_input")
        if journal_pending and journal_pending.get("kind") == "journal":
            value = (text or "").strip()
            if not value:
                return "הטקסט ריק — אפשר לכתוב שוב"
            if len(value) > 4000:
                return "הטקסט ארוך מדי — עד 4,000 תווים"
            if journal_pending.get("date") != date.today().isoformat():
                await self.store.set_meta("journal_text_input", None)
                return "הקלט נסגר בחצות — אפשר לכתוב דרך היומן באתר"
            schedule_id = int(journal_pending["schedule_id"])
            current = await self.store.workout_journal(schedule_id) or {}
            parsed = parse_exercise_text(value)
            outcome = await self.store.get_training_outcome(schedule_id)
            if not outcome or outcome.get("status") != "attended":
                await self.store.set_training_outcome(
                    schedule_id, "attended", "journal")
            await self.store.save_workout_journal(
                schedule_id, coach_feedback=current.get("coach_feedback"),
                class_feedback=current.get("class_feedback"), notes=value,
                exercises=parsed)
            await self.store.set_meta("journal_text_input", None)
            return f"נשמר ביומן ✓ זיהיתי {len(parsed)} תרגילים/שורות"
        pending = await self.store.get_meta("attendance_other_input")
        if not pending:
            return None
        value = (text or "").strip()
        if not value:
            return "הסיבה ריקה — אפשר לכתוב שוב"
        if len(value) > 500:
            return "הסיבה ארוכה מדי — עד 500 תווים"
        schedule_id = int(pending["schedule_id"])
        existing = await self.store.get_training_outcome(schedule_id)
        if existing and ((existing.get("status") or "").startswith("cancelled")
                         or existing.get("status") == "standby_cancelled"):
            await self.store.set_meta("attendance_other_input", None)
            return "האימון כבר בוטל; לא נשמרה סיבת אי-הגעה"
        await self.store.set_training_outcome(
            schedule_id, "missed", "manual", "other", value)
        await self.store.set_meta("attendance_other_input", None)
        session = await self.store.get_session(schedule_id)
        await self.store.log_event(
            "info", "booking",
            f"אי-הגעה עודכנה · {fmt_class(session) if session else schedule_id}",
            "סיבה אחרת נשמרה", schedule_id)
        return "תודה, הסיבה נשמרה"

    async def journal_tick(self) -> None:
        async with self._journal_lock:
            await self._journal_tick()

    async def _journal_tick(self) -> None:
        """Offer the opt-in journal 30 minutes after an attended class ends."""
        level = self.settings.journal["level"]
        if level == "off":
            return
        now = datetime.now()
        today = date.today().isoformat()
        for s in await self.store.journal_candidates():
            if s.get("date") != today:
                continue
            try:
                ended = datetime.fromisoformat(
                    f"{s['date']}T{s.get('end_time') or s['start_time']}")
            except (KeyError, TypeError, ValueError):
                continue
            if now < ended + timedelta(minutes=30):
                continue
            existing = await self.store.workout_journal(s["schedule_id"])
            if existing and existing.get("prompted_at"):
                continue
            try:
                channel = self.notifier.journal_form_channel()
                if channel is None:
                    return
                cid, url = await self.feedback_form.create(s, level, channel=channel)
            except ValueError as err:
                _LOGGER.warning("Journal form not sent: %s", err)
                return
            delivered = await self.notifier.send_journal_form(
                f"איך היה האימון?\n{s.get('category_name') or 'האימון שלך'} · {s.get('coach_name') or ''}\nמשוב קצר, הערות ותרגילים — בטופס אחד.",
                [[{"text": "מילוי משוב", "uri": url}]], channel=channel)
            if delivered:
                await self.store.mark_journal_prompted(s["schedule_id"])
            else:
                await self.store.delete_prompt(cid)

    async def attendance_tick(self) -> None:
        """Ask near class start; default unanswered commitments at midnight."""
        now = datetime.now()
        today = date.today().isoformat()
        tracking_since = await self.store.get_meta("attendance_tracking_since")
        if not tracking_since:
            tracking_since = today
            await self.store.set_meta("attendance_tracking_since", tracking_since)
        sessions = await self.store.my_sessions(date_from=str(tracking_since))
        for s in sessions:
            sid = s["schedule_id"]
            if s.get("user_booked") is None:
                continue
            if await self.store.get_training_outcome(sid):
                continue
            if s["date"] < today:
                await self.store.set_training_outcome(sid, "attended", "timeout")
                await self.store.expire_prompts(
                    sid, ("attendance", "attendance_reason"))
                await self.store.log_event(
                    "info", "booking", f"הגעה סומנה אוטומטית · {fmt_class(s)}",
                    "שאלת ההגעה לא נענתה עד חצות", sid)
                continue
            if s["date"] != today:
                continue
            try:
                start = datetime.fromisoformat(f"{s['date']}T{s['start_time']}")
            except (TypeError, ValueError):
                continue
            if now < start - timedelta(minutes=self.ATTENDANCE_LEAD_MINUTES):
                continue
            if await self.store.live_prompt_for(sid, "attendance"):
                continue
            cid = secrets.token_urlsafe(8)
            await self.store.add_prompt(cid, sid, "attendance")
            delivered = await self.notifier.send(
                _fmt_attendance_question(s),
                [[
                    {"text": "✅ כן", "data": f"attend_yes:{cid}"},
                    {"text": "❌ לא", "data": f"attend_no:{cid}"},
                ]],
                kind="attendance",
                is_answered=lambda cid=cid: self.store.any_answered([cid]),
            )
            if not delivered:
                await self.store.delete_prompt(cid)
                continue
            await self.store.log_event(
                "info", "booking", f"שאלת הגעה נשלחה · {fmt_class(s)}",
                f"הכפתורים פעילים עד חצות · {self.ATTENDANCE_LEAD_MINUTES} דקות לפני",
                sid, notified=True)

    async def send_calendar_file(self, schedule_id: int) -> None:
        """Push the class as a .ics so one tap adds it to any calendar."""
        session = await self.store.get_session(schedule_id)
        if not session:
            return
        try:
            identity = await self.store.get_meta("identity") or {}
            location = ", ".join(
                x for x in (identity.get("studio_name"), identity.get("address")) if x
            )
            ics = build_calendar(
                [session],
                alarms=self.settings.calendar_alarms,
                location=location,
            )
        except Exception as err:  # noqa: BLE001 — never break a booking
            _LOGGER.error("Calendar file build failed for %s: %s", schedule_id, err)
            return
        base = self.settings.base_url
        gcal = google_calendar_url(session, location=location)
        # The caption names the class rather than explaining the file: by the
        # time this arrives you already know you booked something, and what
        # you want to confirm is *which*. The Google URL used to be pasted
        # raw and filled half the screen — it is a button now.
        alarms = self.settings.calendar_alarms
        when = fmt_when(session)
        what = " · ".join(x for x in (session.get("category_name"),
                                      session.get("coach_name")) if x)
        caption = f"📅 <b>{_esc_html(what)}</b>\n{_esc_html(when)}"
        if alarms:
            caption += f"\n🔔 {_esc_html(' · '.join(fmt_alarm(m) for m in alarms))}"
        # no instruction line: a file and a labelled button explain themselves
        buttons = None
        if gcal:
            buttons = [[{"text": "🗓️ Google Calendar", "url": gcal}]]
        await self.notifier.send_document(
            filename=f"arbox-{schedule_id}.ics",
            content=ics.encode("utf-8"),
            caption=caption,
            kind="digest",
            link=f"{base}/api/calendar/event/{schedule_id}.ics" if base else "",
            buttons=buttons,
        )

    @staticmethod
    def _describe(session: dict | None) -> str:
        if not session:
            return "השיעור כבר לא בלוח"
        title = f"ℹ️ {session.get('category_name') or 'שיעור'}"
        if session.get("coach_name"):
            title += f" · {session['coach_name']}"
        bio = (session.get("category_bio") or "").strip()
        return f"{title}\n{bio}" if bio else f"{title}\nאין תיאור לשיעור הזה"

    async def class_reminder_tick(self) -> None:
        """Opt-in nudge before a booked class.

        Off by default: the calendar event already carries alarms, so a second
        reminder is only worth it for people who don't live in their calendar.
        """
        lead = self.settings.class_reminder_minutes
        if lead <= 0:
            return
        now = datetime.now()
        sent: list = await self.store.get_meta("reminded_sessions", []) or []
        changed = False
        for s in await self.store.my_sessions(date_from=date.today().isoformat()):
            if s.get("user_booked") is None or s["schedule_id"] in sent:
                continue
            try:
                start = datetime.fromisoformat(f"{s['date']}T{s['start_time']}")
            except (TypeError, ValueError):
                continue
            # fire once inside the lead window, never for a class already begun
            if now < start - timedelta(minutes=lead) or now >= start:
                continue
            mins = int((start - now).total_seconds() // 60)
            await self.notifier.send(
                f"⏰ בעוד {mins} דק׳: {_fmt_session(s)}", kind="autobook")
            sent.append(s["schedule_id"])
            changed = True
        if changed:
            await self.store.set_meta("reminded_sessions", sent[-100:])

    async def late_cancel_tick(self) -> None:
        """Warn before the point where cancelling starts costing an entry.

        The deadline is the class's own disable_cancellation_time, not a
        studio-wide number: this box runs 12h on most classes but 4h on some
        Functional Strength sessions, so one configured lead time produces the
        right warning for each. Classes with no penalty (0h) are skipped —
        there is nothing to be late for.
        """
        lead = self.settings.late_cancel_warning_minutes
        if lead <= 0:
            return
        now = datetime.now()
        sent: list = await self.store.get_meta("late_cancel_warned", []) or []
        changed = False
        for s in await self.store.my_sessions(date_from=date.today().isoformat()):
            sid = s["schedule_id"]
            if s.get("user_booked") is None or sid in sent:
                continue
            hours = s.get("cancel_hours")
            if not hours:            # None (unknown) or 0 (no penalty)
                continue
            try:
                start = datetime.fromisoformat(f"{s['date']}T{s['start_time']}")
            except (TypeError, ValueError):
                continue
            deadline = start - timedelta(hours=hours)
            # fire once inside the lead window; never after the line is crossed
            if now < deadline - timedelta(minutes=lead) or now >= deadline:
                continue
            mins = int((deadline - now).total_seconds() // 60)
            left = (f"{mins} דק׳" if mins < 60
                    else f"{mins // 60} שע׳" + (f" ו-{mins % 60} דק׳" if mins % 60 else ""))
            text = (f"⏳ עוד {left} לביטול חופשי: {fmt_class(s)}\n"
                    f"אחרי {deadline:%H:%M} ביטול ייספר ככניסה מהמכסה.")
            buttons = None
            if self.settings.base_url:
                buttons = [[{"text": "📋 לצפייה בפרטים",
                             "uri": f"{self.settings.base_url}/mine"}]]
            delivered = await self.notifier.send(
                text, buttons=buttons, kind="latecancel")
            if not delivered:
                # The five-minute tick remains armed throughout the warning
                # window. A dead channel must not turn a failed attempt into a
                # permanent "already warned" marker.
                _LOGGER.warning(
                    "Late-cancel warning %s reached no notification channel; "
                    "will retry before %s", sid, deadline)
                continue
            await self.store.log_event(
                "info", "booking", f"התראת ביטול · {fmt_class(s)}",
                f"הדדליין {deadline:%d/%m %H:%M} ({hours} שעות לפני)",
                sid, notified=True)
            sent.append(sid)
            changed = True
        if changed:
            await self.store.set_meta("late_cancel_warned", sent[-100:])

    # ----------------------------------------------------------- vacations

    async def vacation_announce_tick(self) -> None:
        """Say why the automation is going quiet, and when it comes back.

        Deliberately NOT gated on vacation_blocks(): this message is *about*
        the silence, so the vacation must not suppress its own explanation.
        It rides its own kind, which the user can mute per channel.
        """
        today = date.today()
        opening_day = (today + timedelta(days=1)).isoformat()
        horizon = (today + timedelta(days=FULL_WINDOW_DAYS)).isoformat()
        sessions = await self.store.get_sessions(
            date_from=today.isoformat(), date_to=horizon)
        target_dates = sorted({
            session["date"] for session in sessions
            if _registration_opens_on(session, opening_day)
        })
        if target_dates:
            for target in target_dates:
                await self._announce_vacation_start(target, today.isoformat())
        else:
            # Still announce a last-minute vacation that is already active,
            # even on a night with no known registration opening.
            await self._announce_vacation_start(None, today.isoformat())
        await self._announce_vacation_end(today)

    async def _announce_vacation_start(
        self, target: str | None, today: str,
    ) -> None:
        # Two triggers, one dedupe list. The first is keyed on the digest's own
        # horizon, so it fires on exactly the evening the first message goes
        # missing; matching the whole range rather than only date_from makes it
        # self-healing, since an evening lost to a restart announces the next
        # evening instead of never.
        #
        # The second catches what the first structurally cannot: a vacation
        # entered less than eight days before it ends — the last-minute case,
        # and the common one — never covers the horizon at all, so it used to
        # go entirely unannounced while still firing its end message.
        covering = (await self.store.vacations_covering(target)) if target else []
        ahead_ids = {v["id"] for v in covering}
        active = [v for v in await self.store.vacations_covering(today)
                  if v["id"] not in ahead_ids]
        if not covering and not active:
            return
        sent: list = await self.store.get_meta("vacation_open_announced", []) or []
        changed = False
        for v in covering:
            key = _vac_key(v)
            if key in sent:
                continue
            if _vac_redundant(v, covering):
                # an outer range speaks for this one; recorded anyway so that
                # deleting the outer range later cannot make this announce late
                sent.append(key)
                changed = True
                continue
            if await self._send_vacation_opening(v, target):
                sent.append(key)
                changed = True
        for v in active:
            key = _vac_key(v)
            if key in sent:
                continue
            if await self._send_vacation_opening(v, target, live=True):
                sent.append(key)
                changed = True
        if changed:
            await self.store.set_meta("vacation_open_announced", sent[-100:])

    async def _send_vacation_opening(self, v: dict, target: str,
                                     live: bool = False) -> bool:
        rng = fmt_vac_range(v)
        if live:
            text = (
                f"🏖️ החופשה כבר פעילה\n{rng}\n\n"
                f"עד סוף הטווח {_vac_scope(v)}.\n"
                f"תזכורות על אימונים שכבר הזמנת ימשיכו להגיע כרגיל.\n\n"
                f"אפשר לבטל אותה כאן — מה שדילגתי עליו ועדיין ניתן להזמנה "
                f"יחזור לתור. שיעורים שחלון ההרשמה שלהם כבר נסגר לא יחזרו."
            )
        else:
            text = (
                f"🏖️ החופשה מתחילה להשפיע מהערב\n{rng}\n\n"
                f"בתקופה הזאת {_vac_scope(v)}.\n"
                f"תזכורות על אימונים שכבר הזמנת ימשיכו להגיע כרגיל.\n\n"
                f"ההרשמה לשיעורים של {_he_day(target)} ({target}) נפתחת מחר — "
                f"זו הנקודה האחרונה שבה ביטול החופשה עוד תופס מקום. "
                f"ביום החופשה עצמו החלון כבר ייפתח וייסגר בלעדינו."
            )
        batch = "vac" + secrets.token_urlsafe(6)
        ref = json.dumps({"ids": [v["id"]],
                          "from": v["date_from"], "to": v["date_to"]})
        cid_cancel, cid_keep = secrets.token_urlsafe(8), secrets.token_urlsafe(8)
        for cid, act in ((cid_cancel, "vcancel"), (cid_keep, "vkeep")):
            await self.store.add_prompt(cid, NO_SCHEDULE, act,
                                        batch_id=batch, vacation_ref=ref)
        # no ℹ️ button: that branch looks up a session, and this prompt has none
        buttons = [
            [{"text": "🗑️ בטל את החופשה", "data": f"vcancel:{cid_cancel}"}],
            [{"text": "✅ ממשיך כמתוכנן", "data": f"vkeep:{cid_keep}"}],
        ]
        delivered = await self.notifier.send(
            text, buttons, kind="vacation",
            is_answered=lambda: self.store.any_answered([cid_cancel, cid_keep]),
        )
        if not delivered:
            _LOGGER.warning("Vacation opening announcement delivery failed for %s", rng)
            return False
        await self.store.log_event(
            "info", "vacation", f"הודעת פתיחת חופשה · {rng}",
            "החופשה כבר פעילה" if live
            else f"ההודעה הלילית ל-{target} נחסמה", notified=True)
        return True

    async def _announce_vacation_end(self, today: date) -> None:
        ending = await self.store.vacations_ending_on(today.isoformat())
        if not ending:
            return
        tomorrow = (today + timedelta(days=1)).isoformat()
        still = await self.store.vacations_covering(tomorrow)
        sent: list = await self.store.get_meta("vacation_close_announced", []) or []
        changed = False
        for v in ending:
            key = _vac_key(v)
            if key in sent:
                continue
            lines = [f"🏖️ החופשה {fmt_vac_range(v)} מסתיימת היום."]
            if still:
                # an adjacent range makes "resumes tomorrow" a lie
                last = max(w["date_to"] for w in still)
                lines.append(f"אבל יש טווח נוסף שממשיך עד {last} — "
                             f"האוטומציה עדיין בהשהיה.")
            else:
                lines.append("מחר האוטומציה חוזרת לפעול: הודעה לילית, "
                             "תזמונים והזמנות אוטומטיות כרגיל.")
            base = self.settings.base_url
            lines.append(f"להוספה או עריכה של חופשות: {base}/automations"
                         if base else
                         "להוספה או עריכה של חופשות — פתח/י את האפליקציה.")
            delivered = await self.notifier.send("\n".join(lines), kind="vacation")
            if not delivered:
                _LOGGER.warning("Vacation closing announcement delivery failed for %s",
                                fmt_vac_range(v))
                continue
            await self.store.log_event(
                "info", "vacation", f"הודעת סיום חופשה · {fmt_vac_range(v)}",
                "טווח נוסף ממשיך" if still else "האוטומציה חוזרת מחר",
                notified=True)
            sent.append(key)
            changed = True
        if changed:
            await self.store.set_meta("vacation_close_announced", sent[-100:])

    async def _vacation_callback(self, action: str, prompt: dict,
                                 cid: str = "") -> str:
        """The vacation buttons.

        Every return lands in Telegram's popup, which truncates at 190 chars,
        so each is one line. A handler's reply is delivered button-less on both
        transports — so the second confirmation has to be a fresh send() from
        in here, not buttons attached to the answer.
        """
        ref = json.loads(prompt.get("vacation_ref") or "{}")
        ids = ref.get("ids") or []
        vac = await self.store.get_vacation(ids[0]) if ids else None

        if action == "vkeep":
            # also disarms a confirmation armed by an earlier press
            await self.store.answer_batch(prompt.get("batch_id"))
            await self.store.log_event(
                "info", "vacation", "החופשה אושרה כמתוכנן",
                f"{ref.get('from')} → {ref.get('to')}", notified=True)
            return "✅ ממשיכים כמתוכנן. לא אשאל שוב על החופשה הזו."

        if vac is None:
            return "החופשה כבר לא קיימת — כנראה נמחקה באפליקציה. לא עשיתי כלום."
        if (vac["date_from"], vac["date_to"]) != (ref.get("from"), ref.get("to")):
            # edited since the message went out: deleting now would remove a
            # range the user never saw on the button
            return (f"⚠️ החופשה עודכנה מאז ({vac['date_from']}→{vac['date_to']}). "
                    f"לא ביטלתי — אפשר לבטל אותה באפליקציה.")

        if action == "vcancel":
            cid2 = secrets.token_urlsafe(8)
            await self.store.add_prompt(
                cid2, NO_SCHEDULE, "vconfirm",
                batch_id=prompt.get("batch_id"),
                vacation_ref=prompt.get("vacation_ref"))
            delivered = await self.notifier.send(
                f"⚠️ לבטל את החופשה {fmt_vac_range(vac)}?\n"
                f"האוטומציה תחזור לפעול בכל הטווח, ושיעורים שדילגתי עליהם "
                f"יחזרו לתור.\nלחיצה על הכפתור מבצעת — אין חזרה אחורה.",
                [[{"text": "🗑️ כן, בטל את החופשה", "data": f"vconfirm:{cid2}"}]],
                kind="vacation",
                is_answered=lambda: self.store.any_answered([cid2]),
            )
            if not delivered:
                # the press was already consumed and the message carrying the
                # next button never arrived — without re-arming, the original
                # button is dead and the vacation can only be cancelled in the
                # web UI, which the user has no reason to suspect
                if cid:
                    await self.store.restore_prompt(cid)
                await self.store.delete_prompt(cid2)
                return "❌ שליחת בקשת האישור נכשלה — אפשר ללחוץ שוב."
            return "❓ שלחתי בקשת אישור — לחיצה נוספת תבטל את החופשה."

        # vconfirm. Delete BEFORE reviving: both ticks re-check the vacation
        # at fire time, so a class revived while the row still exists is
        # re-skipped — and re-announced — within five minutes.
        rng = fmt_vac_range(vac)
        await self.store.delete_vacation(vac["id"])
        revived = await self.store.revive_vacation_skips(
            vac["date_from"], vac["date_to"])
        await self.schedule_openings()
        # windows that already opened are taken by the ticks; detached so a
        # slow Arbox round-trip cannot expire the callback query. Both, since
        # the revive covers pins and rule matches alike.
        asyncio.create_task(self.watchlist_tick())
        asyncio.create_task(self.autobook_tick())
        await self.store.log_event(
            "info", "vacation", f"החופשה בוטלה מההתראה · {rng}",
            f"{revived} שיעורים הוחזרו לתור", notified=True)
        return (f"🗑️ החופשה {rng} בוטלה. האוטומציה חזרה לפעול"
                + (f"; {revived} שיעורים חזרו לתור." if revived else "."))

    # ----------------------------------------------------------- watchlist

    async def watchlist_tick(self) -> None:
        """Grab pinned classes the moment their window opens.

        Rules match by pattern; this is the "I want *that* class" path, and it
        works on classes that are still locked — the entry simply waits for the
        window instead of being rejected.
        """
        async with self._tick_lock:
            await self._watchlist_tick()

    async def _watchlist_tick(self) -> None:
        now = datetime.now()
        for w in await self.store.list_watchlist(pending_only=True):
            s = await self.store.get_session(w["schedule_id"])
            if not s:
                continue
            if s.get("user_booked") is not None or s.get("user_in_standby") is not None:
                await self.store.mark_watch(w["schedule_id"], "already booked")
                continue
            # a retained past class with advance_hours 0 still reads as
            # "open" — without this a stale pin would fire a real booking
            # for a class that already happened. Marked, not just skipped,
            # so the age prune can collect it.
            if s["date"] < date.today().isoformat():
                await self.store.mark_watch(
                    w["schedule_id"], "expired — class date passed")
                continue
            if not w["ignore_vacation"] and \
                    await self.store.vacation_blocks(s["date"], "autobook"):
                await self.store.mark_watch(w["schedule_id"], "skipped — vacation")
                await self.store.log_event(
                    "warn", "watchlist", f"דילגתי על תזמון · {fmt_class(s)}",
                    "התאריך בחופשה שהגדרת והתזמון לא סומן כעוקף",
                    w["schedule_id"], notified=True)
                await self.notifier.send(
                    f"🏖️ דילגתי על {_fmt_session(s)} — התאריך בחופשה שהגדרת",
                    kind="autobook")
                continue
            if self._blocked(s):
                await self.store.mark_watch(w["schedule_id"], "blocked category")
                await self.store.log_event(
                    "warn", "watchlist", f"לא ניתן לתפוס · {fmt_class(s)}",
                    "סוג השיעור הוגדר להתעלמות על ידך", w["schedule_id"])
                continue
            membership_override, membership_ready = \
                await self._validate_watch_membership(w, s)
            if not membership_ready:
                continue
            is_open, _ = registration_open(s, now)
            if not is_open:
                continue
            await self._grab_watched(
                s, bool(w["allow_standby"]), membership_override)

    async def _grab_watched(
        self, s: dict, allow_standby: bool,
        membership_override: int | None = None,
    ) -> None:
        sid = s["schedule_id"]
        label = fmt_class(s)
        opt = s.get("booking_option")
        try:
            await self.syncer.ensure_identity()
            if opt == "insertScheduleUser":
                updated, membership_id = await self.perform_membership_action(
                    s, "book", membership_override)
                msg = f"🎯 נתפס עבורך: {label}"
            elif opt == "insertStandby" and allow_standby:
                updated, membership_id = await self.perform_membership_action(
                    s, "standby", membership_override)
                msg = f"🎯⏳ מלא — נכנסת לרשימת ההמתנה: {label}"
            elif opt == "insertStandby":
                await self.store.mark_watch(sid, "full, standby not allowed")
                await self.store.log_event(
                    "warn", "watchlist", f"התזמון לא נתפס · {label}",
                    "השיעור התמלא ולא אישרת רשימת המתנה", sid, notified=True)
                await self.notifier.send(
                    f"🎯❌ {label} התמלא ולא אישרת רשימת המתנה", kind="autobook")
                return
            else:
                return  # not actionable yet; try again next tick
        except PlanningBlocked:
            await self.reconcile_planned_quota()
            return
        except ArboxError as err:
            if err.error_name() == "registerScheduleDisabled":
                return  # window not really open yet — retry on the next tick
            if await self._learn_block(err, s):
                await self.store.mark_watch(sid, "blocked category")
                return
            if await self._defer_transient(s, err, "watchlist"):
                return      # unmarked on purpose — the 5-minute tick retries
            await self.store.mark_watch(sid, f"failed: {err}")
            await self.store.log_event(
                "error", "watchlist", f"תפיסת תזמון נכשלה · {label}", str(err), sid,
                notified=True)
            await self.notifier.send(f"🎯❌ נכשל: {label}\n{err}", kind="autobook")
            return
        if updated and updated.get("id"):
            await self.store.upsert_sessions([updated])
        await self.store.record_booking_success(
            sid, "book" if opt == "insertScheduleUser" else "standby", "pin",
            membership_id)
        await self.store.mark_watch(sid, "booked")
        await self.store.clear_retry(sid, "watchlist")
        await self.store.log_event(
            "info", "watchlist", msg.lstrip("🎯⏳ "),
            f"תזמון · {s.get('free')} מקומות פנויים בזמן התפיסה", sid)
        await self.notifier.send(msg, kind="autobook")
        await self.send_calendar_file(sid)

    async def _learn_block(self, err: ArboxError, s: dict) -> bool:
        # Eligibility is recorded with the exact attempted membership in the
        # central action path. A class refusal is never a global category ban.
        return False

    def _blocked(self, s: dict) -> bool:
        # Explicit user blocks are independent of membership eligibility.
        # Legacy automatic blocks are migrated only with matching log evidence.
        return self.settings.is_blocked(s.get("category_name"))

    async def schedule_openings(self) -> None:
        """Register a one-shot job at each upcoming opening moment.

        The 5-minute tick is a safety net, not a stopwatch: a class that fills
        within seconds needs us there *at* the opening. Jobs are keyed by
        schedule_id so re-running this is idempotent.
        """
        if not self.scheduler:
            return
        now = datetime.now()
        horizon = now + timedelta(hours=36)
        wanted: set[int] = {
            w["schedule_id"] for w in await self.store.list_watchlist(pending_only=True)
        }
        rules = [r for r in await self.store.list_rules()
                 if r["enabled"] and r["mode"] == "autobook"]
        if rules:
            skipped = await self.store.automation_skip_ids()
            for s in await self.store.get_sessions(date_from=date.today().isoformat()):
                if s["schedule_id"] not in skipped and any(rule_matches(r, s) for r in rules):
                    wanted.add(s["schedule_id"])

        opening_jobs = {
            int(job.id.removeprefix("open_")): job
            for job in self.scheduler.get_jobs()
            if job.id.startswith("open_")
            and job.id.removeprefix("open_").isdigit()
        }
        for sid, job in opening_jobs.items():
            if sid not in wanted:
                self.scheduler.remove_job(job.id)

        for sid in wanted:
            job_id = f"open_{sid}"
            existing = opening_jobs.get(sid)
            s = await self.store.get_session(sid)
            if not s or not s.get("advance_hours"):
                if existing:
                    self.scheduler.remove_job(job_id)
                continue
            try:
                start = datetime.fromisoformat(f"{s['date']}T{s['start_time']}")
            except (TypeError, ValueError):
                if existing:
                    self.scheduler.remove_job(job_id)
                continue
            opens = opening_moment(start, s["advance_hours"])
            if not (now < opens <= horizon):
                if existing:
                    self.scheduler.remove_job(job_id)
                continue
            if existing:
                scheduled = existing.next_run_time
                if scheduled.tzinfo is not None:
                    scheduled = scheduled.astimezone().replace(tzinfo=None)
                if abs((scheduled - opens).total_seconds()) \
                        <= OPENING_JITTER_SECONDS[1] + 1:
                    continue
                self.scheduler.remove_job(job_id)
                _LOGGER.info(
                    "Registration opening changed for %s; moving job from %s to %s",
                    sid, scheduled, opens,
                )
            # Randomised slack, not a fixed offset: it absorbs clock skew
            # against the studio's server AND avoids hitting the same second
            # after every opening, which is what a scripted client looks like.
            # Kept under half a minute — a sought-after class can be gone by
            # then, so this is the widest jitter that still wins the race.
            delay = random.uniform(*OPENING_JITTER_SECONDS)
            self.scheduler.add_job(
                self._opening_fired, "date",
                run_date=opens + timedelta(seconds=delay),
                args=[sid], id=job_id, replace_existing=True,
                misfire_grace_time=300,
            )
            _LOGGER.info(
                "Scheduled opening grab for %s at %s (+%.0fs)", sid, opens, delay
            )
        # This sweep also discovers newly-synced rule matches. Reconcile the
        # month here so an over-capacity automation is reported while there is
        # still time to choose, not only when its registration window opens.
        await self.reconcile_planned_quota()

    async def _opening_fired(self, schedule_id: int) -> None:
        """A window just opened — act immediately, then let the ticks follow."""
        _LOGGER.info("Opening moment reached for %s", schedule_id)
        # Refresh the class we are about to grab, not the next 48h: studios can
        # open registration well beyond the near-term span, and the current
        # booking option must be fresh at the exact opening moment.
        # We book from the stored booking_option, so a stale row is the
        # difference between joining the waitlist and reporting a failure.
        s = await self.store.get_session(schedule_id)
        try:
            if s and s.get("date"):
                await self.syncer.sync_range(s["date"], s["date"])
            else:
                await self.syncer.near_term_sync()
        except ArboxError:
            pass
        await self.watchlist_tick()
        await self.autobook_tick()

    # ------------------------------------------------------------ autobook

    async def autobook_tick(self) -> None:
        """Runs every few minutes: book any rule-matched session whose
        registration window just opened. Idempotent via autobook_done."""
        async with self._tick_lock:
            await self._autobook_tick()

    async def _autobook_tick(self) -> None:
        rules = [r for r in await self.store.list_rules()
                 if r["enabled"] and r["mode"] == "autobook"]
        if not rules:
            return
        now = datetime.now()
        skipped = await self.store.automation_skip_ids()
        sessions = await self.store.get_sessions(date_from=date.today().isoformat())
        held = await self.store.get_meta(self.membership_policy.key(0) + ':held_plans') or {}
        for s in sessions:
            if s["schedule_id"] in skipped:
                continue
            if s.get("user_booked") is not None or s.get("user_in_standby") is not None:
                continue
            matched = [r for r in rules if rule_matches(r, s)]
            if not matched:
                continue
            if await self.store.autobook_attempted(s["schedule_id"]):
                continue
            is_open, _ = registration_open(s, now)
            if not is_open:
                continue
            advance = s.get("advance_hours")
            if advance:
                # A window that opened long ago is a backlog, not a trigger:
                # without this a restart replays a whole week at once.
                # advance_hours == 0 has no opening moment, so it is exempt.
                # Derived from the start time, like registration_open, rather
                # than the stored column, which may be NULL or stale.
                try:
                    start = datetime.fromisoformat(f"{s['date']}T{s['start_time']}")
                except (KeyError, TypeError, ValueError):
                    continue
                grace = (opening_moment(start, advance)
                         + timedelta(hours=CATCHUP_GRACE_HOURS))
                if now > grace and not _arrived_after(s, matched, grace) and str(s['schedule_id']) not in held:
                    # A backlog is only a backlog if we could have acted at
                    # the time. A rule written today, or a class the studio
                    # published after its own window opened, never had that
                    # chance — those are booked normally. What is left really
                    # is a miss, and it is recorded once instead of being
                    # re-skipped in silence on every tick for a week.
                    await self._mark_missed_window(s, grace)
                    continue
            if self._blocked(s):
                continue      # already announced once, when it was learned
            if await self.store.autobook_attempted(s["schedule_id"]):
                continue
            if await self.store.vacation_blocks(s["date"], "autobook"):
                _LOGGER.info(
                    "Autobook skipped for %s — %s is inside a vacation",
                    s["schedule_id"], s["date"],
                )
                # Marked, not just skipped. Without the row the
                # autobook_attempted() guard above can never fire, so every
                # tick re-reaches this branch: a 5-minute tick against a
                # 10-minute event dedupe announced the same skip every 15
                # minutes until the class date passed. The pin path has
                # always done this — the literal must match what
                # revive_vacation_skips looks for, em dash included.
                await self.store.mark_autobook(
                    s["schedule_id"], "skipped — vacation")
                await self.store.log_event(
                    "warn", "autobook",
                    f"דילגתי על אוטומציה · {fmt_class(s)}",
                    "התאריך בחופשה שחוסמת הזמנה אוטומטית", s["schedule_id"],
                    notified=True)
                # sent here rather than left to the event-log push, which is
                # gated by each channel's log_level — a setting that has
                # nothing to say about vacations
                await self.notifier.send(
                    f"🏖️ דילגתי על {_fmt_session(s)} — התאריך בחופשה שהגדרת",
                    kind="autobook")
                continue
            await self._try_autobook(s)

    async def _mark_missed_window(self, s: dict, grace: datetime) -> None:
        """Record, once, that a rule match was past its catch-up window.

        Marked rather than skipped: the mark is what stops the next tick from
        arriving at the same branch, and what puts the class in the decisions
        list, where "why was I never booked?" is asked. info, not warn — this
        is a decision, not a failure, and info is never pushed to a channel.
        """
        if await self.store.autobook_attempted(s["schedule_id"]):
            return
        await self.store.mark_autobook(
            s["schedule_id"], "missed — window opened before the rule ran")
        await self.store.log_event(
            "info", "autobook", f"לא נתפס · {fmt_class(s)}",
            f"חלון ההרשמה נפתח לפני יותר מ-{CATCHUP_GRACE_HOURS} שעות "
            f"({grace:%d/%m %H:%M}) — אפשר לתזמן ידנית",
            s["schedule_id"])

    async def _defer_transient(self, s: dict, err: ArboxError, kind: str) -> bool:
        """A blip, not an answer: leave the row pending so a tick retries it.

        Returns True when the failure was absorbed. A dropped connection or a
        502 at the opening moment used to be written down as a permanent
        failure, and the safety-net ticks skip anything already marked — one
        bad second cost a class that stayed bookable for days.
        """
        if not err.transient:
            return False
        n = await self.store.bump_retry(s["schedule_id"], kind)
        if n >= TRANSIENT_MAX_ATTEMPTS:
            return False
        await self.store.log_event(
            "info", kind, f"ניסיון נוסף · {fmt_class(s)}",
            f"תקלה זמנית מול ארבוקס (ניסיון {n}/{TRANSIENT_MAX_ATTEMPTS}): {err}",
            s["schedule_id"])
        _LOGGER.warning("Transient failure on %s (%s attempt %d): %s",
                        s["schedule_id"], kind, n, err)
        return True

    async def _try_autobook(self, s: dict) -> None:
        label = fmt_class(s)
        try:
            await self.syncer.ensure_identity()
            updated, membership_id = await self.perform_membership_action(s, "book")
            if updated and updated.get("id"):
                await self.store.upsert_sessions([updated])
            await self.store.record_booking_success(
                s["schedule_id"], "book", "rule", membership_id)
            await self.store.mark_autobook(s["schedule_id"], "booked")
            await self.store.clear_retry(s["schedule_id"], "autobook")
            await self.store.log_event(
                "info", "autobook", f"הוזמן אוטומטית · {label}",
                f"כלל · {s.get('free')} מקומות פנויים בזמן ההזמנה",
                s["schedule_id"])
            await self.notifier.send(f"🤖 הוזמן אוטומטית: {label}", kind="autobook")
            await self.send_calendar_file(s["schedule_id"])
            _LOGGER.info("Autobooked %s", s["schedule_id"])
        except PlanningBlocked:
            await self.reconcile_planned_quota()
            return
        except ArboxError as err:
            if err.error_name() == "registerScheduleDisabled":
                # fired a hair before the 168h window opened (clock skew) —
                # don't mark done, the next 5-min tick will retry
                _LOGGER.info("Autobook %s: window not open yet, will retry", s["schedule_id"])
                return
            if await self._learn_block(err, s):
                await self.store.mark_autobook(s["schedule_id"], "blocked category")
                return
            if await self._defer_transient(s, err, "autobook"):
                return      # unmarked on purpose — the 5-minute tick retries
            await self.store.mark_autobook(s["schedule_id"], f"failed: {err}")
            await self.store.log_event(
                "error", "autobook", f"הזמנה אוטומטית נכשלה · {label}",
                str(err), s["schedule_id"], notified=True)
            await self.notifier.send(f"🤖❌ הזמנה אוטומטית נכשלה: {label}\n{err}", kind="autobook")
            _LOGGER.error("Autobook %s failed: %s", s["schedule_id"], err)

    # ------------------------------------------------------ studio messages

    async def studio_messages_tick(self) -> None:
        """Hourly: announce new studio messages (feed.boxMessage).

        The studio uses these to say things that matter (schedule changes,
        events); nobody opens the feed unprompted. Dedupe by message id in
        meta; the very first run seeds silently so history doesn't flood.
        """
        try:
            feed = await self.client.feed()
        except ArboxError as err:
            _LOGGER.warning("Studio message check failed: %s", err)
            await self.store.log_event(
                "warn", "studio", "בדיקת הודעות מהסטודיו נכשלה", str(err))
            return
        # the same call already carries the activity counters
        results = (feed.get("scheduleUserStatus") or {}).get("results") or {}
        if results:
            await self.store.set_meta("activity", {
                "attended": results.get("past"),
                "upcoming": results.get("future"),
                "weekly_average": results.get("average"),
            })
        msgs = feed.get("boxMessage") or []
        # archive before deciding what is new: the feed drops old ones, and
        # this hourly pass is the only chance to keep them
        await self.store.save_box_messages(msgs)
        seen: list | None = await self.store.get_meta("seen_box_messages")
        if seen is None:
            await self.store.set_meta(
                "seen_box_messages", [m.get("id") for m in msgs]
            )
            _LOGGER.info("Studio messages: seeded %d without notifying", len(msgs))
            return
        new = [m for m in msgs if m.get("id") not in seen]
        for m in new:
            text = (m.get("message") or m.get("subject") or "").strip()
            if text:
                await self.notifier.send(
                    f"📢 הודעה מהסטודיו:\n{text}", kind="studio"
                )
            seen.append(m.get("id"))
        if new:
            await self.store.set_meta("seen_box_messages", seen[-100:])
            _LOGGER.info("Studio messages: %d new announced", len(new))

    # ------------------------------------------------------ standby events

    async def on_standby_promoted(self, session: dict) -> None:
        await self.store.log_event(
            "info", "booking",
            f"עלית מרשימת ההמתנה · {fmt_class(session)}",
            "Arbox שחרר לך מקום", session.get("schedule_id"))
        await self.notifier.send(
            f"🎉 עלית מרשימת ההמתנה! את/ה רשומ/ה ל: "
            f"{session['date']} {_fmt_session(session)}",
            kind="standby",
        )
        # Promotion consumes capacity even though no action passed through our
        # booking endpoint. Refresh immediately so the next scheduled class is
        # allocated from the true upstream balance.
        asyncio.create_task(self._refresh_memberships_quietly())

    async def standby_watch_tick(self) -> None:
        """Every 5 min: while waitlisted for a near-term class, poll fast.

        Arbox notifies promotions by Expo push to the phone (we cannot and
        should not intercept that) — so we detect them ourselves: the sync
        diff catches user_in_standby -> user_booked, and the feed's
        standbyEntranceApproval catches studios that require the user to
        CONFIRM the freed spot. Runs only while a standby actually exists,
        so the extra upstream load is zero the rest of the time.
        """
        now = datetime.now()
        horizon = now + timedelta(hours=48)
        watching = [
            s for s in await self.store.get_sessions(
                date_from=now.date().isoformat(),
                date_to=horizon.date().isoformat(),
                mine=True,
            )
            if s.get("user_in_standby") is not None
        ]
        if not watching:
            return
        try:
            # diff-based promotion detection lives inside the sync
            await self.syncer.near_term_sync()
            feed = await self.client.feed()
        except ArboxError as err:
            _LOGGER.warning("Standby watch failed: %s", err)
            return
        approvals = feed.get("standbyEntranceApproval") or []
        if not approvals:
            return
        seen: list = await self.store.get_meta("seen_standby_approvals", [])
        for a in approvals:
            aid = a.get("id")
            if aid in seen:
                continue
            seen.append(aid)
            await self.notifier.send(
                "⚠️ התפנה מקום בשיעור שאת/ה בהמתנה אליו — "
                "הסטודיו מחכה לאישור שלך! פתח/י את האפליקציה לאישור הכניסה.",
                kind="standby",
            )
        await self.store.set_meta("seen_standby_approvals", seen[-50:])
