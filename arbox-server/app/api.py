"""REST API for the frontend and the HA integration.

Reads come from SQLite only. Writes (book/cancel/standby/settings) require the
X-Api-Key header — the tailnet is trusted for viewing, not for booking.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from .rules import PlanningBlocked
from .arbox_client import ArboxAuthError, ArboxError
from .ical import build_calendar, google_calendar_url
from .journal import (
    KIND_LABELS, METRIC_LABELS, PACKS, catalog_equipment, catalog_metadata, catalogue,
    exercise_by_id, resolve_exercise, search_catalogue, suggested_pack_ids,
)
from .rules import fmt_class, fmt_when, registration_open, rule_matches

_LOGGER = logging.getLogger(__name__)

from .studio_context import StudioContextRoute

router = APIRouter(prefix="/api", route_class=StudioContextRoute)


def ctx(request: Request):
    return request.app.state


def require_key(request: Request, x_api_key: str | None) -> None:
    if not has_key(request, x_api_key):
        raise HTTPException(401, "bad or missing X-Api-Key")


def has_key(request: Request, x_api_key: str | None) -> bool:
    """Is this a keyed request? compare_digest rather than != — this one
    check guards every booking and every settings write, and a plain
    comparison leaks the key prefix through its own timing."""
    return secrets.compare_digest(
        (x_api_key or "").encode(),
        (ctx(request).settings.api_key or "").encode(),
    )


@router.get("/panel/context")
async def panel_context(request: Request, x_api_key: str | None = Header(None)):
    """Authenticated capability and active-studio handshake for the HA panel."""
    require_key(request, x_api_key)
    s = ctx(request)
    async with s.syncer.exclusive():
        return {
            "studio_id": s.syncer.box_id,
            "name": s.syncer.studio_name,
            "timezone": s.settings.timezone,
            "last_sync": await s.store.get_meta("last_sync"),
            "capabilities": {"studio_guard": True, "panel_api": 1},
        }


# ---------------------------------------------------------------- models

class SetupBody(BaseModel):
    email: str
    password: str
    whitelabel: str = "Arbox"   # the studio's branded app name


class ScheduleAction(BaseModel):
    schedule_id: int
    membership_user_id: int | None = None


class StudioAction(BaseModel):
    studio_id: int


class StudioPreferences(BaseModel):
    default_studio_id: int
    ignored_studio_ids: list[int] = []


REASON_CODES = {
    "work", "illness", "injury", "personal", "fatigue",
    "plans_changed", "other", "none",
}


def _clean_reason(code: str | None, text: str | None) -> tuple[str, str | None]:
    code = (code or "").strip()
    if code not in REASON_CODES:
        raise HTTPException(422, "invalid reason_code")
    cleaned = (text or "").strip() or None
    if code == "other" and not cleaned:
        raise HTTPException(422, "reason_text is required for 'other'")
    if code != "other":
        cleaned = None
    if cleaned and len(cleaned) > 500:
        raise HTTPException(422, "reason_text is too long")
    return code, cleaned


class RuleBody(BaseModel):
    id: int | None = None
    name: str
    enabled: bool = True
    coaches: list[str] = []
    categories: list[str] = []
    weekdays: list[int] = []
    time_from: str | None = None
    time_to: str | None = None
    mode: str = "notify"


class ExerciseBody(BaseModel):
    exercise_id: str | None = None
    name: str
    metric_type: str = "note"
    sets: int | None = None
    reps: int | None = None
    weight: float | None = None
    weight_unit: str | None = "kg"
    duration_seconds: int | None = None
    attempts: int | None = None
    distance: float | None = None
    distance_unit: str | None = "m"
    notes: str | None = None


class JournalBody(BaseModel):
    coach_feedback: str | None = None
    class_feedback: str | None = None
    notes: str | None = None
    exercises: list[ExerciseBody] = []


class CustomExerciseBody(BaseModel):
    name: str
    metric_type: str = "note"


class ExerciseShortcutBody(BaseModel):
    action: str


# ---------------------------------------------------------------- health

@router.get("/health")
async def health(request: Request):
    from .build_info import APP_REVISION, APP_VERSION, BUILD_INFO
    s = ctx(request)
    identity = await s.store.get_meta("identity") or {}
    return {
        "status": "ok",
        "version": APP_VERSION,
        "revision": APP_REVISION,
        "build": BUILD_INFO,
        "configured": s.client.configured,
        "authenticated": s.client.authenticated,
        "whitelabel": s.client.whitelabel,
        "last_sync": await s.store.get_meta("last_sync"),
        "studio": identity.get("studio_name"),
        # the server's own wall clock, so a browser can notice it has drifted.
        # A clock that is wrong shifts every registration grab by the same
        # amount and is otherwise completely silent.
        "timezone": s.settings.timezone,
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


# ----------------------------------------------------------------- setup

@router.post("/setup")
async def setup(request: Request, body: SetupBody):
    s = ctx(request)
    if s.client.configured:
        raise HTTPException(409, "already configured — DELETE /api/setup first")
    try:
        await s.client.login(
            email=body.email, password=body.password,
            whitelabel=(body.whitelabel or "Arbox").strip(),
        )
    except ArboxAuthError:
        s.client.clear_creds()
        raise HTTPException(401, "Arbox rejected the credentials")
    except ArboxError as err:
        s.client.clear_creds()
        raise HTTPException(502, f"Arbox unreachable: {err}")
    asyncio.create_task(_bg_window_sync(s, "initial"))
    # the ONE place the key is handed out, so the UI can store it
    return {"ok": True, "api_key": s.settings.api_key}


@router.delete("/setup")
async def reset_setup(request: Request, x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    ctx(request).client.clear_creds()
    return {"ok": True}


# -------------------------------------------------------------- schedule

@router.get("/schedule")
async def schedule(
    request: Request,
    date_from: str | None = None,
    date_to: str | None = None,
    coach: str | None = None,
    category: str | None = None,
    mine: bool = False,
    refresh: bool = False,
    x_api_key: str | None = Header(None),
):
    s = ctx(request)
    # reading the cached schedule stays open; making the server call Arbox
    # does not — and this path had no debounce at all
    if refresh and has_key(request, x_api_key):
        asyncio.create_task(_bg_window_sync(s, "on-demand"))
    if not date_from:
        date_from = date.today().isoformat()
    if not date_to:
        date_to = (date.today() + timedelta(days=7)).isoformat()
    sessions = await s.store.get_sessions(
        date_from=date_from, date_to=date_to, coach=coach,
        category=category, mine=mine,
    )
    rules = ([r for r in await s.store.list_rules()
              if r["enabled"] and r["mode"] == "autobook"]
             if has_key(request, x_api_key) else [])
    vacations = await s.store.list_vacations() if rules else []
    if has_key(request, x_api_key):
        states = ((await s.rules_engine.quota_status()) or {}).get("plan_states", {})
        for row in sessions:
            row["planning"] = states.get(str(row["schedule_id"]))
    return {
        "sessions": _annotate(
            sessions, lambda name: s.rules_engine._blocked({"category_name": name}),
            rules, vacations,
            await s.store.automation_skip_ids() if has_key(request, x_api_key) else set()),
        "last_sync": await s.store.get_meta("last_sync"),
    }


def _annotate(rows: list[dict], blocked=lambda _n: False,
              autobook_rules: list[dict] | None = None,
              vacations: list[dict] | None = None,
              automation_skips: set[int] | None = None) -> list[dict]:
    """Attach the registration-window verdict every session control reads.

    Both the schedule and "my classes" render the same five-state button, so
    both need this — without it a class whose window is still shut looks
    bookable right now.
    """
    now = datetime.now()
    for row in rows:
        row.pop("raw_json", None)
        matched = [r for r in (autobook_rules or []) if rule_matches(r, row)]
        vacation_blocked = bool(matched) and any(
            v.get("block_autobook") and v.get("date_from") <= row.get("date", "")
            <= v.get("date_to") for v in (vacations or []))
        if automation_skips is not None:
            row["automation_skipped"] = row["schedule_id"] in automation_skips
        row["autobook_match"] = (bool(matched) and not vacation_blocked
                                 and not row.get("automation_skipped"))
        row["autobook_blocked_by_vacation"] = vacation_blocked
        row["autobook_rule_names"] = [r["name"] for r in matched]
        if row.get("booking_option") == "past":
            # history rows: never bookable, and "blocked" would be noise
            row["registration_open"] = False
            row["registration_note"] = "השיעור כבר התקיים"
            row["blocked"] = False
            continue
        is_open, why = registration_open(row, now)
        row["registration_open"] = is_open
        row["registration_note"] = why
        row["blocked"] = blocked(row.get("category_name"))
    return rows


async def _bg_window_sync(s, label: str) -> None:
    try:
        await s.syncer.window_sync()
    except ArboxError as err:
        _LOGGER.error("%s window sync failed: %s", label, err)


class RefreshBody(BaseModel):
    date_from: str | None = None
    date_to: str | None = None


_last_manual_refresh: dict[str, float] = {}


@router.post("/refresh")
async def refresh(request: Request, body: RefreshBody | None = None,
                  x_api_key: str | None = Header(None)):
    """On-demand sync from Arbox — awaits completion and reports the result.

    Optional date_from/date_to narrow it to a day or range (clamped to
    [today, +62d]); omitted -> the full 30-day window. Debounced 10s PER
    RANGE, so tapping today→tomorrow→day-after in a row works, while a
    double-tap on the same button costs Arbox one call, not two.
    """
    import time

    # Keyed: this reaches Arbox under the user's own session. The debounce is
    # not a substitute — it keys on the caller's own date range, so any
    # unauthenticated caller could drive unlimited syncs by varying the dates.
    require_key(request, x_api_key)
    s = ctx(request)
    body = body or RefreshBody()
    key = f"{body.date_from}|{body.date_to}"
    now = time.monotonic()
    if now - _last_manual_refresh.get(key, 0.0) < 10:
        return {
            "ok": True,
            "skipped": "debounced",
            "last_sync": await s.store.get_meta("last_sync"),
        }
    _last_manual_refresh[key] = now
    if len(_last_manual_refresh) > 100:  # bounded — it's just a debounce map
        _last_manual_refresh.clear()
        _last_manual_refresh[key] = now

    try:
        count = await s.syncer.sync_range(body.date_from, body.date_to)
    except ArboxError as err:
        raise HTTPException(502, f"Arbox sync failed: {err}")
    return {
        "ok": True,
        "synced_sessions": count,
        "last_sync": await s.store.get_meta("last_sync"),
    }


@router.get("/facets")
async def facets(request: Request):
    return await ctx(request).store.facets()


@router.get("/me")
async def me(request: Request, x_api_key: str | None = Header(None)):
    """The schedule half is open on the tailnet, as the module docstring says.

    The membership object is not: it is the same personal data /profile
    already refuses to serve without a key, and serving it here made that
    gate decorative.
    """
    s = ctx(request)
    rules = ([r for r in await s.store.list_rules()
              if r["enabled"] and r["mode"] == "autobook"]
             if has_key(request, x_api_key) else [])
    vacations = await s.store.list_vacations() if rules else []
    sessions = await s.store.my_sessions(
        include_watched=True, date_from=date.today().isoformat())
    if has_key(request, x_api_key):
        seen = {row["schedule_id"] for row in sessions}
        plans = await s.rules_engine._planned_sessions(
            date.today().isoformat(), "9999-12-31", include_skipped=True)
        for row in [*plans, *await s.rules_engine.uncertain_sessions()]:
            if row["schedule_id"] not in seen:
                sessions.append(row)
                seen.add(row["schedule_id"])
        sessions.sort(key=lambda row: (row["date"], row.get("start_time") or ""))
    if has_key(request, x_api_key):
        quota = await s.rules_engine.quota_status() or {}
        states = quota.get("plan_states", {})
        changes = {e['schedule_id']:e for e in await s.store.planning_history()}
        sessions = [row for row in sessions if not (row.get('automation_skipped') and
                    changes.get(row['schedule_id'], {}).get('status') == 'planning_change_cancelled')]
        for row in sessions:
            row["planning"] = states.get(str(row["schedule_id"]))
            event = changes.get(row['schedule_id'])
            if event and event['status'] == 'planning_change_accepted':
                row['planning_change'] = {**event['planning_change'], 'accepted_at': event['occurred_at']}
    return {
        "membership": (await s.store.get_meta("membership")
                       if has_key(request, x_api_key) else None),
        "memberships": (await s.store.get_meta("memberships")
                        if has_key(request, x_api_key) else None),
        "sessions": _annotate(
            sessions,
            lambda name: s.rules_engine._blocked({"category_name": name}),
            rules, vacations),
    }


@router.put("/automations/occurrences/{schedule_id}/skip")
async def skip_automation_occurrence(
    schedule_id: int, request: Request, x_api_key: str | None = Header(None),
):
    return await _set_occurrence_skip(schedule_id, True, request, x_api_key)


@router.delete("/automations/occurrences/{schedule_id}/skip")
async def restore_automation_occurrence(
    schedule_id: int, request: Request, x_api_key: str | None = Header(None),
):
    return await _set_occurrence_skip(schedule_id, False, request, x_api_key)


async def _set_occurrence_skip(schedule_id, skipped, request, x_api_key):
    require_key(request, x_api_key)
    s = ctx(request)
    # Share the booking lock: when this returns success the next tick cannot
    # race past the exclusion, and an already-completed booking is not hidden.
    async with s.rules_engine._tick_lock:
        session = await s.store.get_session(schedule_id)
        if not session:
            raise HTTPException(404, "השיעור לא נמצא")
        if session.get("user_booked") is not None or session.get("user_in_standby") is not None:
            raise HTTPException(409, "כבר קיימת הרשמה לשיעור — יש להשתמש בביטול הרשמה")
        try:
            starts = datetime.fromisoformat(f"{session['date']}T{session['start_time']}")
        except (KeyError, TypeError, ValueError):
            raise HTTPException(409, "מועד השיעור אינו זמין")
        if starts <= datetime.now():
            raise HTTPException(409, "השיעור כבר התחיל")
        if skipped:
            plans = await s.rules_engine._planned_sessions(
                session["date"], session["date"], include_skipped=True)
            if not any(p["schedule_id"] == schedule_id and p["planning_source"] == "autobook"
                       for p in plans):
                raise HTTPException(409, "השיעור אינו מופע אוטומטי מתוכנן")
        await s.store.set_automation_skip(schedule_id, skipped)
        await s.store.set_meta("quota_cache", None)
    await s.rules_engine.schedule_openings()
    return {"ok": True, "schedule_id": schedule_id, "automation_skipped": skipped}


_last_feed_fetch = 0.0
FEED_TTL_SECONDS = 300


@router.get("/messages")
async def messages(request: Request, x_api_key: str | None = Header(None)):
    """Studio messages, straight from the user feed (no dedicated endpoint
    exists upstream — the app reads them from feed.boxMessage too)."""
    import time

    global _last_feed_fetch
    s = ctx(request)
    # the archive is the point: if Arbox is unreachable we still serve what
    # we kept, and only lose the read-state and the counters
    #
    # The live call is rate-limited on a clock the caller does not control:
    # this used to hit Arbox on every render of the "my classes" view, and
    # the route takes no key, so anyone who could reach the port could drive
    # unbounded upstream traffic under the user's session. The hourly
    # studio_messages_tick keeps the archive fresh regardless.
    now = time.monotonic()
    may_fetch = has_key(request, x_api_key) or now - _last_feed_fetch > FEED_TTL_SECONDS
    feed: dict = {}
    if may_fetch:
        _last_feed_fetch = now
        try:
            feed = await s.client.feed()
        except ArboxError as err:
            _LOGGER.warning("Messages: feed unavailable, serving archive (%s)", err)
            feed = {}
    live = feed.get("boxMessage") or []
    await s.store.save_box_messages(live)
    unread = {m.get("id") for m in live if not m.get("has_read")}
    # served from the archive, so the list reaches back further than the
    # feed itself does; read-state only exists for what the feed still has
    out = [
        {**m, "has_read": m["id"] not in unread}
        for m in await s.store.list_box_messages()
    ]
    status = feed.get("scheduleUserStatus") or {}
    results = status.get("results") or {}
    return {
        "messages": out,
        "stats": {
            "past_classes": results.get("past"),
            "future_classes": results.get("future"),
            "weekly_average": results.get("average"),
        },
    }


def _ics(body: str, filename: str) -> Response:
    # `inline`, not `attachment`: attachment tells a browser "this is a file to
    # download", which is why iOS/Brave interrupt with a download prompt instead
    # of handing the event to the calendar. With inline the OS opens its
    # text/calendar handler directly; the filename still applies if it does save.
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-cache, max-age=0",
        },
    )


async def _studio_location(s) -> str:
    identity = await s.store.get_meta("identity") or {}
    parts = [identity.get("studio_name"), identity.get("address")]
    return ", ".join(p for p in parts if p)


@router.get("/calendar/export")
async def calendar_export(request: Request, schedule_id: int,
                          x_api_key: str | None = Header(None)):
    """HA exports the same event, alarms and location without exposing this server."""
    require_key(request, x_api_key)
    s = ctx(request)
    session = await s.store.get_session(schedule_id)
    if not session:
        raise HTTPException(404, f"unknown schedule_id {schedule_id}")
    location = await _studio_location(s)
    return {
        "ics": build_calendar([session], alarms=s.settings.calendar_alarms, location=location),
        "google": google_calendar_url(session, location=location),
    }


@router.get("/calendar/event/{schedule_id}.ics")
async def calendar_event(request: Request, schedule_id: int):
    """One class as a downloadable file — the one-tap 'add to my calendar'.

    Deliberately unauthenticated: it returns a single class from the studio's
    own schedule (title, coach, time, description, headcount) with no personal
    identifiers, and both the web link and the HA notification action are
    plain URLs that cannot carry an X-Api-Key header.
    """
    s = ctx(request)
    session = await s.store.get_session(schedule_id)
    if not session:
        raise HTTPException(404, f"unknown schedule_id {schedule_id}")
    return _ics(
        build_calendar(
            [session],
            alarms=s.settings.calendar_alarms,
            location=await _studio_location(s),
        ),
        f"arbox-{schedule_id}.ics",
    )


@router.get("/calendar/event/{schedule_id}/google")
async def calendar_event_google(request: Request, schedule_id: int):
    """Bounce straight into Google Calendar with the event prefilled — the
    desktop-browser answer, where a downloaded .ics is an extra step."""
    s = ctx(request)
    session = await s.store.get_session(schedule_id)
    if not session:
        raise HTTPException(404, f"unknown schedule_id {schedule_id}")
    url = google_calendar_url(session, location=await _studio_location(s))
    if not url:
        raise HTTPException(422, "session has no usable start time")
    return RedirectResponse(url, status_code=302)


@router.get("/quota")
async def quota(request: Request, x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    q = await ctx(request).rules_engine.quota_status()
    return q or {"quota": 0}


@router.get("/summary")
async def summary(request: Request, x_api_key: str | None = Header(None)):
    """Everything the HA integration needs, in one call.

    Keyed, because it carries the membership plan and every booked class —
    and because the design has always said HA authenticates with the key.
    """
    require_key(request, x_api_key)
    s = ctx(request)
    now = datetime.now()
    # date-floored: HA polls this every minute, and with history retained an
    # unfloored fetch would scan a year of rows to keep three
    mine = await s.store.my_sessions(date_from=date.today().isoformat())
    active = [
        m for m in mine
        if datetime.fromisoformat(
            f"{m['date']}T{m.get('end_time') or m['start_time']}"
        ) > now
    ]
    future = [m for m in active if datetime.fromisoformat(
        f"{m['date']}T{m['start_time']}") > now]
    nxt = future[0] if future else None
    week = await s.store.get_sessions(
        date_from=date.today().isoformat(),
        date_to=(date.today() + timedelta(days=7)).isoformat(),
    )
    for row in (*week, *mine):
        row.pop("raw_json", None)
    journal_rows = await s.store.workout_journals()
    return {
        # the wall-clock times below are studio-local; HA cannot know that on
        # its own, and a HA instance left on UTC published every class hours off
        "timezone": s.settings.timezone,
        "membership": await s.store.get_meta("membership"),
        "memberships": await s.store.get_meta("memberships") or [],
        "quota": await s.rules_engine.quota_status(),
        "next_class": nxt,
        "my_sessions": active,
        "week": week,
        "journal": {
            "level": s.settings.journal["level"],
            "entries": journal_rows[:20],
            "count": sum(1 for x in journal_rows if x.get("coach_feedback")
                         or x.get("class_feedback") or x.get("notes")
                         or x.get("exercises")),
        },
        "last_sync": await s.store.get_meta("last_sync"),
    }


# --------------------------------------------------------------- actions

_ACTION_HE = {"book": "הזמנת שיעור", "standby": "כניסה לרשימת המתנה",
              "cancel": "ביטול"}


async def _do_action(
    request: Request, schedule_id: int, kind: str, late_cancel: bool = False,
    reason_code: str | None = None, reason_text: str | None = None,
    membership_user_id: int | None = None,
) -> dict:
    s = ctx(request)
    session = await s.store.get_session(schedule_id)
    if not session:
        raise HTTPException(404, f"unknown schedule_id {schedule_id}")
    allowed = {
        "book": "insertScheduleUser",
        "standby": "insertStandby",
        "cancel": ("cancelScheduleUser", "cancelWaitList"),
    }[kind]
    opt = session.get("booking_option")
    booking_id = (session.get("user_in_standby") if opt == "cancelWaitList"
                  else session.get("user_booked"))
    ok = opt in allowed if isinstance(allowed, tuple) else opt == allowed
    if not ok:
        raise HTTPException(409, f"booking_option is '{opt}', cannot {kind}")
    cancel_reason = None
    if kind == "cancel":
        cancel_reason = _clean_reason(reason_code, reason_text)
    # a category the membership does not cover is refused here rather than
    # upstream: the answer is already known, so the call is pure waste
    if kind != "cancel" and s.rules_engine._blocked(session):
        raise HTTPException(
            409, f"blocked_category: המנוי שלך לא כולל את "
                 f"{session.get('category_name')}. אפשר להסיר בהגדרות")
    # Held across the upstream write and the local upsert: a sync that
    # fetched its payload just before this action would otherwise upsert
    # the pre-action state back over it, and the booking would vanish from
    # "my classes" and the HA sensor until the next sweep.
    async with s.syncer.exclusive():
        try:
            await s.syncer.ensure_identity()
            if kind == "book":
                updated, membership_id = await s.rules_engine.perform_membership_action(
                    session, "book", membership_user_id)
            elif kind == "standby":
                updated, membership_id = await s.rules_engine.perform_membership_action(
                    session, "standby", membership_user_id)
            elif opt == "cancelWaitList":
                # leaving a waitlist is its own endpoint, keyed by the standby id
                updated = await s.client.leave_standby(session["user_in_standby"])
            else:
                # cancelling a booking needs the schedule_user_id (= user_booked)
                # and a late-cancel ack when inside the (12h) window
                if not late_cancel and not await s.client.check_late_cancel(schedule_id):
                    raise HTTPException(
                        409,
                        "late_cancel_required: cancelling now counts as a late "
                        "cancellation — resend with late_cancel: true to confirm",
                    )
                updated = await s.client.cancel(
                    session["user_booked"], schedule_id, late_cancel
                )
        except HTTPException:
            raise
        except PlanningBlocked as err:
            raise HTTPException(409, str(err))
        except ArboxError as err:
            # a manual attempt teaches us the same thing an automatic one would
            await s.rules_engine._learn_block(err, session)
            await s.store.log_event(
                "error", "booking", f"{_ACTION_HE[kind]} נכשל · {fmt_class(session)}",
                str(err), schedule_id)
            raise HTTPException(502, str(err))

        # a deliberate action is worth recording precisely because the automation
        # did not do it: without this line, a class that vanished from "my classes"
        # is indistinguishable from a grab that silently failed
        await s.store.log_event(
            "info", "booking", f"{_ACTION_HE[kind]} · {fmt_class(session)}",
            "פעולה ידנית" + (" · ביטול מאוחר" if late_cancel else ""), schedule_id)

        # every write returns the full updated session — upsert it directly,
        # no follow-up sync round-trip needed
        if updated and updated.get("id"):
            await s.store.upsert_sessions([updated])
        if kind in ("book", "standby"):
            await s.store.record_booking_success(
                schedule_id, kind, "manual", membership_id)
        if kind == "cancel" and cancel_reason:
            was_standby = opt == "cancelWaitList"
            status = ("standby_cancelled" if was_standby else
                      "cancelled_late" if late_cancel else "cancelled_safe")
            deadline_at = None
            cancel_hours = session.get("cancel_hours")
            if not was_standby and cancel_hours is not None:
                try:
                    starts = datetime.fromisoformat(
                        f"{session['date']}T{session['start_time']}")
                    deadline_at = (
                        starts - timedelta(hours=int(cancel_hours))
                    ).isoformat(timespec="minutes")
                except (KeyError, TypeError, ValueError):
                    pass
            await s.store.set_training_outcome(
                schedule_id, status, "cancellation",
                cancel_reason[0], cancel_reason[1],
                booking_id=booking_id,
                counts_entry=False if was_standby else late_cancel,
                deadline_at=deadline_at,
            )
            await s.store.expire_prompts(
                schedule_id, ("attendance", "attendance_reason"))
            pending_text = await s.store.get_meta("attendance_other_input")
            if pending_text and int(pending_text.get("schedule_id", -1)) == schedule_id:
                await s.store.set_meta("attendance_other_input", None)
        if kind == "cancel":
            asyncio.create_task(s.rules_engine._refresh_memberships_quietly())
    fresh = await s.store.get_session(schedule_id)
    if fresh:
        fresh.pop("raw_json", None)
    quota_note = None
    if kind in ("book", "standby"):
        q = await s.rules_engine.quota_status(target_date=session['date']) or {}
        chosen = next((m for m in q.get('memberships', []) if m['id'] == membership_id), {})
        if chosen.get('available') == 0:
            quota_note = f"כל הכניסות במכסה של {chosen.get('plan') or 'המנוי שנבחר'} נוצלו או שמורות לתקופה הזו"
        if q.get('overcommitted'):
            quota_note = 'יש תכנונים נוספים ללא כיסוי במנוי המתאים. בדקו את פירוט המנויים בלשונית שלי'
        if quota_note:
            await s.store.log_event("warn", "quota", quota_note,
                                    fmt_class(session), schedule_id)
    return {
        "ok": True,
        "session": fresh,
        "quota_note": quota_note,
        # the UI turns this into an "add to calendar" affordance right where
        # the booking happened
        "calendar_url": (
            f"/api/calendar/event/{schedule_id}.ics" if kind == "book" else None
        ),
        "google_url": (
            f"/api/calendar/event/{schedule_id}/google" if kind == "book" else None
        ),
    }


class CancelAction(ScheduleAction):
    late_cancel: bool = False
    # Non-interactive clients from older releases cannot open the reason
    # dialog; they remain compatible and are recorded explicitly as no reason.
    reason_code: str = "none"
    reason_text: str | None = None


class AttendanceAction(BaseModel):
    status: str
    reason_code: str | None = None
    reason_text: str | None = None


@router.post("/book")
async def book(request: Request, body: ScheduleAction,
               x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    return await _do_action(
        request, body.schedule_id, "book",
        membership_user_id=body.membership_user_id)


@router.post("/cancel")
async def cancel(request: Request, body: CancelAction,
                 x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    return await _do_action(
        request, body.schedule_id, "cancel", late_cancel=body.late_cancel,
        reason_code=body.reason_code, reason_text=body.reason_text,
    )


@router.post("/standby")
async def standby(request: Request, body: ScheduleAction,
                  x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    return await _do_action(
        request, body.schedule_id, "standby",
        membership_user_id=body.membership_user_id)


@router.get("/profile")
async def profile(request: Request, x_api_key: str | None = Header(None)):
    """Account, membership and studio details — key-gated, it is personal data.

    Served from the meta snapshots that sync already writes, so opening the
    settings screen costs no upstream call.
    """
    require_key(request, x_api_key)
    s = ctx(request)
    prof = await s.store.get_meta("profile") or {}
    membership = await s.store.get_meta("membership") or {}
    activity = await s.store.get_meta("activity") or {}
    # a cached identity means ensure_identity() alone would not refill these,
    # so fetch outright when the snapshot is missing or predates a field
    if not prof or not membership.get("card_ends"):
        try:
            prof = await s.syncer.refresh_profile()
            membership = await s.store.get_meta("membership") or {}
        except ArboxError as err:
            raise HTTPException(502, str(err))
    return {"profile": prof, "membership": membership,
            "memberships": await s.store.get_meta("memberships") or [],
            "studios": await s.store.get_meta("studios") or [],
            "studio_affiliations": await s.store.get_meta("studio_affiliations") or [],
            "selected_studio_id": s.syncer.box_id,
            "default_studio_id": s.settings.preferred_studio_id,
            "ignored_studio_ids": s.settings.ignored_studio_ids,
            "activity": activity,
            "quota": await s.rules_engine.quota_status()}


@router.get("/history")
async def history(request: Request, x_api_key: str | None = Header(None)):
    """My past classes, fetched only when the collapsed section is opened —
    the list grows for a year, and 'שלי' must not pay for it on every visit."""
    require_key(request, x_api_key)
    s = ctx(request)
    return {
        "sessions": await s.store.training_history(),
        "stats": await s.store.attendance_stats(),
        "decisions": await s.store.automation_decisions(),
        "events": await s.store.training_events(),
        "changes": await s.store.planning_history(),
    }


@router.put("/history/{schedule_id}/attendance")
async def set_attendance(
    request: Request, schedule_id: int, body: AttendanceAction,
    x_api_key: str | None = Header(None),
):
    require_key(request, x_api_key)
    s = ctx(request)
    session = await s.store.get_session(schedule_id)
    if not session:
        raise HTTPException(404, f"unknown schedule_id {schedule_id}")
    try:
        ended = datetime.fromisoformat(
            f"{session['date']}T{session.get('end_time') or session['start_time']}"
        )
    except (TypeError, ValueError):
        raise HTTPException(409, "session end time is unknown")
    if ended > datetime.now():
        raise HTTPException(409, "session has not ended yet")
    if body.status == "attended":
        reason_code = reason_text = None
    elif body.status == "missed":
        reason_code, reason_text = _clean_reason(body.reason_code, body.reason_text)
    else:
        raise HTTPException(422, "status must be attended or missed")
    await s.store.set_training_outcome(
        schedule_id, body.status, "manual", reason_code, reason_text)
    await s.store.expire_prompts(
        schedule_id, ("attendance", "attendance_reason"))
    return {"ok": True, "status": body.status}


@router.post("/history/backfill")
async def history_backfill(request: Request,
                           x_api_key: str | None = Header(None)):
    """Import the past year of my classes. Safe to re-run — inserts only."""
    require_key(request, x_api_key)
    s = ctx(request)
    prior = await s.store.get_meta("history_backfill")
    try:
        result = await s.syncer.backfill_history()
    except ArboxError as err:
        raise HTTPException(502, f"backfill failed: {err}")
    return {**result, "already_ran": prior is not None}


# --------------------------------------------------------- workout journal

def _journal_stats(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    teachers: dict[str, dict] = {}
    exercises: dict[str, dict] = {}
    for entry in entries:
        coach = entry.get("coach_name")
        feedback = entry.get("coach_feedback")
        if coach:
            stat = teachers.setdefault(coach, {
                "name": coach, "classes": 0, "positive": 0, "neutral": 0,
                "negative": 0, "not_applicable": 0,
            })
            stat["classes"] += 1
            if feedback in ("positive", "neutral", "negative", "not_applicable"):
                stat[feedback] += 1
        for item in entry.get("exercises") or []:
            name = item.get("name")
            if not name:
                continue
            exercise_id = item.get("exercise_id")
            key = exercise_id or str(name).casefold()
            stat = exercises.setdefault(key, {
                "id": exercise_id, "name": name,
                "metric_type": item.get("metric_type") or "note",
                "sessions": 0, "points": [],
            })
            stat["sessions"] += 1
            reps_total = ((item.get("sets") or 1) * item.get("reps")
                          if item.get("reps") is not None else None)
            weight = item.get("weight")
            stat["points"].append({
                "date": entry.get("date"), "sets": item.get("sets"),
                "reps": item.get("reps"), "reps_total": reps_total,
                "weight": weight,
                "volume": reps_total * weight if reps_total and weight else None,
                "duration_seconds": item.get("duration_seconds"),
                "attempts": item.get("attempts"), "distance": item.get("distance"),
            })
    return (
        sorted(teachers.values(), key=lambda x: (-x["classes"], x["name"])),
        sorted(exercises.values(), key=lambda x: (-x["sessions"], x["name"])),
    )


@router.get("/journal")
async def workout_journal(request: Request,
                          x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    journals = {int(x["schedule_id"]): x for x in await s.store.workout_journals()}
    entries = []
    for session in await s.store.journal_candidates():
        sid = int(session["schedule_id"])
        saved = journals.pop(sid, {})
        entries.append({**session, **saved, "schedule_id": sid,
                        "exercises": saved.get("exercises") or []})
    entries.extend(journals.values())  # survive ordinary schedule retention
    entries.sort(key=lambda x: (x.get("date") or "", x.get("start_time") or ""),
                 reverse=True)
    categories = [str(x.get("category_name") or "") for x in entries]
    selected = s.settings.exercise_packs()
    suggested = suggested_pack_ids(categories)
    active = selected if selected is not None else suggested
    custom = await s.store.custom_exercises()
    pinned = s.settings.exercise_shortcuts()
    hidden = s.settings.hidden_exercises()
    teachers, exercise_stats = _journal_stats(entries)
    return {
        "settings": s.settings.journal, "entries": entries,
        "teachers": teachers, "exercise_stats": exercise_stats,
        "packs": [{"id": p["id"], "name": p["name"], "icon": p["icon"],
                   "suggested": p["id"] in suggested} for p in PACKS],
        "active_pack_ids": active,
        "catalogue": catalogue(active, custom, pinned, hidden),
        "catalogue_meta": catalog_metadata(),
        "metric_labels": METRIC_LABELS, "custom_exercises": custom,
    }


@router.put("/journal/{schedule_id}")
async def save_workout_journal(
    request: Request, schedule_id: int, body: JournalBody,
    x_api_key: str | None = Header(None),
):
    require_key(request, x_api_key)
    s = ctx(request)
    if not await s.store.get_session(schedule_id) and not await s.store.workout_journal(schedule_id):
        raise HTTPException(404, f"unknown schedule_id {schedule_id}")
    feedback_values = {None, "positive", "neutral", "negative", "not_applicable"}
    if (body.coach_feedback not in feedback_values
            or body.class_feedback not in feedback_values):
        raise HTTPException(422, "invalid feedback value")
    notes = (body.notes or "").strip() or None
    if notes and len(notes) > 4000:
        raise HTTPException(422, "notes are too long")
    exercises = []
    for raw in body.exercises[:50]:
        item = raw.model_dump()
        item["name"] = item["name"].strip()
        if not item["name"]:
            continue
        if item["metric_type"] not in METRIC_LABELS:
            raise HTTPException(422, "invalid metric_type")
        known = exercise_by_id(item.get("exercise_id"))
        if not known and not item.get("exercise_id"):
            known = resolve_exercise(item["name"])
        if known:
            item["exercise_id"] = known["id"]
            item["name"] = known["name"]
        elif item.get("exercise_id") and not str(item["exercise_id"]).startswith("custom-"):
            item["exercise_id"] = None
        for field in ("sets", "reps", "duration_seconds", "attempts"):
            if item[field] is not None and not 0 <= item[field] <= 100000:
                raise HTTPException(422, f"invalid {field}")
        for field in ("weight", "distance"):
            if item[field] is not None and not 0 <= item[field] <= 1000000:
                raise HTTPException(422, f"invalid {field}")
        exercises.append(item)
    saved = await s.store.save_workout_journal(
        schedule_id, coach_feedback=body.coach_feedback,
        class_feedback=body.class_feedback, notes=notes, exercises=exercises)
    return {"ok": True, "journal": saved}


@router.post("/journal/exercises")
async def add_custom_exercise(
    request: Request, body: CustomExerciseBody,
    x_api_key: str | None = Header(None),
):
    require_key(request, x_api_key)
    name = body.name.strip()
    if not name or len(name) > 120:
        raise HTTPException(422, "exercise name is required")
    if body.metric_type not in METRIC_LABELS:
        raise HTTPException(422, "invalid metric_type")
    return {"ok": True, "exercise": await ctx(request).store.add_custom_exercise(
        name, body.metric_type)}


@router.delete("/journal/exercises/{exercise_id}")
async def delete_custom_exercise(
    request: Request, exercise_id: int,
    x_api_key: str | None = Header(None),
):
    require_key(request, x_api_key)
    if not await ctx(request).store.delete_custom_exercise(exercise_id):
        raise HTTPException(404, "exercise not found")
    return {"ok": True}


@router.get("/journal/exercise-catalog")
async def exercise_catalog(
    request: Request, q: str = "", kind: str = "", equipment: str = "",
    include_hidden: bool = False, limit: int = 60,
    x_api_key: str | None = Header(None),
):
    require_key(request, x_api_key)
    s = ctx(request)
    settings = s.settings
    selected = settings.exercise_packs()
    if selected is None:
        categories = [x.get("category_name") or "" for x in
                      await s.store.get_sessions(date_from=date.today().isoformat())]
        active_packs = suggested_pack_ids(categories)
    else:
        active_packs = selected
    quick_ids = {
        row["id"] for row in catalogue(
            active_packs, [], settings.exercise_shortcuts(),
            settings.hidden_exercises())
    }
    items, total = search_catalogue(
        q[:120], kind=kind, equipment=equipment,
        pinned_ids=settings.exercise_shortcuts(),
        hidden_ids=settings.hidden_exercises(),
        include_hidden=include_hidden, limit=limit,
    )
    items = [{**item, "shortcut": item["id"] in quick_ids} for item in items]
    return {
        "items": items, "total": total, "kinds": KIND_LABELS,
        "meta": catalog_metadata(), "equipment": catalog_equipment(),
    }


@router.post("/journal/exercise-catalog/{exercise_id}")
async def update_exercise_shortcut(
    request: Request, exercise_id: str, body: ExerciseShortcutBody,
    x_api_key: str | None = Header(None),
):
    require_key(request, x_api_key)
    if not exercise_by_id(exercise_id):
        raise HTTPException(404, "exercise not found")
    if body.action not in {"add", "remove", "restore"}:
        raise HTTPException(422, "action must be add, remove or restore")
    ctx(request).settings.set_exercise_shortcut(exercise_id, body.action)
    return {"ok": True, "action": body.action}


# ----------------------------------------------------------------- events

@router.get("/events")
async def events(request: Request, level: str | None = None,
                 source: str | None = None, limit: int = 200,
                 x_api_key: str | None = Header(None)):
    """The activity log, plus the status strip computed from it.

    Key-gated: the rows name the classes you booked. `status` is derived at
    query time rather than stored — "Home Assistant failed x3" is a count over
    recent events, not a field somebody has to remember to clear.
    """
    require_key(request, x_api_key)
    s = ctx(request)
    rows = await s.store.list_events(level=level, source=source, limit=limit)

    last_sync = await s.store.get_meta("last_sync")
    sync_age = None
    if last_sync:
        try:
            # clamped: a clock that ran backwards (container restart, DST)
            # should read "just now", not "-3 minutes ago"
            sync_age = max(0, int(
                (datetime.now() - datetime.fromisoformat(last_sync)).total_seconds() // 60
            ))
        except ValueError:
            pass

    channels = {}
    for name in ("telegram", "ha"):
        ch = s.settings.telegram if name == "telegram" else s.settings.ha
        if not ch.get("enabled"):
            channels[name] = {"state": "off"}
            continue
        fails = [
            e for e in await s.store.list_events(source="notify", tag=name, limit=50)
            if e["level"] in ("warn", "error")
        ]
        channels[name] = {"state": "failing" if fails else "ok",
                          "failures": len(fails),
                          "last": fails[0]["ts"] if fails else None}

    return {
        "events": rows,
        "counts": await s.store.event_counts(days=7),
        "status": {
            "timezone": s.settings.timezone,
            "server_time": datetime.now().isoformat(timespec="seconds"),
            "sync_age_minutes": sync_age,
            "last_sync": last_sync,
            "channels": channels,
            "pending_pins": len(await s.store.list_watchlist(pending_only=True)),
        },
    }


# ------------------------------------------------------------- watchlist

class WatchBody(BaseModel):
    schedule_id: int
    allow_standby: bool = True
    # set only after the user is shown the conflict and confirms — a vacation
    # gates automation, it should never veto a deliberate choice
    ignore_vacation: bool = False
    # Legacy clients may send this, but it never bypasses a quota gate.
    confirm_over_quota: bool = False
    membership_user_id: int | None = None


class WatchMembershipBody(BaseModel):
    membership_user_id: int | None = None


class ConfirmPlanChangeBody(BaseModel):
    expected_token: str


@router.post('/planning/{schedule_id}/confirm-change')
async def confirm_plan_change(request: Request, schedule_id: int, body: ConfirmPlanChangeBody,
                              x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    try:
        planning = await ctx(request).rules_engine.confirm_plan_change(schedule_id, body.expected_token)
    except ArboxError as err:
        raise HTTPException(409, str(err)) from err
    return {'ok': True, 'planning': planning}


@router.get("/watchlist")
async def get_watchlist(request: Request):
    s = ctx(request)
    rows = await s.store.list_watchlist()
    sessions = await s.store.get_sessions_by_ids(
        [w["schedule_id"] for w in rows])
    out = []
    for w in rows:
        sess = sessions.get(w["schedule_id"])
        if sess:
            sess.pop("raw_json", None)
        out.append({**w, "session": sess})
    return {"watchlist": out}


@router.post("/watchlist")
async def add_watch(request: Request, body: WatchBody,
                    x_api_key: str | None = Header(None)):
    """Pin a class — including one whose registration window is still shut."""
    require_key(request, x_api_key)
    s = ctx(request)
    async with s.syncer.exclusive():
        sess = await s.store.get_session(body.schedule_id)
        if not sess:
            raise HTTPException(404, f"unknown schedule_id {body.schedule_id}")
        if s.rules_engine._blocked(sess):
            raise HTTPException(
                409, f"blocked_category: אין טעם לתזמן את "
                     f"{sess.get('category_name')} — המנוי שלך לא כולל אותה")
        # a pin that a vacation will veto must say so NOW — discovering it days
        # later, when the window opened and nothing happened, is the worst case
        conflict = None
        if not body.ignore_vacation and \
                await s.store.vacation_blocks(sess["date"], "autobook"):
            conflict = (f"⚠️ {sess['date']} נמצא בתוך חופשה שחוסמת הזמנה אוטומטית.\n"
                        f"לתזמן בכל זאת רק את השיעור הזה? (החופשה תמשיך לחסום את השאר)")
            # nothing is stored yet — the client decides and re-posts with the flag
            return {
                "ok": False,
                "needs_confirm": True,
                "confirm_kind": "vacation",
                "conflict": conflict,
            }
        if body.membership_user_id:
            active = await s.rules_engine._active_memberships(sess.get("date"))
            if not any(m.get("id") == body.membership_user_id for m in active):
                raise HTTPException(
                    422, "selected membership is not valid on the class date")
        await s.store.watch(body.schedule_id, body.allow_standby,
                            body.ignore_vacation, body.membership_user_id)
        await s.rules_engine.review_plans()
        await s.rules_engine.schedule_openings()   # arm the exact-moment job now
        # if the window is already open there is no reason to wait for a tick
        await s.rules_engine.watchlist_tick()
        await s.rules_engine.reconcile_planned_quota()
        sess = await s.store.get_session(body.schedule_id)
        projected = await s.rules_engine.quota_status(target_date=sess["date"])
        return {"ok": True, "registration_open": sess.get("registration_opens"),
                "note": registration_open(sess, datetime.now())[1],
                "overrode_vacation": body.ignore_vacation,
                "planning": (projected or {}).get("plan_states", {}).get(str(body.schedule_id)),
                "quota_note": ((projected or {}).get("plan_states", {}).get(str(body.schedule_id), {}).get("reason"))}


@router.delete("/watchlist/{schedule_id}")
async def remove_watch(request: Request, schedule_id: int,
                       x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    async with s.rules_engine._tick_lock:
        await s.store.unwatch(schedule_id)
        await s.rules_engine.reconcile_planned_quota()
    return {"ok": True}


@router.put("/watchlist/{schedule_id}/membership")
async def update_watch_membership(
    request: Request, schedule_id: int, body: WatchMembershipBody,
    x_api_key: str | None = Header(None),
):
    require_key(request, x_api_key)
    s = ctx(request)
    sess = await s.store.get_session(schedule_id)
    if not sess:
        raise HTTPException(404, f"unknown schedule_id {schedule_id}")
    if body.membership_user_id:
        active = await s.rules_engine._active_memberships(sess.get("date"))
        if not any(m.get("id") == body.membership_user_id for m in active):
            raise HTTPException(
                422, "selected membership is not valid on the class date")
    if not await s.store.set_watch_membership(
            schedule_id, body.membership_user_id):
        raise HTTPException(404, "pending watch not found")
    await s.rules_engine.reconcile_planned_quota()
    return {"ok": True}


# ------------------------------------------------------------- vacations

class VacationBody(BaseModel):
    id: int | None = None      # present -> edit in place
    date_from: str
    date_to: str
    block_notify: bool = True
    block_autobook: bool = True


@router.get("/vacations")
async def list_vacations(request: Request):
    """Active ranges still gate the engine; past ones are history — kept and
    returned separately so the UI can tuck them away."""
    today = date.today().isoformat()
    all_v = await ctx(request).store.list_vacations()
    return {
        "active": [v for v in all_v if v["date_to"] >= today],
        "history": [v for v in all_v if v["date_to"] < today],
    }


@router.post("/vacations")
async def add_vacation(request: Request, body: VacationBody,
                       x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    try:
        df, dt = date.fromisoformat(body.date_from), date.fromisoformat(body.date_to)
    except ValueError:
        raise HTTPException(422, "dates must be YYYY-MM-DD")
    if dt < df:
        raise HTTPException(422, "date_to is before date_from")
    if not (body.block_notify or body.block_autobook):
        raise HTTPException(422, "a vacation must block at least one of notify/autobook")
    s = ctx(request)
    # snapshot before the UPDATE: shrinking a range must free the pins that
    # fall outside the new one, and afterwards we can no longer see them
    prev = await s.store.get_vacation(body.id) if body.id else None
    vid = await s.store.add_vacation(
        body.date_from, body.date_to, body.block_notify, body.block_autobook,
        vac_id=body.id,
    )
    revived = 0
    if prev:
        # union of old and new; the revive query leaves alone anything still
        # covered by the updated vacation
        revived = await s.store.revive_vacation_skips(
            min(prev["date_from"], body.date_from),
            max(prev["date_to"], body.date_to))
        if revived:
            await s.rules_engine.schedule_openings()
            asyncio.create_task(s.rules_engine.watchlist_tick())
            asyncio.create_task(s.rules_engine.autobook_tick())
    await s.rules_engine.reconcile_planned_quota()
    return {"ok": True, "id": vid, "revived": revived}


@router.delete("/vacations/{vac_id}")
async def delete_vacation(request: Request, vac_id: int,
                          x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    # the same outcome as cancelling from the notification: deleting here must
    # also put back the classes this vacation skipped, pinned and rule-matched
    v = await s.store.get_vacation(vac_id)
    await s.store.delete_vacation(vac_id)
    revived = 0
    if v:
        revived = await s.store.revive_vacation_skips(v["date_from"], v["date_to"])
        await s.rules_engine.schedule_openings()
        asyncio.create_task(s.rules_engine.watchlist_tick())
        asyncio.create_task(s.rules_engine.autobook_tick())
    await s.rules_engine.reconcile_planned_quota()
    return {"ok": True, "revived": revived}


# ----------------------------------------------------------------- rules

@router.get("/rules")
async def list_rules(request: Request):
    return {"rules": await ctx(request).store.list_rules()}


@router.post("/rules")
async def save_rule(request: Request, body: RuleBody,
                    x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    if body.mode not in ("notify", "autobook"):
        raise HTTPException(422, "mode must be notify|autobook")
    s = ctx(request)
    rid = await s.store.save_rule(body.model_dump())
    await s.rules_engine.schedule_openings()
    await s.rules_engine.review_plans()
    return {"ok": True, "id": rid}


@router.delete("/rules/{rule_id}")
async def delete_rule(request: Request, rule_id: int,
                      x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    await s.store.delete_rule(rule_id)
    await s.rules_engine.reconcile_planned_quota()
    return {"ok": True}


# -------------------------------------------------------------- settings

@router.get("/settings")
async def get_settings(request: Request, x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    out = s.settings.public_view()
    out["available_memberships"] = await s.store.get_meta("memberships") or []
    out["available_studios"] = await s.store.get_meta("studios") or []
    out["studio_affiliations"] = await s.store.get_meta("studio_affiliations") or []
    categories = [x.get("category_name") or "" for x in
                  await s.store.get_sessions(date_from=date.today().isoformat())]
    suggested = suggested_pack_ids(categories)
    out["exercise_pack_catalog"] = [
        {"id": p["id"], "name": p["name"], "icon": p["icon"],
         "suggested": p["id"] in suggested} for p in PACKS
    ]
    out["suggested_exercise_packs"] = suggested
    custom = await s.store.custom_exercises()
    pinned = s.settings.exercise_shortcuts()
    hidden = s.settings.hidden_exercises()
    selected = s.settings.exercise_packs()
    active = selected if selected is not None else suggested
    out["custom_exercises"] = custom
    out["exercise_shortcuts"] = catalogue(active, custom, pinned, hidden)
    out["hidden_exercises"] = [
        {**row, "hidden": True} for exercise_id in hidden
        if (row := exercise_by_id(exercise_id))
    ]
    out["exercise_catalogue_meta"] = catalog_metadata()
    out["exercise_kind_labels"] = KIND_LABELS
    return out


@router.get("/studios")
async def studios(request: Request, x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    await s.syncer.ensure_identity()
    return {
        "selected_studio_id": s.syncer.box_id,
        "default_studio_id": s.settings.preferred_studio_id,
        "studios": await s.store.get_meta("studios") or [],
        "affiliations": await s.store.get_meta("studio_affiliations") or [],
    }


@router.post("/studios/select")
async def select_studio(request: Request, body: StudioAction,
                        x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    try:
        selected = await s.syncer.select_studio(body.studio_id)
        await s.store.set_meta("quota_cache", None)
        await s.store.set_meta("effective_membership_id", None)
        await s.rules_engine.schedule_openings()
    except ArboxError as err:
        raise HTTPException(422, str(err))
    return {"ok": True, "studio": selected}


@router.post("/studios/preferences")
async def studio_preferences(request: Request, body: StudioPreferences,
                             x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    ignored = list(dict.fromkeys(int(x) for x in body.ignored_studio_ids))
    if body.default_studio_id in ignored:
        raise HTTPException(422, "default studio cannot be ignored")
    previous_ignored = s.settings.ignored_studio_ids
    s.settings.set_ignored_studios(ignored)
    try:
        studios = await s.syncer.discover_studios()
        if not studios:
            raise ArboxError("At least one studio with an active membership is required")
        if not any(x["id"] == body.default_studio_id for x in studios):
            raise ArboxError("Default studio has no active membership")
        selected = await s.syncer.select_studio(
            body.default_studio_id, make_default=True)
        await s.store.set_meta("quota_cache", None)
        await s.store.set_meta("effective_membership_id", None)
        await s.rules_engine.schedule_openings()
    except ArboxError as err:
        s.settings.set_ignored_studios(previous_ignored)
        await s.syncer.discover_studios()
        raise HTTPException(422, str(err))
    return {"ok": True, "studio": selected,
            "ignored_studio_ids": s.settings.ignored_studio_ids}


# hour/minute of every cron job, so a timezone change can rebuild them
CRON_JOBS = {
    "window_sync": {"hour": "7,15"},
    "nightly_roll": {"hour": 3, "minute": 5},
}


def _reschedule_cron(s) -> None:
    from apscheduler.triggers.cron import CronTrigger

    tz = s.settings.timezone
    jobs = dict(CRON_JOBS,
                digest={"hour": s.settings.digest_hour, "minute": 0},
                membership_check={"hour": (s.settings.digest_hour - 1) % 24,
                                  "minute": 0},
                far_range_sync={"hour": (s.settings.digest_hour - 1) % 24,
                                "minute": 30},
                vacation_announce={"hour": s.settings.digest_hour, "minute": 2})
    for job in s.scheduler.get_jobs():
        # exact ids, set explicitly in main.py. The old substring match relied
        # on APScheduler naming jobs after the method — it names them uuid4
        # hex, so three of the five cron jobs were never rebuilt and kept
        # firing in the previous timezone until the next restart.
        key = job.id if job.id in jobs else None
        if not key:
            continue
        try:
            s.scheduler.reschedule_job(
                job.id, trigger=CronTrigger(timezone=tz, **jobs[key]))
        except Exception as err:  # noqa: BLE001 — the setting is saved either way
            _LOGGER.error("Could not reschedule %s: %s", job.id, err)
    _LOGGER.info("Cron rebuilt for %s; now %s", tz, datetime.now().isoformat())


@router.post("/settings")
async def update_settings(request: Request,
                          x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    patch = await request.json()
    if patch.get("preferred_membership_id"):
        available = await s.store.get_meta("memberships") or []
        try:
            wanted = int(patch["preferred_membership_id"])
        except (TypeError, ValueError):
            raise HTTPException(422, "preferred_membership_id must be an integer")
        selected = next((m for m in available if m.get("id") == wanted
                         and m.get("active")), None)
        if not selected:
            raise HTTPException(422, "preferred membership is not active")
    s.settings.update(patch)
    if patch.get("preferred_membership_id"):
        s.syncer.membership_user_id = wanted
        await s.store.set_meta("membership", selected)
        await s.store.set_meta("quota_cache", None)
    if "timezone" in patch:
        # cron triggers were built against the old zone; rebuild them all,
        # not just the digest, or half the schedule keeps the previous offset
        s.settings.apply_timezone()
        _reschedule_cron(s)
    if "digest_hour" in patch:
        # the cron trigger was built at startup — apply the new hour live
        from apscheduler.triggers.cron import CronTrigger

        # the vacation announcement hangs off the same hour and must move with it
        for jid, hour, minute in (
            ("membership_check", (s.settings.digest_hour - 1) % 24, 0),
            ("far_range_sync", (s.settings.digest_hour - 1) % 24, 30),
            ("digest", s.settings.digest_hour, 0),
            ("vacation_announce", s.settings.digest_hour, 2),
        ):
            try:
                s.scheduler.reschedule_job(
                    jid, trigger=CronTrigger(hour=hour, minute=minute))
                _LOGGER.info("%s rescheduled to %02d:%02d", jid,
                             hour, minute)
            except Exception as err:  # noqa: BLE001 — setting saved either way
                _LOGGER.error("%s reschedule failed (applies on restart): %s",
                              jid, err)
    return {"ok": True, "settings": s.settings.public_view()}


class JournalPreviewBody(BaseModel):
    level: str


@router.post("/settings/test-journal/{channel}")
async def test_journal(request: Request, channel: str, body: JournalPreviewBody,
                       x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    if channel not in ("telegram", "ha") or body.level not in ("quick", "feedback", "full"):
        raise HTTPException(422, "בחר/י ערוץ ורמת מעקב פעילה לבדיקה")
    try:
        url = await ctx(request).rules_engine.feedback_form.start_preview(channel, body.level)
    except Exception as err:
        raise HTTPException(502, str(err))
    return {"ok": True, "channel": channel, "level": body.level, "url": url}


@router.post("/settings/test-digest")
async def test_digest(request: Request, x_api_key: str | None = Header(None)):
    """Full-dress rehearsal: real digest, real buttons, real transport.
    Pressing a button proves the round trip and books nothing."""
    require_key(request, x_api_key)
    try:
        return {"ok": True, **await ctx(request).rules_engine.send_test_digest()}
    except Exception as err:  # noqa: BLE001 — surface the reason to the UI
        raise HTTPException(502, str(err))


@router.post("/settings/test/{channel}")
async def test_channel(request: Request, channel: str,
                       x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    try:
        await ctx(request).notifier.send_test(channel)
    except Exception as err:  # noqa: BLE001
        raise HTTPException(502, str(err))
    return {"ok": True}


# ------------------------------------------------------------ HA callback

@router.post("/ha/callback")
async def ha_callback(request: Request, x_api_key: str | None = Header(None)):
    """HA automation forwards mobile_app_notification_action here.
    Expected body: {"action": "ARBOX_book:<cid>"} (the action string)."""
    require_key(request, x_api_key)
    body = await request.json()
    action: str = body.get("action", "")
    if action.startswith("ARBOX_"):
        action = action[len("ARBOX_"):]
    s = ctx(request)
    result = await s.rules_engine.handle_callback(
        action, reply_text=str(body.get("reply_text") or "") or None,
        source_channel="ha")
    from .notification_reply import NotificationReply
    if isinstance(result, NotificationReply):
        await s.notifier._send_ha(result.text, result.buttons, force=True, tag=result.tag)
        return {'ok': True, 'result': result.text}
    # the HA companion has no popup channel like Telegram's — send the
    # outcome back as a notification so the press gets visible feedback
    # the outcome text goes back as its own notification (the companion app
    # has no popup channel like Telegram's). A successful booking also gets a
    # calendar file/link, but handle_callback already dispatched that.
    if result:
        try:
            await s.notifier._send_ha(result, None, force=True)
        except Exception as err:  # noqa: BLE001 — feedback is best-effort
            _LOGGER.warning("HA feedback notification failed: %s", err)
    return {"ok": True, "result": result}


class MembershipPolicyBody(BaseModel):
    category_ids: list[int]
    limits: list[dict]
    fingerprint: str


class PlanningReconcileBody(BaseModel):
    confirm_not_booked: bool = False


@router.post('/planning/{schedule_id}/reconcile')
async def reconcile_uncertain_booking(request: Request, schedule_id: int, body: PlanningReconcileBody,
                                      x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    async with s.syncer.exclusive():
        session = await s.store.get_session(schedule_id)
        if not session:
            session = next((row for row in await s.rules_engine.uncertain_sessions()
                            if row['schedule_id'] == schedule_id), None)
        if not session:
            raise HTTPException(404, 'האימון לא נמצא בסטודיו הפעיל')
        try:
            await s.syncer.sync_range(session['date'], session['date'])
            await s.rules_engine.refresh_planning_evidence(force_history=True)
        except ArboxError as err:
            raise HTTPException(502, 'לא ניתן לאמת את מצב האימון כרגע') from err
        current = await s.store.get_session(schedule_id) or {}
        booked = current.get('user_booked') is not None or current.get('user_in_standby') is not None
        key = s.rules_engine.membership_policy.key(0) + ':uncertain'
        pending = await s.store.get_meta(key) or {}
        if booked or body.confirm_not_booked:
            pending.pop(str(schedule_id), None)
            await s.store.set_meta(key, pending)
            await s.rules_engine.reconcile_planned_quota()
            await s.rules_engine.schedule_openings()
            return {'ok': True, 'quota_note': 'ההרשמה נמצאה' if booked else 'ההשהיה הוסרה. התכנון ייבדק שוב לפי ההתאמה והמכסה'}
        return {'ok': True, 'quota_note': 'לא נמצאה הרשמה בסנכרון. ההשהיה נשארה עד שתאשרו שבדקתם גם בארבוקס'}


@router.get("/membership-policies")
async def membership_policies(request: Request, x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    members = await s.store.get_meta("memberships") or []
    return {"memberships": [{**m, "policy": await s.rules_engine.membership_policy.get(m)} for m in members],
            "categories": await s.rules_engine.membership_policy.catalog()}


@router.put("/membership-policies/{membership_id}")
async def save_membership_policy(request: Request, membership_id: int, body: MembershipPolicyBody,
                                 x_api_key: str | None = Header(None)):
    require_key(request, x_api_key)
    s = ctx(request)
    async with s.syncer.exclusive():
        members = await s.store.get_meta("memberships") or []
        member = next((m for m in members if m["id"] == membership_id), None)
        if member is None:
            raise HTTPException(404, "המנוי לא נמצא בסטודיו הפעיל")
        try:
            policy = await s.rules_engine.membership_policy.save_manual(
                member, body.category_ids, body.limits, body.fingerprint)
        except ValueError as err:
            raise HTTPException(409, str(err))
        await s.rules_engine.reconcile_planned_quota()
        await s.rules_engine.schedule_openings()
    return {"ok": True, "policy": policy}
