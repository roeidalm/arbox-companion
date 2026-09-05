"""The only code in the project that talks to apiappv2.arboxapp.com.

Payload shapes and response structures were verified live on 2026-08-26 —
see the repo README. Notably: schedule/betweenDates takes
{from, to, locations_box_id, boxes_id} (the box_fk/start_date shape that
appears in older notes returns 500), and one call returns a full date range.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://apiappv2.arboxapp.com"

CREDS_FILE = "credentials.json"


class ArboxError(Exception):
    """Upstream call failed. Carries the HTTP status and parsed body."""

    def __init__(self, message: str, status: int | None = None, body: Any = None,
                 transient: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        # Worth trying again: a dropped connection, a timeout, a 5xx. Arbox
        # saying "no" is not transient — but a network blip at the opening
        # moment used to be recorded as a permanent refusal and cost the class.
        self.transient = transient or (status is not None and status >= 500)

    def _first_message(self) -> dict:
        if not isinstance(self.body, dict):
            return {}
        err = self.body.get("error") or {}
        for item in err.get("messageToUser") or []:
            if isinstance(item, dict) and item.get("name"):
                return item
        return {}

    def error_name(self) -> str | None:
        """The upstream error code, e.g. 'registerScheduleDisabled'."""
        return self._first_message().get("name")

    def error_value(self) -> dict:
        """The code's payload — the part that names what was refused.

        classTypeRestricts carries {"class": ..., "membershipTypesName": ...}
        and registerScheduleDisabled carries {"hours": 168}, so the refusal
        identifies itself instead of having to be inferred from the schedule.
        """
        v = self._first_message().get("value")
        return v if isinstance(v, dict) else {}


class ArboxAuthError(ArboxError):
    """Login rejected — bad credentials or revoked tokens."""


class ArboxClient:
    """Single authenticated client for the Arbox API.

    Credentials AND tokens persist in DATA_DIR/credentials.json (0600) so a
    container restart never asks anyone to retype anything.
    """

    def __init__(self, data_dir: str, whitelabel: str = "Arbox") -> None:
        self._data_dir = data_dir
        # the studio's whitelabel (branded app name). Set per-install at
        # setup time and persisted with the credentials — this integration
        # is not tied to any one studio.
        self._whitelabel = whitelabel
        self._session: aiohttp.ClientSession | None = None
        self._login_lock = asyncio.Lock()

        self.email: str | None = None
        self._password: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self.user_id: int | None = None

        self._load_creds()

    # ---------------------------------------------------------------- creds

    @property
    def _creds_path(self) -> str:
        return os.path.join(self._data_dir, CREDS_FILE)

    def _load_creds(self) -> None:
        try:
            with open(self._creds_path) as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.error("Unreadable credentials file: %s", err)
            return
        self.email = d.get("email")
        self._password = d.get("password")
        if d.get("whitelabel"):
            self._whitelabel = d["whitelabel"]
        self._access_token = d.get("access_token")
        self._refresh_token = d.get("refresh_token")
        self.user_id = d.get("user_id")

    @property
    def whitelabel(self) -> str:
        return self._whitelabel

    def _save_creds(self) -> None:
        os.makedirs(self._data_dir, exist_ok=True)
        payload = {
            "whitelabel": self._whitelabel,
            "email": self.email,
            "password": self._password,
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "user_id": self.user_id,
        }
        tmp = self._creds_path + ".tmp"
        # 0600 from the moment the file exists. Creating it under the umask
        # and chmod'ing afterwards left the account password readable for the
        # length of the write — and permanently, in the .tmp a crash left
        # behind. fchmod as well as the open mode, because O_CREAT's mode is
        # ignored when the path already exists.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "w") as f:      # closes fd on the way out
            json.dump(payload, f)
        os.replace(tmp, self._creds_path)

    @property
    def configured(self) -> bool:
        return bool(self.email and self._password)

    @property
    def authenticated(self) -> bool:
        return bool(self._access_token)

    def clear_creds(self) -> None:
        self._whitelabel = "Arbox"
        self.email = None
        self._password = None
        self._access_token = None
        self._refresh_token = None
        self.user_id = None
        try:
            os.remove(self._creds_path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------- plumbing

    def _headers(self) -> dict[str, str]:
        h = {
            "whitelabel": self._whitelabel,
            "version": "11",
            "referername": "app",
            "User-Agent": f"{self._whitelabel}/4000606 CFNetwork/3860.700.1 Darwin/25.6.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
        }
        if self._access_token:
            h["accesstoken"] = self._access_token
            h["refreshtoken"] = self._refresh_token or ""
        return h

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        _retry: bool = True,
    ) -> Any:
        if not self._access_token:
            await self.login()
        session = await self._ensure_session()
        failed_token = self._access_token   # snapshot: see login(seen_token=)
        try:
            async with session.request(
                method, f"{API_BASE}{path}", json=body, headers=self._headers()
            ) as resp:
                if resp.status == 401 and _retry:
                    _LOGGER.warning("401 on %s — re-logging in", path)
                    # the token that just failed: if another caller already
                    # replaced it while we waited for the lock, reuse theirs
                    # rather than running a second password login
                    await self.login(force=True, seen_token=failed_token)
                    return await self._request(method, path, body, _retry=False)
                text = await resp.text()
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    # some endpoints answer bare text (checkLateCancel -> "OK")
                    parsed = text
                if resp.status != 200:
                    raise ArboxError(
                        f"{method} {path} -> {resp.status}: {text[:200]}",
                        status=resp.status,
                        body=parsed if isinstance(parsed, dict) else None,
                    )
                return parsed
        except (TimeoutError, aiohttp.ClientError) as err:
            # a timeout is not a ClientError, and it is the most likely way a
            # booking at the opening moment fails on a flaky link
            raise ArboxError(f"{method} {path} failed: {err}",
                             transient=True) from err

    # ----------------------------------------------------------------- auth

    async def login(
        self, email: str | None = None, password: str | None = None,
        force: bool = False, whitelabel: str | None = None,
        seen_token: str | None = None,
    ) -> dict:
        """Login and persist tokens. Serialized so concurrent 401s log in once.

        seen_token is the access token whose 401 prompted the call. If it has
        already been replaced by the time we hold the lock, someone else did
        the work — logging in again would burn a second full authentication
        and, on a single-session backend, invalidate the token they just got.
        """
        async with self._login_lock:
            if self._access_token and not force and email is None:
                return {}
            if (force and email is None and seen_token is not None
                    and self._access_token != seen_token):
                return {}
            if whitelabel:
                self._whitelabel = whitelabel
            if email is not None:
                self.email = email
                self._password = password
            if not self.configured:
                raise ArboxAuthError("No credentials configured")

            session = await self._ensure_session()
            try:
                async with session.post(
                    f"{API_BASE}/api/v2/user/login",
                    json={"email": self.email, "password": self._password},
                    headers={k: v for k, v in self._headers().items()
                             if k not in ("accesstoken", "refreshtoken")},
                ) as resp:
                    if resp.status in (401, 422):
                        raise ArboxAuthError(f"Login rejected ({resp.status})")
                    if resp.status != 200:
                        raise ArboxError(f"Login failed: {resp.status}")
                    data = (await resp.json(content_type=None)).get("data", {})
            except aiohttp.ClientError as err:
                raise ArboxError(f"Login request failed: {err}") from err

            self._access_token = data.get("token")
            self._refresh_token = data.get("refreshToken")
            self.user_id = data.get("id")
            if not self._access_token:
                raise ArboxError("Login response had no token")
            self._save_creds()
            _LOGGER.info("Logged in as user %s", self.user_id)
            return data

    # ------------------------------------------------------------ read APIs

    async def profile(self) -> dict:
        r = await self._request("GET", "/api/v2/user/profile")
        return r.get("data", {})

    async def memberships(self, box_id: int) -> list[dict]:
        r = await self._request("GET", f"/api/v2/boxes/{box_id}/memberships/1/false")
        return r.get("data", [])

    async def schedule_between(
        self, box_id: int, location_id: int, start_date: str, end_date: str
    ) -> list[dict]:
        """All sessions in [start_date, end_date], one call. Dates: YYYY-MM-DD."""
        r = await self._request(
            "POST",
            "/api/v2/schedule/betweenDates",
            {
                "from": f"{start_date}T00:00:00.000Z",
                "to": f"{end_date}T00:00:00.000Z",
                "locations_box_id": location_id,
                "boxes_id": box_id,
            },
        )
        # A 200 whose body has no 'data' is an upstream fault (maintenance, a
        # shape change), not an empty schedule. Defaulting to [] made those
        # indistinguishable, and the caller's job on "no sessions here" is to
        # delete the range.
        if not isinstance(r, dict) or "data" not in r:
            raise ArboxError(
                f"schedule/betweenDates returned no 'data' for "
                f"{start_date}..{end_date}", transient=True)
        return r.get("data") or []

    async def feed(self) -> dict:
        r = await self._request("GET", "/api/v2/user/feed")
        # feed's payload is the top-level object itself
        return r if isinstance(r, dict) else {}

    # ----------------------------------------------------------- write APIs
    # Payloads verified against a HAR capture of the real app (2026-08-26).
    # Every write returns the FULL updated session object in `data` — callers
    # upsert it straight into the store, no follow-up sync needed.

    async def book(self, schedule_id: int, membership_user_id: int) -> dict:
        r = await self._request(
            "POST",
            "/api/v2/scheduleUser/insert",
            {
                "schedule_id": schedule_id,
                "membership_user_id": membership_user_id,
                "extras": {"spot": None},
            },
        )
        return r.get("data", {})

    async def check_late_cancel(self, schedule_id: int) -> bool:
        """True when cancelling now is free; False inside the late window."""
        r = await self._request(
            "POST", "/api/v2/scheduleUser/checkLateCancel",
            {"schedule_id": schedule_id},
        )
        return r == "OK"

    async def cancel(
        self, schedule_user_id: int, schedule_id: int, late_cancel: bool = False
    ) -> dict:
        """Cancel a booking. schedule_user_id is the session's `user_booked`."""
        r = await self._request(
            "POST",
            "/api/v2/scheduleUser/delete",
            {
                "schedule_user_id": schedule_user_id,
                "schedule_id": schedule_id,
                "late_cancel": late_cancel,
            },
        )
        return r.get("data", {})

    async def join_standby(self, schedule_id: int, membership_user_id: int) -> dict:
        r = await self._request(
            "POST",
            "/api/v2/scheduleStandBy/insert",
            {
                "schedule_id": schedule_id,
                "membership_user_id": membership_user_id,
                "extras": {"spot": None},
            },
        )
        return r.get("data", {})

    async def booked_dates(
        self, location_id: int, start_date: str, end_date: str
    ) -> list[str]:
        """Dates (YYYY-MM-DD) the user has bookings on, INCLUDING attended
        history — the one upstream view of month-to-date usage, since our
        rolling window drops past days. Caveat: dates, not sessions — two
        classes on one day count once."""
        r = await self._request(
            "POST",
            "/api/v2/schedule/weekly",
            {
                "from": f"{start_date}T00:00:00.000Z",
                "to": f"{end_date}T23:59:59.999Z",
                "locations_box_id": location_id,
            },
        )
        return r if isinstance(r, list) else []

    async def leave_standby(self, schedule_stand_by_id: int) -> dict:
        """Leave a waitlist. The id is the session's `user_in_standby`."""
        r = await self._request(
            "POST",
            "/api/v2/scheduleStandBy/delete",
            {"schedule_stand_by_id": schedule_stand_by_id},
        )
        return r.get("data", {})
