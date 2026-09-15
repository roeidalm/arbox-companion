"""Calendar monitoring entities and propagation of failed syncs."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError
from custom_components.arbox.button import ArboxGoogleCalendarSyncButton
from custom_components.arbox.sensor import ArboxGoogleCalendarSensor
from custom_components.arbox.coordinator import ArboxCoordinator


class GoogleCalendarTests(unittest.IsolatedAsyncioTestCase):
    def coordinator(self):
        return SimpleNamespace(data={'google_calendar': {'state':'active', 'enabled':True,
            'connected':True, 'last_sync':'2026-09-15T12:00:00+03:00', 'error':None,
            'event_count':9, 'settings_url':'https://example.test/settings?settings=calendar'}},
            config_entry=None, last_update_success=True, base_url='https://example.test',
            async_add_listener=Mock(), async_request_refresh=AsyncMock(),
            sync_google_calendar=AsyncMock())

    async def test_entity_state_availability_and_action(self):
        c=self.coordinator();sensor=ArboxGoogleCalendarSensor(c,'test');button=ArboxGoogleCalendarSyncButton(c,'test')
        self.assertEqual(sensor.native_value,'active')
        self.assertEqual(sensor.extra_state_attributes['event_count'],9)
        self.assertTrue(button.available)
        await button.async_press();c.sync_google_calendar.assert_awaited_once()
        c.data['google_calendar']['enabled']=False
        self.assertFalse(button.available)
        c.data={}
        self.assertIsNone(sensor.native_value)
        self.assertFalse(button.available)

    async def test_setup_removes_legacy_cancel_button_and_never_recreates_it(self):
        from custom_components.arbox.button import async_setup_entry
        c=self.coordinator()
        hass=SimpleNamespace(data={'arbox':{'test':c}})
        registry=Mock()
        registry.async_get_entity_id.return_value='button.arbox_cancel_next_class'
        add=Mock()
        with patch('custom_components.arbox.button.er.async_get',return_value=registry):
            await async_setup_entry(hass,SimpleNamespace(entry_id='test'),add)
        registry.async_get_entity_id.assert_called_once_with('button','arbox','test_cancel_next')
        registry.async_remove.assert_called_once_with('button.arbox_cancel_next_class')
        self.assertEqual([entity.unique_id for entity in add.call_args.args[0]],
                         ['test_refresh','test_google_calendar_sync'])

    async def test_sync_failure_raises_and_refreshes_state(self):
        c=self.coordinator()
        with patch('custom_components.arbox.panel_api.request',AsyncMock(return_value={'error':'Google rejected access'})):
            with self.assertRaisesRegex(HomeAssistantError,'Google rejected access'):
                await ArboxCoordinator.sync_google_calendar(c)
        c.async_request_refresh.assert_awaited_once()
