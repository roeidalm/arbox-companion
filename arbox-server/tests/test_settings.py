import json
import stat

from app.settings import Settings


def test_new_settings_persist_key_with_private_permissions(tmp_path):
    settings = Settings(str(tmp_path))
    path = tmp_path / "settings.json"

    assert settings.api_key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert Settings(str(tmp_path)).api_key == settings.api_key


def test_public_view_masks_notification_secrets(tmp_path):
    settings = Settings(str(tmp_path))
    settings.update({
        "telegram": {"bot_token": "telegram-secret", "chat_id": "123"},
        "ha": {"webhook_url": "https://ha.example/api/webhook/secret"},
    })

    public = settings.public_view()
    assert public["telegram"]["bot_token"] == "***"
    assert public["ha"]["webhook_url"] == "***"
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert raw["telegram"]["bot_token"] == "telegram-secret"
    assert raw["ha"]["webhook_url"].endswith("/secret")


def test_updates_are_clamped_and_normalized(tmp_path):
    settings = Settings(str(tmp_path))
    settings.update({
        "digest_hour": 99,
        "calendar_alarms": [60, 60, -1, 10081, "120"],
        "monthly_quota": -4,
        "blocked_categories": ["  Pilates ", "", "Yoga"],
        "timezone": "Not/A_Zone",
    })

    assert settings.digest_hour == 23
    assert settings.calendar_alarms == [120, 60]
    assert settings.monthly_quota == 0
    assert settings.blocked_categories == ["Pilates", "Yoga"]
    assert settings.timezone == "Asia/Jerusalem"


def test_quota_override_only_applies_to_its_month(tmp_path):
    settings = Settings(str(tmp_path))
    settings.update({
        "monthly_quota": 5,
        "quota_override": {"month": "2026-09", "quota": 3},
    })

    assert settings.quota_for_month("2026-09") == 3
    assert settings.quota_for_month("2026-10") == 5


def test_quota_settings_are_scoped_per_studio_and_survive_restart(tmp_path):
    settings = Settings(str(tmp_path))
    settings.select_studio(10)
    settings.update({
        "monthly_quota": 5,
        "quota_override": {"month": "2026-09", "quota": 7},
    })

    settings.activate_studio(20, previous_box_id=10)
    assert settings.monthly_quota == 0
    assert settings.quota_for_month("2026-09") == 0
    settings.update({
        "monthly_quota": 10,
        "quota_override": {"month": "2026-09", "quota": 12},
    })

    settings.activate_studio(10, previous_box_id=20)
    assert settings.monthly_quota == 5
    assert settings.quota_for_month("2026-09") == 7
    settings.activate_studio(20, previous_box_id=10, make_default=True)
    reloaded = Settings(str(tmp_path))
    assert reloaded.preferred_studio_id == 20
    assert reloaded.monthly_quota == 10
    assert reloaded.quota_for_month("2026-09") == 12


def test_legacy_quota_is_assigned_only_to_first_studio(tmp_path):
    settings = Settings(str(tmp_path))
    settings.update({
        "monthly_quota": 5,
        "quota_override": {"month": "2026-09", "quota": 7},
    })
    settings.select_studio(10)
    assert settings.quota_for_month("2026-09") == 7

    settings.activate_studio(20, previous_box_id=10)
    assert settings.monthly_quota == 0
    assert settings.quota_for_month("2026-09") == 0


def test_studio_switch_remembers_membership_per_studio(tmp_path):
    settings = Settings(str(tmp_path))
    settings.select_studio(10)
    settings.update({"preferred_membership_id": 101})
    settings.select_studio(20)
    assert settings.preferred_membership_id is None
    settings.update({"preferred_membership_id": 202})

    settings.select_studio(10)
    assert settings.preferred_membership_id == 101
    settings.select_studio(20)
    assert settings.preferred_membership_id == 202


def test_first_studio_discovery_keeps_legacy_membership_preference(tmp_path):
    settings = Settings(str(tmp_path))
    settings.update({"preferred_membership_id": 101})
    settings.select_studio(10)
    assert settings.preferred_studio_id == 10
    assert settings.preferred_membership_id == 101


def test_temporary_studio_switch_does_not_change_default(tmp_path):
    settings = Settings(str(tmp_path))
    settings.select_studio(10)
    settings.update({"preferred_membership_id": 101})
    settings.activate_studio(20, previous_box_id=10, make_default=False)
    assert settings.preferred_studio_id == 10
    settings.update({"preferred_membership_id": 202})
    settings.activate_studio(10, previous_box_id=20, make_default=False)
    assert settings.preferred_membership_id == 101
    assert settings.preferred_studio_id == 10


def test_ignored_studios_are_normalized(tmp_path):
    settings = Settings(str(tmp_path))
    settings.set_ignored_studios([20, 20, "30"])
    assert settings.ignored_studio_ids == [20, 30]


def test_blocked_categories_are_scoped_per_studio(tmp_path):
    settings = Settings(str(tmp_path))
    settings.select_studio(10)
    settings.update({"blocked_categories": ["Pilates"]})
    settings.activate_studio(20, previous_box_id=10)
    assert settings.blocked_categories == []
    settings.update({"blocked_categories": ["Yoga"]})
    settings.activate_studio(10, previous_box_id=20)
    assert settings.blocked_categories == ["Pilates"]
    settings.activate_studio(20, previous_box_id=10)
    assert settings.blocked_categories == ["Yoga"]


def test_legacy_notification_kinds_are_backfilled_once(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "api_key": "stable-key",
        "kinds_migrated": 0,
        "telegram": {"kinds": ["digest", "autobook"]},
        "ha": {"kinds": ["digest"]},
    }))

    settings = Settings(str(tmp_path))
    assert {"latecancel", "log", "vacation", "attendance"} <= set(
        settings.telegram["kinds"])
    assert {"vacation", "attendance"} <= set(settings.ha["kinds"])
    assert Settings(str(tmp_path)).telegram["kinds"] == settings.telegram["kinds"]
