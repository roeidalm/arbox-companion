"""One ledger for display and booking gates; no upstream IO or plan-name guesses."""
from __future__ import annotations

from .membership_policy import eligible, period_bounds


REASONS = {
    "needs_review": "דורש השלמה — ההרשמה האוטומטית מושהית",
    "no_membership": "אין מנוי מאומת שמתאים לאימון בתאריך הזה",
    "no_capacity": "המכסה של המנוי המתאים מלאה — ההרשמה מושהית",
    "unattributed": "יש אימונים שטרם שויכו למנוי — נדרש סנכרון ובירור",
    "uncertain": "תוצאת ההזמנה לא ידועה — ממתינים לבירור, ללא ניסיון נוסף",
    "session_changed": "פרטי האימון השתנו — ההרשמה מושהית עד לאישור מחדש",
}


def valid(member, day):
    return bool(member.get("active") and (not member.get("start") or member["start"] <= day)
                and (not member.get("end") or member["end"] >= day))


def plan_quota(members: list[dict], commitments: list[dict], plans: list[dict],
               month: str, uncertain: dict | None = None, *, anchor: str | None = None) -> dict:
    """Reserve each intent once. Card capacity spans months and the whole card.

    Unknown actual attribution is never assigned to a preferred card. It stops
    new allocations until reconciled. Waiting lists conservatively reserve
    capacity too: their promotion is controlled by the studio, not this server.
    """
    uncertain = uncertain or {}
    by_id = {m["id"]: m for m in members}
    ledger = {m["id"]: [] for m in members}
    unmatched = [r for r in commitments if r.get("membership_user_id") not in by_id]
    for row in commitments:
        mid = row.get("membership_user_id")
        if mid in ledger:
            ledger[mid].append(row)

    def remaining(member, session):
        mid = member["id"]
        values = []
        for limit in member.get("policy", {}).get("limits", []):
            start, end = period_bounds(session["date"], limit["period"], member,
                                       limit.get("week_start", 0))
            count = sum(start <= r["date"] <= end for r in ledger[mid])
            values.append(limit["count"] - count)
            if limit["period"] == "card" and member.get("sessions_left") is not None:
                # Arbox's balance has already deducted existing reservations.
                pending = sum(r.get("commitment") in ("planned", "standby", "uncertain")
                              for r in ledger[mid] if start <= r["date"] <= end)
                values.append(int(member["sessions_left"]) - pending)
        return min(values) if values else 0

    states, allocations = {}, {}
    unique = {int(p["schedule_id"]): p for p in plans}

    def candidates(plan):
        options = [m for m in members if valid(m, plan['date']) and eligible(m, plan)
                   and plan.get('membership_user_id') in (None, m['id'])]
        return sorted(options, key=lambda m: (len(m['policy']['category_ids']),
                                             m.get('end') or '9999-12-31', m['id']))

    def assign(plan, seen, budget):
        """Augment earlier automatic allocations instead of wasting capacity.

        Only tentative plans move. Actual bookings and explicit choices stay
        fixed. The search budget bounds unusual studios with very large plans.
        """
        sid = int(plan['schedule_id'])
        if sid in seen or budget[0] <= 0:
            return None
        budget[0] -= 1
        seen = seen | {sid}
        options = candidates(plan)
        chosen = next((m for m in options if remaining(m, plan) > 0), None)
        if chosen is None:
            for m in options:
                for old in list(ledger[m['id']]):
                    old_sid = int(old['schedule_id'])
                    if (old.get('commitment') != 'planned' or old_sid in seen
                            or unique[old_sid].get('membership_user_id') is not None):
                        continue
                    ledger[m['id']].remove(old)
                    placeholder = {**plan, 'commitment':'planned', 'membership_user_id':m['id']}
                    if remaining(m, plan) > 0:
                        ledger[m['id']].append(placeholder)
                        moved = assign(unique[old_sid], seen, budget)
                        ledger[m['id']].remove(placeholder)
                        if moved is not None:
                            chosen = m
                            break
                    ledger[m['id']].append(old)
                if chosen is not None:
                    break
        if chosen is None:
            return None
        mid = chosen['id']
        ledger[mid].append({**plan, 'commitment':'planned', 'membership_user_id':mid})
        allocations[str(sid)] = mid
        states[str(sid)] = {'state':'ready', 'membership_user_id':mid, 'reason':'מכוסה במנוי ובמכסה'}
        return mid

    for sid, plan in unique.items():
        if plan.get('intent_change') and str(sid) not in uncertain:
            states[str(sid)] = {**plan['intent_change'], 'membership_user_id':plan.get('membership_user_id')}
            continue
        possible = [m for m in members if valid(m, plan["date"])]
        explicit = plan.get("membership_user_id")
        if explicit is not None:
            possible = [m for m in possible if m["id"] == explicit]
        allowed = [m for m in possible if eligible(m, plan)]
        allowed.sort(key=lambda m: (len(m["policy"].get("category_ids", [])),
                                   m.get("end") or "9999-12-31", m["id"]))
        # An old unknown workout in another month should not stop this month.
        unknown_here = any(r["date"][:7] == plan["date"][:7] or any(
            x["period"] == "card" and (not m.get("start") or r["date"] >= m["start"])
            and (not m.get("end") or r["date"] <= m["end"])
            for m in allowed for x in m["policy"]["limits"]) for r in unmatched)
        reason = "uncertain" if str(sid) in uncertain else "unattributed" if unknown_here else None
        chosen = assign(plan, set(), [1000]) if not reason else None
        if chosen is None:
            reason = reason or ("no_capacity" if allowed else "needs_review" if any(
                m.get("policy", {}).get("state") != "ready" or
                m.get("policy", {}).get("unmatched") for m in possible) else "no_membership")
            states[str(sid)] = {"state": reason, "reason": REASONS[reason],
                                "membership_user_id": explicit}

    details = []
    anchor = anchor or month + "-01"
    for member in members:
        policy = member.get("policy", {})
        limits = policy.get("limits", [])
        if ((member.get("end") and member["end"][:7] < month)
                or (member.get("start") and member["start"][:7] > month)):
            continue
        primary = next((x for x in limits if x["period"] == "card"), None) or next(
            (x for x in limits if x["period"] == "month"), None) or next(iter(limits), None)
        period = primary["period"] if primary else "month"
        start, end = period_bounds(anchor, period, member, (primary or {}).get("week_start", 0))
        rows = [r for r in ledger[member["id"]] if start <= r["date"] <= end]
        counts = {key: sum(r["commitment"] == key for r in rows)
                  for key in ("used", "reserved", "standby", "planned", "uncertain")}
        total = primary["count"] if primary else None
        if period == "card" and member.get("sessions_left") is not None and total is not None:
            counts["used"] = max(counts["used"], total - int(member["sessions_left"]) - counts["reserved"])
        available = max(0, total - counts["used"] - counts["reserved"] - counts["standby"] - counts["uncertain"]) if total is not None else None
        if period == "card" and member.get("sessions_left") is not None and available is not None:
            available = min(available, max(0, int(member["sessions_left"]) - counts["standby"]))
        details.append({**member, **counts, "quota": total, "quota_source": policy.get("source", "unknown"),
                        "period": period, "period_start": start, "period_end": end,
                        "pending_standby": counts["standby"], "available": available,
                        "available_after_planned": max(0, available - counts["planned"]) if available is not None else None})
    displayed = [p for p in unique.values() if p["date"][:7] == month]
    unresolved = [p["schedule_id"] for p in displayed if states[str(p["schedule_id"])]["state"] in
                  ("needs_review", "unattributed", "uncertain", "session_changed")]
    uncovered = [p["schedule_id"] for p in displayed if states[str(p["schedule_id"])]["state"] in
                 ("no_capacity", "no_membership")]
    actual = [r for r in commitments if r["date"][:7] == month]
    return {
        "month": month, "quota": sum(d["quota"] or 0 for d in details), "quota_source": "memberships",
        "mixed_periods": len({d["period"] for d in details}) > 1,
        "used": sum(r["commitment"] == "used" for r in actual),
        "reserved": sum(r["commitment"] == "reserved" for r in actual),
        "pending_standby": sum(r["commitment"] == "standby" for r in actual),
        "planned": sum(str(p["schedule_id"]) in allocations for p in displayed),
        "planned_total": len(displayed),
        "planned_scheduled": sum(p.get("planning_source") == "scheduled" for p in displayed),
        "planned_autobook": sum(p.get("planning_source") == "autobook" for p in displayed),
        "remaining": sum(d["available"] or 0 for d in details),
        "available_after_planned": sum(d["available_after_planned"] or 0 for d in details),
        "memberships": details, "plan_allocations": allocations, "plan_states": states,
        "uncovered_plans": uncovered, "unresolved_plans": unresolved,
        "unattributed_sessions": [r["schedule_id"] for r in unmatched if r["date"][:7] == month],
        "overcommitted": bool(uncovered or any(d["quota"] is not None and
                              d["used"] + d["reserved"] + d["pending_standby"] > d["quota"] for d in details)),
    }
