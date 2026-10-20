"""Read member-visible social data; never guess undocumented friendship writes.

The profile contract was observed on 2026-10-20. Numeric relationship status 1
was observed but its meaning has not been independently verified. Keep it
unclassified until a native-client capture establishes the status mapping.
"""
from .arbox_client import ArboxError
from .attendees import avatar_url, roster


def positive_id(value):
    return value if type(value) is int and value > 0 else None


def relationships(profile, studio_id):
    if not isinstance(profile, dict) or not positive_id(profile.get("id")):
        raise ArboxError("Invalid social profile")
    rows = profile.get("friend_connection")
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise ArboxError("Invalid relationship list")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ArboxError("Invalid relationship")
        if row.get("deleted_at"):
            continue
        sender, recipient = row.get("users_id"), row.get("friend_users_id")
        if profile["id"] not in (sender, recipient) or sender == recipient:
            continue
        other = recipient if sender == profile["id"] else sender
        user = row.get("user")
        if (not positive_id(other) or not isinstance(user, dict)
                or user.get("id") != other or other in seen):
            continue
        # This field is a sparse-key object in the native profile, not always a
        # list. Missing membership information must not expose another studio.
        boxes = user.get("users_boxes_active")
        if isinstance(boxes, dict):
            boxes = list(boxes.values())
        if not isinstance(boxes, list) or not any(
            isinstance(box, dict) and box.get("box_fk") == studio_id for box in boxes
        ):
            continue
        name = user.get("full_name")
        if not isinstance(name, str) or not name.strip():
            name = " ".join(value.strip() for value in
                            (user.get("first_name"), user.get("last_name"))
                            if isinstance(value, str) and value.strip())
        if not name.strip():
            continue
        seen.add(other)
        item = {"user_id": other, "name": name.strip(), "status": "unverified"}
        image = avatar_url(user.get("image"))
        if image:
            item["image_url"] = image
        result.append(item)
    return result


def session_connections(row, people, studio_id, location_id):
    """Match IDs only after applying exactly the roster visibility policy."""
    if (not isinstance(row, dict) or row.get("box_fk") != studio_id
            or row.get("locations_box_fk") != location_id
            or roster(row)["status"] != "available"):
        return []
    users = row.get("booked_users", row.get("schedule_user"))
    visible = {user.get("id") for user in users
               if not user.get("late_cancellation") and positive_id(user.get("id"))}
    return [person for person in people if person["user_id"] in visible]


async def load_friends(client, store, *, studio_id, location_id, start, end):
    # Bypass the combined sanitized profile deliberately: use only the owning
    # connection and project the minimum fields here. Never persist profiles.
    profile = await client.for_box(studio_id).profile()
    people = relationships(profile, studio_id)
    result = {
        "studio_id": studio_id, "date_from": start, "date_to": end,
        "status": "available" if people is not None else "unavailable",
        "capabilities": {"manage": False, "search": False, "classified_status": False},
        "people": people or [], "sessions": [],
    }
    if not people:
        return result
    rows = await client.schedule_between(studio_id, location_id, start, end)
    if not isinstance(rows, list):
        raise ArboxError("Invalid social schedule")
    seen = set()
    for row in rows:
        matches = session_connections(row, people, studio_id, location_id)
        if not matches or not positive_id(row.get("id")) or row["id"] in seen:
            continue
        cached = await store.get_session(row["id"])
        # Only offer a route into an existing, studio-scoped booking card.
        if (not cached or cached.get("box_id") != studio_id
                or not start <= str(cached.get("date", "")) <= end
                or cached.get("status") == "cancelled_studio"):
            continue
        seen.add(row["id"])
        result["sessions"].append({
            **{key: cached.get(key) for key in (
                "schedule_id", "date", "start_time", "end_time",
                "category_name", "coach_name")},
            "connections": matches,
        })
    result["sessions"].sort(key=lambda row: (row["date"], row["start_time"] or ""))
    return result
