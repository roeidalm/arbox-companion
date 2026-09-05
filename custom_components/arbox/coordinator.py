"""Coordinator — polls arbox-server's /api/summary.

This integration never talks to Arbox itself; the server owns that session
(one upstream client, no token races). Reads here hit the server's SQLite.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_API_KEY, CONF_BASE_URL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class ArboxCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch the combined summary from arbox-server."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=DEFAULT_SCAN_INTERVAL),
            # passed explicitly: HA deprecated the implicit fetch from context
            # in 2024.8 and warns about it on every startup
            config_entry=entry,
        )
        self._base_url: str = entry.data[CONF_BASE_URL].rstrip("/")
        self._api_key: str = entry.data.get(CONF_API_KEY, "")
        self._session = async_get_clientsession(hass)

    @property
    def base_url(self) -> str:
        return self._base_url

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with self._session.get(
                f"{self._base_url}/api/summary",
                # /api/summary carries the membership plan and every booked
                # class, so the server now asks for the key here too
                headers={"X-Api-Key": self._api_key},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    raise ConfigEntryAuthFailed(
                        "arbox-server rejected the API key")
                if resp.status != 200:
                    raise UpdateFailed(f"arbox-server returned {resp.status}")
                return await resp.json()
        except (TimeoutError, aiohttp.ClientError) as err:
            # a timeout is not a ClientError, and a hung server is exactly
            # when this needs to fail as "unavailable" rather than crash
            raise UpdateFailed(f"Cannot reach arbox-server: {err}") from err

    async def _action(
        self, endpoint: str, schedule_id: int, extra: dict[str, Any] | None = None
    ) -> bool:
        """Proxy a booking action to the server."""
        body: dict[str, Any] = {"schedule_id": schedule_id, **(extra or {})}
        try:
            async with self._session.post(
                f"{self._base_url}/api/{endpoint}",
                json=body,
                headers={"X-Api-Key": self._api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.error("%s failed (%s): %s", endpoint, resp.status, text[:200])
                    return False
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("%s request failed: %s", endpoint, err)
            return False
        await self.async_request_refresh()
        return True

    async def refresh_from_arbox(self) -> bool:
        """Ask the server to sync upstream now, then refresh our copy."""
        try:
            async with self._session.post(
                f"{self._base_url}/api/refresh",
                json={},
                headers={"X-Api-Key": self._api_key},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.error("refresh failed (%s)", resp.status)
                    return False
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.error("refresh request failed: %s", err)
            return False
        await self.async_request_refresh()
        return True

    async def book_class(self, schedule_id: int) -> bool:
        return await self._action("book", schedule_id)

    async def cancel_booking(
        self, schedule_id: int, late_cancel: bool = False,
        reason_code: str = "none", reason_text: str | None = None,
    ) -> bool:
        # inside the studio's cancellation window the server requires an
        # explicit late_cancel acknowledgement (409 otherwise)
        return await self._action("cancel", schedule_id, {
            "late_cancel": late_cancel,
            "reason_code": reason_code,
            "reason_text": reason_text,
        })

    async def join_standby(self, schedule_id: int) -> bool:
        return await self._action("standby", schedule_id)
