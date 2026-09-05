#!/usr/bin/env python3
"""Build the committed, metadata-only exercise catalogue.

The server never downloads this at runtime.  Updates are deliberate and pinned
to one upstream revision so a release stays reproducible.
"""

from __future__ import annotations

import json
import pathlib
import urllib.request


SOURCE_REPOSITORY = "https://github.com/hasaneyldrm/exercises-dataset"
SOURCE_REVISION = "7455efae41b330c265e7cd4b78dfa848e7ce5ebd"
SOURCE_URL = (
    "https://raw.githubusercontent.com/hasaneyldrm/exercises-dataset/"
    f"{SOURCE_REVISION}/data/exercises.json"
)
OUTPUT = pathlib.Path(__file__).parents[1] / "app" / "exercises_catalog.json"

FEATURED = {
    "barbell bench press", "barbell deadlift", "barbell full squat",
    "dumbbell biceps curl", "dumbbell bench press", "dumbbell lateral raise",
    "dumbbell lunge", "dumbbell shoulder press", "front plank",
    "goblet squat", "pull-up", "push-up", "romanian deadlift",
    "seated hamstring stretch", "standing calves stretch",
    "world greatest stretch",
}


def classify(row: dict) -> tuple[str, str]:
    name = str(row.get("name") or "").casefold()
    category = str(row.get("category") or "").casefold()
    equipment = str(row.get("equipment") or "").casefold()
    if category == "cardio":
        return "cardio", "distance"
    if any(word in name for word in (
        "stretch", "yoga", "mobility", "foam roll", "roller",
    )):
        return "flexibility", "duration"
    if any(word in name for word in (
        "handstand", "headstand", "planche", "front lever", "back lever",
        "muscle-up", "human flag",
    )):
        return "skill", "attempts"
    if equipment in {
        "barbell", "dumbbell", "kettlebell", "weighted", "cable", "band",
        "resistance band", "smith machine", "ez barbell", "olympic barbell",
        "leverage machine", "sled machine", "trap bar", "medicine ball",
        "hammer", "tire", "rope",
    }:
        return "strength", "strength"
    return "bodyweight", "reps"


def main() -> None:
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "arbox-catalog-builder"})
    with urllib.request.urlopen(request, timeout=60) as response:
        source = json.load(response)
    records = []
    for row in source:
        kind, metric = classify(row)
        source_name = str(row["name"]).strip()
        name = source_name[:1].upper() + source_name[1:]
        records.append({
            "id": f"exdb-{row['id']}",
            "name": name,
            "kind": kind,
            "metric_type": metric,
            "body_part": row.get("body_part") or row.get("category"),
            "target": row.get("target"),
            "muscle_group": row.get("muscle_group"),
            "secondary_muscles": row.get("secondary_muscles") or [],
            "equipment": row.get("equipment"),
            "featured": source_name.casefold() in FEATURED,
        })
    payload = {
        "source_repository": SOURCE_REPOSITORY,
        "source_revision": SOURCE_REVISION,
        "license": "MIT (metadata and instruction text; media excluded)",
        "exercises": records,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(records)} exercises to {OUTPUT}")


if __name__ == "__main__":
    main()
