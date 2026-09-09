"""Tiered sync from Arbox into the local store.

The UI and HA never wait on Arbox — they read SQLite; these jobs keep it fresh
at the pace each field actually changes:

- window_sync: one betweenDates call for a rolling window, 2x/day + on demand
- near_term_sync: one 48h call every 30 min (free spots, my standby position)
- nightly roll: drop past days, extend the window

Also detects standby promotions (Arbox books you off the waitlist) and floats
them to the notifier.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable

from .arbox_client import ArboxClient, ArboxError
from .store import Store
from .studio_context import ReentrantAsyncLock, check_identity_change

_LOGGER = logging.getLogger(__name__)

WINDOW_DAYS = 14
NEAR_TERM_DAYS = 2
FULL_WINDOW_DAYS = 30
FAR_WINDOW_START_DAYS = WINDOW_DAYS + 1


class Syncer:
    def __init__(
        self,
        client: ArboxClient,
        store: Store,
        on_standby_promoted: Callable[[dict], Awaitable[None]] | None = None,
        settings=None,
    ) -> None:
        self.client = client
        self.store = store
        self.settings = settings
        self.on_standby_promoted = on_standby_promoted
        self.on_sync = None
        self._lock = ReentrantAsyncLock()
        self.box_id: int | None = None
        self.location_id: int | None = None
        self.membership_user_id: int | None = None
        self.memberships: list[dict] = []
        self.studio_name: str | None = None
        self.address: str | None = None
        self.studio_count = 0

    def _apply_studio(self, studio: dict) -> None:
        check_identity_change(self, studio.get("id"))
        self.box_id = studio.get("id")
        self.location_id = studio.get("location_id")
        self.studio_name = studio.get("name")
        self.address = studio.get("address")
        self.store.active_box_id = self.box_id

    @staticmethod
    def _membership_snapshots(rows: list[dict]) -> list[dict]:
        snapshots = []
        for row in rows:
            mt = row.get("membership_types") or {}
            plan_name = mt.get("name")
            snapshots.append({
                "extra_advance_hours": int(
                    mt.get("extra_enable_registration_time") or 0),
                "plan_quota": Syncer._parse_plan_quota(plan_name),
                "id": row.get("id"),
                "membership_type_id": mt.get("id") or row.get("membership_type_fk"),
                "plan": plan_name,
                "price": mt.get("price"),
                "active": bool(row.get("active")),
                "start": row.get("start"),
                "end": row.get("end"),
                "sessions_left": row.get("sessions_left"),
                "sessions_on_purchase": row.get("sessions_on_purchase"),
                "day_of_payment": row.get("day_of_payment"),
                "card_ends": row.get("card_ends"),
                "card_exp": row.get("exp_date"),
                "recurring": bool(mt.get("is_recurring_payment")),
            })
        return snapshots

    async def discover_studios(self, profile: dict | None = None) -> list[dict]:
        """Return only affiliations where the account can actually train.

        `users_boxes[0]` is not meaningful: Arbox may put a coach affiliation
        before the studio where the same person is a paying member. Probing the
        memberships endpoint is the authoritative distinction.
        """
        profile = profile or await self.client.profile()
        studios = []
        affiliations = []
        seen = set()
        ignored = set(self.settings.ignored_studio_ids if self.settings else [])
        for ub in profile.get("users_boxes") or []:
            box_id = ub.get("box_fk")
            if not box_id or box_id in seen:
                continue
            seen.add(box_id)
            box = ub.get("box") or {}
            loc = ub.get("locations_box") or {}
            base = {
                "id": box_id,
                "location_id": ub.get("locations_box_fk"),
                "name": box.get("name") or f"סטודיו {box_id}",
                "address": box.get("address") or loc.get("address") or loc.get("location"),
                "roles": ub.get("rolesArray") or [],
                "ignored": box_id in ignored,
            }
            if box_id in ignored:
                # Keep only the identity already present in the profile so it
                # can be restored later. No membership, schedule or feed call
                # is made for an ignored studio.
                affiliations.append({**base, "has_active_membership": None})
                continue
            try:
                rows = await self.client.memberships(box_id)
            except ArboxError as err:
                _LOGGER.warning("Could not inspect memberships for box %s: %s",
                                box_id, err)
                affiliations.append({**base, "has_active_membership": None})
                continue
            active = [m for m in rows if m.get("active")]
            affiliations.append({**base, "has_active_membership": bool(active)})
            if not active:
                continue
            studios.append({
                **base,
                "memberships": self._membership_snapshots(active),
            })
        await self.store.set_meta("studio_affiliations", affiliations)
        await self.store.set_meta("studios", studios)
        self.studio_count = len(studios)
        return studios

    def exclusive(self):
        """The sync lock, for callers that must not be overwritten by a sync.

        A booking and a sync race over the same rows: a sync that fetched its
        payload a second before the user booked will happily upsert the class
        back to unbooked, and it stays that way until the next sweep.
        """
        return self._lock

    # ------------------------------------------------------------ identity

    async def ensure_identity(self) -> None:
        """Derive identity without changing studio during another operation."""
        async with self._lock:
            await self._ensure_identity()

    async def _ensure_identity(self) -> None:
        """Derive box/location/membership ids from the API; cache in meta."""
        if self.box_id and self.membership_user_id:
            return
        cached = await self.store.get_meta("identity")
        if cached:
            check_identity_change(self, cached.get("box_id"))
            self.box_id = cached.get("box_id")
            self.location_id = cached.get("location_id")
            self.membership_user_id = cached.get("membership_user_id")
            self.studio_name = cached.get("studio_name")
            self.address = cached.get("address")
            self.store.active_box_id = self.box_id
            cached_studios = await self.store.get_meta("studios") or []
            self.studio_count = len(cached_studios)
            # "address" arrived after the first release — re-derive once so an
            # existing install fills it without anyone clearing the DB
            preferred = self.settings.preferred_studio_id if self.settings else None
            ignored = set(self.settings.ignored_studio_ids if self.settings else [])
            if (self.box_id and self.membership_user_id and "address" in cached
                    and cached_studios and self.box_id not in ignored
                    and (preferred is None or preferred == self.box_id)):
                return

        profile = await self.client.profile()
        studios = await self.discover_studios(profile)
        if not studios:
            raise ArboxError("No studio with an active membership was found")
        preferred = self.settings.preferred_studio_id if self.settings else None
        selected = next((x for x in studios if x["id"] == preferred), studios[0])
        self._apply_studio(selected)
        # Preserve the already-notified state for the original studio only.
        # A newly selected studio must get its own first inventory notice.
        if cached and cached.get("box_id") == self.box_id:
            legacy_fp = await self.store.get_meta("membership_notified_fingerprint")
            scoped_key = f"membership_notified_fingerprint:{self.box_id}"
            if legacy_fp and await self.store.get_meta(scoped_key) is None:
                await self.store.set_meta(scoped_key, legacy_fp)
        if self.settings and self.settings.preferred_studio_id != self.box_id:
            self.settings.select_studio(self.box_id)
        await self._store_profile(profile)
        if not self.box_id:
            raise ArboxError("Could not derive box id from profile")

        membership = await self._store_membership()
        if membership:
            self.membership_user_id = membership["id"]

        await self.store.set_meta("identity", {
            "box_id": self.box_id,
            "location_id": self.location_id,
            "membership_user_id": self.membership_user_id,
            "studio_name": self.studio_name,
            "address": self.address,
        })
        _LOGGER.info(
            "Identity: box=%s location=%s membership_user=%s",
            self.box_id, self.location_id, self.membership_user_id,
        )

    @staticmethod
    def _parse_plan_quota(plan_name: str | None) -> int | None:
        """Read the monthly allowance out of the plan name.

        Arbox exposes no numeric allowance — sessions_left, sessions and
        sessions_on_purchase are all null on a "plan" membership, confirmed by
        snapshotting the API before and after a session was consumed. The only
        place the number exists is the studio's own plan title, e.g.
        "מנוי בואו נזוז- 1 בשבוע (5 בחודש), הוראת קבע". Parsing it keeps the
        quota self-updating when the plan changes, instead of a hand-entered
        number that silently goes stale.
        """
        if not plan_name:
            return None
        import re
        # "(5 בחודש)" / "5 בחודש" — monthly allowance
        m = re.search(r"(\d+)\s*(?:בחודש|לחודש)", plan_name)
        if m:
            return int(m.group(1))
        # fall back to a weekly figure, normalised to a month
        m = re.search(r"(\d+)\s*(?:בשבוע|לשבוע)", plan_name)
        if m:
            return int(m.group(1)) * 4
        return None

    async def _store_profile(self, profile: dict | None = None) -> dict:
        """Persist the account/studio snapshot the profile screen reads.

        Callable on its own: identity is cached, so waiting for it to be
        re-derived would leave newly added fields empty indefinitely.
        """
        if profile is None:
            profile = await self.client.profile()
        ub = next((x for x in (profile.get("users_boxes") or [])
                   if x.get("box_fk") == self.box_id), {})
        box = ub.get("box") or {}
        loc = ub.get("locations_box") or {}
        snapshot = {
            "full_name": profile.get("full_name"),
            "email": profile.get("email"),
            "phone": profile.get("phone") or ub.get("phone"),
            "birthday": profile.get("birthday"),
            "member_since": profile.get("created_at"),
            "medical_cert": bool(ub.get("medical_cert")),
            "has_waiver": bool(ub.get("has_waiver")),
            "total_debt": ub.get("total_debt"),
            "currency": profile.get("currencySymbol"),
            "studio": {
                "name": box.get("name"),
                "address": box.get("address") or loc.get("address"),
                "city": box.get("city"),
                "phone": box.get("phone"),
                "email": box.get("email"),
            },
        }
        await self.store.set_meta("profile", snapshot)
        return snapshot

    async def _store_membership(self) -> dict | None:
        """Fetch and persist every membership, retaining a legacy primary.

        `membership` remains the preferred/legacy object for HA and older API
        clients; `memberships` is the complete inventory used for booking.
        """
        assert self.box_id
        memberships = await self.client.memberships(self.box_id)
        if not memberships:
            await self.store.set_meta("memberships", [])
            await self.store.set_meta("membership", None)
            self.memberships = []
            self.membership_user_id = None
            return None
        snapshots = self._membership_snapshots(memberships)
        await self.store.set_meta("memberships", snapshots)
        self.memberships = snapshots

        preferred = self.settings.preferred_membership_id if self.settings else None
        legacy = self.membership_user_id
        available = [m for m in snapshots if m.get('active')] or snapshots
        selected = next(
            (m for wanted in (preferred, legacy) if wanted
             for m in available if m.get("id") == wanted), available[0])
        self.membership_user_id = selected["id"]
        await self.store.set_meta("membership", selected)
        identity = await self.store.get_meta("identity") or {}
        if identity:
            identity["membership_user_id"] = self.membership_user_id
            await self.store.set_meta("identity", identity)
        return selected

    # ---------------------------------------------------------------- syncs

    async def _pull_range(self, start: str, end: str) -> int:
        await self.ensure_identity()
        assert self.box_id and self.location_id

        before = {
            s["schedule_id"]: s
            for s in await self.store.get_sessions(date_from=start, date_to=end)
        }

        sessions = await self.client.schedule_between(
            self.box_id, self.location_id, start, end
        )
        memberships = await self.store.get_meta("memberships") or []
        membership = await self.store.get_meta("membership") or {}
        extra_advance = max(
            [int(m.get("extra_advance_hours") or 0) for m in memberships]
            or [int(membership.get("extra_advance_hours") or 0)])
        await self.store.upsert_sessions(
            sessions, extra_advance_hours=extra_advance, box_id=self.box_id
        )
        # The resync-delete is clamped to today: with history retained, a
        # range that reaches into the past must never prune it — one empty
        # upstream response would destroy attendance Arbox may not re-serve.
        del_start = max(start, date.today().isoformat())
        if end >= del_start:
            # An empty pull over more than one day is far likelier to be Arbox
            # in maintenance (200 with no 'data' key) than a genuinely empty
            # fortnight, and acting on it deletes the whole window — booked
            # classes, the HA sensor, today's reminder and any pin that lands
            # on it. A single empty day is a real thing (a holiday), so that
            # one is still allowed through.
            spans_days = len({s["date"] for s in before.values()
                              if s["date"] >= del_start}) > 1
            if not sessions and spans_days:
                _LOGGER.error(
                    "Refusing to prune %s..%s: upstream returned nothing for a "
                    "range that holds %d local sessions", del_start, end,
                    len(before))
                await self.store.log_event(
                    "warn", "sync", "סנכרון החזיר טווח ריק — לא נמחק כלום",
                    f"{del_start}–{end}: ארבוקס לא החזיר שיעורים, "
                    f"והמידע המקומי נשמר")
            else:
                await self.store.delete_sessions_in_range_not_in(
                    del_start, end, [s["id"] for s in sessions]
                )
        await self.store.set_meta("last_sync", datetime.now().isoformat())
        _LOGGER.info("Synced %s..%s: %d sessions", start, end, len(sessions))
        # info-level and frequent: the log is filtered by level, and "the last
        # sync succeeded" is exactly what the status strip needs to prove
        await self.store.log_event(
            "info", "sync", f"סנכרון · {len(sessions)} שיעורים", f"{start}–{end}")

        await self._detect_standby_promotions(before, sessions)
        await self.store.set_meta("quota_cache", None)
        if self.on_sync:
            await self.on_sync()
        return len(sessions)

    async def _detect_standby_promotions(
        self, before: dict[int, dict], fresh: list[dict]
    ) -> None:
        if not self.on_standby_promoted:
            return
        for s in fresh:
            old = before.get(s["id"])
            if not old:
                continue
            was_standby = old.get("user_in_standby") is not None
            now_booked = s.get("user_booked") is not None
            if was_standby and now_booked:
                _LOGGER.info("Standby promoted for schedule %s", s["id"])
                row = await self.store.get_session(s["id"])
                if row:
                    await self.on_standby_promoted(row)

    async def window_sync(self) -> None:
        """Full rolling window in ONE upstream call (verified: range works)."""
        async with self._lock:
            start = date.today().isoformat()
            end = (date.today() + timedelta(days=WINDOW_DAYS)).isoformat()
            await self._pull_range(start, end)

    async def sync_range(self, start: str | None, end: str | None) -> int:
        """On-demand sync of a specific day or range (awaited, not background).

        Bounds are clamped to [today, today+62d]. The floor stays today
        forever: normal sync must never pull the past, because the resync
        delete and the upsert would both chew on retained history. Backfill
        is its own insert-only path.
        """
        today = date.today()
        lo, hi = today, today + timedelta(days=62)

        def clamp(value: str | None, default: date) -> date:
            if not value:
                return default
            try:
                d = date.fromisoformat(value)
            except ValueError:
                return default
            return min(max(d, lo), hi)

        start_d = clamp(start, today)
        end_d = clamp(end, today + timedelta(days=FULL_WINDOW_DAYS))
        if end_d < start_d:
            end_d = start_d
        async with self._lock:
            return await self._pull_range(start_d.isoformat(), end_d.isoformat())

    async def near_term_sync(self) -> None:
        """Every 30 min: the next two days, where free counts move fastest."""
        async with self._lock:
            start = date.today().isoformat()
            end = (date.today() + timedelta(days=NEAR_TERM_DAYS)).isoformat()
            await self._pull_range(start, end)

    async def refresh_selected(self, sessions: list[dict]) -> set[int]:
        """One read for personal commitments, without running probes/notices.

        Arbox exposes a date-range read, not a per-id read. Only selected ids
        are written/reviewed; unrelated classes in the response are ignored.
        Missing entries are retained locally and block booking until reviewed.
        """
        if not sessions:
            return set()
        async with self._lock:
            await self.ensure_identity()
            selected = {s['schedule_id'] for s in sessions}
            days = [s['date'] for s in sessions]
            raw = await self.client.schedule_between(self.box_id, self.location_id, min(days), max(days))
            raw = [s for s in raw if s['id'] in selected]
            memberships = await self.store.get_meta('memberships') or []
            bonus = max((int(m.get('extra_advance_hours') or 0) for m in memberships), default=0)
            await self.store.upsert_sessions(raw, extra_advance_hours=bonus, box_id=self.box_id)
            found = {s['id'] for s in raw}
            for sid in selected - found:
                await self.store.db.execute(
                    'UPDATE planning_intents SET changed=2 WHERE schedule_id=? AND box_id=?',
                    (sid, self.box_id))
            await self.store.db.commit()
            await self.store.set_meta('quota_cache', None)
            return found

    async def mid_range_sync(self) -> None:
        """Every 2h: the whole window, so a booking or cancellation made in
        the Arbox app to a class further out than the near-term span shows
        up within hours rather than waiting for the twice-daily sweep.

        Separate from near_term_sync on purpose: this pulls ~70 sessions
        against ~15, and there is no reason to carry that every 30 minutes
        when the classes it covers are days away.
        """
        await self.window_sync()

    async def far_range_sync(self) -> None:
        """Refresh days 15–30 once daily, just before the nightly digest."""
        async with self._lock:
            today = date.today()
            start = (today + timedelta(days=FAR_WINDOW_START_DAYS)).isoformat()
            end = (today + timedelta(days=FULL_WINDOW_DAYS)).isoformat()
            await self._pull_range(start, end)

    async def nightly_roll(self) -> None:
        """03:05 — apply retention, prune bookkeeping, refresh the window."""
        r = self.settings.retention if self.settings else \
            {"attended_days": 365, "past_days": 30, "future_days": 30,
             "decisions_days": 180, "messages_days": 365}
        decisions_days = r.pop("decisions_days", 180)
        messages_days = r.pop("messages_days", 365)
        counts = await self.store.prune_sessions(**r)
        pruned = await self.store.prune_bookkeeping(
            days=30, decisions_days=decisions_days, messages_days=messages_days)
        _LOGGER.info(
            "Nightly roll: pruned %s sessions, %d bookkeeping rows",
            counts, pruned,
        )
        if any(counts.values()):
            await self.store.log_event(
                "info", "sync", "ניקוי לילי",
                f"נמחקו {counts['attended']} מהיסטוריה, {counts['past']} "
                f"שיעורי עבר, {counts['future']} מעבר לאופק")
        await self.window_sync()

    async def backfill_history(self, months: int = 12) -> dict:
        """One-time import of my past classes, in chunks Arbox handles well.

        Deliberately bypasses _pull_range: no resync-delete, no
        standby-promotion diff (year-old promotions are noise), and no
        per-chunk sync events. The lock is taken per chunk so the 30-minute
        near-term sync can interleave instead of queueing behind ~7 pulls.
        """
        await self.ensure_identity()
        assert self.box_id and self.location_id
        today = date.today()
        end = today - timedelta(days=1)
        floor = today - timedelta(days=months * 31)
        fetched = inserted = chunks = 0
        while end >= floor:
            start = max(floor, end - timedelta(days=57))
            async with self._lock:
                sessions = await self.client.schedule_between(
                    self.box_id, self.location_id,
                    start.isoformat(), end.isoformat(),
                )
                fetched += len(sessions)
                inserted += await self.store.insert_attended_sessions(
                    sessions, box_id=self.box_id)
            chunks += 1
            end = start - timedelta(days=1)
            await asyncio.sleep(1.0)
        await self.store.set_meta("history_backfill", {
            "at": datetime.now().isoformat(), "inserted": inserted,
        })
        await self.store.log_event(
            "info", "sync", f"ייבוא היסטוריה · {inserted} שיעורים",
            f"{chunks} משיכות, {fetched} שיעורים נסרקו, {months} חודשים אחורה")
        return {"chunks": chunks, "fetched": fetched, "inserted": inserted}

    async def refresh_membership(self) -> list[dict]:
        async with self._lock:
            await self.ensure_identity()
            profile = await self.client.profile()
            studios = await self.discover_studios(profile)
            selected = next((x for x in studios if x["id"] == self.box_id), None)
            if not selected and studios:
                selected = studios[0]
                self._apply_studio(selected)
                if self.settings:
                    self.settings.select_studio(self.box_id)
            await self._store_membership()
            await self._store_profile(profile)
            return await self.store.get_meta("memberships") or []

    async def select_studio(self, box_id: int, make_default: bool = False) -> dict:
        """Switch the active studio and rebuild its local identity snapshots."""
        async with self._lock:
            previous_box_id = self.box_id
            profile = await self.client.profile()
            studios = await self.discover_studios(profile)
            selected = next((x for x in studios if x["id"] == box_id), None)
            if not selected:
                raise ArboxError("Studio has no active membership")
            check_identity_change(self, selected.get("id"))
            self.membership_user_id = None
            self.memberships = []
            self._apply_studio(selected)
            if self.settings:
                self.settings.activate_studio(
                    box_id, previous_box_id=previous_box_id,
                    make_default=make_default)
            await self._store_profile(profile)
            membership = await self._store_membership()
            await self.store.set_meta("identity", {
                "box_id": self.box_id, "location_id": self.location_id,
                "membership_user_id": membership.get("id") if membership else None,
                "studio_name": self.studio_name, "address": self.address,
            })
        await self.window_sync()
        return selected

    async def refresh_profile(self) -> dict:
        """Force-refresh the account + membership snapshots."""
        async with self._lock:
            await self.ensure_identity()
            await self._store_membership()
            return await self._store_profile()
