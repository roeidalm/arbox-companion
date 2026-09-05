"""Single-workout capabilities: no global API key, no chat state machine."""
from __future__ import annotations

import asyncio
from copy import copy
from datetime import date
import json
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .journal import METRIC_LABELS, catalogue, exercise_by_id, resolve_exercise, suggested_pack_ids

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


class Exercise(BaseModel):
    exercise_id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    metric_type: str = "note"
    sets: int | None = Field(None, ge=0, le=100000)
    reps: int | None = Field(None, ge=0, le=100000)
    weight: float | None = Field(None, ge=0, le=1000000, allow_inf_nan=False)
    weight_unit: str = "kg"
    duration_seconds: int | None = Field(None, ge=0, le=100000)
    attempts: int | None = Field(None, ge=0, le=100000)
    distance: float | None = Field(None, ge=0, le=1000000, allow_inf_nan=False)
    distance_unit: str = "m"
    notes: str | None = Field(None, max_length=4000)


class FeedbackBody(BaseModel):
    coach_feedback: str | None = None
    class_feedback: str | None = None
    notes: str | None = Field(None, max_length=4000)
    exercises: list[Exercise] = Field(default_factory=list, max_length=50)

    def cleaned(self):
        allowed = {None, "positive", "neutral", "negative", "not_applicable"}
        if self.coach_feedback not in allowed or self.class_feedback not in allowed:
            raise HTTPException(422, "דירוג לא תקין")
        if not self.coach_feedback and not self.class_feedback and not (self.notes or "").strip() and not self.exercises:
            raise HTTPException(422, "בחרו דירוג או הוסיפו הערה לפני השמירה")
        items = []
        for raw in self.exercises:
            item = raw.model_dump()
            item["name"] = item["name"].strip()
            if not item["name"] or item["metric_type"] not in METRIC_LABELS:
                raise HTTPException(422, "יש לבחור תרגיל וסוג מדידה תקינים")
            if item["weight_unit"] not in ("kg", "lb") or item["distance_unit"] not in ("m", "km"):
                raise HTTPException(422, "יחידת מדידה לא תקינה")
            known = exercise_by_id(item["exercise_id"]) or resolve_exercise(item["name"])
            if known:
                item.update(exercise_id=known["id"], name=known["name"])
            elif item["exercise_id"] and not item["exercise_id"].startswith("custom-"):
                item["exercise_id"] = None
            items.append(item)
        return dict(coach_feedback=self.coach_feedback, class_feedback=self.class_feedback,
                    notes=(self.notes or "").strip() or None, exercises=items)


class FeedbackForm:
    def __init__(self, store, notifier):
        self.store, self.notifier = store, notifier
        self.settings = notifier.settings
        self.lock = asyncio.Lock()

    async def create(self, session: dict, level: str, *, demo=False, channel=None):
        in_ha = channel == "ha" and self.settings.ha.get("feedback_in_ha") is True
        base = self.settings.base_url.rstrip("/")
        parsed = urlsplit(base)
        if not in_ha and (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.fragment or parsed.query):
            raise ValueError("יש להגדיר בהגדרות כתובת שרת שנגישה מהטלפון לפני שליחת משוב")
        token = secrets.token_urlsafe(32)
        payload = {"box_id": session.get("box_id") if session.get("box_id") is not None else self.store.active_box_id, "session": {
            key: session.get(key) for key in ("schedule_id", "category_name", "coach_name", "date", "start_time", "end_time")},
            "level": level, "demo": demo, "expires": time.time() + (1800 if demo else 7 * 86400)}
        await self.store.add_prompt(token, session.get("schedule_id") or 0, "feedback_form",
                                    dry_run=demo, payload=json.dumps(payload))
        return token, f"/arbox-feedback#{token}" if in_ha else f"{base}/feedback#{token}"

    async def start_preview(self, channel, level):
        if channel not in ("telegram", "ha") or level not in ("quick", "feedback", "full"):
            raise ValueError("בחר/י ערוץ ורמת מעקב פעילה לבדיקה")
        token, url = await self.create({"category_name": "Movement & Strength", "coach_name": "מאמן/ת לדוגמה",
                                        "date": date.today().isoformat(), "start_time": "18:00", "end_time": "19:15"}, level, demo=True, channel=channel)
        try:
            await self.notifier.send_preview(channel, "🧪 איך היה האימון?\nבדיקת טופס בלבד — שום תשובה לא תישמר ביומן.",
                                             [[{"text": "מילוי משוב", "uri": url}]])
        except Exception:
            await self.store.delete_prompt(token)
            raise
        return url

    async def resolve(self, token):
        if not token or len(token) > 100:
            raise HTTPException(401, "הקישור חסר או אינו תקין")
        row = await self.store.peek_prompt(token)
        if not row or row["action"] != "feedback_form":
            raise HTTPException(401, "הקישור חסר או אינו תקין")
        payload = json.loads(row["payload"])
        if payload["expires"] < time.time():
            raise HTTPException(410, "תוקף הקישור פג. אפשר למלא משוב דרך יומן האימונים, או להתחיל בדיקה חדשה בהגדרות.")
        # Bind the capability to its original studio without changing the live
        # syncer's selected studio. Store methods all scope by this attribute.
        scoped = copy(self.store)
        scoped.active_box_id = payload["box_id"]
        return row, payload, scoped

    async def read(self, token):
        row, payload, scoped = await self.resolve(token)
        session = payload["session"]
        selected = self.settings.exercise_packs()
        active = selected if selected is not None else suggested_pack_ids([session.get("category_name") or "strength"])
        return {"session": session, "demo": payload["demo"], "level": payload["level"],
                "complete": bool(row["answered_at"]),
                "catalogue": catalogue(active, await scoped.custom_exercises(), self.settings.exercise_shortcuts(), self.settings.hidden_exercises()),
                "metric_labels": METRIC_LABELS}

    async def save(self, token, body):
        values = body.cleaned()
        async with self.lock:
            row, payload, scoped = await self.resolve(token)
            if row["answered_at"]:
                return {"ok": True, "demo": payload["demo"], "already_saved": True}
            if not payload["demo"]:
                sid = row["schedule_id"]
                if not await scoped.get_session(sid) and not await scoped.workout_journal(sid):
                    raise HTTPException(404, "האימון כבר אינו זמין ביומן")
                await scoped.save_workout_journal(sid, **values)
                outcome = await scoped.get_training_outcome(sid)
                if not outcome or outcome.get("status") != "attended":
                    await scoped.set_training_outcome(sid, "attended", "journal")
            await scoped.take_prompt(token)
            return {"ok": True, "demo": payload["demo"]}


def service(request):
    return request.app.state.rules_engine.feedback_form


@router.get("")
async def read_feedback(request: Request, x_feedback_token: str | None = Header(None)):
    return await service(request).read(x_feedback_token)


@router.put("")
async def save_feedback(request: Request, body: FeedbackBody, x_feedback_token: str | None = Header(None)):
    return await service(request).save(x_feedback_token, body)
