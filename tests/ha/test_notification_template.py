"""HA must render the relay payload as a dictionary and omit unrelated tags."""
from pathlib import Path
import tempfile
import unittest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template
from homeassistant.util.yaml import load_yaml


class NotificationTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_preserves_actions_and_only_replaces_tagged_notifications(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            hass = HomeAssistant(directory)
            for file in ('arbox-callback-automation.yaml', 'arbox-notification-blueprint.yaml'):
                doc = load_yaml(str(root / 'dashboard' / file))
                automation = doc[0] if isinstance(doc, list) else doc
                template = Template(automation['action'][0]['data']['data'], hass)
                actions = [{'action':'ARBOX_plan:opaque','title':'מנוי','authenticationRequired':True}]
                plain = template.async_render({'trigger':{'json':{'actions':actions}}})
                self.assertEqual(plain, {'actions':actions})
                tagged = template.async_render({'trigger':{'json':{'actions':actions, 'tag':'scope', 'alert_once':True}}})
                self.assertEqual(tagged, {'actions':actions,'tag':'scope','alert_once':True})
