"""Panel authorization, fixed transport, studio binding and concurrent reads."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from custom_components.arbox import panel_api as api
from test_feedback import coordinator, Response


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.entry = SimpleNamespace(domain="arbox", title="My Arbox", options={})
        self.server = coordinator()
        self.hass = SimpleNamespace(data={"arbox": {"one": self.server}},
            config_entries=SimpleNamespace(async_get_entry=lambda key: self.entry if key == "one" else None))
        self.user = SimpleNamespace(id="user", is_active=True, is_admin=False)
        self.conn = SimpleNamespace(user=self.user, send_error=Mock(), send_result=Mock())

    async def test_permissions_admin_view_action_and_unconfigured(self):
        self.assertFalse(api.authorized(self.hass, None, "one"))
        self.assertFalse(api.authorized(self.hass, self.user, "one"))
        self.entry.options[api.VIEW_USERS] = ["user"]
        self.assertTrue(api.authorized(self.hass, self.user, "one"))
        self.assertFalse(api.authorized(self.hass, self.user, "one", True))
        self.entry.options[api.ACTION_USERS] = ["user"]
        self.assertTrue(api.authorized(self.hass, self.user, "one", True))
        self.user.is_admin = True
        self.entry.options = {}
        self.assertTrue(api.authorized(self.hass, self.user, "one", True))
        self.assertFalse(api.authorized(self.hass, self.user, "other"))
        self.user.is_active = False
        self.assertFalse(api.authorized(self.hass, self.user, "one"))

    async def test_entries_do_not_disclose_unauthorized_accounts(self):
        await api.ws_entries.__wrapped__(self.hass, self.conn, {"id": 1})
        self.assertEqual(self.conn.send_result.call_args.args[1], {"entries": []})
        self.entry.options[api.VIEW_USERS] = ["user"]
        await api.ws_entries.__wrapped__(self.hass, self.conn, {"id": 2})
        self.assertEqual(self.conn.send_result.call_args.args[1], {"entries": [
            {"entry_id": "one", "title": "My Arbox", "can_write": False}]})

    async def test_fixed_mutation_endpoint_with_studio_header(self):
        self.entry.options[api.ACTION_USERS] = ["user"]
        await api.ws_action.__wrapped__(self.hass, self.conn, {"id": 1, "entry_id": "one",
            "action": "skip", "studio_id": 8, "data": {"schedule_id": 123}})
        args, kw = self.server._session.request.call_args
        self.assertEqual(args, ("PUT", "http://internal-arbox:8000/api/automations/occurrences/123/skip"))
        self.assertEqual(kw["headers"]["X-Arbox-Studio-Id"], "8")
        self.assertFalse(kw["allow_redirects"])
        self.assertNotIn("must-never-leave-ha", str(self.conn.send_result.call_args))
        self.server._session.request.reset_mock()
        for data in ({"schedule_id": "../../settings"}, {"schedule_id": 1, "url": "http://evil"}):
            await api.ws_action.__wrapped__(self.hass, self.conn, {"id": 1, "entry_id": "one",
                "action": "skip", "studio_id": 8, "data": data})
        self.server._session.request.assert_not_called()

    async def test_read_cannot_trigger_refresh_and_readonly_cannot_mutate(self):
        self.entry.options[api.VIEW_USERS] = ["user"]
        await api.ws_read.__wrapped__(self.hass, self.conn, {"id": 1, "entry_id": "one",
            "resource": "schedule", "params": {"refresh": True}})
        await api.ws_action.__wrapped__(self.hass, self.conn, {"id": 2, "entry_id": "one",
            "action": "refresh", "studio_id": 8, "data": {}})
        self.server._session.request.assert_not_called()
        self.assertEqual(self.conn.send_error.call_args.args[1], "forbidden")

    async def test_coalesced_reads_recheck_revocation(self):
        self.entry.options[api.VIEW_USERS] = ["user"]
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def request(server, method, path, *args, **kwargs):
            calls.append(path)
            if path == "/panel/context":
                await asyncio.sleep(.01)
                return {"studio_id": 8}
            started.set()
            await finish.wait()
            return {"sessions": []}
        msg = {"id": 1, "entry_id": "one", "resource": "me", "params": {}}
        with patch.object(api, "request", request):
            first = asyncio.create_task(api.ws_read.__wrapped__(self.hass, self.conn, msg))
            second = asyncio.create_task(api.ws_read.__wrapped__(self.hass, self.conn, {**msg, "id": 2}))
            await started.wait()
            self.entry.options = {}
            finish.set()
            await asyncio.gather(first, second)
        self.assertEqual(calls.count("/me"), 1)
        self.conn.send_result.assert_not_called()
        self.assertEqual(self.conn.send_error.call_count, 2)

    async def test_timeout_uncertain_without_retry_and_structured_conflict(self):
        self.server._session.request.side_effect = TimeoutError()
        with self.assertRaises(api.PanelError) as raised:
            await api.request(self.server, "POST", "/book", 8, data={"schedule_id": 1})
        self.assertEqual(raised.exception.code, "uncertain")
        self.assertEqual(self.server._session.request.call_count, 1)
        self.server._session.request.side_effect = None
        self.server._session.request.return_value = Response(409, {"detail": {"code": "studio_changed", "studio_id": 9}})
        with self.assertRaises(api.PanelError) as raised:
            await api.request(self.server, "POST", "/book", 8, data={"schedule_id": 1})
        self.assertIn('"studio_changed"', str(raised.exception))


class OptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_options_select_active_users_and_preserve_other_options(self):
        from custom_components.arbox.config_flow import ArboxOptionsFlow
        from unittest.mock import AsyncMock
        entry = SimpleNamespace(options={"other": True, api.VIEW_USERS: ["deleted"]})
        flow = ArboxOptionsFlow()
        flow.handler = "one"
        flow.hass = SimpleNamespace(
            auth=SimpleNamespace(async_get_users=AsyncMock(return_value=[
                SimpleNamespace(id="person", name="Person", is_active=True, system_generated=False),
                SimpleNamespace(id="internal", name="System", is_active=True, system_generated=True)])),
            config_entries=SimpleNamespace(async_get_known_entry=lambda key: entry))
        form = await flow.async_step_init()
        self.assertEqual(form["type"], "form")
        self.assertEqual(form["data_schema"]({})[api.VIEW_USERS], [])
        invalid = await flow.async_step_init({api.VIEW_USERS: ["internal"]})
        self.assertEqual(invalid["errors"]["base"], "invalid_user")
        result = await flow.async_step_init({api.ACTION_USERS: ["person"]})
        self.assertEqual(result["data"], {"other": True, api.VIEW_USERS: [], api.ACTION_USERS: ["person"]})
