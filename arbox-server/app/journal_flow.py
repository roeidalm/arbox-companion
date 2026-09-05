"""Shared notification content for real workout journals and rehearsals."""


def feedback_buttons(cid: str, *, coach: bool = True) -> list[list[dict]]:
    prefix = "journal_coach" if coach else "journal_class"
    return [[
        {"text": "🙂 התחברתי" if coach else "🙂 נהניתי", "data": f"{prefix}_pos:{cid}"},
        {"text": "😐 ניטרלי", "data": f"{prefix}_neutral:{cid}"},
        {"text": "🙁 פחות", "data": f"{prefix}_neg:{cid}"},
    ], [
        {"text": "לא רלוונטי", "data": f"{prefix}_na:{cid}"},
        {"text": "דלג", "data": f"journal_skip:{cid}"},
    ]]


def notes_button(cid: str) -> dict:
    return {"text": "🏋️ מה עשיתי", "data": f"journal_notes:{cid}",
            "text_input": True, "placeholder": "סקוואט 60 ק״ג 3x8; מתח 3x6"}


def notes_prompt(cid: str) -> tuple[str, list[list[dict]]]:
    return "רוצה גם לתעד מה עשית?", [[
        notes_button(cid), {"text": "דלג", "data": f"journal_skip:{cid}"},
    ]]


def initial_prompt(session: dict, level: str, cid: str) -> tuple[str, list[list[dict]]]:
    title = (f"📝 איך היה האימון?\n{session.get('category_name') or 'שיעור'}"
             + (f" · {session['coach_name']}" if session.get("coach_name") else ""))
    if level == "full":
        return title, [[
            {"text": "🙂 משוב", "data": f"journal_feedback:{cid}"},
            notes_button(cid), {"text": "דלג", "data": f"journal_skip:{cid}"},
        ]]
    title += "\nעל המאמן/ת:" if session.get("coach_name") else "\nעל השיעור:"
    return title, feedback_buttons(cid, coach=bool(session.get("coach_name")))
