"""Exercise HA's actual ConfigEntry setup, options dispatch and reload APIs."""
import tempfile
import unittest

from aiohttp import web
from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.data_entry_flow import InvalidData

from custom_components.arbox.panel_api import VIEW_USERS, ACTION_USERS


class PanelInstallationTests(unittest.IsolatedAsyncioTestCase):
    async def test_install_options_and_unload_reload(self):
        async def summary(request):
            return web.json_response({"timezone": "Asia/Jerusalem", "membership": {},
                "my_sessions": [], "week": [], "journal": {}, "quota": {}, "next_class": None})
        backend = web.Application()
        backend.router.add_get('/api/summary', summary)
        runner = web.AppRunner(backend)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 18125)
        await site.start()
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            loader.async_setup(hass)
            hass.config.skip_pip = True
            try:
                await bootstrap.async_from_config_dict({"frontend": {}, "http": {
                    "server_host": "127.0.0.1", "server_port": 18126}}, hass)
                await hass.auth.async_create_user('Owner', group_ids=['system-admin'])
                user = await hass.auth.async_create_user('Reader', group_ids=['system-users'])
                entry = ConfigEntry(domain='arbox', title='Installation test',
                    data={'base_url': 'http://127.0.0.1:18125', 'api_key': 'synthetic'},
                    options={'keep_option': True}, source='user', version=2, minor_version=1,
                    unique_id='test-install', discovery_keys={}, subentries_data=[])
                await hass.config_entries.async_add(entry)
                self.assertEqual(entry.state, ConfigEntryState.LOADED)
                self.assertIn(entry.entry_id, hass.data['arbox'])
                self.assertIn('arbox', hass.data['frontend_panels'])
                self.assertIn('arbox-feedback', hass.data['frontend_panels'])
                flow = await hass.config_entries.options.async_init(entry.entry_id)
                self.assertEqual(flow['step_id'], 'init')
                with self.assertRaises(InvalidData):
                    await hass.config_entries.options.async_configure(flow['flow_id'], {
                        VIEW_USERS: ['missing-user'], ACTION_USERS: []})
                result = await hass.config_entries.options.async_configure(flow['flow_id'], {
                    VIEW_USERS: [user.id], ACTION_USERS: []})
                self.assertEqual(result['type'], 'create_entry')
                self.assertEqual(entry.options[VIEW_USERS], [user.id])
                self.assertTrue(entry.options['keep_option'])
                self.assertTrue(await hass.config_entries.async_unload(entry.entry_id))
                self.assertNotIn('arbox', hass.data['frontend_panels'])
                self.assertNotIn('arbox-feedback', hass.data['frontend_panels'])
                self.assertTrue(await hass.config_entries.async_setup(entry.entry_id))
                self.assertIn('arbox', hass.data['frontend_panels'])
                self.assertEqual(entry.options[VIEW_USERS], [user.id])
            finally:
                await hass.async_stop()
                await runner.cleanup()
