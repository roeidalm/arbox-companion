"""Workout-journal vocabulary and lightweight exercise parsing.

The catalogue is deliberately local and deterministic.  Arbox exposes class
categories, not exercises, so suggestions combine small starter packs with
what this person actually logged at the active studio.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


PACKS: list[dict[str, Any]] = [
    {
        "id": "strength", "name": "כוח", "icon": "🏋️",
        "keywords": ("strength", "כוח", "gym", "conditioning"),
        "exercises": (
            ("סקוואט", "strength"), ("דדליפט", "strength"),
            ("לאנג׳", "strength"), ("לחיצת כתפיים", "strength"),
            ("לחיצת חזה", "strength"), ("חתירה", "strength"),
            ("מתח", "reps"), ("מקבילים", "reps"),
            ("שכיבות סמיכה", "reps"), ("פלאנק", "duration"),
        ),
    },
    {
        "id": "flexibility", "name": "גמישות", "icon": "🧘",
        "keywords": ("flex", "split", "arches", "גמישות", "mobility"),
        "exercises": (
            ("שפגט קדמי", "duration"), ("שפגט אמצע", "duration"),
            ("Pancake", "duration"), ("גשר", "duration"),
            ("מתיחת כתפיים", "duration"), ("פתיחת אגן", "duration"),
            ("קיפול לפנים", "duration"), ("קשת גב", "duration"),
        ),
    },
    {
        "id": "handstand", "name": "עמידות ידיים", "icon": "🤸",
        "keywords": ("handstand", "hs", "עמידת", "inversion"),
        "exercises": (
            ("עמידת ידיים לקיר", "duration"),
            ("עמידת ידיים חופשית", "duration"),
            ("Kick-ups", "attempts"), ("הליכת קיר", "reps"),
            ("Hollow body", "duration"), ("Arch body", "duration"),
            ("לחיצות עמידת ידיים", "reps"), ("איזון על הידיים", "duration"),
        ),
    },
    {
        "id": "movement", "name": "תנועה ואקרובטיקה", "icon": "🌊",
        "keywords": ("movement", "acro", "תנועה", "אקרובט"),
        "exercises": (
            ("גלגול קדימה", "reps"), ("גלגול אחורה", "reps"),
            ("גלגלון", "reps"), ("עמידת ראש", "duration"),
            ("Macaco", "attempts"), ("קפיצות", "reps"),
            ("מעברי רצפה", "attempts"), ("רצף תנועה", "duration"),
        ),
    },
]

METRIC_LABELS = {
    "strength": "משקל, סטים וחזרות",
    "reps": "סטים וחזרות",
    "duration": "זמן",
    "attempts": "ניסיונות",
    "distance": "מרחק",
    "note": "טקסט חופשי",
}

KIND_LABELS = {
    "strength": "כוח ומשקולות",
    "bodyweight": "משקל גוף",
    "flexibility": "גמישות ומתיחות",
    "skill": "מיומנות",
    "handstand": "עמידות ידיים",
    "movement": "תנועה ואקרובטיקה",
    "cardio": "אירובי",
    "custom": "אישי",
}

_ALIASES = {
    "סקוואט": ("squat",),
    "דדליפט": ("deadlift",),
    "לאנג׳": ("lunge",),
    "לחיצת כתפיים": ("shoulder press", "overhead press"),
    "לחיצת חזה": ("bench press", "chest press"),
    "חתירה": ("row",),
    "מתח": ("pull-up", "pull up"),
    "מקבילים": ("dip", "dips"),
    "שכיבות סמיכה": ("push-up", "push up"),
    "פלאנק": ("plank",),
    "שפגט קדמי": ("front split",),
    "שפגט אמצע": ("middle split", "side split"),
    "גשר": ("bridge",),
    "עמידת ידיים לקיר": ("wall handstand",),
    "עמידת ידיים חופשית": ("free handstand", "handstand"),
}


def _normal(value: str) -> str:
    return " ".join(str(value or "").casefold().replace("–", "-").split())


def _local_id(name: str) -> str:
    return "local-" + hashlib.sha1(_normal(name).encode()).hexdigest()[:12]


@lru_cache(maxsize=1)
def _catalog_source() -> dict:
    path = Path(__file__).with_name("exercises_catalog.json")
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def _built_in_exercises() -> tuple[dict, ...]:
    rows: list[dict] = []
    for pack in PACKS:
        for name, metric in pack["exercises"]:
            rows.append({
                "id": _local_id(name), "name": name,
                "aliases": list(_ALIASES.get(name, ())),
                "metric_type": metric, "kind": pack["id"],
                "pack_id": pack["id"], "featured": True,
                "body_part": None, "target": None, "equipment": None,
                "source": "arbox",
            })
    for source in _catalog_source()["exercises"]:
        rows.append({**source, "aliases": [], "pack_id": None,
                     "source": "exercises-dataset"})
    return tuple(rows)


@lru_cache(maxsize=1)
def _exercise_index() -> dict[str, dict]:
    return {str(row["id"]): row for row in _built_in_exercises()}


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, dict]:
    index: dict[str, dict] = {}
    # Local names deliberately win over broad English entries such as
    # "squat": old free-text journals stay on the familiar generic exercise.
    for row in reversed(_built_in_exercises()):
        for value in [row["name"], *(row.get("aliases") or [])]:
            index[_normal(value)] = row
    return index


def exercise_by_id(exercise_id: str | None) -> dict | None:
    return _exercise_index().get(str(exercise_id or ""))


def resolve_exercise(name: str) -> dict | None:
    return _alias_index().get(_normal(name))


def catalog_metadata() -> dict:
    source = _catalog_source()
    return {
        "count": len(source["exercises"]),
        "source_repository": source["source_repository"],
        "source_revision": source["source_revision"],
        "license": source["license"],
    }


def catalog_equipment() -> list[str]:
    return sorted({
        str(row["equipment"]) for row in _built_in_exercises()
        if row.get("equipment")
    })


def suggested_pack_ids(categories: list[str]) -> list[str]:
    haystack = " ".join(categories).casefold()
    found = [
        pack["id"] for pack in PACKS
        if any(word.casefold() in haystack for word in pack["keywords"])
    ]
    return found or ["strength"]


def catalogue(
    pack_ids: list[str], custom: list[dict] | None = None,
    pinned_ids: list[str] | None = None, hidden_ids: list[str] | None = None,
) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    hidden = set(hidden_ids or [])
    wanted = set(pinned_ids or [])
    for row in _built_in_exercises():
        if row["id"] in hidden:
            continue
        if row.get("pack_id") not in pack_ids and row["id"] not in wanted:
            continue
        key = _normal(row["name"])
        if key not in seen:
            out.append({**row, "custom": False, "pinned": row["id"] in wanted})
            seen.add(key)
    for row in custom or []:
        name = str(row.get("name") or "").strip()
        key = _normal(name)
        if name and key not in seen:
            out.append({**row, "custom_id": row["id"],
                        "id": "custom-" + hashlib.sha1(key.encode()).hexdigest()[:12],
                        "name": name, "kind": "custom", "custom": True})
            seen.add(key)
    return out


def search_catalogue(
    query: str = "", *, kind: str = "", equipment: str = "",
    pinned_ids: list[str] | None = None, hidden_ids: list[str] | None = None,
    include_hidden: bool = False, limit: int = 60,
) -> tuple[list[dict], int]:
    needle = _normal(query)
    hidden = set(hidden_ids or [])
    pinned = set(pinned_ids or [])
    matches: list[tuple[tuple, dict]] = []
    seen_names: set[str] = set()
    for row in _built_in_exercises():
        is_hidden = row["id"] in hidden
        if is_hidden and not include_hidden:
            continue
        if kind and row.get("kind") != kind:
            continue
        if equipment and row.get("equipment") != equipment:
            continue
        searchable = " ".join(str(x or "") for x in (
            row.get("name"), *(row.get("aliases") or []), row.get("body_part"),
            row.get("target"), row.get("muscle_group"), row.get("equipment"),
        )).casefold()
        if needle and needle not in searchable:
            continue
        key = _normal(row["name"])
        if key in seen_names:
            continue
        seen_names.add(key)
        name = _normal(row["name"])
        names = [name, *(_normal(x) for x in (row.get("aliases") or []))]
        rank = (
            0 if row["id"] in pinned else 1,
            0 if needle and needle in names else 1,
            0 if needle and any(x.startswith(needle) for x in names) else 1,
            0 if row.get("featured") else 1,
            0 if row.get("source") == "arbox" else 1,
            name,
        )
        matches.append((rank, {**row, "hidden": is_hidden,
                               "pinned": row["id"] in pinned}))
    matches.sort(key=lambda item: item[0])
    rows = [row for _, row in matches]
    return rows[:max(1, min(100, int(limit)))], len(rows)


_STRENGTH_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<weight>\d+(?:[.,]\d+)?)\s*(?:ק(?:\"|״)?ג|kg)"
    r"(?:\s+|\s*[·,@]\s*)(?P<sets>\d+)\s*[x×]\s*(?P<reps>\d+)$",
    re.IGNORECASE,
)
_SETS_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<sets>\d+)\s*[x×]\s*(?P<reps>\d+)$",
    re.IGNORECASE,
)


def parse_exercise_line(value: str) -> dict:
    """Parse only unambiguous shorthand; preserve every other line as notes."""
    text = " ".join(str(value or "").strip().split())
    hit = _STRENGTH_RE.match(text)
    if hit:
        data = hit.groupdict()
        result = {
            "name": data["name"], "metric_type": "strength",
            "weight": float(data["weight"].replace(",", ".")),
            "weight_unit": "kg", "sets": int(data["sets"]),
            "reps": int(data["reps"]), "notes": None,
        }
        match = resolve_exercise(result["name"])
        if match:
            result.update(exercise_id=match["id"], name=match["name"])
        return result
    hit = _SETS_RE.match(text)
    if hit:
        data = hit.groupdict()
        result = {
            "name": data["name"], "metric_type": "reps",
            "sets": int(data["sets"]), "reps": int(data["reps"]),
            "weight": None, "weight_unit": None, "notes": None,
        }
        match = resolve_exercise(result["name"])
        if match:
            result.update(exercise_id=match["id"], name=match["name"])
        return result
    match = resolve_exercise(text)
    return {"exercise_id": match["id"] if match else None,
            "name": match["name"] if match else text[:120] or "תיעוד",
            "metric_type": match["metric_type"] if match else "note",
            "sets": None, "reps": None, "weight": None,
            "weight_unit": None, "notes": None if match else text[:500] or None}


def parse_exercise_text(value: str) -> list[dict]:
    parts = [p.strip() for p in re.split(r"[;\n]+", str(value or "")) if p.strip()]
    return [parse_exercise_line(part) for part in parts[:30]]
