"""Run against real Home Assistant libraries, with isolated HTTP transports."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import voluptuous as vol

from custom_components.arbox import feedback as api

TOKEN = "a" * 43


class Response:
    def __init__(self, status=200, data=None):
        self.status = status
        self.data = data or {"demo": True, "complete": False}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.data


def coordinator(status=200, data=None):
    return SimpleNamespace(
        base_url="http://internal-arbox:8000",
        _api_key="must-never-leave-ha",
        _session=SimpleNamespace(request=Mock(return_value=Response(status, data))),
    )


def connection(user=True):
    return SimpleNamespace(user=object() if user else None, send_result=Mock(), send_error=Mock())


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_fixed_internal_endpoint_and_only_capability_header(self):
        server = coordinator()
        await api._request(server, TOKEN)
        args, kwargs = server._session.request.call_args
        self.assertEqual(args, ("GET", "http://internal-arbox:8000/api/feedback"))
        self.assertEqual(kwargs["headers"], {"X-Feedback-Token": TOKEN})
        self.assertFalse(kwargs["allow_redirects"])
        await api._request(server, TOKEN, {"notes": "hello"})
        args, kwargs = server._session.request.call_args
        self.assertEqual(args[0], "PUT")
        self.assertEqual(kwargs["json"], {"notes": "hello"})

    async def test_multiple_servers_resolve_by_token_and_bind_save(self):
        first, second = coordinator(401), coordinator()
        hass = SimpleNamespace(data={"arbox": {"first": first, "second": second}})
        conn = connection()
        await api.ws_read.__wrapped__(hass, conn, {"id": 1, "token": TOKEN})
        result = conn.send_result.call_args.args[1]
        self.assertEqual(result["entry_id"], "second")
        first._session.request.reset_mock()
        second._session.request.reset_mock()
        await api.ws_save.__wrapped__(hass, conn, {
            "id": 2, "token": TOKEN, "entry_id": "second", "feedback": {"notes": "test"},
        })
        first._session.request.assert_not_called()
        self.assertEqual(second._session.request.call_args.args[0], "PUT")

    async def test_unauthenticated_or_unloaded_entry_never_reaches_server(self):
        server = coordinator()
        hass = SimpleNamespace(data={"arbox": {"one": server}})
        conn = connection(False)
        await api.ws_read.__wrapped__(hass, conn, {"id": 1, "token": TOKEN})
        await api.ws_save.__wrapped__(hass, conn, {
            "id": 2, "token": TOKEN, "entry_id": "one", "feedback": {},
        })
        self.assertEqual(conn.send_error.call_count, 2)
        server._session.request.assert_not_called()
        conn = connection()
        await api.ws_save.__wrapped__(hass, conn, {
            "id": 3, "token": TOKEN, "entry_id": "missing", "feedback": {},
        })
        self.assertEqual(conn.send_error.call_args.args[1], "not_configured")
        server._session.request.assert_not_called()

    async def test_expiry_and_errors_are_safe_and_retryable(self):
        for status, code in ((410, "expired"), (401, "invalid_token"),
                             (422, "invalid_feedback"), (302, "server_error")):
            with self.subTest(status=status), self.assertRaises(api.FeedbackError) as err:
                await api._request(coordinator(status), TOKEN)
            self.assertEqual(err.exception.code, code)
            self.assertNotIn(TOKEN, str(err.exception))
            self.assertNotIn("internal-arbox", str(err.exception))
        server = coordinator()
        server._session.request.side_effect = TimeoutError("secret destination")
        with self.assertRaises(api.FeedbackError) as err:
            await api._request(server, TOKEN)
        self.assertEqual(err.exception.code, "unavailable")
        self.assertNotIn("secret", str(err.exception))

    async def test_schema_rejects_urls_short_tokens_and_extra_fields(self):
        for token in ("", "a" * 42, "https://arbox.example", "a" * 44):
            with self.subTest(token=token), self.assertRaises(vol.Invalid):
                api.ws_read._ws_schema({"id": 1, "type": "arbox/feedback/read", "token": token})
        with self.assertRaises(vol.Invalid):
            api.ws_read._ws_schema({"id": 1, "type": "arbox/feedback/read",
                                    "token": TOKEN, "url": "http://other"})

    async def test_registration_reload_and_multiple_entries(self):
        hass = SimpleNamespace(data={}, http=SimpleNamespace(async_register_static_paths=AsyncMock()))
        with patch.object(api.panel_custom, "async_register_panel", new_callable=AsyncMock) as panel, \
             patch.object(api.websocket_api, "async_register_command") as command, \
             patch.object(api.frontend, "async_remove_panel") as remove:
            await asyncio.gather(api.async_setup_feedback(hass), api.async_setup_feedback(hass))
            hass.http.async_register_static_paths.assert_awaited_once()
            self.assertEqual(command.call_count, 5)
            self.assertEqual(panel.await_count, 2)
            self.assertIsNone(panel.call_args_list[0].kwargs["sidebar_title"])
            self.assertEqual(panel.call_args_list[1].kwargs["sidebar_title"], "Arbox")
            self.assertFalse(panel.call_args.kwargs["require_admin"])
            api.async_unload_feedback(hass)
            self.assertEqual([c.args[1] for c in remove.call_args_list], ["arbox-feedback", "arbox"])
            await api.async_setup_feedback(hass)
            self.assertEqual(panel.await_count, 4)
            hass.http.async_register_static_paths.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
