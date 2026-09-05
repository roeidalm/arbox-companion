"""Isolated real-HA panel QA. Synthetic data only; no access to real Arbox.

Run inside official HA image with this repo mounted at /work and map port18123.
Login: panel-demo / panel-demo-only. Disposable HA config lives in /tmp.
"""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from aiohttp import web
from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from custom_components.arbox.feedback import async_setup_feedback


def fixtures():
    today = datetime.now().date()
    def workout(sid, day, name, coach, hour, **extra):
        return dict(schedule_id=sid, date=str(today + timedelta(days=day)),
            start_time=hour, end_time=f'{int(hour[:2])+1:02d}:{hour[3:]}',
            category_name=name, coach_name=coach, free=5, max_users=15,
            registration_open=True, booking_option="insertScheduleUser", **extra)
    rows = [workout(101, 0, "Movement", "נועה", "20:15", user_booked=801),
            workout(102, 1, "Mobility & Strength", "גיל", "18:00", watched=True),
            workout(103, 2, "Movement basics", "רוני", "08:00", planning_source="autobook", autobook_match=True),
            workout(104, 3, "Handstand", "נועה", "19:00"),
            workout(105, 4, "Movement", "נועה", "09:30", automation_skipped=True, autobook_match=True)]
    past = [workout(91, -2, "Movement", "נועה", "09:00", status="attended"),
            workout(92, -5, "Mobility & Strength", "גיל", "18:00", status="attended",
                    class_feedback="positive", coach_feedback="positive", notes="הרגשתי שיפור בתנועה", exercises=[])]
    quota = {"quota": 10, "used": 2, "remaining": 8, "reserved": 1,
             "planned_scheduled": 1, "planned_autobook": 1, "projected_remaining": 5, "available_after_planned": 5,
             "memberships": [{"id": 1, "quota": 10, "used": 2, "reserved": 1, "planned": 2, "available_after_planned": 5}]}
    membership = {"id": 1, "name": "כרטיסיית תנועה", "plan": "כרטיסיית תנועה", "active": True}
    return {
        "panel/context": {"studio_id": 8, "name": "סטודיו לתנועה · הדגמה", "timezone": "Asia/Jerusalem",
                          "last_sync": datetime.now().isoformat(), "capabilities": {"studio_guard": True, "panel_api": 1}},
        "summary": {"timezone": "Asia/Jerusalem", "next_class": rows[0], "membership": membership,
                    "memberships": [membership], "quota": quota, "my_sessions": rows[:1], "week": rows},
        "schedule": {"sessions": rows}, "me": {"sessions": rows[:3] + rows[4:], "membership": membership},
        "quota": quota, "history": {"sessions": past, "stats": {}},
        "journal": {"entries": past, "settings": {"level": "full"}, "catalogue": [
            {"id": "squat", "name": "סקוואט", "metric_type": "strength"}], "metric_labels": {"strength": "כוח"}},
        "rules": {"rules": [{"id": 1, "name": "תנועה בבוקר", "enabled": True,
                    "mode": "autobook", "categories": ["Movement"], "coaches": [], "weekdays": [1], "time_from": "08:00", "time_to": "11:00"}]},
        "vacations": {"history": [], "active": [{"id": 1, "date_from": str(today+timedelta(days=14)),
                    "date_to": str(today+timedelta(days=18)), "block_notify": True, "block_autobook": True}]},
        "facets": {"categories": ["Movement", "Mobility & Strength", "Handstand"], "coaches": ["נועה", "רוני", "גיל"]},
        "watchlist": {"watchlist": []},
    }


