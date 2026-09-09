"""Authenticated, permission-checked fixed API surface for the Arbox panel."""
from __future__ import annotations

import asyncio
import json

import aiohttp
import voluptuous as vol
from homeassistant.components import websocket_api

from .const import DOMAIN

VIEW_USERS = "panel_view_users"
ACTION_USERS = "panel_action_users"
STATE = "arbox_panel_requests"
READS = {
    "context": ("/panel/context", set()),
    "summary": ("/summary", set()), "me": ("/me", set()),
    "mine": ("/me", set()), "quota": ("/quota", set()),
    "schedule": ("/schedule", {"date_from", "date_to", "coach", "category", "mine"}),
    "facets": ("/facets", set()), "history": ("/history", set()),
    "journal": ("/journal", set()), "rules": ("/rules", set()),
    "vacations": ("/vacations", set()), "watchlist": ("/watchlist", set()),
    "membership_policies": ("/membership-policies", set()),
    "calendar_export": ("/calendar/export", {"schedule_id"}),
}
# Method, path, path identifier, permitted JSON fields. Identifiers must be integers.
ACTIONS = {
    "planning_reconcile": ("POST", "/planning/{id}/reconcile", "schedule_id", {"confirm_not_booked"}),
    "membership_policy_save": ("PUT", "/membership-policies/{id}", "membership_id", {"category_ids", "limits", "fingerprint"}),
    "book": ("POST", "/book", None, {"schedule_id", "membership_user_id"}),
    "standby": ("POST", "/standby", None, {"schedule_id", "membership_user_id"}),
    "cancel": ("POST", "/cancel", None, {"schedule_id", "late_cancel", "reason_code", "reason_text"}),
    "refresh": ("POST", "/refresh", None, set()),
    "watch": ("POST", "/watchlist", None, {"schedule_id", "allow_standby", "ignore_vacation", "confirm_over_quota", "membership_user_id"}),
    "unwatch": ("DELETE", "/watchlist/{id}", "schedule_id", set()),
    "watch_membership": ("PUT", "/watchlist/{id}/membership", "schedule_id", {"membership_user_id"}),
    "skip": ("PUT", "/automations/occurrences/{id}/skip", "schedule_id", set()),
    "restore": ("DELETE", "/automations/occurrences/{id}/skip", "schedule_id", set()),
    "attendance": ("PUT", "/history/{id}/attendance", "schedule_id", {"status", "reason_code", "reason_text"}),
    "journal_save": ("PUT", "/journal/{id}", "schedule_id", {"coach_feedback", "class_feedback", "notes", "exercises"}),
    "rule_save": ("POST", "/rules", None, {"id", "name", "enabled", "coaches", "categories", "weekdays", "time_from", "time_to", "mode"}),
    "rule_delete": ("DELETE", "/rules/{id}", "rule_id", set()),
    "vacation_save": ("POST", "/vacations", None, {"id", "date_from", "date_to", "block_notify", "block_autobook"}),
    "vacation_delete": ("DELETE", "/vacations/{id}", "vac_id", set()),
}


class PanelError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def authorized(hass, user, entry_id, write=False):
    if user is None or not user.is_active:
        return False
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        return False
    return user.is_admin or user.id in entry.options.get(ACTION_USERS, []) or (
        not write and user.id in entry.options.get(VIEW_USERS, []))


def require_access(hass, connection, entry_id, write=False):
    if not authorized(hass, connection.user, entry_id, write):
        raise PanelError("forbidden", "אין הרשאה לפעולה זו בחיבור Arbox")
    coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
    if coordinator is None:
        raise PanelError("unavailable", "חיבור Arbox אינו זמין")
    return coordinator


async def request(coordinator, method, path, studio_id=None, params=None, data=None):
    headers = {"X-Api-Key": coordinator._api_key}
    if studio_id is not None:
        headers["X-Arbox-Studio-Id"] = str(studio_id)
    try:
        async with coordinator._session.request(
            method, coordinator.base_url + "/api" + path,
            headers=headers, params=params,
            **({"json": data} if data is not None else {}),
            timeout=aiohttp.ClientTimeout(total=90),
            allow_redirects=False,
        ) as response:
            if response.status in (400, 409, 422):
                body = await response.json()
                detail = body.get("detail", "הפעולה לא הושלמה")
                raise PanelError("conflict" if response.status == 409 else "invalid_input",
                                 json.dumps(detail, ensure_ascii=False))
            if response.status == 404:
                raise PanelError("not_found", "המידע אינו זמין. ודאו ששרת Arbox מעודכן")
            if response.status in (401, 403):
                raise PanelError("server_auth", "יש לבדוק את הגדרת החיבור לשרת Arbox")
            if not 200 <= response.status < 300:
                raise PanelError("unavailable" if method == "GET" else "uncertain",
                                 "השרת לא השלים את הבקשה. רעננו ובדקו את המצב לפני פעולה נוספת")
            return await response.json()
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise PanelError("unavailable" if method == "GET" else "uncertain",
                         "החיבור לשרת הופסק. רעננו ובדקו את המצב לפני פעולה נוספת") from err


