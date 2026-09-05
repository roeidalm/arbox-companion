"""SQLite store — the source of truth the UI and HA read from.

Nothing here talks to Arbox; sync.py writes, everyone else reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Any

import aiosqlite

_LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    schedule_id       INTEGER PRIMARY KEY,
    box_id            INTEGER,                -- studio/box this class belongs to
    date              TEXT NOT NULL,        -- YYYY-MM-DD
    start_time        TEXT NOT NULL,        -- HH:MM
    end_time          TEXT,
    coach_id          INTEGER,
    coach_name        TEXT,
    category_id       INTEGER,
    category_name     TEXT,
    category_color    TEXT,
    category_bio      TEXT,
    series_id         INTEGER,
    max_users         INTEGER,
    free              INTEGER,
    registered        INTEGER,
    stand_by          INTEGER,
    booking_option    TEXT,
    user_booked       INTEGER,             -- schedule_user_id when booked
    membership_user_id INTEGER,            -- membership used for this booking
    user_in_standby   INTEGER,             -- standby record id when waitlisted
    stand_by_position INTEGER,
    registration_opens TEXT,               -- ISO datetime, from enable_registration_time
    advance_hours      INTEGER,            -- 0 = always open, >0 = opens N h before, NULL = unknown
    block_hours        INTEGER,            -- no booking within N h of the start
    cancel_hours       INTEGER,            -- late-cancel window before start
    raw_json          TEXT NOT NULL,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_date ON sessions(date);

-- What happened to one of the user's commitments.  Kept separately from
-- sessions because an Arbox sync clears user_booked after a cancellation;
-- that upstream fact must not erase the local history or its reason.
CREATE TABLE IF NOT EXISTS training_outcomes (
    schedule_id INTEGER PRIMARY KEY,
    box_id      INTEGER,
    date        TEXT,
    start_time  TEXT,
    end_time    TEXT,
    category_name TEXT,
    coach_name  TEXT,
    status      TEXT NOT NULL,              -- attended | missed | cancelled_*
    reason_code TEXT,
    reason_text TEXT,
    source      TEXT NOT NULL,              -- manual | timeout | cancellation
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Structured lifecycle for one class occurrence. schedule_id identifies the
-- class; booking_id identifies one registration cycle when Arbox supplies it.
-- Unlike training_outcomes, these rows are append-only: rebooking changes the
-- current state without erasing the cancellation that preceded it.
CREATE TABLE IF NOT EXISTS training_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id   INTEGER NOT NULL,
    box_id        INTEGER,
    booking_id    INTEGER,
    membership_user_id INTEGER,
    event_type    TEXT NOT NULL,
    source        TEXT NOT NULL,
    occurred_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    date          TEXT,
    start_time    TEXT,
    end_time      TEXT,
    category_name TEXT,
    coach_name    TEXT,
    reason_code   TEXT,
    reason_text   TEXT,
    counts_entry  INTEGER,
    deadline_at   TEXT
);
CREATE INDEX IF NOT EXISTS training_events_schedule
    ON training_events(schedule_id, occurred_at, id);

-- Optional, personal post-class journal.  It snapshots the class identity so
-- notes and trends survive normal schedule retention and studio edits.
CREATE TABLE IF NOT EXISTS workout_journals (
    schedule_id     INTEGER PRIMARY KEY,
    box_id          INTEGER,
    date            TEXT,
    start_time      TEXT,
    end_time        TEXT,
    category_name   TEXT,
    coach_name      TEXT,
    coach_feedback  TEXT,                    -- positive | neutral | negative | not_applicable
    class_feedback  TEXT,                    -- positive | neutral | negative | not_applicable
    notes           TEXT,
    prompted_at     TEXT,
    dismissed_at    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS workout_journals_date
    ON workout_journals(box_id, date DESC);

CREATE TABLE IF NOT EXISTS workout_exercises (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id     INTEGER NOT NULL,
    box_id          INTEGER,
    position        INTEGER NOT NULL DEFAULT 0,
    exercise_id     TEXT,                    -- stable catalogue/custom id
    name            TEXT NOT NULL,
    metric_type     TEXT NOT NULL DEFAULT 'note',
    sets            INTEGER,
    reps            INTEGER,
    weight          REAL,
    weight_unit     TEXT,
    duration_seconds INTEGER,
    attempts        INTEGER,
    distance        REAL,
    distance_unit   TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS workout_exercises_session
    ON workout_exercises(schedule_id, position, id);
CREATE INDEX IF NOT EXISTS workout_exercises_name
    ON workout_exercises(box_id, name, created_at DESC);
CREATE TABLE IF NOT EXISTS custom_exercises (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id      INTEGER,
    name        TEXT NOT NULL,
    metric_type TEXT NOT NULL DEFAULT 'note',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(box_id, name)
);

CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    box_id      INTEGER,                      -- studio scope for this automation
    name        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    coaches     TEXT NOT NULL DEFAULT '[]',   -- JSON list of coach names ([] = any)
    categories  TEXT NOT NULL DEFAULT '[]',   -- JSON list of category names ([] = any)
    weekdays    TEXT NOT NULL DEFAULT '[]',   -- JSON list of ints 0=Mon..6=Sun ([] = any)
    time_from   TEXT,                          -- HH:MM or NULL
    time_to     TEXT,
    mode        TEXT NOT NULL DEFAULT 'notify' -- notify | autobook
);

CREATE TABLE IF NOT EXISTS pending_prompts (
    callback_id TEXT PRIMARY KEY,             -- opaque id embedded in the button
    schedule_id INTEGER NOT NULL,
    action      TEXT NOT NULL,                -- book | standby
    dry_run     INTEGER NOT NULL DEFAULT 0,   -- end-to-end test: never books
    batch_id    TEXT,
    vacation_ref TEXT,                        -- JSON snapshot of the vacation
    payload     TEXT,                         -- generic callback snapshot
                                              -- this prompt is about; set only
                                              -- when schedule_id is NO_SCHEDULE                         -- groups one digest's prompts
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS autobook_done (
    schedule_id INTEGER PRIMARY KEY,          -- attempted, never retry-spam
    result      TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS automation_skips (
    schedule_id INTEGER NOT NULL,
    box_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (schedule_id, box_id)
);

CREATE TABLE IF NOT EXISTS watchlist (
    schedule_id   INTEGER PRIMARY KEY,       -- pin one specific class
    allow_standby INTEGER NOT NULL DEFAULT 1,-- join the waitlist if it is full
    ignore_vacation INTEGER NOT NULL DEFAULT 0,-- deliberate override of a vacation block
    membership_user_id INTEGER,              -- per-class override; NULL = default
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    result        TEXT                       -- NULL = still waiting
);

CREATE TABLE IF NOT EXISTS vacations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date_from      TEXT NOT NULL,            -- YYYY-MM-DD inclusive
    date_to        TEXT NOT NULL,            -- YYYY-MM-DD inclusive
    block_notify   INTEGER NOT NULL DEFAULT 1,
    block_autobook INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Studio announcements. The feed only ever carries the recent ones, so
-- keeping a copy is the only way "what did they say in March?" survives.
CREATE TABLE IF NOT EXISTS box_messages (
    id         INTEGER PRIMARY KEY,
    box_id     INTEGER,
    subject    TEXT,
    message    TEXT,
    created_at TEXT,
    seen_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- What the system did, and what it failed to do. A table rather than a log
-- file because docker logs die with the container on every deploy, and a
-- booking that silently never happened is exactly the thing you go looking
-- for days later. It is also the only place a failed *notification* can be
-- reported: when the channel is down, the failure cannot be notified.
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    level       TEXT NOT NULL,            -- info | warn | error
    source      TEXT NOT NULL,            -- watchlist | autobook | booking | ...
    message     TEXT NOT NULL,
    detail      TEXT,                     -- one line of context, shown under it
    schedule_id INTEGER,
    tag         TEXT                      -- which instance within the source,
                                          -- e.g. the channel that failed
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts DESC);
-- the channel-status probe (source+tag, newest 50) runs twice per log view;
-- without this it walks the whole table to completion on a healthy install,
-- where there is nothing to find.
CREATE INDEX IF NOT EXISTS events_source_tag ON events (source, tag, id DESC);

-- How many times a booking attempt failed for a reason worth retrying.
-- Kept out of watchlist.result / autobook_done because a value in either of
-- those *is* the "stop trying" signal: the counter needs somewhere to live
-- that does not end the retry it is counting.
CREATE TABLE IF NOT EXISTS retry_counts (
    schedule_id INTEGER NOT NULL,
    kind        TEXT NOT NULL,          -- watchlist | autobook
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (schedule_id, kind)
);
"""

EVENT_LEVELS = ("info", "warn", "error")
EVENT_SOURCES = ("watchlist", "autobook", "booking", "sync", "notify",
                 "studio", "quota", "vacation", "system")

# pending_prompts.schedule_id is NOT NULL and SQLite cannot relax that without
# rebuilding the table. A prompt that is not about a class carries this
# sentinel; get_session(0) returns None, so every existing reader degrades
# safely instead of pointing at someone else's class.
NO_SCHEDULE = 0
EVENT_DEDUPE_MINUTES = 10

