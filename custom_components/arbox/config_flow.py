"""Config flow — connect to arbox-server (URL + API key), validated live."""

from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.core import callback
from homeassistant.helpers import selector
from .panel_api import VIEW_USERS, ACTION_USERS

from .const import CONF_API_KEY, CONF_BASE_URL, DEFAULT_BASE_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class ArboxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Arbox (server) config flow."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return ArboxOptionsFlow()


    async def _validate(self, base_url: str, api_key: str) -> dict[str, str]:
        """Prove the server is reachable and the key works. Errors, if any."""
        errors: dict[str, str] = {}
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{base_url}/api/health",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    errors["base"] = "cannot_connect"
                else:
                    health = await resp.json()
                    if not health.get("configured"):
                        errors["base"] = "server_not_configured"
            if not errors:
                # /api/settings is read-only and key-gated — proves the
                # key works before booking services ever depend on it
                async with session.get(
                    f"{base_url}/api/settings",
                    headers={"X-Api-Key": api_key},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 401:
                        errors["base"] = "invalid_auth"
                    elif resp.status != 200:
                        errors["base"] = "cannot_connect"
        except (TimeoutError, aiohttp.ClientError):
            errors["base"] = "cannot_connect"
        return errors

    async def async_step_reconfigure(self, user_input=None):
        """Change the server URL or a rotated API key in place.

        Without this the only way to fix either was to delete the entry and
        add it again, which throws away every entity's area, label and
        customisation — and the v1 migration told users to do exactly that.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            errors = await self._validate(base_url, user_input[CONF_API_KEY])
            if not errors:
                await self.async_set_unique_id(base_url)
                self._abort_if_unique_id_mismatch(reason="wrong_server")
                return self.async_update_reload_and_abort(
                    entry,
                    data={CONF_BASE_URL: base_url,
                          CONF_API_KEY: user_input[CONF_API_KEY]},
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_BASE_URL,
                             default=entry.data.get(CONF_BASE_URL,
                                                    DEFAULT_BASE_URL)): str,
                vol.Required(CONF_API_KEY,
                             default=entry.data.get(CONF_API_KEY, "")): str,
            }),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        """A rejected key sends us here; the form is the reconfigure one."""
        return await self.async_step_reconfigure()

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].rstrip("/")
            api_key = user_input[CONF_API_KEY]
            errors = await self._validate(base_url, api_key)

            if not errors:
                await self.async_set_unique_id(base_url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Arbox",
                    data={CONF_BASE_URL: base_url, CONF_API_KEY: api_key},
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
                vol.Required(CONF_API_KEY): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )


class ArboxOptionsFlow(config_entries.OptionsFlow):
    """Grant individual HA users panel access without sharing the API key."""

    async def async_step_init(self, user_input=None):
        users = [user for user in await self.hass.auth.async_get_users()
                 if user.is_active and not user.system_generated]
        allowed = {user.id for user in users}
        errors = {}
        if user_input is not None:
            if any(set(user_input.get(key, [])) - allowed for key in (VIEW_USERS, ACTION_USERS)):
                errors["base"] = "invalid_user"
            else:
                return self.async_create_entry(title="", data={
                    **self.config_entry.options,
                    VIEW_USERS: user_input.get(VIEW_USERS, []),
                    ACTION_USERS: user_input.get(ACTION_USERS, []),
                })
        options = [{"value": user.id, "label": user.name or user.id} for user in users]
        return self.async_show_form(step_id="init", errors=errors, data_schema=vol.Schema({
            vol.Optional(key, default=[uid for uid in self.config_entry.options.get(key, [])
                                      if uid in allowed]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True, mode="dropdown"))
            for key in (VIEW_USERS, ACTION_USERS)
        }))
