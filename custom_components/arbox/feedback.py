"""Authenticated HA transport for one-workout feedback capabilities.

The browser only talks to HA. Destinations and HTTP methods are fixed here;
neither server addresses nor the integration API key come from the browser.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import voluptuous as vol

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .panel_api import async_register_panel_api

PANEL_PATH = "arbox-feedback"
STATIC_PATH = "/arbox_frontend"
FRONTEND_STATE = "arbox_frontend"
TOKEN = vol.All(str, vol.Match(r"^[A-Za-z0-9_-]{43}$"))


class FeedbackError(Exception):
    """Safe error to display without leaking URLs, credentials or payloads."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def _request(coordinator, token, feedback=None):
    method = "GET" if feedback is None else "PUT"
    try:
        async with coordinator._session.request(
            method, f"{coordinator.base_url}/api/feedback",
            headers={"X-Feedback-Token": token},
            **({"json": feedback} if feedback is not None else {}),
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=False,
        ) as response:
            if response.status in (401, 403):
                raise FeedbackError("invalid_token", "הקישור אינו תקין או שייך לשרת אחר")
            if response.status == 410:
                raise FeedbackError("expired", "תוקף הקישור פג. התחילו בדיקה חדשה או עדכנו את האימון ביומן.")
            if response.status == 422:
                raise FeedbackError("invalid_feedback", "יש לבדוק את הערכים בטופס ולנסות שוב")
            if response.status == 404:
                raise FeedbackError("not_found", "הטופס אינו זמין. ודאו ששרת Arbox מעודכן.")
            if response.status != 200:
                raise FeedbackError("server_error", "שרת Arbox לא הצליח להשלים את הפעולה. נסו שוב.")
            result = await response.json()
            if not isinstance(result, dict):
                raise FeedbackError("server_error", "שרת Arbox החזיר תשובה לא תקינה")
            return result
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise FeedbackError("unavailable", "HA לא הצליח להגיע לשרת Arbox ברשת הפנימית. נסו שוב.") from err


async def _read(hass, token, entry_id=None):
    entries = hass.data.get(DOMAIN, {})
    if entry_id:
        entries = {entry_id: entries[entry_id]} if entry_id in entries else {}
    if not entries:
        raise FeedbackError("not_configured", "יש להגדיר ולהפעיל את אינטגרציית Arbox ב־HA")
    # A notification does not need to know HA's config-entry identifier. Try
    # only configured servers, then bind the save to the matching entry.
    limit = asyncio.Semaphore(4)

    async def probe(key, coordinator):
        async with limit:
            try:
                return key, await _request(coordinator, token)
            except FeedbackError as err:
                return key, err

    results = await asyncio.gather(*(probe(key, value) for key, value in entries.items()))
    for key, result in results:
        if isinstance(result, dict):
            return {**result, "entry_id": key}
    errors = [result for _, result in results]
    for code in ("expired", "unavailable", "not_found", "server_error"):
        if error := next((e for e in errors if e.code == code), None):
            raise error
    raise errors[0]


@websocket_api.websocket_command({
    vol.Required("type"): "arbox/feedback/read",
    vol.Required("token"): TOKEN,
    vol.Optional("entry_id"): str,
})
@websocket_api.async_response
async def ws_read(hass, connection, msg):
    if connection.user is None:
        connection.send_error(msg["id"], "unauthorized", "נדרשת התחברות ל־HA")
        return
    try:
        result = await _read(hass, msg["token"], msg.get("entry_id"))
    except FeedbackError as err:
        connection.send_error(msg["id"], err.code, str(err))
    else:
        connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): "arbox/feedback/save",
    vol.Required("token"): TOKEN,
    vol.Required("entry_id"): str,
    vol.Required("feedback"): dict,
})
@websocket_api.async_response
async def ws_save(hass, connection, msg):
    if connection.user is None:
        connection.send_error(msg["id"], "unauthorized", "נדרשת התחברות ל־HA")
        return
    coordinator = hass.data.get(DOMAIN, {}).get(msg["entry_id"])
    if coordinator is None:
        connection.send_error(msg["id"], "not_configured", "אינטגרציית Arbox אינה זמינה")
        return
    if len(json.dumps(msg["feedback"])) > 1000000:
        connection.send_error(msg["id"], "invalid_feedback", "המשוב ארוך מדי")
        return
    try:
        result = await _request(coordinator, msg["token"], msg["feedback"])
    except FeedbackError as err:
        connection.send_error(msg["id"], err.code, str(err))
    else:
        connection.send_result(msg["id"], result)


async def async_setup_feedback(hass: HomeAssistant):
    state = hass.data.setdefault(FRONTEND_STATE, {"lock": asyncio.Lock()})
    async with state["lock"]:
        if not state.get("registered"):
            await hass.http.async_register_static_paths([
                StaticPathConfig(STATIC_PATH, str(Path(__file__).parent / "frontend"), False),
            ])
            websocket_api.async_register_command(hass, ws_read)
            websocket_api.async_register_command(hass, ws_save)
            async_register_panel_api(hass)
            state["registered"] = True
        if not state.get("panel"):
            await panel_custom.async_register_panel(
                hass, frontend_url_path=PANEL_PATH,
                webcomponent_name="arbox-feedback-panel",
                module_url=f"{STATIC_PATH}/panel.js?v=2.9.0",
                sidebar_title=None,
                require_admin=False,
            )
            await panel_custom.async_register_panel(
                hass, frontend_url_path="arbox",
                webcomponent_name="arbox-app-panel",
                module_url=f"{STATIC_PATH}/app-panel.js?v=3.4.1",
                sidebar_title="Arbox", sidebar_icon="mdi:weight-lifter",
                require_admin=False,
            )
            state["panel"] = True


def async_unload_feedback(hass: HomeAssistant):
    state = hass.data.get(FRONTEND_STATE, {})
    if state.get("panel"):
        frontend.async_remove_panel(hass, PANEL_PATH)
        frontend.async_remove_panel(hass, "arbox")
        state["panel"] = False