SESSION_COLS = [
    "schedule_id", "box_id", "date", "start_time", "end_time", "coach_id", "coach_name",
    "category_id", "category_name", "category_color", "category_bio",
    "series_id", "max_users",
    "free", "registered", "stand_by", "booking_option", "user_booked",
    "membership_user_id",
    "user_in_standby", "stand_by_position", "registration_opens",
    "advance_hours", "block_hours", "cancel_hours", "raw_json",
]


def flatten_session(s: dict, extra_advance_hours: int = 0,
                    box_id: int | None = None) -> dict[str, Any]:
    """Project an Arbox session object onto our row shape.

    Field names verified live: the category dict is `box_categories` (plural),
    end_time is real (classes are 75m/2h, not 1h), coach.last_name can be None.
    """
    coach = s.get("coach") or {}
    cat = s.get("box_categories") or {}
    series = s.get("series") or {}
    # last_name can be None and full_name can embed it as the string "None"
    coach_name = " ".join(
        str(p) for p in (coach.get("first_name"), coach.get("last_name"))
        if p and str(p) != "None"
    ).strip() or (coach.get("full_name") or "").replace("None", "").strip()
    # tri-state on purpose: 0 means "no advance limit" (real value at this
    # studio — Open gym and Functional Strength carry it), >0 means a window,
    # and absent means we simply don't know. Collapsing 0 into "unknown" is
    # what previously made always-open classes invisible to autobook.
    raw_advance = s.get("enable_registration_time")
    advance_hours = None
    if raw_advance is not None:
        try:
            # a membership bonus widens the window, so it adds to the base
            advance_hours = max(0, int(raw_advance)) + max(0, int(extra_advance_hours))
        except (TypeError, ValueError):
            advance_hours = None
    block_hours = None
    raw_block = (s.get("series") or {}).get("block_registration_time")
    if raw_block is not None:
        try:
            block_hours = max(0, int(raw_block))
        except (TypeError, ValueError):
            block_hours = None

    # hours before the start after which cancelling still costs an entry.
    # Per class, not per studio: this box runs 12h on most, 4h on some
    # Functional Strength sessions, and 0 (no penalty) on Open gym.
    cancel_hours = None
    raw_cancel = s.get("disable_cancellation_time")
    if raw_cancel is not None:
        try:
            cancel_hours = max(0, int(raw_cancel))
        except (TypeError, ValueError):
            cancel_hours = None

    reg_opens = None
    if advance_hours:
        # class start minus the registration window, in the studio's own tz
        from datetime import datetime, timedelta
        try:
            start = datetime.fromisoformat(f"{s['date']}T{s['time']}")
            reg_opens = (start - timedelta(hours=advance_hours)).isoformat()
        except (KeyError, ValueError):
            pass
    membership_user_id = s.get("_selected_membership_id")
    if membership_user_id is None and s.get("user_booked") is not None:
        # The schedule payload contains every attendee. Match our booking id;
        # names are neither unique nor stable, while schedule_user_id is.
        for booked in s.get("schedule_user") or []:
            if booked.get("schedule_user_id") == s.get("user_booked"):
                membership_user_id = booked.get("membership_user_fk")
                break
    return {
        "schedule_id": s["id"],
        "box_id": box_id,
        "date": s.get("date"),
        "start_time": s.get("time"),
        "end_time": s.get("end_time"),
        "coach_id": coach.get("id"),
        "coach_name": coach_name or None,
        "category_id": cat.get("id"),
        "category_name": cat.get("name") or series.get("series_name"),
        "category_color": cat.get("category_color"),
        "category_bio": cat.get("bio"),
        "series_id": series.get("id"),
        "max_users": s.get("max_users"),
        "free": s.get("free"),
        "registered": s.get("registered"),
        "stand_by": s.get("stand_by"),
        "booking_option": s.get("booking_option"),
        "user_booked": s.get("user_booked"),
        "membership_user_id": membership_user_id,
        "user_in_standby": s.get("user_in_standby"),
        "stand_by_position": s.get("stand_by_position"),
        "registration_opens": reg_opens,
        "advance_hours": advance_hours,
        "block_hours": block_hours,
        "cancel_hours": cancel_hours,
        "raw_json": json.dumps(
            {k: s.get(k) for k in ("id", "date", "time", "end_time",
                                   "disable_cancellation_time",
                                   "enable_registration_time", "status")},
            ensure_ascii=False,
        ),
    }


