import hashlib
import json
import platform

from app.build_info import (
    BUILD_ENV_FIELDS,
    DEFAULT_BUILD_INFO,
    load_build_info,
    write_build_info_from_environment,
)


def test_build_info_reads_baked_version_and_revision(tmp_path):
    path = tmp_path / "build-info.json"
    path.write_text(json.dumps({
        "schema": 1,
        "version": "v1.37.0",
        "revision": "0123456789abcdef",
        "source_date": "2026-09-01T16:00:00+03:00",
        "source_repository": "https://github.com/roeidalm/arbox-companion",
        "revision_url": "https://github.com/roeidalm/arbox-companion/commit/0123456789abcdef",
        "release_url": "https://github.com/roeidalm/arbox-companion/releases/tag/v1.37.0",
        "image": "ghcr.io/roeidalm/arbox-server:1.37.0",
        "build_run_url": "https://github.com/roeidalm/arbox-companion/actions/runs/123",
        "python_version": "3.12.14",
        "architecture": "aarch64",
        "requirements_lock_sha256": "abc123",
    }))

    assert load_build_info(path) == {
        "schema": 1,
        "version": "v1.37.0",
        "revision": "0123456789abcdef",
        "source_date": "2026-09-01T16:00:00+03:00",
        "source_repository": "https://github.com/roeidalm/arbox-companion",
        "revision_url": "https://github.com/roeidalm/arbox-companion/commit/0123456789abcdef",
        "release_url": "https://github.com/roeidalm/arbox-companion/releases/tag/v1.37.0",
        "image": "ghcr.io/roeidalm/arbox-server:1.37.0",
        "build_run_url": "https://github.com/roeidalm/arbox-companion/actions/runs/123",
        "python_version": "3.12.14",
        "architecture": "aarch64",
        "requirements_lock_sha256": "abc123",
    }


def test_build_info_falls_back_for_source_checkout_or_invalid_file(tmp_path):
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json")

    assert load_build_info(missing) == DEFAULT_BUILD_INFO
    assert load_build_info(invalid) == DEFAULT_BUILD_INFO


def test_build_info_is_written_from_build_args_not_runtime_app_version(
    tmp_path, monkeypatch
):
    expected = {}
    for field, env_name in BUILD_ENV_FIELDS.items():
        value = f"baked-{field}"
        monkeypatch.setenv(env_name, value)
        expected[field] = value
    monkeypatch.setenv("APP_VERSION", "runtime-lie")
    path = tmp_path / "build-info.json"
    lock = tmp_path / "requirements.lock"
    lock.write_bytes(b"fully pinned dependencies\n")

    written = write_build_info_from_environment(path, lock)

    assert written == {
        "schema": 1,
        **expected,
        "python_version": platform.python_version(),
        "architecture": platform.machine(),
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
    }
    assert load_build_info(path) == written
    assert "runtime-lie" not in path.read_text()
