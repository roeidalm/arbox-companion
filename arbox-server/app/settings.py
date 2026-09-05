"""Server settings persisted in DATA_DIR/settings.json (0600).

Holds the API key and notification-channel config the user edits in the UI.
Never committed anywhere — /data is the bind mount of /srv/appdata/arbox.
"""

from __future__ import annotations

import json
import os
import secrets

SETTINGS_FILE = "settings.json"

DEFAULTS: dict = {
    "api_key": None,              # generated on first run
    "kinds_migrated": 0,          # see KINDS_MIGRATION
    "calendar_alarms": [60],      # reminder lead times in minutes; [] = none
    # externally reachable address, used to build calendar links the
    # phone can open from an HA notification. Empty = no link action.
    "base_url": "",
    # Every time the app reasons about — a registration window opening, the
    # digest hour, an event timestamp — is the studio's wall clock. Pinning it
    # here rather than trusting the container's TZ makes it one thing the user
    # can see and set, instead of an invisible property of the host.
    "timezone": "Asia/Jerusalem",
    "digest_hour": 20,            # nightly digest send hour (studio tz)
    # push reminder before a booked class; 0 = off (the calendar event already
    # carries its own alarms, so this is opt-in rather than a second default)
    "class_reminder_minutes": 0,
    # Class categories this membership cannot book. Filled automatically the
    # first time Arbox answers classTypeRestricts, and editable by hand.
    # Names, not ids: the error hands us the name, and it is what a person
    # would type.
    "blocked_categories": [],
    "blocked_categories_by_studio": {},
    # Lead time before the *cancellation deadline*, not before the class.
    # The deadline itself is per-class (disable_cancellation_time, which is
    # 12h for most of this studio but 4h on some sessions), so one number
    # here adapts to every class instead of being wrong for the short ones.
    # 0 = off.
    "late_cancel_warning_minutes": 60,
    "monthly_quota": 0,           # entries per calendar month; 0 = unlimited/off
    # one-off override for a single month (e.g. a prorated first month after
    # joining mid-month): {"month": "YYYY-MM", "quota": N}. Ignored — and
    # effectively self-clearing — once the calendar moves past that month.
    "quota_override": None,
    # Per-studio manual quota knobs. Membership-derived quotas already come
    # from the active studio; these overrides must follow the same boundary.
    "studio_quota_settings": {},
    # Stable membership_user id chosen by the user as first priority. None
    # preserves the membership that predated multi-membership support.
    "preferred_membership_id": None,
    # Selected studio (box_fk). None lets discovery choose the first studio
    # that actually has an active membership — never merely users_boxes[0].
    "preferred_studio_id": None,
    "ignored_studio_ids": [],
    "studio_membership_preferences": {},
    # How long the local copy keeps rows, in days. Attended classes are the
    # user's own history and worth a year; strangers' past classes only need
    # to cover "what ran last month"; the future horizon bounds how far a
    # deliberate 2-month fetch survives the nightly prune.
    "retention": {
        "attended_days": 365,
        "past_days": 30,
        "future_days": 30,
        # skip/failure decisions ("why wasn't I booked?") — kept separately
        # from the 30-day operational bookkeeping so the question stays
        # answerable for months back
        "decisions_days": 180,
        "messages_days": 365,
    },
    "notify": {
        # channel priority; with escalation_minutes > 0 an actionable message
        # goes to the first channel only, and to the rest if unanswered after
        # that many minutes. 0 = all eligible channels at once.
        "order": ["telegram", "ha"],
        "escalation_minutes": 0,
    },
    # Post-class journal is opt-in.  No migration enables its notification
    # kind for existing users: choosing a level and channels is deliberate.
    "journal": {
        "level": "off",          # off | quick | feedback | full
        "delay_minutes": 30,
    },
    # Explicit overrides keyed by studio.  Missing means the catalogue derives
    # suitable starter packs from the class categories already seen there.
    "exercise_packs_by_studio": {},
    # The full imported catalogue stays available through search. These two
    # lists only control the small shortcut shelf shown while logging.
    "exercise_shortcuts_by_studio": {},
    "hidden_exercises_by_studio": {},
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "kinds": ["digest", "autobook", "standby", "studio", "latecancel", "log",
                  "vacation", "attendance", "membership"],
        # minimum event-log severity to push on the "log" kind: warn | error
        "log_level": "error",
    },
    "ha": {
        "enabled": False,
        # An HA webhook is deliberately used instead of a long-lived token:
        # the unguessable id is the secret, and it can only fire the one
        # automation attached to it - least privilege, nothing stored here
        # grants API access. e.g. http://homeassistant:8123/api/webhook/arbox_notify
        "webhook_url": "",
        # Requires the Arbox HA integration with the bundled feedback panel.
        "feedback_in_ha": False,
        "kinds": ["digest", "autobook", "standby", "studio", "latecancel", "log",
                  "vacation", "attendance", "membership"],
        "log_level": "error",
    },
}

