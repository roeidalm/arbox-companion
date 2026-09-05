"""Exercise the real HA login gate, panel registration and WebSocket dispatch."""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant import loader, bootstrap
from homeassistant.setup import async_setup_component

from custom_components.arbox.feedback import async_setup_feedback
from test_feedback import Response, TOKEN


class FeedbackWebSocketTest(unittest.IsolatedAsyncioTestCase):
    async def test_panel_and_authenticated_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            loader.async_setup(hass)
            hass.config.skip_pip = True
            config = {"http": {"server_host": "127.0.0.1", "server_port": 18123}, "frontend": {}}
            try:
                self.assertIsNotNone(await bootstrap.async_from_config_dict(config, hass))
                self.assertIn("frontend", hass.config.components)
                self.assertTrue(await async_setup_component(hass, "websocket_api", config))
                request = Mock(return_value=Response(data={"demo": True, "complete": False}))
                hass.data["arbox"] = {"test": SimpleNamespace(
                    base_url="http://internal-arbox:8000", _api_key="internal-test-key", _session=SimpleNamespace(request=request),
                )}
                await async_setup_feedback(hass)
                await hass.async_start()
                await hass.auth.async_create_user("Owner", group_ids=["system-admin"])
                user = await hass.auth.async_create_user("Feedback test", group_ids=["system-users"])
                refresh = await hass.auth.async_create_refresh_token(user, client_id="http://127.0.0.1:18123/")
                access = hass.auth.async_create_access_token(refresh)
                async with aiohttp.ClientSession() as client:
                    async with client.get("http://127.0.0.1:18123/arbox_frontend/panel.js") as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn("arbox/feedback/read", await response.text())
                    async with client.ws_connect("http://127.0.0.1:18123/api/websocket") as ws:
                        self.assertEqual((await ws.receive_json())["type"], "auth_required")
                        await ws.send_json({"type": "auth", "access_token": access})
                        self.assertEqual((await ws.receive_json())["type"], "auth_ok")
                        await ws.send_json({"id": 1, "type": "arbox/feedback/read", "token": TOKEN})
                        result = await ws.receive_json()
                        self.assertTrue(result["success"], result)
                        self.assertEqual(result["result"]["entry_id"], "test")
                        await ws.send_json({"id": 2, "type": "arbox/feedback/save", "token": TOKEN,
                                            "entry_id": "test", "feedback": {"notes": "Test"}})
                        self.assertTrue((await ws.receive_json())["success"])
                        self.assertEqual(request.call_args.args[0], "PUT")
                        # Same real authenticated socket: ordinary users see no
                        # full account until granted access by an administrator.
                        entry = SimpleNamespace(domain="arbox", title="Panel test", options={})
                        original_get = hass.config_entries.async_get_entry
                        hass.config_entries.async_get_entry = lambda key: entry if key == "test" else original_get(key)
                        request.reset_mock()
                        await ws.send_json({"id": 3, "type": "arbox/panel/entries"})
                        self.assertEqual((await ws.receive_json())["result"], {"entries": []})
                        await ws.send_json({"id": 4, "type": "arbox/panel/read", "entry_id": "test", "resource": "me"})
                        self.assertEqual((await ws.receive_json())["error"]["code"], "forbidden")
                        request.assert_not_called()
                        entry.options = {"panel_view_users": [user.id]}
                        request.return_value = Response(data={"studio_id": 8, "sessions": []})
                        await ws.send_json({"id": 5, "type": "arbox/panel/read", "entry_id": "test", "resource": "me"})
                        panel_result = await ws.receive_json()
                        self.assertTrue(panel_result["success"], panel_result)
                        self.assertFalse(panel_result["result"]["can_write"])
                        self.assertEqual(request.call_args.kwargs["headers"]["X-Arbox-Studio-Id"], "8")
                        await ws.send_json({"id": 6, "type": "arbox/panel/action", "entry_id": "test", "action": "refresh", "studio_id": 8})
                        self.assertEqual((await ws.receive_json())["error"]["code"], "forbidden")
                        entry.options = {"panel_action_users": [user.id]}
                        await ws.send_json({"id": 7, "type": "arbox/panel/action", "entry_id": "test", "action": "refresh", "studio_id": 8})
                        self.assertTrue((await ws.receive_json())["success"])
                        await ws.send_json({"id": 8, "type": "arbox/panel/action", "entry_id": "test", "action": "refresh"})
                        self.assertEqual((await ws.receive_json())["error"]["code"], "invalid_format")
                        hass.config_entries.async_get_entry = original_get
                    request.reset_mock()
                    async with client.ws_connect("http://127.0.0.1:18123/api/websocket") as ws:
                        await ws.receive_json()
                        await ws.send_json({"id": 1, "type": "arbox/feedback/read", "token": TOKEN})
                        self.assertEqual((await ws.receive_json())["type"], "auth_invalid")
                        request.assert_not_called()
            finally:
                await hass.async_stop()


if __name__ == "__main__":
    unittest.main()