async def coalesced(hass, key, factory):
    pending = hass.data.setdefault(STATE, {})
    task = pending.get(key)
    if task is None:
        task = asyncio.create_task(factory())
        pending[key] = task
        def finished(done):
            if pending.get(key) is done:
                pending.pop(key, None)
            # Retrieve exceptions even if all waiting browser clients disconnected.
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
    return await asyncio.shield(task)


@websocket_api.websocket_command({vol.Required("type"): "arbox/panel/entries"})
@websocket_api.async_response
async def ws_entries(hass, connection, msg):
    if connection.user is None:
        connection.send_error(msg["id"], "unauthorized", "נדרשת התחברות ל־HA")
        return
    entries = []
    for entry_id in hass.data.get(DOMAIN, {}):
        if authorized(hass, connection.user, entry_id):
            entry = hass.config_entries.async_get_entry(entry_id)
            entries.append({"entry_id": entry_id, "title": entry.title,
                            "can_write": authorized(hass, connection.user, entry_id, True)})
    connection.send_result(msg["id"], {"entries": entries})


@websocket_api.websocket_command({
    vol.Required("type"): "arbox/panel/read", vol.Required("entry_id"): str,
    vol.Required("resource"): vol.In(READS), vol.Optional("params", default={}): dict,
})
@websocket_api.async_response
async def ws_read(hass, connection, msg):
    try:
        coordinator = require_access(hass, connection, msg["entry_id"])
        path, allowed = READS[msg["resource"]]
        params = msg["params"]
        if (set(params) - allowed or len(json.dumps(params)) > 4096
                or any(type(value) not in (str, bool, int) for value in params.values())):
            raise PanelError("invalid_input", "פרמטרים לא תקינים")
        context = await coalesced(hass, (msg["entry_id"], id(coordinator), "context"),
                                  lambda: request(coordinator, "GET", "/panel/context"))
        if not isinstance(context, dict) or not context.get("studio_id"):
            raise PanelError("studio_unavailable", "אין סטודיו פעיל בשרת")
        key = (msg["entry_id"], id(coordinator), path, context["studio_id"], json.dumps(params, sort_keys=True))
        data = context if path == "/panel/context" else await coalesced(
            hass, key, lambda: request(coordinator, "GET", path, context["studio_id"], params={key: str(value).lower() if isinstance(value, bool) else value
                    for key, value in params.items()}))
        if require_access(hass, connection, msg["entry_id"]) is not coordinator:
            raise PanelError("unavailable", "החיבור השתנה. רעננו את התצוגה")
        connection.send_result(msg["id"], {"data": data, "context": context,
            "can_write": authorized(hass, connection.user, msg["entry_id"], True)})
    except PanelError as err:
        connection.send_error(msg["id"], err.code, str(err))


@websocket_api.websocket_command({
    vol.Required("type"): "arbox/panel/action", vol.Required("entry_id"): str,
    vol.Required("action"): vol.In(ACTIONS),
    vol.Required("studio_id"): vol.All(int, vol.Range(min=1)),
    vol.Optional("data", default={}): dict,
})
@websocket_api.async_response
async def ws_action(hass, connection, msg):
    try:
        coordinator = require_access(hass, connection, msg["entry_id"], True)
        method, path, identifier, fields = ACTIONS[msg["action"]]
        data = dict(msg["data"])
        if len(json.dumps(data)) > 1000000:
            raise PanelError("invalid_input", "הבקשה ארוכה מדי")
        if identifier:
            value = data.pop(identifier, None)
            if type(value) is not int or value < 1:
                raise PanelError("invalid_input", "מזהה לא תקין")
            path = path.format(id=value)
        if set(data) - fields:
            raise PanelError("invalid_input", "שדות לא תקינים")
        result = await request(coordinator, method, path, msg["studio_id"], data=data)
        require_access(hass, connection, msg["entry_id"], True)
        connection.send_result(msg["id"], result)
    except PanelError as err:
        connection.send_error(msg["id"], err.code, str(err))


def async_register_panel_api(hass):
    for command in (ws_entries, ws_read, ws_action):
        websocket_api.async_register_command(hass, command)