# severity order, low to high — a channel set to "warn" also gets errors
LOG_LEVELS = ("warn", "error")

# Kinds added after the first release. An existing install has a stored kinds
# list that predates them, and _eligible() treats absence as "off", so they
# would stay silent forever. Each entry names the existing opt-ins that imply
# the new one.
KINDS_BACKFILL = {
    "latecancel": ("autobook",),
    "log": ("autobook",),
    # a vacation silences the nightly registration offers and/or autobook, so
    # wanting either is reason enough to hear that it started
    "vacation": ("digest", "autobook"),
    "attendance": ("digest", "autobook"),
    "membership": ("digest", "autobook"),
}
# Bump when KINDS_BACKFILL gains an entry. Guarded by a stored marker so the
# backfill runs once per install rather than on every construction — without
# it, unchecking one of these kinds survives only until the next restart and
# is then silently undone.
KINDS_MIGRATION = 3


class Settings:
    def __init__(self, data_dir: str) -> None:
        self._path = os.path.join(data_dir, SETTINGS_FILE)
        self._data: dict = json.loads(json.dumps(DEFAULTS))  # deep copy
        try:
            with open(self._path) as f:
                stored = json.load(f)
            # pre-multi-alarm installs stored a single int; promote it before
            # the defaults merge, which would otherwise mask it
            if "calendar_alarms" not in stored and stored.get("calendar_alarm_minutes"):
                stored["calendar_alarms"] = [stored["calendar_alarm_minutes"]]
                stored.pop("calendar_alarm_minutes", None)
            for k, v in stored.items():
                if isinstance(v, dict) and isinstance(self._data.get(k), dict):
                    if not self._data[k]:
                        # Empty defaults are dynamic maps keyed by studio or
                        # membership id.  There is no fixed allow-list to
                        # merge against; replacing the empty map is what
                        # makes those preferences survive a restart.
                        self._data[k] = dict(v)
                    else:
                        for kk, vv in v.items():
                            if kk in self._data[k]:
                                self._data[k][kk] = vv
                else:
                    self._data[k] = v
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        # a stored kinds list replaces the default wholesale, so kinds added
        # after that file was written would stay off forever
        if int(self._data.get("kinds_migrated") or 0) < KINDS_MIGRATION:
            for section in ("telegram", "ha"):
                kinds = self._data[section].get("kinds") or []
                for new_kind, implied_by in KINDS_BACKFILL.items():
                    if new_kind not in kinds and any(k in kinds for k in implied_by):
                        kinds.append(new_kind)
                self._data[section]["kinds"] = kinds
            self._data["kinds_migrated"] = KINDS_MIGRATION
            self.save()
        if not self._data.get("api_key"):
            self._data["api_key"] = secrets.token_urlsafe(24)
            self.save()
        self._active_studio_id = self.preferred_studio_id
        if self._active_studio_id:
            self._migrate_legacy_quota_to_studio(self._active_studio_id)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        # 0600 before a byte is written, not after: this file holds the API
        # key, the Telegram bot token and the HA webhook URL, and it is
        # rewritten on every settings change and every auto-learned category.
        # fchmod too — O_CREAT's mode does nothing if a stale .tmp survives.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        with os.fdopen(fd, "w") as f:   # closes fd on the way out
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    @property
    def api_key(self) -> str:
        return self._data["api_key"]

    @property
    def base_url(self) -> str:
        return self._data.get("base_url", "")

    @property
    def calendar_alarms(self) -> list[int]:
        raw = self._data.get("calendar_alarms") or []
        out = []
        for m in raw if isinstance(raw, list) else []:
            try:
                v = int(m)
            except (TypeError, ValueError):
                continue
            if 0 < v <= 10080 and v not in out:   # cap at a week
                out.append(v)
        return sorted(out, reverse=True)

    @property
    def class_reminder_minutes(self) -> int:
        try:
            return int(self._data.get("class_reminder_minutes", 0))
        except (TypeError, ValueError):
            return 0

    @property
    def monthly_quota(self) -> int:
        active = getattr(self, "_active_studio_id", None)
        scoped = self._data.get("studio_quota_settings") or {}
        raw = (scoped.get(str(active), {}).get("monthly_quota", 0)
               if active and scoped else self._data.get("monthly_quota", 0))
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def quota_override(self) -> dict | None:
        active = getattr(self, "_active_studio_id", None)
        scoped = self._data.get("studio_quota_settings") or {}
        if active and scoped:
            value = scoped.get(str(active), {}).get("quota_override")
        else:
            value = self._data.get("quota_override")
        return value if isinstance(value, dict) else None

    @property
    def preferred_membership_id(self) -> int | None:
        try:
            raw = self._data.get("preferred_membership_id")
            return int(raw) if raw else None
        except (TypeError, ValueError):
            return None

    @property
    def preferred_studio_id(self) -> int | None:
        try:
            raw = self._data.get("preferred_studio_id")
            return int(raw) if raw else None
        except (TypeError, ValueError):
            return None

    @property
    def ignored_studio_ids(self) -> list[int]:
        out = []
        for raw in self._data.get("ignored_studio_ids") or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value not in out:
                out.append(value)
        return out

    def set_ignored_studios(self, box_ids: list[int]) -> None:
        self._data["ignored_studio_ids"] = list(dict.fromkeys(int(x) for x in box_ids))
        self.save()

    def activate_studio(self, box_id: int, previous_box_id: int | None = None,
                        make_default: bool = False) -> None:
        """Load a studio's membership preference; optionally make it default."""
        prefs = dict(self._data.get("studio_membership_preferences") or {})
        if previous_box_id and self.preferred_membership_id:
            prefs[str(previous_box_id)] = self.preferred_membership_id
        self._data["preferred_membership_id"] = prefs.get(str(box_id))
        self._data["studio_membership_preferences"] = prefs
        if make_default:
            self._data["preferred_studio_id"] = int(box_id)
        self._active_studio_id = int(box_id)
        self.save()

    def _migrate_legacy_quota_to_studio(self, box_id: int) -> None:
        """Assign pre-multi-studio manual quota values to their old studio."""
        scoped = dict(self._data.get("studio_quota_settings") or {})
        if scoped or not (
                self._data.get("monthly_quota") or self._data.get("quota_override")):
            return
        scoped[str(box_id)] = {
            "monthly_quota": self._data.get("monthly_quota", 0),
            "quota_override": self._data.get("quota_override"),
        }
        self._data["studio_quota_settings"] = scoped
        self.save()

    def select_studio(self, box_id: int) -> None:
        """Persist the studio while keeping a separate default membership per box."""
        prefs = dict(self._data.get("studio_membership_preferences") or {})
        current = self.preferred_studio_id
        if current and self.preferred_membership_id:
            prefs[str(current)] = self.preferred_membership_id
        elif not current and self.preferred_membership_id:
            # Upgrade from the single-studio setting: that preference belongs
            # to the first eligible studio we are discovering now.
            prefs[str(box_id)] = self.preferred_membership_id
        self._data["preferred_studio_id"] = int(box_id)
        self._data["preferred_membership_id"] = prefs.get(str(box_id))
        self._data["studio_membership_preferences"] = prefs
        blocked_by_studio = dict(
            self._data.get("blocked_categories_by_studio") or {})
        if not blocked_by_studio and self._data.get("blocked_categories"):
            blocked_by_studio[str(box_id)] = list(self._data["blocked_categories"])
            self._data["blocked_categories_by_studio"] = blocked_by_studio
        self._migrate_legacy_quota_to_studio(box_id)
        self._active_studio_id = int(box_id)
        self.save()

    def quota_for_month(self, month: str) -> int:
        """month: YYYY-MM. The override wins only for its own month."""
        ov = self.quota_override
        if ov and ov.get("month") == month:
            try:
                return int(ov["quota"])
            except (TypeError, ValueError):
                pass
        return self.monthly_quota

    @property
    def retention(self) -> dict:
        """The three windows, clamped — garbage falls back, never raises."""
        raw = self._data.get("retention") or {}
        out = {}
        for key, default, lo, hi in (
            ("attended_days", 365, 30, 3650),
            ("past_days", 30, 7, 3650),
            ("future_days", 30, 30, 62),
            ("decisions_days", 180, 30, 3650),
            ("messages_days", 365, 30, 3650),
        ):
            try:
                out[key] = min(hi, max(lo, int(raw.get(key, default))))
            except (TypeError, ValueError):
                out[key] = default
        return out

    def log_level_for(self, channel: str) -> str:
        ch = self._data.get(channel) or {}
        lvl = str(ch.get("log_level") or "error")
        return lvl if lvl in LOG_LEVELS else "error"

    def wants_log(self, channel: str, level: str) -> bool:
        """Is this event severe enough for that channel?"""
        if level not in LOG_LEVELS:
            return False        # info is never pushed; the log is for reading
        want = self.log_level_for(channel)
        return LOG_LEVELS.index(level) >= LOG_LEVELS.index(want)

    @property
    def blocked_categories(self) -> list[str]:
        active = getattr(self, "_active_studio_id", None)
        scoped = self._data.get("blocked_categories_by_studio") or {}
        raw = (scoped.get(str(active), []) if active and scoped
               else self._data.get("blocked_categories") or [])
        out = []
        for c in raw if isinstance(raw, list) else []:
            name = str(c).strip()
            if name and name not in out:
                out.append(name)
        return out

    def is_blocked(self, category_name: str | None) -> bool:
        return bool(category_name) and category_name in self.blocked_categories

    def block_category(self, category_name: str) -> bool:
        """Add a category. True when it was not already there."""
        name = str(category_name or "").strip()
        if not name or name in self.blocked_categories:
            return False
        values = self.blocked_categories + [name]
        active = getattr(self, "_active_studio_id", None)
        if active:
            scoped = dict(self._data.get("blocked_categories_by_studio") or {})
            scoped[str(active)] = values
            self._data["blocked_categories_by_studio"] = scoped
        else:
            self._data["blocked_categories"] = values
        self.save()
        return True

    @property
    def late_cancel_warning_minutes(self) -> int:
        try:
            return max(0, int(self._data.get("late_cancel_warning_minutes", 60)))
        except (TypeError, ValueError):
            return 60

    @property
    def timezone(self) -> str:
        """The studio's timezone. Falls back rather than raising: an
        unreadable value must not stop the server from starting."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        tz = self._data.get("timezone") or DEFAULTS["timezone"]
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, KeyError, ModuleNotFoundError):
            return DEFAULTS["timezone"]
        return tz

    def apply_timezone(self) -> str:
        """Point the whole process at the configured zone.

        Everything downstream reads local time implicitly — datetime.now(),
        APScheduler's cron triggers, and SQLite's datetime('now','localtime')
        for event stamps. Setting TZ once here keeps all three in agreement,
        which is cheaper and safer than making every call site tz-aware.
        """
        import time as _time

        tz = self.timezone
        os.environ["TZ"] = tz
        try:
            _time.tzset()
        except AttributeError:      # non-POSIX; the env var is all we can do
            pass
        return tz

    @property
    def digest_hour(self) -> int:
        return int(self._data.get("digest_hour", 20))

    @property
    def telegram(self) -> dict:
        return self._data["telegram"]

    @property
    def ha(self) -> dict:
        return self._data["ha"]

    @property
    def notify(self) -> dict:
        return self._data["notify"]

    @property
    def journal(self) -> dict:
        raw = self._data.get("journal") or {}
        level = str(raw.get("level") or "off")
        if level not in ("off", "quick", "feedback", "full"):
            level = "off"
        return {"level": level, "delay_minutes": 30}

    def exercise_packs(self, studio_id: int | None = None) -> list[str] | None:
        studio_id = studio_id or getattr(self, "_active_studio_id", None)
        scoped = self._data.get("exercise_packs_by_studio") or {}
        value = scoped.get(str(studio_id)) if studio_id else None
        if not isinstance(value, list):
            return None
        allowed = {"strength", "flexibility", "handstand", "movement"}
        return [str(x) for x in value if str(x) in allowed]

    def _exercise_ids(self, key: str, studio_id: int | None = None) -> list[str]:
        studio_id = studio_id or getattr(self, "_active_studio_id", None)
        scoped = self._data.get(key) or {}
        value = scoped.get(str(studio_id)) if studio_id else None
        if not isinstance(value, list):
            return []
        return list(dict.fromkeys(str(x) for x in value if str(x).strip()))

    def exercise_shortcuts(self, studio_id: int | None = None) -> list[str]:
        return self._exercise_ids("exercise_shortcuts_by_studio", studio_id)

    def hidden_exercises(self, studio_id: int | None = None) -> list[str]:
        return self._exercise_ids("hidden_exercises_by_studio", studio_id)

    def set_exercise_shortcut(self, exercise_id: str, action: str) -> None:
        active = getattr(self, "_active_studio_id", None)
        if not active:
            return
        pins = self.exercise_shortcuts(active)
        hidden = self.hidden_exercises(active)
        if action == "add":
            if exercise_id not in pins:
                pins.append(exercise_id)
            hidden = [x for x in hidden if x != exercise_id]
        elif action == "remove":
            pins = [x for x in pins if x != exercise_id]
            if exercise_id not in hidden:
                hidden.append(exercise_id)
        elif action == "restore":
            hidden = [x for x in hidden if x != exercise_id]
        else:
            raise ValueError("invalid exercise shortcut action")
        pin_map = dict(self._data.get("exercise_shortcuts_by_studio") or {})
        hidden_map = dict(self._data.get("hidden_exercises_by_studio") or {})
        pin_map[str(active)] = pins
        hidden_map[str(active)] = hidden
        self._data["exercise_shortcuts_by_studio"] = pin_map
        self._data["hidden_exercises_by_studio"] = hidden_map
        self.save()

    def public_view(self) -> dict:
        """Settings for the UI — secrets masked."""
        tg = dict(self._data["telegram"])
        tg["bot_token"] = "***" if tg.get("bot_token") else ""
        # the unguessable id in the URL is the whole authentication for that
        # webhook — anyone who reads it off the settings page can fire the
        # user's HA automation with any payload they like
        ha = dict(self._data["ha"])
        ha["webhook_url"] = "***" if ha.get("webhook_url") else ""
        return {"timezone": self.timezone,
                "retention": self.retention,
                "blocked_categories": self.blocked_categories,
                "late_cancel_warning_minutes": self.late_cancel_warning_minutes,
                "digest_hour": self.digest_hour,
                "class_reminder_minutes": self.class_reminder_minutes,
                "monthly_quota": self.monthly_quota,
                "preferred_membership_id": self._data.get("preferred_membership_id"),
                "preferred_studio_id": self._data.get("preferred_studio_id"),
                "ignored_studio_ids": self.ignored_studio_ids,
                "quota_override": self.quota_override, "telegram": tg,
                "ha": ha,
                "notify": dict(self._data["notify"]),
                "journal": self.journal,
                "exercise_packs": self.exercise_packs(),
                "calendar_alarms": self.calendar_alarms,
                "base_url": self._data.get("base_url", "")}

    def update(self, patch: dict) -> None:
        """Apply a settings patch from the UI. '***' means keep the stored secret."""
        if "blocked_categories" in patch:
            values = [
                str(c).strip() for c in (patch["blocked_categories"] or [])
                if str(c).strip()
            ]
            active = getattr(self, "_active_studio_id", None)
            if active:
                scoped = dict(self._data.get("blocked_categories_by_studio") or {})
                scoped[str(active)] = values
                self._data["blocked_categories_by_studio"] = scoped
            else:
                self._data["blocked_categories"] = values
        if "late_cancel_warning_minutes" in patch:
            self._data["late_cancel_warning_minutes"] = max(
                0, int(patch["late_cancel_warning_minutes"] or 0))
        if "timezone" in patch:
            # stored as given; the property is what validates, so a typo
            # degrades to the default instead of stopping the server
            self._data["timezone"] = str(patch["timezone"] or "").strip()
        if "digest_hour" in patch:
            # clamped, not trusted: an out-of-range hour is accepted by the
            # live reschedule (which logs its own error and moves on) and then
            # kills the next startup, where CronTrigger is built with no
            # try/except and the server never gets to bind.
            try:
                hour = int(patch["digest_hour"])
            except (TypeError, ValueError):
                hour = DEFAULTS["digest_hour"]
            self._data["digest_hour"] = min(23, max(0, hour))
        if "class_reminder_minutes" in patch:
            self._data["class_reminder_minutes"] = max(
                0, int(patch["class_reminder_minutes"] or 0))
        if "monthly_quota" in patch:
            value = max(0, int(patch["monthly_quota"] or 0))
            active = getattr(self, "_active_studio_id", None)
            if active:
                scoped = dict(self._data.get("studio_quota_settings") or {})
                entry = dict(scoped.get(str(active)) or {})
                entry["monthly_quota"] = value
                scoped[str(active)] = entry
                self._data["studio_quota_settings"] = scoped
            else:
                self._data["monthly_quota"] = value
        if "preferred_membership_id" in patch:
            raw = patch.get("preferred_membership_id")
            try:
                self._data["preferred_membership_id"] = int(raw) if raw else None
            except (TypeError, ValueError):
                self._data["preferred_membership_id"] = None
            active_studio_id = getattr(
                self, "_active_studio_id", None) or self.preferred_studio_id
            if active_studio_id:
                prefs = dict(self._data.get("studio_membership_preferences") or {})
                if self._data["preferred_membership_id"]:
                    prefs[str(active_studio_id)] = self._data["preferred_membership_id"]
                else:
                    prefs.pop(str(active_studio_id), None)
                self._data["studio_membership_preferences"] = prefs
        if "quota_override" in patch:
            ov = patch["quota_override"]
            if ov and isinstance(ov, dict) and ov.get("month") and int(ov.get("quota") or 0) > 0:
                value = {
                    "month": str(ov["month"])[:7], "quota": int(ov["quota"]),
                }
            else:
                value = None
            active = getattr(self, "_active_studio_id", None)
            if active:
                scoped = dict(self._data.get("studio_quota_settings") or {})
                entry = dict(scoped.get(str(active)) or {})
                entry["quota_override"] = value
                scoped[str(active)] = entry
                self._data["studio_quota_settings"] = scoped
            else:
                self._data["quota_override"] = value
        if "calendar_alarms" in patch:
            self._data["calendar_alarms"] = [
                int(m) for m in (patch["calendar_alarms"] or [])
            ]
            self._data.pop("calendar_alarm_minutes", None)  # legacy key
        if "base_url" in patch:
            self._data["base_url"] = str(patch["base_url"]).rstrip("/")
        if "exercise_packs" in patch:
            active = getattr(self, "_active_studio_id", None)
            if active:
                allowed = {"strength", "flexibility", "handstand", "movement"}
                scoped = dict(self._data.get("exercise_packs_by_studio") or {})
                scoped[str(active)] = [
                    str(x) for x in (patch.get("exercise_packs") or [])
                    if str(x) in allowed
                ]
                self._data["exercise_packs_by_studio"] = scoped
        for section in ("telegram", "ha", "notify", "retention", "journal"):
            if section in patch and isinstance(patch[section], dict):
                for k, v in patch[section].items():
                    if v == "***":
                        continue
                    if k in self._data[section]:
                        self._data[section][k] = v
        if "journal" in patch:
            level = str((patch.get("journal") or {}).get("level") or "off")
            self._data["journal"]["level"] = (
                level if level in ("off", "quick", "feedback", "full") else "off"
            )
            self._data["journal"]["delay_minutes"] = 30
        self.save()