class Store:
    def __init__(self, db_path: str) -> None:
        # set by the app once the notifier exists: pushes warn/error events to
        # whichever channels asked for them
        self.on_event = None
        self._path = db_path
        self._db: aiosqlite.Connection | None = None
        # The event loop only holds a weak reference to a bare create_task, so
        # a push could be collected mid-flight and the notification the events
        # table exists to guarantee would vanish without a trace.
        self._push_tasks: set[asyncio.Task] = set()
        self.active_box_id: int | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        # additive migrations for DBs created before a column existed
        for table, col, ddl in (
            ("pending_prompts", "dry_run",
             "ALTER TABLE pending_prompts ADD COLUMN dry_run INTEGER NOT NULL DEFAULT 0"),
            ("pending_prompts", "batch_id",
             "ALTER TABLE pending_prompts ADD COLUMN batch_id TEXT"),
            ("sessions", "category_bio",
             "ALTER TABLE sessions ADD COLUMN category_bio TEXT"),
            ("sessions", "advance_hours",
             "ALTER TABLE sessions ADD COLUMN advance_hours INTEGER"),
            ("sessions", "block_hours",
             "ALTER TABLE sessions ADD COLUMN block_hours INTEGER"),
            ("watchlist", "ignore_vacation",
             "ALTER TABLE watchlist ADD COLUMN ignore_vacation INTEGER NOT NULL DEFAULT 0"),
            ("watchlist", "membership_user_id",
             "ALTER TABLE watchlist ADD COLUMN membership_user_id INTEGER"),
            ("events", "tag", "ALTER TABLE events ADD COLUMN tag TEXT"),
            ("pending_prompts", "vacation_ref",
             "ALTER TABLE pending_prompts ADD COLUMN vacation_ref TEXT"),
            ("pending_prompts", "payload",
             "ALTER TABLE pending_prompts ADD COLUMN payload TEXT"),
            ("sessions", "cancel_hours",
             "ALTER TABLE sessions ADD COLUMN cancel_hours INTEGER"),
            ("sessions", "membership_user_id",
             "ALTER TABLE sessions ADD COLUMN membership_user_id INTEGER"),
            ("sessions", "box_id",
             "ALTER TABLE sessions ADD COLUMN box_id INTEGER"),
            ("rules", "box_id",
             "ALTER TABLE rules ADD COLUMN box_id INTEGER"),
            ("box_messages", "box_id",
             "ALTER TABLE box_messages ADD COLUMN box_id INTEGER"),
            ("training_outcomes", "box_id",
             "ALTER TABLE training_outcomes ADD COLUMN box_id INTEGER"),
            ("training_events", "box_id",
             "ALTER TABLE training_events ADD COLUMN box_id INTEGER"),
            ("training_events", "membership_user_id",
             "ALTER TABLE training_events ADD COLUMN membership_user_id INTEGER"),
            # when this class first entered the DB, as opposed to updated_at,
            # which every sync rewrites. The autobook catch-up cap needs to
            # tell "we have known about this for days and did nothing" from
            # "the studio published it after its window had already opened".
            ("sessions", "first_seen",
             "ALTER TABLE sessions ADD COLUMN first_seen TEXT"),
            # same question for rules: a rule created today must be allowed to
            # act on windows that opened before it existed.
            ("rules", "created_at",
             "ALTER TABLE rules ADD COLUMN created_at TEXT"),
            ("training_outcomes", "date",
             "ALTER TABLE training_outcomes ADD COLUMN date TEXT"),
            ("training_outcomes", "start_time",
             "ALTER TABLE training_outcomes ADD COLUMN start_time TEXT"),
            ("training_outcomes", "end_time",
             "ALTER TABLE training_outcomes ADD COLUMN end_time TEXT"),
            ("training_outcomes", "category_name",
             "ALTER TABLE training_outcomes ADD COLUMN category_name TEXT"),
            ("training_outcomes", "coach_name",
             "ALTER TABLE training_outcomes ADD COLUMN coach_name TEXT"),
            ("workout_exercises", "exercise_id",
             "ALTER TABLE workout_exercises ADD COLUMN exercise_id TEXT"),
        ):
            cur = await self._db.execute(f"PRAGMA table_info({table})")
            cols = {r[1] for r in await cur.fetchall()}
            if col not in cols:
                await self._db.execute(ddl)
                if (table, col) == ("rules", "created_at"):
                    # Rules that predate this column are "old": on the upgrade
                    # tick they must not suddenly sweep up every already-open
                    # window they never acted on.
                    await self._db.execute(
                        "UPDATE rules SET created_at = '2000-01-01 00:00:00' "
                        "WHERE created_at IS NULL")
        cur = await self._db.execute("SELECT value FROM meta WHERE key='identity'")
        identity_row = await cur.fetchone()
        if identity_row:
            try:
                self.active_box_id = json.loads(identity_row[0]).get("box_id")
            except (TypeError, json.JSONDecodeError):
                pass
        if self.active_box_id:
            await self._db.execute(
                "UPDATE sessions SET box_id=? WHERE box_id IS NULL",
                (self.active_box_id,))
            await self._db.execute(
                "UPDATE rules SET box_id=? WHERE box_id IS NULL",
                (self.active_box_id,))
            await self._db.execute(
                "UPDATE box_messages SET box_id=? WHERE box_id IS NULL",
                (self.active_box_id,))
            await self._db.execute(
                "UPDATE training_outcomes SET box_id=? WHERE box_id IS NULL",
                (self.active_box_id,))
            await self._db.execute(
                "UPDATE training_events SET box_id=? WHERE box_id IS NULL",
                (self.active_box_id,))
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store not opened"
        return self._db

    # ------------------------------------------------------------- sessions

    async def upsert_sessions(
        self, sessions: list[dict], extra_advance_hours: int = 0,
        box_id: int | None = None,
    ) -> None:
        box_id = box_id or self.active_box_id
        rows = [flatten_session(s, extra_advance_hours, box_id) for s in sessions]
        placeholders = ", ".join(f":{c}" for c in SESSION_COLS)
        updates = ", ".join(
            ("membership_user_id=COALESCE(excluded.membership_user_id,"
             "sessions.membership_user_id)" if c == "membership_user_id"
             else f"{c}=excluded.{c}")
            for c in SESSION_COLS if c != "schedule_id"
        )
        # first_seen is set here and never in the UPDATE arm: it records when
        # we learned of the class, which is what tells a backlog from a class
        # the studio published late.
        await self.db.executemany(
            f"INSERT INTO sessions ({', '.join(SESSION_COLS)}, first_seen) "
            f"VALUES ({placeholders}, datetime('now', 'localtime')) "
            f"ON CONFLICT(schedule_id) DO UPDATE SET {updates}, "
            f"updated_at=datetime('now')",
            rows,
        )
        await self.db.commit()

    async def prune_sessions(
        self, attended_days: int, past_days: int, future_days: int
    ) -> dict[str, int]:
        """Retention-aware prune — the ONLY place session rows are deleted by
        age. Attended history keeps longest; other past classes only need to
        cover recent memory; the future bound caps how long a deliberate
        far-ahead fetch lingers after its purpose passed."""
        today = date.today()
        attended_cutoff = (today - timedelta(days=attended_days)).isoformat()
        past_cutoff = (today - timedelta(days=past_days)).isoformat()
        future_cutoff = (today + timedelta(days=future_days)).isoformat()
        t = today.isoformat()
        counts = {}
        cur = await self.db.execute(
            "DELETE FROM sessions WHERE date < ? "
            "AND (user_booked IS NOT NULL OR user_in_standby IS NOT NULL "
            "OR schedule_id IN (SELECT schedule_id FROM training_outcomes)) "
            "AND date < ?", (t, attended_cutoff))
        counts["attended"] = cur.rowcount
        cur = await self.db.execute(
            "DELETE FROM sessions WHERE date < ? "
            "AND user_booked IS NULL AND user_in_standby IS NULL "
            "AND schedule_id NOT IN (SELECT schedule_id FROM training_outcomes) "
            "AND date < ?", (t, past_cutoff))
        counts["past"] = cur.rowcount
        # Never evict a class the user is committed to. Deleting the row here
        # used to cascade: prune_bookkeeping then removed the orphaned pin, so
        # a class pinned past the horizon lost both its data and the pin, with
        # nothing said. Booked and standby rows stay for the same reason —
        # they are what "my classes" and the HA sensor read.
        cur = await self.db.execute(
            "DELETE FROM sessions WHERE date > ? "
            "AND user_booked IS NULL AND user_in_standby IS NULL "
            "AND schedule_id NOT IN "
            "(SELECT schedule_id FROM watchlist WHERE result IS NULL)",
            (future_cutoff,))
        counts["future"] = cur.rowcount
        # Outcome snapshots can outlive the schedule row, but follow the same
        # long history horizon rather than accumulating forever.
        await self.db.execute(
            "DELETE FROM training_outcomes WHERE date IS NOT NULL AND date < ?",
            (attended_cutoff,))
        await self.db.execute(
            "DELETE FROM training_events WHERE date IS NOT NULL AND date < ?",
            (attended_cutoff,))
        await self.db.commit()
        return counts

    async def delete_sessions_in_range_not_in(
        self, start: str, end: str, keep_ids: list[int]
    ) -> int:
        """Remove sessions the studio deleted from a range we just resynced.

        An empty keep_ids means the fresh pull returned nothing for the range
        (e.g. a holiday wiped the day) — everything local in it is stale.
        NOT IN (NULL) would match no rows, so that case gets its own query.
        """
        if keep_ids:
            marks = ",".join("?" for _ in keep_ids)
            cur = await self.db.execute(
                f"DELETE FROM sessions WHERE date >= ? AND date <= ? "
                f"AND (? IS NULL OR box_id=?) AND schedule_id NOT IN ({marks})",
                (start, end, self.active_box_id, self.active_box_id, *keep_ids),
            )
        else:
            cur = await self.db.execute(
                "DELETE FROM sessions WHERE date >= ? AND date <= ? "
                "AND (? IS NULL OR box_id=?)",
                (start, end, self.active_box_id, self.active_box_id),
            )
        await self.db.commit()
        return cur.rowcount

    async def get_sessions(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        coach: str | None = None,
        category: str | None = None,
        mine: bool = False,
        watched_as_mine: bool = False,
    ) -> list[dict]:
        # explicit column list: raw_json is only wanted by get_session, and
        # this query feeds every hot read path (API, HA poll, scheduler ticks)
        cols = ", ".join(f"s.{c}" for c in SESSION_COLS if c != "raw_json")
        # the pin travels with the row: "מתוזמן" is a status the class has, not
        # a separate list the caller has to fetch and cross-reference
        # first_seen rides along: the autobook catch-up cap reads it to tell a
        # class we have known about for days from one the studio published
        # after its own window had already opened.
        q = (f"SELECT {cols}, s.updated_at, s.first_seen, "
             "(w.schedule_id IS NOT NULL) AS watched "
             "FROM sessions s "
             "LEFT JOIN watchlist w "
             "  ON w.schedule_id = s.schedule_id AND w.result IS NULL "
             "WHERE 1=1")
        args: list[Any] = []
        if self.active_box_id is not None:
            q += " AND s.box_id = ?"; args.append(self.active_box_id)
        if date_from:
            q += " AND s.date >= ?"; args.append(date_from)
        if date_to:
            q += " AND s.date <= ?"; args.append(date_to)
        if coach:
            q += " AND s.coach_name = ?"; args.append(coach)
        if category:
            q += " AND s.category_name = ?"; args.append(category)
        if mine:
            q += " AND (s.user_booked IS NOT NULL OR s.user_in_standby IS NOT NULL"
            # a pin is a commitment too, and the screen listing my classes is
            # where I would go to change my mind about it — but it is not a
            # booking, so callers that mean "classes I am attending" (the HA
            # next-class sensor, the pre-class reminder) must not see it
            q += " OR w.schedule_id IS NOT NULL)" if watched_as_mine else ")"
        q += " ORDER BY s.date, s.start_time"
        cur = await self.db.execute(q, args)
        rows = []
        for r in await cur.fetchall():
            d = dict(r)
            d["watched"] = bool(d["watched"])
            rows.append(d)
        return rows

    async def get_session(self, schedule_id: int) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM sessions WHERE schedule_id = ? "
            "AND (? IS NULL OR box_id=?)",
            (schedule_id, self.active_box_id, self.active_box_id)
        )
        r = await cur.fetchone()
        return dict(r) if r else None

    async def my_sessions(self, include_watched: bool = False,
                          date_from: str | None = None) -> list[dict]:
        return await self.get_sessions(mine=True, watched_as_mine=include_watched,
                                       date_from=date_from)

    async def insert_attended_sessions(
        self, sessions: list[dict], extra_advance_hours: int = 0,
        box_id: int | None = None,
    ) -> int:
        """History import: insert MY past classes, never touching what exists.

        INSERT OR IGNORE by construction — a re-pull of a past range must not
        be able to erase the attendance marker retention exists to keep
        (upsert would overwrite user_booked with whatever came back).
        """
        today = date.today().isoformat()
        rows = []
        for s in sessions:
            if s.get("user_booked") is None and s.get("user_in_standby") is None:
                continue
            row = flatten_session(s, extra_advance_hours,
                                  box_id or self.active_box_id)
            # the frontend's dimming and the booking 409 both key off this
            # string, and upstream marking on old rows is unverified
            if row["date"] and row["date"] < today:
                row["booking_option"] = "past"
            rows.append(row)
        if not rows:
            return 0
        placeholders = ", ".join(f":{c}" for c in SESSION_COLS)
        cur = await self.db.executemany(
            f"INSERT OR IGNORE INTO sessions "
            f"({', '.join(SESSION_COLS)}, first_seen) "
            f"VALUES ({placeholders}, datetime('now', 'localtime'))",
            rows,
        )
        # the statement's own count, not the connection's: total_changes moves
        # whenever any other task writes, and this number is shown to the user
        inserted = cur.rowcount
        await self.db.commit()
        return max(0, inserted)

    async def save_box_messages(self, msgs: list[dict]) -> int:
        """Archive announcements, keeping the first time we saw each.

        Upsert rather than INSERT OR IGNORE: the studio edits announcements
        ("closed Friday" -> "closed Thursday") and re-serves them under the
        same id, and ignoring the row froze the wrong text forever. seen_at is
        left out of the UPDATE, so the original timestamp still survives.
        """
        rows = [
            {"id": m.get("id"), "subject": m.get("subject"),
             "message": m.get("message"), "created_at": m.get("created_at"),
             "box_id": self.active_box_id}
            for m in msgs if m.get("id") is not None
        ]
        if not rows:
            return 0
        cur = await self.db.execute(
            "SELECT id FROM box_messages WHERE id IN (%s)"
            % ",".join("?" for _ in rows), [r["id"] for r in rows])
        known = {r[0] for r in await cur.fetchall()}
        await self.db.executemany(
            "INSERT INTO box_messages (id, box_id, subject, message, created_at) "
            "VALUES (:id, :box_id, :subject, :message, :created_at) "
            "ON CONFLICT(id) DO UPDATE SET subject=excluded.subject, "
            "message=excluded.message, created_at=excluded.created_at, "
            "box_id=excluded.box_id", rows)
        await self.db.commit()
        # counted from the ids themselves: total_changes is connection-global,
        # so a concurrent write from any other task inflated the old figure
        return sum(1 for r in rows if r["id"] not in known)

    async def list_box_messages(self, limit: int = 300) -> list[dict]:
        cur = await self.db.execute(
            "SELECT id, subject, message, created_at FROM box_messages "
            "WHERE (? IS NULL OR box_id=?) "
            "ORDER BY COALESCE(created_at, seen_at) DESC LIMIT ?",
            (self.active_box_id, self.active_box_id, limit))
        return [dict(r) for r in await cur.fetchall()]

    async def get_sessions_by_ids(self, ids: list[int]) -> dict[int, dict]:
        """Several sessions in one query, keyed by id — the watchlist view
        used to run one round-trip per pin."""
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        cur = await self.db.execute(
            f"SELECT * FROM sessions WHERE schedule_id IN ({marks}) "
            "AND (? IS NULL OR box_id=?)",
            (*ids, self.active_box_id, self.active_box_id))
        return {r["schedule_id"]: dict(r) for r in await cur.fetchall()}

    async def booked_count_between(self, start: str, end: str) -> int:
        """How many bookings sit in this range — sessions, not days.

        Arbox's own endpoint answers in dates, so two classes on one day count
        once there; the quota is spent per class, and the digest actively
        encourages a second class on a day you already train.
        """
        cur = await self.db.execute(
            "SELECT COUNT(*) FROM sessions WHERE date >= ? AND date <= ? "
            "AND user_booked IS NOT NULL AND (? IS NULL OR box_id=?)",
            (start, end, self.active_box_id, self.active_box_id))
        r = await cur.fetchone()
        return int(r[0]) if r else 0

    async def quota_commitments(
        self, start: str, end: str, now_stamp: str,
    ) -> list[dict]:
        """Classify commitments as consumed, reserved or standby.

        A future booking is deliberately not consumed. Once its scheduled
        start passes it consumes an entry regardless of the attendance answer;
        safe cancellations never do, while late cancellations do immediately.
        """
        cur = await self.db.execute(
            "SELECT s.schedule_id,s.date,s.start_time,s.user_booked,"
            "s.user_in_standby,s.membership_user_id,o.status "
            "FROM sessions s LEFT JOIN training_outcomes o "
            "ON o.schedule_id=s.schedule_id WHERE s.date>=? AND s.date<=? "
            "AND (? IS NULL OR s.box_id=?) "
            "AND (s.user_booked IS NOT NULL OR s.user_in_standby IS NOT NULL "
            "OR o.status IS NOT NULL)",
            (start, end, self.active_box_id, self.active_box_id))
        out = []
        for raw in await cur.fetchall():
            row = dict(raw)
            status = row.get("status") or ""
            if status == "cancelled_late":
                row["commitment"] = "used"
            elif status in ("attended", "missed"):
                row["commitment"] = "used"
            elif status.startswith("cancelled") or status == "standby_cancelled":
                continue
            elif row.get("user_in_standby") is not None and row.get("user_booked") is None:
                row["commitment"] = "standby"
            elif row.get("user_booked") is not None:
                stamp = f"{row['date']} {str(row.get('start_time') or '')[:5]}"
                row["commitment"] = "used" if stamp <= now_stamp else "reserved"
            else:
                continue
            out.append(row)
        return out

    async def automation_skip_ids(self) -> set[int]:
        cur = await self.db.execute(
            "SELECT k.schedule_id FROM automation_skips k "
            "JOIN sessions s ON s.schedule_id=k.schedule_id "
            "AND COALESCE(s.box_id,0)=k.box_id "
            "WHERE (? IS NULL OR s.box_id=?)",
            (self.active_box_id, self.active_box_id),
        )
        return {int(r[0]) for r in await cur.fetchall()}

    async def set_automation_skip(self, schedule_id: int, skipped: bool) -> None:
        session = await self.get_session(schedule_id)
        if not session:
            raise ValueError("unknown session")
        args = (schedule_id, session.get("box_id") or 0)
        if skipped:
            await self.db.execute(
                "INSERT OR IGNORE INTO automation_skips(schedule_id,box_id) VALUES (?,?)", args)
        else:
            await self.db.execute(
                "DELETE FROM automation_skips WHERE schedule_id=? AND box_id=?", args)
        await self.db.commit()

    async def planned_sessions(self, start: str, end: str) -> list[dict]:
        """Pending one-class schedules that may consume future capacity."""
        cur = await self.db.execute(
            "SELECT s.schedule_id,s.date,s.start_time,s.end_time,"
            "s.category_name,s.coach_name,s.registration_opens,"
            "w.membership_user_id,w.ignore_vacation,w.created_at "
            "FROM watchlist w JOIN sessions s ON s.schedule_id=w.schedule_id "
            "WHERE w.result IS NULL AND s.date>=? AND s.date<=? "
            "AND (? IS NULL OR s.box_id=?) "
            "AND s.user_booked IS NULL AND s.user_in_standby IS NULL "
            "ORDER BY COALESCE(s.registration_opens, "
            "s.date || 'T' || COALESCE(s.start_time,'23:59')), w.created_at, "
            "s.schedule_id",
            (start, end, self.active_box_id, self.active_box_id),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def training_history(self, now: Any | None = None) -> list[dict]:
        """Completed commitments and cancellations, newest first.

        A booked session enters the timeline at its end time, not at midnight.
        Old rows that predate explicit attendance tracking retain the former,
        sensible default (booked means attended) while new unanswered rows are
        exposed as pending until the midnight timeout records the outcome.
        """
        from datetime import datetime

        now = now or datetime.now()
        cur = await self.db.execute(
            "SELECT * FROM ("
            "SELECT o.schedule_id, COALESCE(s.date,o.date) AS date, "
            "COALESCE(s.start_time,o.start_time) AS start_time, "
            "COALESCE(s.end_time,o.end_time) AS end_time, "
            "COALESCE(s.category_name,o.category_name) AS category_name, "
            "COALESCE(s.coach_name,o.coach_name) AS coach_name, "
            "s.user_booked, s.user_in_standby, o.status, o.reason_code, "
            "o.reason_text, o.source, o.updated_at "
            "FROM training_outcomes o LEFT JOIN sessions s "
            "ON s.schedule_id=o.schedule_id "
            "WHERE (? IS NULL OR o.box_id=?) "
            "UNION ALL "
            "SELECT s.schedule_id,s.date,s.start_time,s.end_time,s.category_name,"
            "s.coach_name,s.user_booked,s.user_in_standby,NULL,NULL,NULL,NULL,NULL "
            "FROM sessions s LEFT JOIN training_outcomes o "
            "ON o.schedule_id=s.schedule_id WHERE o.schedule_id IS NULL AND "
            "(? IS NULL OR s.box_id=?) AND "
            "(s.user_booked IS NOT NULL OR s.user_in_standby IS NOT NULL)"
            ") ORDER BY date DESC, start_time DESC",
            (self.active_box_id, self.active_box_id,
             self.active_box_id, self.active_box_id),
        )
        tracking_since = await self.get_meta("attendance_tracking_since")
        rows = []
        for raw in await cur.fetchall():
            row = dict(raw)
            status = row.get("status")
            if status and status.startswith("cancelled"):
                rows.append(row)
                continue
            if status == "standby_cancelled":
                rows.append(row)
                continue
            try:
                ended = datetime.fromisoformat(
                    f"{row['date']}T{row.get('end_time') or row['start_time']}"
                )
            except (TypeError, ValueError):
                continue
            if ended > now:
                continue
            if status is None:
                if row.get("user_in_standby") is not None and row.get("user_booked") is None:
                    row["status"] = "standby"
                elif not tracking_since or row["date"] < str(tracking_since):
                    row["status"] = "attended"
                    row["source"] = "legacy"
                else:
                    row["status"] = "pending"
            rows.append(row)
        return rows

    async def set_training_outcome(
        self, schedule_id: int, status: str, source: str,
        reason_code: str | None = None, reason_text: str | None = None,
        *, booking_id: int | None = None, counts_entry: bool | None = None,
        deadline_at: str | None = None,
    ) -> None:
        session = await self.get_session(schedule_id)
        snapshot = session or {}
        prior = await self.get_training_outcome(schedule_id)
        await self.db.execute(
            "INSERT INTO training_outcomes "
            "(schedule_id,box_id,date,start_time,end_time,category_name,coach_name,"
            "status,reason_code,reason_text,source) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(schedule_id) DO UPDATE SET status=excluded.status, "
            "box_id=COALESCE(excluded.box_id,box_id), "
            "reason_code=excluded.reason_code, reason_text=excluded.reason_text, "
            "source=excluded.source, date=COALESCE(excluded.date,date), "
            "start_time=COALESCE(excluded.start_time,start_time), "
            "end_time=COALESCE(excluded.end_time,end_time), "
            "category_name=COALESCE(excluded.category_name,category_name), "
            "coach_name=COALESCE(excluded.coach_name,coach_name), "
            "updated_at=datetime('now','localtime')",
            (schedule_id, snapshot.get("box_id") or self.active_box_id,
             snapshot.get("date"), snapshot.get("start_time"),
             snapshot.get("end_time"), snapshot.get("category_name"),
             snapshot.get("coach_name"), status, reason_code, reason_text, source),
        )
        changed = not prior or any((
            prior.get("status") != status,
            prior.get("reason_code") != reason_code,
            prior.get("reason_text") != reason_text,
        ))
        if changed:
            event_type = ("reason_updated" if prior
                          and prior.get("status") == status else status)
            await self._insert_training_event(
                schedule_id, event_type, source, booking_id=booking_id,
                reason_code=reason_code, reason_text=reason_text,
                counts_entry=counts_entry, deadline_at=deadline_at,
                snapshot=snapshot,
            )
        await self.db.commit()

    # ------------------------------------------------------ workout journal

    async def workout_journal(self, schedule_id: int) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM workout_journals WHERE schedule_id=? AND "
            "(? IS NULL OR box_id=?)",
            (schedule_id, self.active_box_id, self.active_box_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        out = dict(row)
        cur = await self.db.execute(
            "SELECT id,position,exercise_id,name,metric_type,sets,reps,weight,weight_unit,"
            "duration_seconds,attempts,distance,distance_unit,notes "
            "FROM workout_exercises WHERE schedule_id=? ORDER BY position,id",
            (schedule_id,),
        )
        out["exercises"] = [dict(x) for x in await cur.fetchall()]
        return out

    async def workout_journals(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM workout_journals WHERE (? IS NULL OR box_id=?) "
            "ORDER BY date DESC,start_time DESC",
            (self.active_box_id, self.active_box_id),
        )
        rows = [dict(x) for x in await cur.fetchall()]
        if not rows:
            return []
        ids = [int(x["schedule_id"]) for x in rows]
        marks = ",".join("?" for _ in ids)
        cur = await self.db.execute(
            f"SELECT id,schedule_id,position,exercise_id,name,metric_type,sets,reps,weight,"
            f"weight_unit,duration_seconds,attempts,distance,distance_unit,notes "
            f"FROM workout_exercises WHERE schedule_id IN ({marks}) "
            "ORDER BY schedule_id,position,id", ids,
        )
        by_session: dict[int, list[dict]] = {sid: [] for sid in ids}
        for raw in await cur.fetchall():
            item = dict(raw)
            by_session[int(item.pop("schedule_id"))].append(item)
        for row in rows:
            row["exercises"] = by_session.get(int(row["schedule_id"]), [])
        return rows

    async def save_workout_journal(
        self, schedule_id: int, *, coach_feedback: str | None = None,
        class_feedback: str | None = None, notes: str | None = None,
        exercises: list[dict] | None = None,
    ) -> dict:
        session = await self.get_session(schedule_id) or {}
        prior = await self.workout_journal(schedule_id) or {}
        snapshot = {k: session.get(k) or prior.get(k) for k in (
            "box_id", "date", "start_time", "end_time", "category_name",
            "coach_name",
        )}
        await self.db.execute(
            "INSERT INTO workout_journals "
            "(schedule_id,box_id,date,start_time,end_time,category_name,coach_name,"
            "coach_feedback,class_feedback,notes) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(schedule_id) DO UPDATE SET "
            "box_id=COALESCE(excluded.box_id,box_id),"
            "date=COALESCE(excluded.date,date),"
            "start_time=COALESCE(excluded.start_time,start_time),"
            "end_time=COALESCE(excluded.end_time,end_time),"
            "category_name=COALESCE(excluded.category_name,category_name),"
            "coach_name=COALESCE(excluded.coach_name,coach_name),"
            "coach_feedback=excluded.coach_feedback,"
            "class_feedback=excluded.class_feedback,notes=excluded.notes,"
            "dismissed_at=NULL,updated_at=datetime('now','localtime')",
            (schedule_id, snapshot["box_id"] or self.active_box_id,
             snapshot["date"], snapshot["start_time"], snapshot["end_time"],
             snapshot["category_name"], snapshot["coach_name"],
             coach_feedback, class_feedback, notes),
        )
        if exercises is not None:
            await self.db.execute(
                "DELETE FROM workout_exercises WHERE schedule_id=?", (schedule_id,))
            rows = []
            for position, exercise in enumerate(exercises[:50]):
                name = str(exercise.get("name") or "").strip()[:120]
                if not name:
                    continue
                rows.append((
                    schedule_id, snapshot["box_id"] or self.active_box_id, position,
                    str(exercise.get("exercise_id") or "").strip()[:80] or None,
                    name, str(exercise.get("metric_type") or "note")[:20],
                    exercise.get("sets"), exercise.get("reps"),
                    exercise.get("weight"), exercise.get("weight_unit"),
                    exercise.get("duration_seconds"), exercise.get("attempts"),
                    exercise.get("distance"), exercise.get("distance_unit"),
                    str(exercise.get("notes") or "").strip()[:500] or None,
                ))
            if rows:
                await self.db.executemany(
                    "INSERT INTO workout_exercises "
                    "(schedule_id,box_id,position,exercise_id,name,metric_type,sets,reps,weight,"
                    "weight_unit,duration_seconds,attempts,distance,distance_unit,notes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
                )
        await self.db.commit()
        return (await self.workout_journal(schedule_id)) or {}

    async def mark_journal_prompted(self, schedule_id: int) -> None:
        session = await self.get_session(schedule_id) or {}
        await self.db.execute(
            "INSERT INTO workout_journals "
            "(schedule_id,box_id,date,start_time,end_time,category_name,coach_name,prompted_at) "
            "VALUES (?,?,?,?,?,?,?,datetime('now','localtime')) "
            "ON CONFLICT(schedule_id) DO UPDATE SET "
            "prompted_at=COALESCE(prompted_at,datetime('now','localtime'))",
            (schedule_id, session.get("box_id") or self.active_box_id,
             session.get("date"), session.get("start_time"), session.get("end_time"),
             session.get("category_name"), session.get("coach_name")),
        )
        await self.db.commit()

    async def dismiss_workout_journal(self, schedule_id: int) -> None:
        await self.mark_journal_prompted(schedule_id)
        await self.db.execute(
            "UPDATE workout_journals SET dismissed_at=datetime('now','localtime'),"
            "updated_at=datetime('now','localtime') WHERE schedule_id=?",
            (schedule_id,),
        )
        await self.db.commit()

    async def custom_exercises(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT id,name,metric_type FROM custom_exercises "
            "WHERE (? IS NULL OR box_id=?) ORDER BY name COLLATE NOCASE",
            (self.active_box_id, self.active_box_id),
        )
        return [dict(x) for x in await cur.fetchall()]

    async def add_custom_exercise(self, name: str, metric_type: str) -> dict:
        cur = await self.db.execute(
            "SELECT id FROM custom_exercises WHERE box_id IS ? AND name=?",
            (self.active_box_id, name))
        existing = await cur.fetchone()
        if existing:
            await self.db.execute(
                "UPDATE custom_exercises SET metric_type=?,"
                "updated_at=datetime('now','localtime') WHERE id=?",
                (metric_type, existing["id"]),
            )
        else:
            await self.db.execute(
                "INSERT INTO custom_exercises (box_id,name,metric_type) VALUES (?,?,?)",
                (self.active_box_id, name, metric_type),
            )
        await self.db.commit()
        cur = await self.db.execute(
            "SELECT id,name,metric_type FROM custom_exercises WHERE "
            "box_id IS ? AND name=?", (self.active_box_id, name))
        return dict(await cur.fetchone())

    async def delete_custom_exercise(self, exercise_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM custom_exercises WHERE id=? AND (? IS NULL OR box_id=?)",
            (exercise_id, self.active_box_id, self.active_box_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def journal_candidates(self) -> list[dict]:
        """Completed likely-attended classes, including unanswered today."""
        return [row for row in await self.training_history()
                if row.get("status") in ("attended", "pending")]

    async def _insert_training_event(
        self, schedule_id: int, event_type: str, source: str, *,
        booking_id: int | None = None, membership_user_id: int | None = None,
        reason_code: str | None = None,
        reason_text: str | None = None, counts_entry: bool | None = None,
        deadline_at: str | None = None, snapshot: dict | None = None,
    ) -> None:
        snapshot = snapshot if snapshot is not None else (
            await self.get_session(schedule_id) or {})
        await self.db.execute(
            "INSERT INTO training_events "
            "(schedule_id,box_id,booking_id,membership_user_id,event_type,source,date,start_time,end_time,"
            "category_name,coach_name,reason_code,reason_text,counts_entry,deadline_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (schedule_id, snapshot.get("box_id") or self.active_box_id, booking_id,
             membership_user_id or snapshot.get("membership_user_id"), event_type, source,
             snapshot.get("date"), snapshot.get("start_time"),
             snapshot.get("end_time"), snapshot.get("category_name"),
             snapshot.get("coach_name"), reason_code, reason_text,
             None if counts_entry is None else int(counts_entry), deadline_at),
        )

    async def record_booking_success(
        self, schedule_id: int, action: str, source: str,
        membership_user_id: int | None = None,
    ) -> str:
        """Append a booking cycle and make it the current truth.

        A successful rebooking supersedes a cancellation outcome, but the
        append-only event remains available for the class timeline.
        """
        session = await self.get_session(schedule_id) or {}
        prior = await self.get_training_outcome(schedule_id)
        was_cancelled = bool(prior and (
            (prior.get("status") or "").startswith("cancelled")
            or prior.get("status") == "standby_cancelled"
        ))
        if action == "book":
            event_type = "rebooked" if was_cancelled else "booked"
            booking_id = session.get("user_booked")
        else:
            event_type = "standby_rejoined" if was_cancelled else "standby_joined"
            booking_id = session.get("user_in_standby")
        await self._insert_training_event(
            schedule_id, event_type, source, booking_id=booking_id,
            membership_user_id=membership_user_id,
            snapshot=session,
        )
        if was_cancelled:
            await self.db.execute(
                "DELETE FROM training_outcomes WHERE schedule_id=?", (schedule_id,))
        await self.db.commit()
        return event_type

    async def training_events(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM training_events WHERE (? IS NULL OR box_id=?) "
            "ORDER BY occurred_at, id",
            (self.active_box_id, self.active_box_id))
        return [dict(r) for r in await cur.fetchall()]

    async def get_training_outcome(self, schedule_id: int) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM training_outcomes WHERE schedule_id=?", (schedule_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def live_prompt_for(self, schedule_id: int, action: str) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM pending_prompts WHERE schedule_id=? AND action=? "
            "AND answered_at IS NULL LIMIT 1", (schedule_id, action))
        return await cur.fetchone() is not None

    async def expire_prompts(self, schedule_id: int, actions: tuple[str, ...]) -> None:
        marks = ",".join("?" for _ in actions)
        await self.db.execute(
            f"UPDATE pending_prompts SET answered_at=datetime('now') "
            f"WHERE schedule_id=? AND action IN ({marks}) AND answered_at IS NULL",
            (schedule_id, *actions),
        )
        await self.db.commit()

    async def automation_decisions(self) -> list[dict]:
        """What the automation decided NOT to do, and why.

        Pins and autobook attempts whose outcome was anything but a booking —
        vacation skips, blocked categories, expired dates, failures. The
        bookkeeping tables are age-pruned at 30 days, so this is a rolling
        month of decisions. LEFT JOIN: the class row can outlive or predecease
        the decision row, and a missing session must not hide the decision.
        """
        cur = await self.db.execute(
            "SELECT w.schedule_id, w.result, w.created_at AS at, 'pin' AS source, "
            "  s.date, s.start_time, s.end_time, s.category_name, s.coach_name "
            "FROM watchlist w LEFT JOIN sessions s ON s.schedule_id = w.schedule_id "
            "WHERE w.result IS NOT NULL "
            "  AND w.result NOT IN ('booked', 'already booked') "
            "UNION ALL "
            "SELECT a.schedule_id, a.result, a.at, 'rule' AS source, "
            "  s.date, s.start_time, s.end_time, s.category_name, s.coach_name "
            "FROM autobook_done a LEFT JOIN sessions s ON s.schedule_id = a.schedule_id "
            "WHERE a.result IS NOT NULL AND a.result != 'booked' "
            "ORDER BY at DESC"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def attendance_stats(self, months: int = 12) -> dict:
        """Locally-counted history — window-limited by retention, unlike the
        all-time counter Arbox's feed reports."""
        cutoff = (date.today() - timedelta(days=months * 31)).isoformat()
        today = date.today().isoformat()
        # An explicit outcome wins. Legacy booked rows (before tracking was
        # enabled) preserve the old count; missed/cancelled/pending never do.
        tracking_since = await self.get_meta("attendance_tracking_since") or today
        where = ("WHERE s.date >= ? AND s.date <= ? "
                 "AND (? IS NULL OR s.box_id=?) AND "
                 "(o.status='attended' OR (o.status IS NULL AND s.user_booked IS NOT NULL "
                 "AND s.date < ?))")
        cur = await self.db.execute(
            f"SELECT COUNT(*) FROM sessions s LEFT JOIN training_outcomes o "
            f"ON o.schedule_id=s.schedule_id {where}",
            (cutoff, today, self.active_box_id, self.active_box_id, tracking_since))
        total = (await cur.fetchone())[0]
        cur = await self.db.execute(
            f"SELECT s.category_name, COUNT(*) n FROM sessions s "
            f"LEFT JOIN training_outcomes o ON o.schedule_id=s.schedule_id {where} "
            "GROUP BY s.category_name ORDER BY n DESC",
            (cutoff, today, self.active_box_id, self.active_box_id, tracking_since))
        by_category = [{"name": r[0], "count": r[1]} for r in await cur.fetchall()]
        cur = await self.db.execute(
            f"SELECT s.coach_name, COUNT(*) n FROM sessions s "
            f"LEFT JOIN training_outcomes o ON o.schedule_id=s.schedule_id {where} "
            "AND s.coach_name IS NOT NULL "
            "GROUP BY s.coach_name ORDER BY n DESC",
            (cutoff, today, self.active_box_id, self.active_box_id, tracking_since))
        by_coach = [{"name": r[0], "count": r[1]} for r in await cur.fetchall()]
        return {"since": cutoff, "total": total,
                "by_category": by_category, "by_coach": by_coach}

    async def facets(self) -> dict:
        """Distinct coaches/categories for the filter chips."""
        # floored: with a year of history retained, an unfloored DISTINCT
        # would keep coaches who left and retired categories in the chips
        floor = (date.today() - timedelta(days=60)).isoformat()
        cur = await self.db.execute(
            "SELECT DISTINCT coach_name FROM sessions "
            "WHERE coach_name IS NOT NULL AND date >= ? "
            "AND (? IS NULL OR box_id=?) ORDER BY coach_name",
            (floor, self.active_box_id, self.active_box_id),
        )
        coaches = [r[0] for r in await cur.fetchall()]
        cur = await self.db.execute(
            "SELECT DISTINCT category_name, category_color FROM sessions "
            "WHERE category_name IS NOT NULL AND date >= ? "
            "AND (? IS NULL OR box_id=?) ORDER BY category_name",
            (floor, self.active_box_id, self.active_box_id),
        )
        categories = [
            {"name": r[0], "color": r[1]} for r in await cur.fetchall()
        ]
        return {"coaches": coaches, "categories": categories}

    # ---------------------------------------------------------------- rules

    async def list_rules(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM rules WHERE (? IS NULL OR box_id=?) ORDER BY id",
            (self.active_box_id, self.active_box_id))
        rules = []
        for r in await cur.fetchall():
            d = dict(r)
            for k in ("coaches", "categories", "weekdays"):
                d[k] = json.loads(d[k])
            d["enabled"] = bool(d["enabled"])
            rules.append(d)
        return rules

    async def save_rule(self, rule: dict) -> int:
        fields = {
            "box_id": self.active_box_id,
            "name": rule["name"],
            "enabled": int(rule.get("enabled", True)),
            "coaches": json.dumps(rule.get("coaches", []), ensure_ascii=False),
            "categories": json.dumps(rule.get("categories", []), ensure_ascii=False),
            "weekdays": json.dumps(rule.get("weekdays", [])),
            "time_from": rule.get("time_from"),
            "time_to": rule.get("time_to"),
            "mode": rule.get("mode", "notify"),
        }
        if rule.get("id"):
            sets = ", ".join(f"{k}=?" for k in fields)
            await self.db.execute(
                f"UPDATE rules SET {sets} WHERE id=? "
                "AND (? IS NULL OR box_id=?)",
                (*fields.values(), rule["id"],
                 self.active_box_id, self.active_box_id),
            )
            rid = rule["id"]
        else:
            # created_at is what lets autobook tell "this rule existed when
            # the window opened and did nothing" from "this rule is new" —
            # the second must be allowed to act on windows already open.
            cur = await self.db.execute(
                f"INSERT INTO rules ({', '.join(fields)}, created_at) "
                f"VALUES ({', '.join('?' for _ in fields)}, "
                f"datetime('now', 'localtime'))",
                tuple(fields.values()),
            )
            rid = cur.lastrowid
        await self.db.commit()
        return rid

    async def delete_rule(self, rule_id: int) -> None:
        await self.db.execute(
            "DELETE FROM rules WHERE id=? AND (? IS NULL OR box_id=?)",
            (rule_id, self.active_box_id, self.active_box_id))
        await self.db.commit()

    # ------------------------------------------------------ pending prompts

    async def add_prompt(
        self, callback_id: str, schedule_id: int, action: str,
        dry_run: bool = False, batch_id: str | None = None,
        vacation_ref: str | None = None, payload: str | None = None,
    ) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO pending_prompts "
            "(callback_id, schedule_id, action, dry_run, batch_id, vacation_ref, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (callback_id, schedule_id, action, int(dry_run), batch_id,
             vacation_ref, payload),
        )
        await self.db.commit()

    async def answer_batch(self, batch_id: str | None) -> int:
        """Disarm every still-live prompt in a batch.

        Choosing "keep the vacation" has to kill an already-armed delete
        confirmation too, or a stray press on the older message still deletes.
        """
        if not batch_id:
            return 0
        cur = await self.db.execute(
            "UPDATE pending_prompts SET answered_at=datetime('now') "
            "WHERE batch_id=? AND answered_at IS NULL", (batch_id,))
        await self.db.commit()
        return cur.rowcount

    async def peek_prompt(self, callback_id: str) -> dict | None:
        """Look a prompt up WITHOUT consuming it (info buttons must never
        disarm the booking button that shares the row)."""
        cur = await self.db.execute(
            "SELECT * FROM pending_prompts WHERE callback_id=?", (callback_id,)
        )
        r = await cur.fetchone()
        return dict(r) if r else None

    async def batch_prompts(self, batch_id: str) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM pending_prompts WHERE batch_id=?", (batch_id,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def any_answered(self, callback_ids: list[str]) -> bool:
        """Did any of these prompts get a (non-info) answer yet?"""
        if not callback_ids:
            return False
        marks = ",".join("?" for _ in callback_ids)
        cur = await self.db.execute(
            f"SELECT 1 FROM pending_prompts WHERE callback_id IN ({marks}) "
            f"AND answered_at IS NOT NULL LIMIT 1",
            callback_ids,
        )
        return await cur.fetchone() is not None

    async def delete_prompt(self, callback_id: str) -> None:
        """Drop a prompt whose message never went out, so its dead button
        cannot be pressed later from a chat that did receive it."""
        await self.db.execute(
            "DELETE FROM pending_prompts WHERE callback_id=?", (callback_id,))
        await self.db.commit()

    async def restore_prompt(self, callback_id: str) -> None:
        """Re-arm a prompt whose booking attempt failed transiently."""
        await self.db.execute(
            "UPDATE pending_prompts SET answered_at=NULL WHERE callback_id=?",
            (callback_id,),
        )
        await self.db.commit()

    async def take_prompt(self, callback_id: str) -> dict | None:
        """Consume a prompt exactly once.

        Claim first, read second. The same button exists on two channels once
        escalation is on, and SELECT-then-UPDATE across two awaits let both
        presses pass the "unanswered" test and fire two bookings — the second
        of which fails as "already booked" and reads like a bug.
        """
        cur = await self.db.execute(
            "UPDATE pending_prompts SET answered_at=datetime('now') "
            "WHERE callback_id=? AND answered_at IS NULL",
            (callback_id,),
        )
        await self.db.commit()
        if not cur.rowcount:
            return None
        cur = await self.db.execute(
            "SELECT * FROM pending_prompts WHERE callback_id=?", (callback_id,))
        r = await cur.fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------ watchlist

    async def list_watchlist(self, pending_only: bool = False) -> list[dict]:
        q = ("SELECT w.* FROM watchlist w JOIN sessions s "
             "ON s.schedule_id=w.schedule_id WHERE "
             "(? IS NULL OR s.box_id=?)")
        args = [self.active_box_id, self.active_box_id]
        if pending_only:
            q += " AND w.result IS NULL"
        cur = await self.db.execute(q + " ORDER BY w.created_at", args)
        return [dict(r) for r in await cur.fetchall()]

    async def watch(self, schedule_id: int, allow_standby: bool = True,
                    ignore_vacation: bool = False,
                    membership_user_id: int | None = None) -> None:
        await self.db.execute(
            "INSERT INTO watchlist (schedule_id, allow_standby, ignore_vacation, "
            "membership_user_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(schedule_id) DO UPDATE SET "
            "allow_standby=excluded.allow_standby, "
            "ignore_vacation=excluded.ignore_vacation, "
            "membership_user_id=excluded.membership_user_id, result=NULL",
            (schedule_id, int(allow_standby), int(ignore_vacation),
             membership_user_id),
        )
        await self.db.commit()

    async def unwatch(self, schedule_id: int) -> None:
        await self.db.execute("DELETE FROM watchlist WHERE schedule_id=?", (schedule_id,))
        await self.db.commit()

    async def set_watch_membership(
        self, schedule_id: int, membership_user_id: int | None,
    ) -> bool:
        cur = await self.db.execute(
            "UPDATE watchlist SET membership_user_id=? "
            "WHERE schedule_id=? AND result IS NULL",
            (membership_user_id, schedule_id))
        await self.db.commit()
        return bool(cur.rowcount)

    async def mark_watch(self, schedule_id: int, result: str) -> None:
        await self.db.execute(
            "UPDATE watchlist SET result=? WHERE schedule_id=?", (result, schedule_id)
        )
        await self.db.commit()

    # ------------------------------------------------------------ vacations

    async def list_vacations(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM vacations ORDER BY date_from"
        )
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["block_notify"] = bool(d["block_notify"])
            d["block_autobook"] = bool(d["block_autobook"])
            out.append(d)
        return out

    async def add_vacation(
        self, date_from: str, date_to: str,
        block_notify: bool = True, block_autobook: bool = True,
        vac_id: int | None = None,
    ) -> int:
        """Insert, or update in place when vac_id is given (same pattern as
        save_rule — a typo shouldn't force recreating the range)."""
        if vac_id:
            await self.db.execute(
                "UPDATE vacations SET date_from=?, date_to=?, "
                "block_notify=?, block_autobook=? WHERE id=?",
                (date_from, date_to, int(block_notify), int(block_autobook), vac_id),
            )
            await self.db.commit()
            return vac_id
        cur = await self.db.execute(
            "INSERT INTO vacations (date_from, date_to, block_notify, block_autobook) "
            "VALUES (?, ?, ?, ?)",
            (date_from, date_to, int(block_notify), int(block_autobook)),
        )
        await self.db.commit()
        return cur.lastrowid

    async def get_vacation(self, vac_id: int) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM vacations WHERE id=?", (vac_id,))
        r = await cur.fetchone()
        if not r:
            return None
        d = dict(r)
        d["block_notify"] = bool(d["block_notify"])
        d["block_autobook"] = bool(d["block_autobook"])
        return d

    async def vacations_covering(self, day: str) -> list[dict]:
        """Ordered so overlapping ranges resolve deterministically."""
        cur = await self.db.execute(
            "SELECT * FROM vacations WHERE date_from <= ? AND date_to >= ? "
            "ORDER BY date_from, id", (day, day))
        return [dict(r) for r in await cur.fetchall()]

    async def vacations_ending_on(self, day: str) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM vacations WHERE date_to = ? ORDER BY date_from, id",
            (day,))
        return [dict(r) for r in await cur.fetchall()]

    async def revive_vacation_skips(self, date_from: str, date_to: str) -> int:
        """Put classes this vacation skipped back in the queue — pinned ones
        and the ones a rule would have taken.

        Call this AFTER deleting the vacation row: the NOT EXISTS clause below
        leaves alone anything still covered, and both ticks re-check the
        vacation at fire time — so reviving while the row still exists means
        every class is re-skipped, and re-announced, within five minutes.

        The literal must match what the two ticks write, em dash included.
        """
        # the half of the window clause both statements share
        pending = (
            "  SELECT s.schedule_id FROM sessions s"
            "  WHERE s.date >= ? AND s.date <= ?"
            # a class that already happened must not come back only for the
            # next tick to mark it expired
            "    AND s.date >= date('now', 'localtime')"
            "    AND NOT EXISTS ("
            "      SELECT 1 FROM vacations v"
            "      WHERE v.date_from <= s.date AND v.date_to >= s.date"
            "        AND v.block_autobook = 1)"
        )
        cur = await self.db.execute(
            "UPDATE watchlist SET result = NULL "
            "WHERE result = 'skipped — vacation' AND schedule_id IN ("
            + pending + ")",
            (date_from, date_to),
        )
        revived = cur.rowcount
        # autobook_done has no pending state — autobook_attempted() tests for
        # the row itself — so the rule path is revived by deleting it, not by
        # nulling a column
        cur = await self.db.execute(
            "DELETE FROM autobook_done "
            "WHERE result = 'skipped — vacation' AND schedule_id IN ("
            + pending + ")",
            (date_from, date_to),
        )
        await self.db.commit()
        return revived + cur.rowcount

    async def delete_vacation(self, vac_id: int) -> None:
        await self.db.execute("DELETE FROM vacations WHERE id=?", (vac_id,))
        await self.db.commit()

    async def vacation_blocks(self, date: str, kind: str) -> bool:
        """Is `date` inside a vacation that blocks `kind` (notify/autobook)?"""
        col = "block_notify" if kind == "notify" else "block_autobook"
        cur = await self.db.execute(
            f"SELECT 1 FROM vacations WHERE date_from <= ? AND date_to >= ? "
            f"AND {col} = 1 LIMIT 1",
            (date, date),
        )
        return await cur.fetchone() is not None

    # ------------------------------------------------------------- autobook

    async def autobook_attempted(self, schedule_id: int) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM autobook_done WHERE schedule_id=?", (schedule_id,)
        )
        return await cur.fetchone() is not None

    async def mark_autobook(self, schedule_id: int, result: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO autobook_done (schedule_id, result) VALUES (?, ?)",
            (schedule_id, result),
        )
        await self.db.commit()

    async def prune_bookkeeping(self, days: int = 30, decisions_days: int = 180,
                                messages_days: int = 365) -> int:
        """Drop prompt/autobook rows older than `days` — both are append-only
        and reference classes that stop existing once their date passes."""
        total = 0
        # A pending pin whose class vanished from the schedule is a decision
        # the user is still waiting on: resolve it visibly instead of deleting
        # it, or "why was I never booked?" has no answer anywhere.
        cur = await self.db.execute(
            "SELECT schedule_id FROM watchlist WHERE result IS NULL "
            "AND schedule_id NOT IN (SELECT schedule_id FROM sessions)")
        orphaned = [r[0] for r in await cur.fetchall()]
        if orphaned:
            await self.db.execute(
                "UPDATE watchlist SET result = 'expired — class no longer listed' "
                "WHERE result IS NULL AND schedule_id NOT IN "
                "(SELECT schedule_id FROM sessions)")
            await self.db.commit()
            for sid in orphaned:
                await self.log_event(
                    "warn", "watchlist", "תזמון בוטל · השיעור נעלם מהלוח",
                    "השיעור כבר לא מופיע אצל ארבוקס — התזמון לא ימומש", sid)
        cur = await self.db.execute(
            "DELETE FROM watchlist WHERE result IS NOT NULL "
            "AND schedule_id NOT IN (SELECT schedule_id FROM sessions) "
            "AND created_at < datetime('now', ?)", (f"-{decisions_days} days",))
        total_extra = cur.rowcount
        # resolved pins used to die with their sessions; with sessions now
        # retained for up to a year, they need their own age limit
        cur = await self.db.execute(
            "DELETE FROM watchlist WHERE result IS NOT NULL "
            "AND created_at < datetime('now', ?)", (f"-{decisions_days} days",))
        total_extra += cur.rowcount
        cur = await self.db.execute(
            "DELETE FROM retry_counts WHERE schedule_id NOT IN "
            "(SELECT schedule_id FROM sessions)")
        total_extra += cur.rowcount
        for table, col, keep in (("pending_prompts", "created_at", days),
                                 ("autobook_done", "at", decisions_days),
                                 ("box_messages", "seen_at", messages_days),
                                 ("events", "ts", days)):
            cur = await self.db.execute(
                f"DELETE FROM {table} WHERE {col} < datetime('now', ?)",
                (f"-{keep} days",),
            )
            total += cur.rowcount
        await self.db.commit()
        return total + total_extra

    # ---------------------------------------------------------- retry counts

    async def bump_retry(self, schedule_id: int, kind: str) -> int:
        """Count one retryable failure for this class; return the new total."""
        await self.db.execute(
            "INSERT INTO retry_counts (schedule_id, kind, attempts) VALUES (?, ?, 1) "
            "ON CONFLICT(schedule_id, kind) DO UPDATE SET "
            "attempts = attempts + 1, last_at = datetime('now', 'localtime')",
            (schedule_id, kind),
        )
        await self.db.commit()
        cur = await self.db.execute(
            "SELECT attempts FROM retry_counts WHERE schedule_id=? AND kind=?",
            (schedule_id, kind))
        r = await cur.fetchone()
        return int(r[0]) if r else 1

    async def clear_retry(self, schedule_id: int, kind: str) -> None:
        await self.db.execute(
            "DELETE FROM retry_counts WHERE schedule_id=? AND kind=?",
            (schedule_id, kind))
        await self.db.commit()

    # --------------------------------------------------------------- events

    async def log_event(
        self,
        level: str,
        source: str,
        message: str,
        detail: str | None = None,
        schedule_id: int | None = None,
        tag: str | None = None,
        notified: bool = False,
    ) -> None:
        """Record one thing that happened. Never raises: a failure to write
        the log must not take down the operation being logged.

        An identical event inside DEDUPE_MINUTES is dropped. A tick that keeps
        failing for the same reason says the same thing every few minutes, and
        a log you have to scroll past is one you stop reading — the recurring
        cadence still shows, just not three times per restart.
        """
        try:
            cur = await self.db.execute(
                "SELECT 1 FROM events WHERE level=? AND source=? AND message=? "
                "AND IFNULL(detail,'')=IFNULL(?,'') "
                "AND ts >= datetime('now', 'localtime', ?) LIMIT 1",
                (level, source, message, detail, f"-{EVENT_DEDUPE_MINUTES} minutes"),
            )
            if await cur.fetchone():
                return
        except Exception:  # noqa: BLE001 — never block on the dedupe check
            pass
        try:
            await self.db.execute(
                "INSERT INTO events "
                "(level, source, message, detail, schedule_id, tag) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (level, source, message, detail, schedule_id, tag),
            )
            await self.db.commit()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not write event: %s/%s %s", level, source, message)
            return
        # Push it, if anyone asked to hear about events at this severity.
        #   - source "notify" is never pushed: it is *there* because a channel
        #     failed, and announcing that through the channels is a loop
        #   - notified=True means the call site already sent its own message,
        #     so pushing again would double up
        # Detached so a slow or failing send never blocks the operation that
        # was being logged.
        if self.on_event and not notified and source != "notify":
            try:
                task = asyncio.create_task(
                    self.on_event(level, source, message, detail))
                self._push_tasks.add(task)
                task.add_done_callback(self._push_tasks.discard)
            except RuntimeError:      # no running loop (tests, shutdown)
                pass

    async def list_events(
        self,
        level: str | None = None,
        source: str | None = None,
        tag: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        q = "SELECT * FROM events WHERE 1=1"
        args: list[Any] = []
        if level:
            q += " AND level = ?"; args.append(level)
        if source:
            q += " AND source = ?"; args.append(source)
        if tag:
            q += " AND tag = ?"; args.append(tag)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        cur = await self.db.execute(q, args)
        return [dict(r) for r in await cur.fetchall()]

    async def event_counts(self, days: int = 7) -> dict[str, int]:
        """Per-level totals for the filter chips."""
        cur = await self.db.execute(
            "SELECT level, COUNT(*) FROM events "
            "WHERE ts >= datetime('now', 'localtime', ?) GROUP BY level",
            (f"-{days} days",),
        )
        counts = {lvl: 0 for lvl in EVENT_LEVELS}
        for lvl, n in await cur.fetchall():
            counts[lvl] = n
        return counts

    async def last_event(self, source: str, level: str | None = None) -> dict | None:
        q = "SELECT * FROM events WHERE source = ?"
        args: list[Any] = [source]
        if level:
            q += " AND level = ?"; args.append(level)
        cur = await self.db.execute(q + " ORDER BY id DESC LIMIT 1", args)
        r = await cur.fetchone()
        return dict(r) if r else None

    # ----------------------------------------------------------------- meta

    async def get_meta(self, key: str, default: Any = None) -> Any:
        cur = await self.db.execute("SELECT value FROM meta WHERE key=?", (key,))
        r = await cur.fetchone()
        return json.loads(r[0]) if r else default

    async def set_meta(self, key: str, value: Any) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await self.db.commit()
