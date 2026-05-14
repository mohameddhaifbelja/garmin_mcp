"""Unit tests for :mod:`src.audit`.

The audit module is the only undo trail for Garmin writes (DESIGN.md §12), so
the tests pin its on-disk contract tightly:

* Each ``record`` call appends exactly one JSON line.
* Lines are valid JSON, ordered, and contain the four required keys.
* Timestamps are UTC iso8601 with the ``Z`` suffix (not ``+00:00``).
* Parent directories are created on first call and chmod'd to ``0o600`` per
  the spawn instructions (overrides the AC wording of "File mode 0600 on the
  directory" to mean the *directory mode*).
* Oversized string values inside ``args`` / ``result`` get truncated to 100
  chars + ``...`` so the log file stays bounded.

After each :func:`audit.record` call the parent directory ends up locked at
``0o600`` (no execute bit). Tests that need to read the resulting file have
to temporarily chmod the directory back to ``0o700`` — that mirrors what the
production process does itself on every subsequent call.

The ``path`` kwarg is used everywhere so the tests do not depend on
``src.config`` (which requires env vars at import time).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from src import audit

_ISO_Z_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def _read_under_locked_dir(log_path: Path) -> str:
    """Re-open ``log_path`` after temporarily relaxing the parent's 0o600 mode."""
    log_path.parent.chmod(0o700)
    try:
        return log_path.read_text(encoding="utf-8")
    finally:
        log_path.parent.chmod(0o600)


def _relax_recursively(root: Path) -> None:
    """Restore 0o700 on every directory under ``root`` so pytest can clean up.

    Walks pre-order, chmodding each directory *before* descending into it so
    a 0o600 dir doesn't block the recursion.
    """
    try:
        root.chmod(0o700)
    except FileNotFoundError:
        return
    for entry in root.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            _relax_recursively(entry)


@pytest.fixture(autouse=True)
def _relax_locked_dirs(tmp_path: Path) -> Iterator[None]:
    """Restore search permissions under ``tmp_path`` after each test.

    The audit module locks the log's parent directory at ``0o600`` (no execute
    bit). Pytest's tmp_path finaliser can't recurse into such a directory and
    emits noisy warnings. Walking the tree and re-chmodding to ``0o700`` on
    teardown lets the fixture clean up the way it was designed to.
    """
    yield
    _relax_recursively(tmp_path)


def test_record_appends_two_ordered_json_lines(tmp_path: Path) -> None:
    """Two calls produce two valid JSON lines in call order."""
    log_path = tmp_path / "writes.jsonl"

    audit.record(
        tool="create_and_schedule",
        args={"name": "easy 8k"},
        result={"workout_id": 1},
        path=log_path,
    )
    audit.record(
        tool="delete_workout",
        args={"workout_id": 1},
        result={"deleted": True},
        path=log_path,
    )

    raw_lines = _read_under_locked_dir(log_path).splitlines()
    assert len(raw_lines) == 2

    first = json.loads(raw_lines[0])
    second = json.loads(raw_lines[1])

    assert first["tool"] == "create_and_schedule"
    assert first["args"] == {"name": "easy 8k"}
    assert first["result"] == {"workout_id": 1}

    assert second["tool"] == "delete_workout"
    assert second["args"] == {"workout_id": 1}
    assert second["result"] == {"deleted": True}


def test_record_timestamp_is_utc_iso8601_with_z_suffix(tmp_path: Path) -> None:
    """``ts`` must be UTC iso8601 ending in ``Z`` (not ``+00:00``)."""
    log_path = tmp_path / "writes.jsonl"

    audit.record(tool="noop", args={}, result={}, path=log_path)

    line = _read_under_locked_dir(log_path).splitlines()[0]
    payload = json.loads(line)

    assert "ts" in payload
    assert _ISO_Z_PATTERN.match(payload["ts"]), payload["ts"]
    assert not payload["ts"].endswith("+00:00")


def test_record_creates_missing_parent_directories(tmp_path: Path) -> None:
    """A nested target path materialises every missing parent directory."""
    log_path = tmp_path / "deep" / "nested" / "writes.jsonl"

    assert not log_path.parent.exists()

    audit.record(tool="noop", args={}, result={}, path=log_path)

    # Parent exists and is a directory (this stat needs no exec bit on the dir itself).
    assert log_path.parent.is_dir()
    # Verify the file landed inside it after relaxing the 0o600 lock.
    log_path.parent.chmod(0o700)
    try:
        assert log_path.is_file()
    finally:
        log_path.parent.chmod(0o600)


def test_record_sets_directory_permissions_to_0600(tmp_path: Path) -> None:
    """Parent directory created by ``record`` has mode ``0o600``."""
    log_path = tmp_path / "secrets" / "writes.jsonl"

    audit.record(tool="noop", args={}, result={}, path=log_path)

    mode = log_path.parent.stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_record_truncates_long_string_values(tmp_path: Path) -> None:
    """Strings >100 chars in args/result are truncated to ``value[:100] + '...'``."""
    log_path = tmp_path / "writes.jsonl"

    long_value = "x" * 250
    audit.record(
        tool="create_workout",
        args={"description": long_value, "short": "ok"},
        result={"echo": long_value},
        path=log_path,
    )

    payload = json.loads(_read_under_locked_dir(log_path).splitlines()[0])
    expected = ("x" * 100) + "..."
    assert payload["args"]["description"] == expected
    assert payload["args"]["short"] == "ok"
    assert payload["result"]["echo"] == expected


def test_record_truncates_strings_inside_nested_structures(tmp_path: Path) -> None:
    """Truncation recurses into nested dicts and lists."""
    log_path = tmp_path / "writes.jsonl"

    long_value = "y" * 150
    audit.record(
        tool="nested",
        args={
            "steps": [
                {"note": long_value, "n": 1},
                {"tags": [long_value, "short"]},
            ],
        },
        result={},
        path=log_path,
    )

    payload = json.loads(_read_under_locked_dir(log_path).splitlines()[0])
    expected = ("y" * 100) + "..."
    assert payload["args"]["steps"][0]["note"] == expected
    assert payload["args"]["steps"][0]["n"] == 1
    assert payload["args"]["steps"][1]["tags"][0] == expected
    assert payload["args"]["steps"][1]["tags"][1] == "short"


def test_record_serialises_unexpected_types_via_default_str(tmp_path: Path) -> None:
    """Values that are not natively JSON-serialisable fall back to ``str()``."""
    log_path = tmp_path / "writes.jsonl"

    class Custom:
        def __str__(self) -> str:
            return "custom-object"

    audit.record(
        tool="weird",
        args={"obj": Custom()},
        result={"path": Path("/tmp/x")},
        path=log_path,
    )

    payload = json.loads(_read_under_locked_dir(log_path).splitlines()[0])
    assert payload["args"]["obj"] == "custom-object"
    assert payload["result"]["path"] == "/tmp/x"
