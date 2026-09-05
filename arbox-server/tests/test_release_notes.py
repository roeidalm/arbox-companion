"""Keep shipped announcements small, valid and useful in the release workflow."""

import json
import re
from pathlib import Path


def test_release_notes_manifest():
    data = json.loads((Path(__file__).parents[1] / "frontend/release-notes.json").read_text())
    assert data["schema"] == 1
    assert data["current"]["id"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", data["current"]["after"])
    assert 1 <= len(data["current"]["notes"]) <= 3
    versions = [release["version"] for release in data["releases"]]
    assert len(versions) == len(set(versions))
    for version in versions:
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    for release in [data["current"], *data["releases"]]:
        for note in release["notes"]:
            assert note["kind"] in {"added", "changed", "removed", "action"}
            assert 0 < len(note["title"]) <= 100
            assert 0 < len(note["text"]) <= 400
            if "href" in note:
                assert note["href"] in {"/mine", "/journal", "/settings", "/automations", "/system", "/schedule"}
                assert note["link_label"]
