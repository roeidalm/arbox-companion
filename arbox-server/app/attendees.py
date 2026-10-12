"""Project the member-visible roster without persisting other members' data."""

from urllib.parse import urlsplit

from .arbox_client import ArboxError


def avatar_url(value) -> str | None:
    # Both studios return public images from Arbox's Cloudinary account.
    # Keep this in step with index.html's image CSP; never expose arbitrary
    # upstream URLs as browser requests or fetch them with account credentials.
    if (not isinstance(value, str) or len(value) > 2048
            or any(char.isspace() or ord(char) < 32 or char == "\\" for char in value)):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.netloc == "res.cloudinary.com"
                and url.path.startswith("/arbox/image/upload/")
                and not url.fragment):
            return value
    except ValueError:
        pass
    return None


def roster(session: dict) -> dict:
    # betweenDates supplies location visibility flags on each session. Respect
    # whoIsBooked even if Arbox includes names in the same response.
    flags = [*(session.get("disable_pages_app") or []),
             *((session.get("locations_box") or {}).get("disable_pages_app") or [])]
    registered = session.get("registered")
    registered = registered if type(registered) is int and registered >= 0 else None
    result = {"registered": registered, "attendees": []}
    if any(isinstance(flag, dict) and flag.get("area") == "schedule"
           and flag.get("section_name") == "whoIsBooked" for flag in flags):
        return {**result, "status": "hidden"}
    # Both arrays were verified against Moveom and Rashty on 2026-10-12.
    # Prefer the explicitly provided booked_users, including an empty array.
    users = session.get("booked_users", session.get("schedule_user"))
    if users is None:
        return {**result, "status": "unavailable"}
    if not isinstance(users, list) or any(not isinstance(user, dict) for user in users):
        raise ArboxError("Attendee response had an unexpected shape")
    seen = set()
    for user in users:
        if user.get("late_cancellation"):
            continue
        identity = user.get("schedule_user_id") or user.get("id")
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
        name = user.get("full_name")
        if not isinstance(name, str) or not name.strip() or "None" in name.split():
            name = " ".join(part.strip() for part in
                            (user.get("first_name"), user.get("last_name"))
                            if isinstance(part, str) and part.strip() and part != "None")
        if name.strip():
            person = {"name": name.strip()}
            image = avatar_url(user.get("image"))
            if image:
                person["image_url"] = image
            result["attendees"].append(person)
    # Missing/withheld names must never look like an empty class.
    status = "available" if result["attendees"] or registered == 0 else "unavailable"
    return {**result, "status": status}


async def load_attendees(client, *, studio_id, location_id, session):
    rows = await client.schedule_between(studio_id, location_id, session["date"], session["date"])
    if not isinstance(rows, list):
        raise ArboxError("Schedule response had an unexpected shape")
    fresh = next((row for row in rows if isinstance(row, dict)
                  and row.get("id") == session["schedule_id"]), None)
    if fresh is None:
        return None
    if fresh.get("box_fk") != studio_id or fresh.get("locations_box_fk") != location_id:
        raise ArboxError("Attendee response belonged to a different studio")
    return {"studio_id": studio_id, "schedule_id": session["schedule_id"], **roster(fresh)}
