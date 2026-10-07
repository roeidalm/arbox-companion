"""Isolated branded Arbox logins, with one stable owner per enabled studio.

The original credentials.json remains the legacy login for rollback. Additional
sessions live in private directories and never overwrite its tokens. Discovery
only reads upstream data; a newly found studio must be enabled explicitly.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .arbox_client import ArboxClient, ArboxError


class ConnectionError(ArboxError):
    """A safe, user-facing failure that does not contain upstream secrets."""


class _IdentityError(ConnectionError):
    """The token cannot be trusted for this account, even with cached data."""


def _brand(value: str) -> str:
    if not isinstance(value, str):
        raise ConnectionError("שם האפליקציה אינו תקין")
    value = value.strip()
    # A header value, not a URL, path or arbitrary header block.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", value):
        raise ConnectionError("שם האפליקציה אינו תקין")
    return "Arbox" if value.casefold() == "arbox" else value


def _connection_id(brand: str) -> str:
    return "brand-" + hashlib.sha256(brand.casefold().encode()).hexdigest()[:16]


def _positive_id(value) -> int | None:
    return value if type(value) is int and value > 0 else None


def _safe_profile(profile: dict) -> dict:
    """Persist only fields needed by studio discovery and the profile screen."""
    result = {key: profile.get(key) for key in (
        "id", "full_name", "email", "phone", "birthday", "created_at", "currencySymbol")}
    affiliations = []
    for row in profile.get("users_boxes") or []:
        if not isinstance(row, dict) or not _positive_id(row.get("box_fk")):
            continue
        safe = {key: row.get(key) for key in (
            "box_fk", "locations_box_fk", "rolesArray", "phone", "medical_cert",
            "has_waiver", "total_debt")}
        safe["box"] = {key: (row.get("box") or {}).get(key) for key in (
            "id", "name", "address", "city", "phone", "email")}
        safe["locations_box"] = {key: (row.get("locations_box") or {}).get(key)
                                 for key in ("id", "address", "location")}
        affiliations.append(safe)
    result["users_boxes"] = affiliations
    return result


class Connections:
    def __init__(self, data_dir: str, legacy_client: ArboxClient,
                 client_factory: Callable = ArboxClient) -> None:
        self.legacy = legacy_client
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "connections.json"
        self._factory = client_factory
        self._clients = {"legacy": legacy_client}
        self._lock = asyncio.Lock()
        self.active_box_id: Callable[[], int | None] = lambda: None
        self._entries: dict[str, dict] = {}
        self._owners: dict[str, str] = {}
        self._account_id = legacy_client.user_id
        self._load()
        self._ensure_legacy()

    def _ensure_legacy(self) -> None:
        self._entries.setdefault("legacy", {
            "whitelabel": self.legacy.whitelabel, "status": "unchecked",
            "error": None, "last_checked": None, "profile": {}, "memberships": {},
        })
        self._entries["legacy"]["whitelabel"] = self.legacy.whitelabel

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text())
            if data.get("version") != 1 or data.get("user_id") != self.legacy.user_id:
                return
            entries = data.get("connections") or {}
            for connection_id, entry in entries.items():
                if not isinstance(entry, dict):
                    continue
                brand = _brand(entry.get("whitelabel"))
                if connection_id != "legacy" and connection_id != _connection_id(brand):
                    continue
                if connection_id == "legacy" and brand.casefold() != self.legacy.whitelabel.casefold():
                    continue
                self._entries[connection_id] = entry
            for box_id, connection_id in (data.get("owners") or {}).items():
                if str(box_id).isdigit() and connection_id in self._entries:
                    self._owners[str(box_id)] = connection_id
        except (OSError, ValueError, TypeError, AttributeError, ConnectionError):
            # Existing legacy login remains usable if an optional manifest was
            # damaged. Never infer an owner from an untrusted connection path.
            return

    def _save(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "user_id": self._account_id,
                   "connections": self._entries, "owners": self._owners}
        temporary = self._path.with_suffix(".json.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                fd = None
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            if fd is not None:
                os.close(fd)

    def _client(self, connection_id: str) -> ArboxClient:
        if connection_id not in self._clients:
            entry = self._entries[connection_id]
            directory = self._data_dir / "connections" / connection_id
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
            self._clients[connection_id] = self._factory(
                str(directory), whitelabel=entry["whitelabel"])
        return self._clients[connection_id]

    def _ordered(self):
        return sorted(self._entries, key=lambda key: (key != "legacy", key))

    def for_box(self, box_id: int | None) -> ArboxClient:
        if box_id is None:
            return self.legacy
        owner = self._owners.get(str(box_id))
        if owner is not None:
            if self._entries[owner].get("identity_mismatch"):
                raise ConnectionError("החיבור לסטודיו דורש אימות מחדש")
            client = self._client(owner)
            if owner != "legacy" and client.user_id != self._account_id:
                raise ConnectionError("החיבור לסטודיו דורש אימות מחדש")
            return client
        # The initial legacy login predates the registry. Its first profile
        # fetch supplies ownership, while cached identity remains usable.
        if not self._entries.get("legacy", {}).get("profile"):
            return self.legacy
        raise ConnectionError("הסטודיו אינו מחובר למערכת")

    def _active(self) -> ArboxClient:
        return self.for_box(self.active_box_id())

    @property
    def configured(self) -> bool:
        return self.legacy.configured

    @property
    def authenticated(self) -> bool:
        try:
            return self._active().authenticated
        except ConnectionError:
            return False

    @property
    def email(self):
        return self.legacy.email

    @property
    def user_id(self):
        return self.legacy.user_id

    @property
    def whitelabel(self):
        owner = self._owners.get(str(self.active_box_id()))
        return self._entries[owner]["whitelabel"] if owner else self.legacy.whitelabel

    def __getattr__(self, name):
        # Calls without a box argument (booking, quota, membership history)
        # inherit Store's task-local studio context supplied by the caller.
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._active(), name)

    def _validate_identity(self, client, profile: dict) -> None:
        expected = _positive_id(self._account_id) or _positive_id(self.legacy.user_id)
        if (expected is None or _positive_id(client.user_id) != expected
                or _positive_id(self.legacy.user_id) != expected):
            raise _IdentityError("החיבור החזיר חשבון אחר ולא צורף")
        if profile.get("id") is not None and profile.get("id") != expected:
            raise _IdentityError("החיבור החזיר חשבון אחר ולא צורף")

    async def _refresh(self, connection_id: str, *, memberships: bool,
                       allow_login: bool = False) -> None:
        entry = self._entries[connection_id]
        client = self._client(connection_id)
        try:
            if not client.configured:
                if not allow_login or not self.legacy.configured:
                    raise ConnectionError("יש להתחבר מחדש כדי לבדוק את החיבור")
                await client.login(email=self.legacy.email,
                                   password=self.legacy._password,
                                   whitelabel=entry["whitelabel"])
            elif not client.authenticated:
                await client.login()
            # Verify tokens loaded from disk before using their discovery data.
            if connection_id != "legacy" and client.user_id != self.legacy.user_id:
                raise _IdentityError("החיבור החזיר חשבון אחר ולא צורף")
            profile = await client.profile()
            if not isinstance(profile, dict) or not isinstance(profile.get("users_boxes"), list):
                raise ConnectionError("Arbox החזיר תשובת סטודיואים לא תקינה")
            self._validate_identity(client, profile)
            safe = _safe_profile(profile)
            # Successful refresh can remove stale affiliations, but a transient
            # failure never discards the last verified inventory.
            entry["profile"] = safe
            if connection_id == "legacy":
                for row in safe["users_boxes"]:
                    self._owners.setdefault(str(row["box_fk"]), "legacy")
            if memberships:
                snapshot = entry.setdefault("memberships", {})
                for row in safe["users_boxes"]:
                    box_id = row["box_fk"]
                    try:
                        rows = await client.memberships(box_id)
                        if not isinstance(rows, list):
                            raise ConnectionError("תשובת מנויים לא תקינה")
                        snapshot[str(box_id)] = {
                            "has_active_membership": any(r.get("active") for r in rows
                                                         if isinstance(r, dict))}
                    except (ArboxError, OSError, TimeoutError):
                        snapshot.setdefault(str(box_id), {"has_active_membership": None})
            entry.update(status="connected", error=None, identity_mismatch=False)
        except _IdentityError:
            entry.update(status="error", error="החיבור החזיר חשבון אחר ולא צורף",
                         identity_mismatch=True)
            if connection_id != "legacy":
                client.clear_creds()
        except (ArboxError, OSError, TimeoutError):
            # Never expose str(upstream_error): it may contain tokens or PII.
            entry.update(status="error", error="לא ניתן לאמת את החיבור כרגע; המידע הקודם נשמר")
            if connection_id != "legacy" and client.user_id != self.legacy.user_id:
                client.clear_creds()
                entry["error"] = "החיבור החזיר חשבון אחר ולא צורף"
        finally:
            entry["last_checked"] = datetime.now(timezone.utc).isoformat()

    async def discover(self, whitelabel: str | None = None) -> dict:
        """Check Arbox, the saved brand(s), and an optional new branded app."""
        requested = _brand(whitelabel) if whitelabel is not None else None
        async with self._lock:
            if not self.legacy.configured:
                raise ConnectionError("יש להתחבר ל־Arbox לפני הוספת חיבור")
            self._ensure_legacy()
            for brand in ("Arbox", requested):
                if not brand or any(e["whitelabel"].casefold() == brand.casefold()
                                    for e in self._entries.values()):
                    continue
                if len(self._entries) >= 8:
                    raise ConnectionError("ניתן להגדיר עד שמונה חיבורים")
                self._entries[_connection_id(brand)] = {
                    "whitelabel": brand, "status": "unchecked", "error": None,
                    "last_checked": None, "profile": {}, "memberships": {},
                }
            for connection_id in self._ordered():
                await self._refresh(connection_id, memberships=True, allow_login=True)
            self._account_id = self.legacy.user_id
            self._save()
            return self.public()

    async def profile(self) -> dict:
        """Refresh current connections, retaining verified data during failures."""
        async with self._lock:
            self._ensure_legacy()
            # Include candidates in the cache, but never in users_boxes until
            # explicitly enabled. Existing tokens may refresh normally.
            for connection_id in self._ordered():
                if connection_id == "legacy" or connection_id in self._owners.values():
                    await self._refresh(connection_id, memberships=False)
            self._account_id = self.legacy.user_id
            self._save()
            combined = copy.deepcopy(self._entries["legacy"].get("profile") or {})
            combined["users_boxes"] = []
            seen = set()
            for connection_id in self._ordered():
                for row in self._entries[connection_id].get("profile", {}).get("users_boxes", []):
                    box_id = row["box_fk"]
                    if box_id in seen or self._owners.get(str(box_id)) != connection_id:
                        continue
                    combined["users_boxes"].append(copy.deepcopy(row))
                    seen.add(box_id)
            if not combined["users_boxes"] and self._entries["legacy"]["status"] == "error":
                raise ConnectionError("לא ניתן לטעון את רשימת הסטודיואים כרגע")
            return combined

    def enable(self, connection_id: str, box_id: int) -> dict:
        entry = self._entries.get(connection_id)
        if not entry or entry.get("status") != "connected" or not _positive_id(box_id):
            raise ConnectionError("יש לאמת את החיבור לפני הוספת הסטודיו")
        rows = entry.get("profile", {}).get("users_boxes", [])
        if not any(row.get("box_fk") == box_id for row in rows):
            raise ConnectionError("הסטודיו לא נמצא בחיבור הזה")
        membership = entry.get("memberships", {}).get(str(box_id), {})
        if membership.get("has_active_membership") is not True:
            raise ConnectionError("לא נמצא מנוי פעיל בסטודיו הזה")
        self._owners.setdefault(str(box_id), connection_id)
        self._save()
        return self.public()

    def public(self) -> dict:
        connections = []
        enabled = set()
        for connection_id in self._ordered():
            entry = self._entries[connection_id]
            studios = []
            seen = set()
            for row in entry.get("profile", {}).get("users_boxes", []):
                box_id = row["box_fk"]
                if box_id in seen:
                    continue
                seen.add(box_id)
                box, loc = row.get("box") or {}, row.get("locations_box") or {}
                owner = self._owners.get(str(box_id))
                if owner:
                    enabled.add(box_id)
                studios.append({
                    "id": box_id, "name": box.get("name") or f"סטודיו {box_id}",
                    "location_id": row.get("locations_box_fk"),
                    "address": box.get("address") or loc.get("address") or loc.get("location"),
                    "enabled": owner is not None, "duplicate": owner is not None and owner != connection_id,
                    "has_active_membership": entry.get("memberships", {}).get(str(box_id), {}).get("has_active_membership"),
                })
            connections.append({
                "id": connection_id, "whitelabel": entry["whitelabel"],
                "legacy": connection_id == "legacy", "status": entry.get("status", "unchecked"),
                "error": entry.get("error"), "last_checked": entry.get("last_checked"),
                "studios": studios,
            })
        return {"connections": connections, "studio_count": len(enabled)}

    async def memberships(self, box_id: int) -> list[dict]:
        return await self.for_box(box_id).memberships(box_id)

    async def schedule_between(self, box_id: int, location_id: int,
                               start_date: str, end_date: str) -> list[dict]:
        return await self.for_box(box_id).schedule_between(box_id, location_id, start_date, end_date)

    async def login(self, *args, **kwargs):
        previous = (self.legacy.user_id, self.legacy.whitelabel.casefold())
        result = await self.legacy.login(*args, **kwargs)
        if previous != (self.legacy.user_id, self.legacy.whitelabel.casefold()):
            self._reset_additional()
        self._account_id = self.legacy.user_id
        self._ensure_legacy()
        return result

    def _reset_additional(self):
        for connection_id in self._entries:
            if connection_id != "legacy":
                self._client(connection_id).clear_creds()
        self._entries = {}
        self._owners = {}
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass

    def clear_creds(self) -> None:
        self._reset_additional()
        self.legacy.clear_creds()
        self._account_id = None
        self._ensure_legacy()

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
