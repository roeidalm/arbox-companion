"""Short-lived journal rehearsals. No settings, attendance or journal writes."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import secrets
import time

from .journal import parse_exercise_text
from .journal_flow import feedback_buttons, initial_prompt, notes_prompt

DEMO_SESSION = {"category_name": "אימון לדוגמה", "coach_name": "מאמן/ת לדוגמה"}
LABELS = {"pos": "חיובי", "neutral": "ניטרלי", "neg": "פחות", "na": "לא רלוונטי"}


class JournalPreview:
    """One active rehearsal per channel, expiring after 30 minutes/restart.

    Callbacks use the real Telegram/HA transports and shared journal buttons,
    but opaque preview tokens can never enter the production prompt handler.
    """

    def __init__(self, notifier):
        self.notifier = notifier
        self.sessions: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self.expired_text = False

    def _expire(self):
        if any(v["channel"] == "telegram" and v["stage"] == "text"
               and v["expires"] <= time.monotonic() for v in self.sessions.values()):
            self.expired_text = True
        self.sessions = {k: v for k, v in self.sessions.items()
                         if v["expires"] > time.monotonic()}

    async def _send(self, state: dict, title: str, buttons: list[list[dict]]):
        buttons = deepcopy(buttons)
        for row in buttons:
            for button in row:
                button["data"] = button["data"].replace("journal_", "preview_", 1)
        await self.notifier.send_preview(
            state["channel"], "🧪 בדיקה בלבד · לא נשמר ביומן\n" + title, buttons)

    async def start(self, channel: str, level: str):
        if channel not in ("telegram", "ha") or level not in ("quick", "feedback", "full"):
            raise ValueError("בחר/י ערוץ ורמת מעקב פעילה לבדיקה")
        async with self.lock:
            self._expire()
            cid = secrets.token_urlsafe(12)
            state = {"channel": channel, "level": level, "stage": "initial",
                     "expires": time.monotonic() + 1800, "answers": {}}
            title, buttons = initial_prompt(DEMO_SESSION, level, cid)
            await self._send(state, title, buttons)
            self.sessions = {k: v for k, v in self.sessions.items() if v["channel"] != channel}
            self.sessions[cid] = state

    def _finish(self, cid: str, state: dict, skipped=False) -> str:
        self.sessions.pop(cid, None)
        lines = ["🧪 הבדיקה הסתיימה ✓", "לא נשמר דבר ביומן ולא שונו הגדרות."]
        for subject, value in state["answers"].items():
            lines.append(f"{subject}: {value}")
        if skipped:
            lines.append("בחרת לדלג.")
        return "\n".join(lines)

    async def callback(self, action: str, cid: str, channel: str | None,
                       reply_text: str | None = None) -> str:
        async with self.lock:
            self._expire()
            original = self.sessions.get(cid)
            if not original or original["channel"] != channel:
                return "🧪 הבדיקה הסתיימה או פגה — אפשר להתחיל בדיקה חדשה בהגדרות"
            state = deepcopy(original)
            stage = state["stage"]
            if action == "preview_skip":
                return self._finish(cid, state, skipped=True)
            if action == "preview_notes" and state["level"] == "full" and stage in ("initial", "notes", "text"):
                if reply_text and reply_text.strip():
                    return self._notes(cid, state, reply_text)
                if channel == "ha":
                    return "🧪 לא התקבל טקסט. יש לוודא ש־HA מעביר reply_text, ואז לנסות שוב באותו כפתור."
                state["stage"] = "text"
                self.expired_text = False
                self.sessions[cid] = state
                return "🧪 כתבו מה עשיתם, למשל: סקוואט 60 ק״ג 3x8; מתח 3x6. לביטול: /cancel"
            next_cid = secrets.token_urlsafe(12)
            if action == "preview_feedback" and stage == "initial" and state["level"] == "full":
                state["stage"] = "coach"
                title = f"איך היה עם {DEMO_SESSION['coach_name']}?"
                buttons = feedback_buttons(next_cid)
            else:
                subject, _, value = action.removeprefix("preview_").partition("_")
                expected = "class" if stage == "class" else "coach"
                if (subject != expected or value not in LABELS
                        or stage not in ("coach", "class", "initial")
                        or (stage == "initial" and state["level"] == "full")):
                    return "🧪 הכפתור אינו מתאים לשלב הנוכחי בבדיקה"
                state["answers"]["מאמן/ת" if subject == "coach" else "שיעור"] = LABELS[value]
                if subject == "coach" and state["level"] != "quick":
                    state["stage"] = "class"
                    title = "ואיך היה השיעור עצמו?\nאימון לדוגמה"
                    buttons = feedback_buttons(next_cid, coach=False)
                elif subject == "class" and state["level"] == "full":
                    state["stage"] = "notes"
                    title, buttons = notes_prompt(next_cid)
                else:
                    return self._finish(cid, state)
            # Keep the original button usable if transport delivery fails.
            await self._send(state, title, buttons)
            self.sessions.pop(cid, None)
            self.sessions[next_cid] = state
            return "🧪 התשובה התקבלה לבדיקה — ממשיכים בהודעה הבאה"

    def _notes(self, cid: str, state: dict, text: str) -> str:
        text = text.strip()
        if not text or len(text) > 4000:
            return "🧪 יש להזין בין 1 ל־4,000 תווים"
        parsed = parse_exercise_text(text)
        state["answers"]["מה עשיתי"] = text[:3000] + ("…" if len(text) > 3000 else "")
        state["answers"]["תרגילים/שורות שזוהו"] = str(len(parsed))
        return self._finish(cid, state)

    async def message(self, text: str) -> str | None:
        async with self.lock:
            self._expire()
            for cid, state in self.sessions.items():
                if state["channel"] == "telegram" and state["stage"] == "text":
                    if text.strip() == "/cancel":
                        return self._finish(cid, state, skipped=True)
                    return self._notes(cid, state, text)
            if self.expired_text:
                self.expired_text = False
                return "🧪 הבדיקה פגה — הטקסט לא נשמר. אפשר להתחיל בדיקה חדשה בהגדרות."
        return None
