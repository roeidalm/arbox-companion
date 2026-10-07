"""Run background work for each studio without changing the selected panel."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from copy import deepcopy

from .studio_context import _expected_context, check_identity_change

_LOGGER = logging.getLogger(__name__)
_MISSING = object()


class StudioUnavailable(RuntimeError):
    """The requested studio cannot safely supply an operation's context."""


class StudioRuntime:
    """Serialize temporary studio contexts with bookings, syncs and the UI.

    Store metadata is scoped by ``active_box_id``. Changing these in-memory
    fields routes both local reads and the connection registry to the target
    studio; it does not change the user's persisted studio selection.
    """

    _SYNC_FIELDS = (
        "box_id", "location_id", "membership_user_id", "memberships",
        "studio_name", "address", "studio_count",
    )

    def __init__(self, syncer, engine, settings, store):
        self.syncer = syncer
        self.engine = engine
        self.settings = settings
        self.store = store

    async def _studios(self):
        ignored = set(self.settings.ignored_studio_ids if self.settings else [])
        studios = []
        seen = set()
        for studio in await self.store.get_meta("studios") or []:
            box_id = studio.get("id")
            if (not isinstance(box_id, int) or isinstance(box_id, bool)
                    or box_id <= 0 or box_id in seen or box_id in ignored
                    or studio.get("ignored")):
                continue
            seen.add(box_id)
            studios.append(studio)
        return studios

    def _snapshot(self):
        return {
            "syncer": {name: getattr(self.syncer, name, _MISSING)
                       for name in self._SYNC_FIELDS},
            "box_id": self.store.active_box_id,
        }

    async def _restore(self, snapshot):
        for name, value in snapshot["syncer"].items():
            if value is _MISSING:
                self.syncer.__dict__.pop(name, None)
            else:
                setattr(self.syncer, name, value)
        self.store.active_box_id = snapshot["box_id"]
        # Membership refreshes may replace or remove a selected studio's
        # primary membership. Restore its selection, not a stale membership
        # captured before this job saved the new inventory.
        if self.syncer.box_id and self.syncer.box_id == self.store.active_box_id:
            identity = await self.store.get_meta("identity")
            memberships = await self.store.get_meta("memberships")
            primary = await self.store.get_meta("membership")
            if (identity and identity.get("box_id") == self.syncer.box_id
                    and memberships is not None):
                self.syncer.memberships = deepcopy(memberships)
                selected = self._selected_membership(memberships, identity, primary)
                member_id = selected.get("id") if selected else None
                self.syncer.membership_user_id = member_id
                if identity.get("membership_user_id") != member_id:
                    await self.store.set_meta("identity", {**identity, "membership_user_id": member_id})
                if primary != selected:
                    await self.store.set_meta("membership", selected)

    def _selected_membership(self, memberships, identity, primary):
        # Explicit default selection wins over an older identity snapshot.
        # Older callbacks persisted the new primary without updating identity.
        wanted = (self.settings.preferred_membership_id if self.settings else None,
                  (primary or {}).get("id"), identity.get("membership_user_id"))
        return next((row for member_id in wanted if member_id
                     for row in memberships
                     if type(row.get("id")) is int and row["id"] > 0
                     and row.get("active") and row["id"] == member_id), None)

    def _usable_membership(self):
        return any(type(row.get("id")) is int and row["id"] > 0
                   and row.get("active")
                   and row["id"] == self.syncer.membership_user_id
                   for row in self.syncer.memberships)

    async def _activate(self, studio):
        box_id = studio["id"]
        check_identity_change(self.syncer, box_id)
        self.store.active_box_id = box_id
        identity = await self.store.get_meta("identity") or {}
        if identity and identity.get("box_id") != box_id:
            raise StudioUnavailable(f"Cached identity does not match studio {box_id}")

        self.syncer.box_id = box_id
        self.syncer.location_id = studio.get("location_id") or identity.get("location_id")
        self.syncer.studio_name = studio.get("name") or identity.get("studio_name")
        self.syncer.address = studio.get("address") or identity.get("address")
        memberships = await self.store.get_meta("memberships")
        membership = await self.store.get_meta("membership")
        self.syncer.memberships = deepcopy(memberships or [])
        selected = self._selected_membership(self.syncer.memberships, identity, membership)
        self.syncer.membership_user_id = selected.get("id") if selected else None

        if not self.syncer.location_id:
            raise StudioUnavailable(f"Studio {box_id} has no known location")
        if (not identity or memberships is None or not membership
                or not self._usable_membership()):
            # Do not call ensure_identity: an empty studio may otherwise fall
            # back to the user's preferred studio before this job executes.
            await self.syncer._store_membership()
        if not self._usable_membership():
            raise StudioUnavailable(f"Studio {box_id} has no usable membership")
        primary = next(row for row in self.syncer.memberships
                       if row.get("id") == self.syncer.membership_user_id)
        if primary != membership:
            await self.store.set_meta("membership", primary)
        await self.store.set_meta("identity", {
            "box_id": box_id,
            "location_id": self.syncer.location_id,
            "membership_user_id": self.syncer.membership_user_id,
            "studio_name": self.syncer.studio_name,
            "address": self.syncer.address,
        })
        if await self.store.get_meta("profile") is None:
            profile = await self.syncer.client.profile()
            if not any(row.get("box_fk") == box_id
                       for row in profile.get("users_boxes") or []):
                raise StudioUnavailable(f"Studio {box_id} is absent from the account profile")
            await self.syncer._store_profile(profile)

    async def _run(self, studio, func, args, kwargs):
        snapshot = self._snapshot()
        token = None
        try:
            check_identity_change(self.syncer, studio["id"])
            token = _expected_context.set(
                (self.syncer, studio["id"], asyncio.current_task()))
            scope = (self.settings.background_studio(studio["id"])
                     if self.settings else nullcontext())
            with scope:
                await self._activate(studio)
                return await func(*args, **kwargs)
        finally:
            if token is not None:
                _expected_context.reset(token)
            await self._restore(snapshot)

    async def run_all(self, func, *args, **kwargs):
        """Run once per enabled studio; one unavailable connection is isolated."""
        results = {}
        async with self.syncer.exclusive():
            for studio in await self._studios():
                try:
                    results[studio["id"]] = await self._run(studio, func, args, kwargs)
                except Exception:
                    _LOGGER.exception("Background operation %s failed for studio %s",
                                      getattr(func, "__name__", type(func).__name__),
                                      studio["id"])
        return results

    async def run_for(self, box_id, func, *args, **kwargs):
        """Run an exact-studio opening job, never falling back to another one."""
        async with self.syncer.exclusive():
            studio = next((row for row in await self._studios()
                           if row["id"] == box_id), None)
            if studio is None:
                raise StudioUnavailable(f"Studio {box_id} is unavailable or ignored")
            return await self._run(studio, func, args, kwargs)
