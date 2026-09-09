"""Evidence-backed membership eligibility. Never infer it from a plan's name.

Only exact, unique category matches are automatic. Unrecognised restrictions
remain visible for the account owner to resolve. No registration is performed
by this module: reading the shop is safe even when a class is already open.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from datetime import date, timedelta

from .arbox_client import ArboxError

POLICY_TTL = 24 * 60 * 60


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def fingerprint(member: dict) -> str:
    # Balances change on every booking. They do not change eligibility.
    fields = ("id", "membership_type_id", "active", "start", "end",
              "sessions_on_purchase", "recurring")
    return hashlib.sha256(json.dumps({k: member.get(k) for k in fields},
                                    sort_keys=True).encode()).hexdigest()[:24]


def parse_limits(sections: list) -> tuple[list[dict], list[str]]:
    limits, unresolved = [], []
    for section in sections:
        header = normalized(str(section.get("header") or ""))
        if header == "available classes":
            continue
        values = [str(v).strip() for v in section.get("values") or [] if str(v).strip()]
        for value in values:
            match = re.fullmatch(r"up to (\d+) sessions? per (day|week|month)",
                                 normalized(value)) if header == "frequency limitations" else None
            if match and match[2] != "week":
                limits.append({"count": int(match[1]), "period": match[2]})
            else:
                unresolved.append(f"{section.get('header')}: {value}")
    return limits, unresolved


def period_bounds(day: str, period: str, member: dict, week_start: int = 0) -> tuple[str, str]:
    d = date.fromisoformat(day)
    if period == "card":
        return member.get("start") or "0001-01-01", member.get("end") or "9999-12-31"
    if period == "month":
        start = d.replace(day=1)
        return start.isoformat(), ((start + timedelta(days=32)).replace(day=1)
                                   - timedelta(days=1)).isoformat()
    if period == "week":
        start = d - timedelta(days=(d.weekday() - week_start) % 7)
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    return day, day


class MembershipPolicy:
    def __init__(self, store, client, syncer):
        self.store, self.client, self.syncer = store, client, syncer

    def key(self, member_id: int) -> str:
        account = str(getattr(self.client, "email", None) or
                      getattr(self.client, "user_id", None) or "local")
        account = hashlib.sha256(account.encode()).hexdigest()[:24]
        return f"membership_policy:{account}:{self.store.active_box_id or 0}:{member_id}"

    async def catalog(self) -> list[dict]:
        rows = await self.store.get_sessions()
        cats = {(r.get("category_id"), r.get("category_name")) for r in rows
                if r.get("category_id") is not None and r.get("category_name")}
        return [{"id": cid, "name": name} for cid, name in sorted(cats)]

    async def get(self, member: dict) -> dict:
        saved = await self.store.get_meta(self.key(member["id"])) or {}
        current = saved.get("fingerprint") == fingerprint(member)
        stale = bool(saved.get("source") in ("shop", "rejection") and
                     time.time() - saved.get("verified_at", 0) > 7 * POLICY_TTL)
        catalog = await self.catalog()
        index = {}
        for cat in catalog:
            index.setdefault(normalized(cat["name"]), set()).add(cat["id"])
        ids = list(saved.get("category_ids") or []) if saved.get("source") == "manual" else []
        unmatched = []
        if saved.get("source") != "manual":
            for name in saved.get("category_names") or []:
                matches = index.get(normalized(name), set())
                if len(matches) == 1:
                    ids.extend(matches)
                else:
                    unmatched.append(name)
        else:
            known = {c["id"] for c in catalog}
            unmatched = [str(cid) for cid in ids if cid not in known]
        category_known = bool(current and saved.get("categories_known")
                              and not saved.get("contradiction") and not stale)
        limits = list(saved.get("limits") or []) if current else []
        finite = member.get("sessions_on_purchase")
        if finite is not None:
            limits = [x for x in limits if x["period"] != "card"]
            limits.append({"count": max(0, int(finite)), "period": "card"})
        quota_known = bool(limits and not saved.get("unsupported") and
                           (current or finite is not None))
        confirmed = list(saved.get('confirmed_category_ids') or []) if current and not saved.get('contradiction') else []
        return {
            **saved, "fingerprint": fingerprint(member), "category_ids": sorted(set(ids)),
            "categories_known": category_known, "quota_known": quota_known,
            "limits": limits, "unmatched": unmatched,
            "confirmed_category_ids": confirmed,
            "state": "ready" if (category_known or confirmed) and quota_known else "needs_review",
            "reason": ("פרטי המנוי השתנו — נדרש אימות מחדש" if saved and not current else
                       "מידע הזכאות התיישן — נדרש רענון או אישור ידני" if stale else
                       "השרת דחה את ההגדרה — נדרש לבדוק את ההתאמה" if saved.get("contradiction") else
                       "נדרשת השלמת סוגי השיעורים המותרים" if not category_known else
                       "נדרשת השלמת המכסה והתקופה" if not quota_known else ""),
        }

    async def refresh(self, memberships: list[dict]) -> None:
        """Read each unchanged membership at most daily, including failed reads."""
        for member in memberships:
            key = self.key(member["id"])
            previous = await self.store.get_meta(key) or {}
            current = previous.get("fingerprint") == fingerprint(member)
            if previous.get('source') == 'manual' and not current:
                # The user confirmed a different membership revision. Preserve
                # the draft and request confirmation, even if shop data exists.
                continue
            if current and time.time() - previous.get("checked_at", 0) < POLICY_TTL:
                continue
            state = dict(previous) if current else {}
            state.update(fingerprint=fingerprint(member), checked_at=time.time())
            # Manual evidence remains explicit; a new membership fingerprint
            # requires a new confirmation instead of inheriting old choices.
            if current and state.get("source") == "manual":
                await self.store.set_meta(key, state)
                continue
            try:
                data = await self.client.membership_details(
                    int(member["membership_type_id"]), int(self.syncer.location_id))
                sections = data.get("limitations")
                if not isinstance(sections, list):
                    raise ArboxError("לא התקבלו מגבלות מנוי מהסטודיו")
                if any(not isinstance(s, dict) or not isinstance(s.get('values'), list) for s in sections):
                    raise ArboxError("הסטודיו החזיר מגבלות בפורמט שלא ניתן לאמת")
                classes = [s for s in sections if normalized(str(s.get("header") or ""))
                           == "available classes"]
                limits, unsupported = parse_limits(sections)
                state.update(source="shop", verified_at=time.time(), limits=limits,
                             unsupported=unsupported, categories_known=bool(classes),
                             category_names=[str(v).strip() for s in classes
                                             for v in s.get("values") or [] if str(v).strip()],
                             evidence=sections, read_error=None)
            except (ArboxError, TypeError, ValueError, KeyError) as err:
                # Failure says nothing about eligibility. Retain prior explicit
                # evidence; show the failed refresh without inventing a whitelist.
                state["read_error"] = str(err)
            await self.store.set_meta(key, state)

    async def learn_rejection(self, member: dict, session: dict, err: ArboxError) -> bool:
        message = err.message("classTypeRestricts")
        if not message:
            return False
        key = self.key(member["id"])
        state = await self.store.get_meta(key) or {}
        value = message.get("value") or {}
        names = [s.strip() for s in str(value.get("allowedText") or "").splitlines() if s.strip()]
        state.update(fingerprint=fingerprint(member), verified_at=time.time(),
                     rejection=message, checked_at=time.time())
        if state.get("source") == "manual":
            state["contradiction"] = True
        else:
            state.update(source="rejection", category_names=names,
                         categories_known=bool(names), contradiction=False)
        denied = set(state.get("denied_category_ids") or [])
        if session.get("category_id") is not None:
            denied.add(session["category_id"])
        state["denied_category_ids"] = sorted(denied)
        await self.store.set_meta(key, state)
        await self.store.set_meta("quota_cache", None)
        return True

    async def confirm_category(self, member: dict, category_id: int, expected_fingerprint: str):
        if expected_fingerprint != fingerprint(member):
            raise ValueError('פרטי המנוי השתנו')
        policy = await self.get(member)
        if (not policy.get('quota_known') or policy.get('contradiction')
                or category_id in policy.get('denied_category_ids', [])
                or category_id not in {c['id'] for c in await self.catalog()}):
            raise ValueError('לא ניתן לאשר את ההתאמה הזו')
        saved = await self.store.get_meta(self.key(member['id'])) or {}
        if saved.get('fingerprint') != expected_fingerprint:
            saved = {'fingerprint': expected_fingerprint}
        saved['confirmed_category_ids'] = sorted(set(policy.get('confirmed_category_ids', [])) | {category_id})
        saved['category_confirmed_at'] = time.time()
        await self.store.set_meta(self.key(member['id']), saved)
        await self.store.set_meta('quota_cache', None)

    async def save_manual(self, member: dict, category_ids: list[int], limits: list[dict],
                          expected_fingerprint: str) -> dict:
        if expected_fingerprint != fingerprint(member):
            raise ValueError("פרטי המנוי השתנו. רעננו ובדקו את ההגדרה שוב")
        known = {c["id"] for c in await self.catalog()}
        if not category_ids or not set(category_ids) <= known:
            raise ValueError("בחרו סוגי שיעורים מהרשימה של הסטודיו הפעיל")
        if not limits and member.get("sessions_on_purchase") is None:
            raise ValueError("נדרשת מכסה מפורשת ותקופה")
        if any(type(x.get("count")) is not int or not 0 < x["count"] <= 10000
               or x.get("period") not in ("day", "week", "month")
               or (x.get("period") == "week" and (type(x.get("week_start")) is not int
                                                    or not 0 <= x["week_start"] <= 6))
               for x in limits):
            raise ValueError("מכסה או תקופה לא תקינות")
        old = await self.store.get_meta(self.key(member["id"])) or {}
        state = {"source": "manual", "fingerprint": fingerprint(member),
                 "category_ids": sorted(set(category_ids)), "categories_known": True,
                 "limits": limits, "verified_at": time.time(), "checked_at": time.time(),
                 "previous_evidence": old.get("rejection") or old.get("evidence")}
        await self.store.set_meta(self.key(member["id"]), state)
        await self.store.set_meta("quota_cache", None)
        return await self.get(member)


def eligible(member: dict, session: dict) -> bool:
    policy = member.get("policy") or {}
    return bool(policy.get("state") == "ready"
                and session.get('category_id') in (policy.get('category_ids', [])
                    if policy.get('categories_known', True) else policy.get('confirmed_category_ids', []))
                and session.get("category_id") not in policy.get("denied_category_ids", []))
