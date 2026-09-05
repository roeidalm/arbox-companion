"""Immutable release provenance baked into the container image at build time."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from pathlib import Path
from typing import Any


BUILD_INFO_PATH = Path(__file__).with_name("build-info.json")
DEFAULT_BUILD_INFO: dict[str, Any] = {
    "schema": 1,
    "version": "dev",
    "revision": "unknown",
    "source_date": "unknown",
    "source_repository": "unknown",
    "revision_url": "unknown",
    "release_url": "unknown",
    "image": "unknown",
    "build_run_url": "unknown",
    "python_version": "unknown",
    "architecture": "unknown",
    "requirements_lock_sha256": "unknown",
}

BUILD_ENV_FIELDS = {
    "version": "BUILD_VERSION",
    "revision": "BUILD_REVISION",
    "source_date": "BUILD_SOURCE_DATE",
    "source_repository": "BUILD_SOURCE_REPOSITORY",
    "revision_url": "BUILD_REVISION_URL",
    "release_url": "BUILD_RELEASE_URL",
    "image": "BUILD_IMAGE",
    "build_run_url": "BUILD_RUN_URL",
}


def load_build_info(path: Path = BUILD_INFO_PATH) -> dict[str, Any]:
    """Read build provenance, falling back safely for source checkouts/tests."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return DEFAULT_BUILD_INFO.copy()
    if not isinstance(raw, dict):
        return DEFAULT_BUILD_INFO.copy()
    out = DEFAULT_BUILD_INFO.copy()
    try:
        out["schema"] = max(1, int(raw.get("schema") or 1))
    except (TypeError, ValueError):
        out["schema"] = 1
    for field in DEFAULT_BUILD_INFO.keys() - {"schema"}:
        out[field] = str(raw.get(field) or DEFAULT_BUILD_INFO[field])
    return out


def write_build_info_from_environment(
    path: Path = BUILD_INFO_PATH,
    requirements_lock_path: Path = Path("/requirements.lock"),
) -> dict[str, Any]:
    """Create deterministic provenance from non-secret Docker build args."""
    info = DEFAULT_BUILD_INFO.copy()
    for field, env_name in BUILD_ENV_FIELDS.items():
        info[field] = str(os.environ.get(env_name) or info[field])
    info["python_version"] = platform.python_version()
    info["architecture"] = platform.machine() or "unknown"
    try:
        info["requirements_lock_sha256"] = hashlib.sha256(
            requirements_lock_path.read_bytes()
        ).hexdigest()
    except OSError:
        info["requirements_lock_sha256"] = "unknown"
    path.write_text(
        json.dumps(info, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return info


BUILD_INFO = load_build_info()
APP_VERSION = BUILD_INFO["version"]
APP_REVISION = BUILD_INFO["revision"]


if __name__ == "__main__":
    write_build_info_from_environment()
