from datetime import date, timedelta

import pytest

from app.journal import (
    catalog_metadata, catalogue, parse_exercise_text, search_catalogue,
    suggested_pack_ids,
)
from app.store import Store


def raw_session(schedule_id: int, day: str):
    return {
        "id": schedule_id, "date": day, "time": "18:00", "end_time": "19:00",
        "coach": {"id": 7, "first_name": "Dana", "last_name": None},
        "box_categories": {"id": 4, "name": "Functional Strength"},
        "series": {}, "enable_registration_time": 168,
        "booking_option": "past", "user_booked": 99,
        "user_in_standby": None,
    }


def test_exercise_parser_keeps_short_input_easy():
    rows = parse_exercise_text("סקוואט 60 ק״ג 3x8; מתח 3x6\nהרגיש טוב")
    assert rows[0]["name"] == "סקוואט"
    assert rows[0]["metric_type"] == "strength"
    assert rows[0]["weight"] == 60.0
    assert (rows[0]["sets"], rows[0]["reps"]) == (3, 8)
    assert rows[1]["metric_type"] == "reps"
    assert rows[2]["metric_type"] == "note"
    assert "הרגיש טוב" in rows[2]["notes"]


def test_catalogue_suggests_studio_relevant_packs():
    ids = suggested_pack_ids(["HS, Mobility & Strength", "Movement basics"])
    assert {"strength", "handstand", "movement"} <= set(ids)
    names = {x["name"] for x in catalogue(ids)}
    assert {"סקוואט", "עמידת ידיים לקיר", "גלגול קדימה"} <= names


def test_imported_catalogue_is_searchable_and_classified():
    assert catalog_metadata()["count"] == 1324
    squats, total = search_catalogue("squat", limit=8)
    assert total > 20
    assert squats[0]["name"] == "סקוואט"  # Hebrew alias wins
    stretches, total = search_catalogue("stretch", kind="flexibility", limit=100)
    assert total > 40
    assert all(x["metric_type"] == "duration" for x in stretches)


def test_catalogue_shortcuts_can_be_hidden_and_pinned():
    base = catalogue(["strength"])
    squat_id = next(x["id"] for x in base if x["name"] == "סקוואט")
    imported = search_catalogue("Barbell full squat", limit=1)[0][0]
    changed = catalogue(["strength"], pinned_ids=[imported["id"]],
                        hidden_ids=[squat_id])
    names = {x["name"] for x in changed}
    assert "סקוואט" not in names
    assert imported["name"] in names


@pytest.mark.asyncio
async def test_journal_round_trip_is_scoped_and_survives_session_cleanup(tmp_path):
    store = Store(str(tmp_path / "arbox.db"))
    await store.open()
    try:
        store.active_box_id = 10
        day = (date.today() - timedelta(days=2)).isoformat()
        await store.upsert_sessions([raw_session(501, day)], box_id=10)
        await store.set_training_outcome(501, "attended", "manual")
        saved = await store.save_workout_journal(
            501, coach_feedback="positive", class_feedback="negative",
            notes="עבודה טובה", exercises=[{
                "exercise_id": "local-test", "name": "סקוואט", "metric_type": "strength",
                "sets": 3, "reps": 8, "weight": 60, "weight_unit": "kg",
            }])
        assert saved["coach_name"] == "Dana"
        assert saved["exercises"][0]["weight"] == 60
        assert saved["exercises"][0]["exercise_id"] == "local-test"
        assert [x["schedule_id"] for x in await store.journal_candidates()] == [501]

        await store.db.execute("DELETE FROM sessions WHERE schedule_id=501")
        await store.db.commit()
        after = await store.workout_journal(501)
        assert after["category_name"] == "Functional Strength"
        assert after["notes"] == "עבודה טובה"

        store.active_box_id = 20
        assert await store.workout_journal(501) is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_custom_exercise_can_be_updated_without_duplication(tmp_path):
    store = Store(str(tmp_path / "arbox.db"))
    await store.open()
    try:
        store.active_box_id = 10
        first = await store.add_custom_exercise("סקוואט בולגרי", "strength")
        updated = await store.add_custom_exercise("סקוואט בולגרי", "reps")
        assert updated["id"] == first["id"]
        assert updated["metric_type"] == "reps"
        assert len(await store.custom_exercises()) == 1
    finally:
        await store.close()


def test_journal_defaults_off_and_packs_are_scoped(tmp_path):
    from app.settings import Settings

    settings = Settings(str(tmp_path))
    assert settings.journal == {"level": "off", "delay_minutes": 30}
    settings.select_studio(10)
    settings.update({"journal": {"level": "full"},
                     "exercise_packs": ["strength", "handstand"]})
    assert settings.journal["level"] == "full"
    assert settings.exercise_packs() == ["strength", "handstand"]
    settings.activate_studio(20, previous_box_id=10)
    assert settings.exercise_packs() is None


def test_exercise_shortcuts_are_scoped_and_reversible(tmp_path):
    from app.settings import Settings

    settings = Settings(str(tmp_path))
    settings.select_studio(10)
    settings.set_exercise_shortcut("exdb-0043", "add")
    assert settings.exercise_shortcuts() == ["exdb-0043"]
    settings.set_exercise_shortcut("exdb-0043", "remove")
    assert settings.exercise_shortcuts() == []
    assert settings.hidden_exercises() == ["exdb-0043"]
    settings.set_exercise_shortcut("exdb-0043", "restore")
    assert settings.hidden_exercises() == []
    settings.activate_studio(20, previous_box_id=10)
    assert settings.exercise_shortcuts() == []