async def main():
    data = fixtures()
    async def backend(req):
        if req.headers.get("X-Api-Key") != "synthetic-preview":
            return web.json_response({"detail": "unauthorized"}, status=401)
        resource = req.match_info["resource"]
        if req.method != "GET":
            if req.headers.get("X-Arbox-Studio-Id") != "8":
                return web.json_response({"detail": {"code": "studio_changed", "studio_id": 8}}, status=409)
            body = await req.json() if req.can_read_body else {}
            parts = resource.split("/")
            sid = body.get("schedule_id") or next((int(p) for p in parts if p.isdigit()), None)
            rows = data["schedule"]["sessions"]
            past = data["history"]["sessions"]
            row = next((r for r in rows + past if r["schedule_id"] == sid), None)
            if resource in ("book", "standby", "cancel") and row:
                if resource == "book":
                    row.update(user_booked=1000+sid, user_in_standby=None, watched=False, booking_option="cancelScheduleUser")
                elif resource == "standby":
                    row.update(user_in_standby=1000+sid, booking_option="cancelWaitList")
                else:
                    row.update(user_booked=None, user_in_standby=None, booking_option="insertScheduleUser")
            elif parts[0] == "watchlist" and row:
                row["watched"] = req.method != "DELETE"
                row["membership_user_id"] = body.get("membership_user_id")
            elif parts[0] == "automations" and row:
                row["automation_skipped"] = req.method == "PUT"
            elif parts[0] in ("journal", "history") and row:
                row.update(body)
            elif parts[0] in ("rules", "vacations"):
                collection = data["rules"]["rules"] if parts[0] == "rules" else data["vacations"]["active"]
                key = sid if req.method == "DELETE" else body.get("id")
                existing = next((r for r in collection if r["id"] == key), None)
                if req.method == "DELETE":
                    if existing: collection.remove(existing)
                elif existing:
                    existing.update(body)
                else:
                    collection.append({**body, "id": max((r["id"] for r in collection), default=0)+1})
            elif resource == "refresh":
                data["panel/context"]["last_sync"] = datetime.now().isoformat()
            else:
                return web.json_response({"detail": "Preview operation unavailable"}, status=404)
            data["me"]["sessions"] = [r for r in rows if r.get("user_booked") or r.get("user_in_standby") or r.get("watched") or r.get("autobook_match")]
            booked = [r for r in rows if r.get("user_booked")]
            data["summary"]["next_class"] = booked[0] if booked else None
            data["summary"]["my_sessions"] = booked
            q = data["quota"]
            q["reserved"] = len(booked)
            q["planned_scheduled"] = sum(bool(r.get("watched")) for r in rows)
            q["planned_autobook"] = sum(bool(r.get("autobook_match") and not r.get("automation_skipped") and not r.get("user_booked")) for r in rows)
            q["projected_remaining"] = q["quota"] - q["used"] - q["reserved"] - q["planned_scheduled"] - q["planned_autobook"]
            return web.json_response({"ok": True, "entry": row})
        if resource not in data:
            return web.json_response({"detail": "not found"}, status=404)
        return web.json_response(data[resource])
    app = web.Application()
    app.router.add_route('*', '/api/{resource:.*}', backend)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 18124).start()
    with tempfile.TemporaryDirectory(prefix='arbox-ha-preview-') as directory:
        hass = HomeAssistant(directory)
        loader.async_setup(hass)
        hass.config.skip_pip = True
        await Store(hass, 4, "onboarding").async_save({"done": ["user", "core_config", "integration", "analytics"]})
        config = {"homeassistant": {"name": "Arbox Preview", "time_zone": "Asia/Jerusalem"},
                  "http": {"server_host": "0.0.0.0", "server_port": 18123}, "frontend": {}}
        await bootstrap.async_from_config_dict(config, hass)
        user = await hass.auth.async_create_user("Panel Demo", group_ids=["system-admin"])
        provider = hass.auth.get_auth_provider("homeassistant", None)
        await provider.async_initialize()
        await hass.async_add_executor_job(provider.data.add_auth, 'panel-demo', 'panel-demo-only')
        credentials = await provider.async_get_or_create_credentials({"username": "panel-demo"})
        await hass.auth.async_link_user(user, credentials)
        entry = SimpleNamespace(domain="arbox", title="Arbox · הדגמה", options={})
        original_get = hass.config_entries.async_get_entry
        hass.config_entries.async_get_entry = lambda key: entry if key == 'panel-demo' else original_get(key)
        hass.data["arbox"] = {"panel-demo": SimpleNamespace(base_url="http://127.0.0.1:18124",
            _api_key="synthetic-preview", _session=async_get_clientsession(hass))}
        await async_setup_feedback(hass)
        await hass.async_start()
        print("HA preview ready: http://localhost:18123/arbox — panel-demo / panel-demo-only", flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await hass.async_stop()
            await runner.cleanup()


asyncio.run(main())
