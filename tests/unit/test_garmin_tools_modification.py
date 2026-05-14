"""Unit tests for the T14 modification subset of :mod:`src.garmin.tools`.

Covers the four tools T14 adds on top of T11:

* ``get_scheduled_workout`` — read path that pipes Garmin payloads through
  the reverse translator and returns canonical-Workout dicts.
* ``replace_scheduled_workout`` — atomic fetch / unschedule / delete /
  upload / schedule with best-effort rollback at the final step.
* ``unschedule_workout`` — thin wrapper, audit-logged.
* ``delete_workout`` — thin wrapper, audit-logged.

The stubs deliberately reproduce only the :class:`GarminClient` methods the
T14 tools touch; any unexpected method call raises ``AttributeError`` and
fails the test loudly. The audit recorder is monkeypatched in-place via the
:data:`tools.audit_record` re-export, mirroring the seam T11's test suite
established.

The rollback contract is best-effort and documented in the tools module
docstring. We test all five failure points individually, asserting:

* what was called before the failure,
* what was *not* called after, and
* whether any compensating cleanup (the ``delete_workout`` after a failed
  ``schedule_workout``) ran.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from src.garmin import tools as tools_module
from src.garmin.tools import (
    delete_workout,
    get_scheduled_workout,
    register,
    replace_scheduled_workout,
    unschedule_workout,
)
from src.models import HRRangeTarget, Step, TimeDuration, Workout

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def new_workout() -> Workout:
    """A minimal valid canonical workout used as the replacement payload."""
    return Workout(
        name="Replacement run",
        sport="road_run",
        description="20 min Z2",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=1200),
                target=HRRangeTarget(min_bpm=141, max_bpm=155),
            ),
        ],
    )


@pytest.fixture
def original_scheduled_payload() -> dict[str, Any]:
    """A Garmin ``get_scheduled_workout_by_id`` response shape.

    Carries the two fields T14 reads (``workoutId``, ``calendarDate``) plus
    the canonical workout shape the reverse translator needs to round-trip
    into a :class:`Workout`. The step list mirrors what T11 / T08 / T13
    pin for an HR-zone interval so the reverse translator returns a valid
    canonical workout without hitting the opaque-fallback branch.
    """
    return {
        "workoutId": 12345,
        "calendarDate": "2026-06-01",
        "workoutName": "Original easy run",
        "description": "[mcp][road_run] Easy 30 min",
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "workoutSteps": [
                    {
                        "type": "ExecutableStepDTO",
                        "stepOrder": 1,
                        "stepType": {
                            "stepTypeId": 3,
                            "stepTypeKey": "interval",
                        },
                        "endCondition": {
                            "conditionTypeId": 2,
                            "conditionTypeKey": "time",
                        },
                        "endConditionValue": 1800,
                        "targetType": {
                            "workoutTargetTypeId": 4,
                            "workoutTargetTypeKey": "heart.rate.zone",
                            "targetValueOne": 141,
                            "targetValueTwo": 155,
                        },
                    },
                ],
            }
        ],
    }


class _StubClient:
    """Reproduces only the :class:`GarminClient` methods T14's tools call.

    Each method appends to ``calls`` so tests can assert call ordering plus
    arguments. Behavior of each method is controlled by the corresponding
    ``*_response`` attribute (return value) or ``*_error`` attribute (an
    exception to raise instead of returning). Setting an ``*_error`` short-
    circuits the call entirely — the method records the attempted call,
    then raises.
    """

    def __init__(self, original_payload: dict[str, Any]) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        # Default responses for the happy path.
        self.get_scheduled_response: dict[str, Any] = original_payload
        self.upload_response: dict[str, Any] = {"workoutId": 99999}
        self.schedule_response: dict[str, Any] = {"workoutScheduleId": 88888}
        # Per-method error injection. ``None`` means "don't raise".
        self.get_scheduled_error: Exception | None = None
        self.unschedule_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.upload_error: Exception | None = None
        self.schedule_error: Exception | None = None

    def get_scheduled_workout_by_id(self, scheduled_id: int) -> dict[str, Any]:
        self.calls.append(("get_scheduled_workout_by_id", (scheduled_id,), {}))
        if self.get_scheduled_error is not None:
            raise self.get_scheduled_error
        return self.get_scheduled_response

    def unschedule_workout(self, scheduled_id: int) -> Any:
        self.calls.append(("unschedule_workout", (scheduled_id,), {}))
        if self.unschedule_error is not None:
            raise self.unschedule_error
        return None

    def delete_workout(self, workout_id: int) -> Any:
        self.calls.append(("delete_workout", (workout_id,), {}))
        if self.delete_error is not None:
            raise self.delete_error
        return None

    def upload_running_workout(self, running_workout: Any) -> dict[str, Any]:
        self.calls.append(("upload_running_workout", (running_workout,), {}))
        if self.upload_error is not None:
            raise self.upload_error
        return self.upload_response

    def schedule_workout(self, workout_id: int, date_iso: str) -> dict[str, Any]:
        self.calls.append(("schedule_workout", (workout_id, date_iso), {}))
        if self.schedule_error is not None:
            raise self.schedule_error
        return self.schedule_response


@pytest.fixture
def stub_client(
    monkeypatch: pytest.MonkeyPatch,
    original_scheduled_payload: dict[str, Any],
) -> _StubClient:
    """Replace the module-level Garmin singleton with a fresh stub."""
    stub = _StubClient(original_scheduled_payload)
    monkeypatch.setattr(tools_module, "_client", stub)
    return stub


@pytest.fixture
def audit_log(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Capture every :func:`audit.record` call without touching the filesystem."""
    captured: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def _capture(tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        captured.append((tool, args, result))

    monkeypatch.setattr(tools_module, "audit_record", _capture)
    return captured


def _method_names(stub: _StubClient) -> list[str]:
    """Project ``stub.calls`` to just the method-name sequence for assertions."""
    return [name for name, _args, _kwargs in stub.calls]


# --- get_scheduled_workout --------------------------------------------------


def test_get_scheduled_workout_returns_canonical_dict(
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Happy path: fetch + reverse-translate + serialise."""
    result = get_scheduled_workout(54321)

    # Single Garmin call to the by-id endpoint.
    assert _method_names(stub_client) == ["get_scheduled_workout_by_id"]
    assert stub_client.calls[0][1] == (54321,)

    # The result is a canonical-Workout dict (not a Pydantic instance, not a
    # raw Garmin payload). The [mcp][road_run] prefix is stripped, the
    # sport extracted, and the single HR-interval round-tripped.
    assert result["name"] == "Original easy run"
    assert result["sport"] == "road_run"
    assert result["description"] == "Easy 30 min"
    assert len(result["steps"]) == 1
    step = result["steps"][0]
    assert step["kind"] == "active"
    assert step["duration"] == {"kind": "time", "seconds": 1800}
    assert step["target"] == {"kind": "hr_range", "min_bpm": 141, "max_bpm": 155}


def test_get_scheduled_workout_does_not_audit_log(
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Reads must not write to the audit log (which is for writes only)."""
    get_scheduled_workout(54321)
    assert audit_log == []


# --- replace_scheduled_workout: happy path ----------------------------------


def test_replace_scheduled_workout_happy_path(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """The full dance: fetch, unschedule, delete, upload, schedule."""
    result = replace_scheduled_workout(54321, new_workout)

    assert result == {
        "new_workout_id": 99999,
        "new_scheduled_id": 88888,
        "original_workout_id": 12345,
        "date": "2026-06-01",
    }

    # The five Garmin calls happen in the documented order.
    assert _method_names(stub_client) == [
        "get_scheduled_workout_by_id",
        "unschedule_workout",
        "delete_workout",
        "upload_running_workout",
        "schedule_workout",
    ]

    # The delete is keyed on the *original* workout id; the schedule on the
    # *new* workout id and the *original* calendar date.
    delete_args = stub_client.calls[2][1]
    assert delete_args == (12345,)
    schedule_args = stub_client.calls[4][1]
    assert schedule_args == (99999, "2026-06-01")


def test_replace_scheduled_workout_audit_log(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Exactly one audit row, capturing the new and original ids + date."""
    replace_scheduled_workout(54321, new_workout)

    assert len(audit_log) == 1
    tool, args, result = audit_log[0]
    assert tool == "replace_scheduled_workout"
    assert args == {"scheduled_id": 54321, "new_workout_name": "Replacement run"}
    assert result == {
        "new_workout_id": 99999,
        "new_scheduled_id": 88888,
        "original_workout_id": 12345,
        "date": "2026-06-01",
    }


def test_replace_scheduled_workout_accepts_alt_date_key(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """If Garmin returns ``date`` instead of ``calendarDate``, still work.

    T11 already documents the multi-key tolerance for the list view; the
    same shape applies to the by-id endpoint until Garmin pins it.
    """
    stub_client.get_scheduled_response = dict(stub_client.get_scheduled_response)
    stub_client.get_scheduled_response.pop("calendarDate")
    stub_client.get_scheduled_response["date"] = "2026-07-04"

    result = replace_scheduled_workout(54321, new_workout)
    assert result["date"] == "2026-07-04"


def test_replace_scheduled_workout_accepts_alt_schedule_id_key(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """The schedule-id fallback (id / scheduledWorkoutId) carries through."""
    stub_client.schedule_response = {"id": 77777}

    result = replace_scheduled_workout(54321, new_workout)
    assert result["new_scheduled_id"] == 77777


# --- replace_scheduled_workout: rollback paths ------------------------------


def test_replace_scheduled_workout_rollback_when_fetch_fails(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Fetch fails → propagate, no other ops attempted, no audit row."""
    stub_client.get_scheduled_error = RuntimeError("not found")

    with pytest.raises(RuntimeError, match="not found"):
        replace_scheduled_workout(54321, new_workout)

    assert _method_names(stub_client) == ["get_scheduled_workout_by_id"]
    assert audit_log == []


def test_replace_scheduled_workout_rollback_when_unschedule_fails(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Unschedule fails after fetch → no delete, no upload, no schedule."""
    stub_client.unschedule_error = RuntimeError("unschedule denied")

    with pytest.raises(RuntimeError, match="unschedule denied"):
        replace_scheduled_workout(54321, new_workout)

    assert _method_names(stub_client) == [
        "get_scheduled_workout_by_id",
        "unschedule_workout",
    ]
    assert audit_log == []


def test_replace_scheduled_workout_rollback_when_delete_fails(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Delete fails after unschedule → no upload, no schedule, no audit.

    Known partial state: the calendar entry is already gone but the
    library template still exists. Documented in the module docstring.
    """
    stub_client.delete_error = RuntimeError("delete denied")

    with pytest.raises(RuntimeError, match="delete denied"):
        replace_scheduled_workout(54321, new_workout)

    assert _method_names(stub_client) == [
        "get_scheduled_workout_by_id",
        "unschedule_workout",
        "delete_workout",
    ]
    assert audit_log == []


def test_replace_scheduled_workout_rollback_when_upload_fails(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Upload fails after delete → no schedule, no audit.

    Known partial state: the original is gone (calendar and library), and
    no new workout has been created. No safe auto-recovery — see module
    docstring option 1.
    """
    stub_client.upload_error = RuntimeError("upload denied")

    with pytest.raises(RuntimeError, match="upload denied"):
        replace_scheduled_workout(54321, new_workout)

    assert _method_names(stub_client) == [
        "get_scheduled_workout_by_id",
        "unschedule_workout",
        "delete_workout",
        "upload_running_workout",
    ]
    assert audit_log == []


def test_replace_scheduled_workout_rollback_when_schedule_fails_deletes_new(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Schedule fails after upload → delete the new workout, no audit.

    This is the recoverable case: the upload succeeded so we have a
    library orphan to clean up before raising.
    """
    stub_client.schedule_error = RuntimeError("schedule denied")

    with pytest.raises(RuntimeError, match="schedule denied"):
        replace_scheduled_workout(54321, new_workout)

    # The compensating delete must target the *new* workout id, not the
    # original — the original is already gone.
    assert _method_names(stub_client) == [
        "get_scheduled_workout_by_id",
        "unschedule_workout",
        "delete_workout",
        "upload_running_workout",
        "schedule_workout",
        "delete_workout",
    ]
    cleanup_args = stub_client.calls[-1][1]
    assert cleanup_args == (99999,)
    assert audit_log == []


def test_replace_scheduled_workout_schedule_fail_swallows_cleanup_error(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """If the cleanup delete also fails, the original schedule error wins.

    Per the module docstring, secondary failures must not mask the
    primary one — the caller needs to see *why* the operation failed,
    not the fact that cleanup struggled too.
    """
    # First delete (of original workout) succeeds; second delete (cleanup
    # after failed schedule) fails. We reconfigure ``delete_error`` mid-test
    # by patching the stub method itself so the two calls diverge.
    delete_call_count = {"n": 0}
    original_delete = stub_client.delete_workout

    def _flaky_delete(workout_id: int) -> Any:
        delete_call_count["n"] += 1
        if delete_call_count["n"] == 1:
            return original_delete(workout_id)
        # Second call: record it then raise.
        stub_client.calls.append(("delete_workout", (workout_id,), {}))
        raise RuntimeError("cleanup-failed")

    stub_client.delete_workout = _flaky_delete  # type: ignore[method-assign]
    stub_client.schedule_error = RuntimeError("schedule-failed")

    with pytest.raises(RuntimeError, match="schedule-failed"):
        replace_scheduled_workout(54321, new_workout)


def test_replace_scheduled_workout_raises_when_no_workout_id_on_fetch(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Fetched payload missing ``workoutId`` → raise before any mutation.

    A silent ``None`` here would propagate into the delete call and brick
    the library. Fail loudly, audit nothing.
    """
    stub_client.get_scheduled_response = {"calendarDate": "2026-06-01"}

    with pytest.raises(ValueError, match="missing workoutId"):
        replace_scheduled_workout(54321, new_workout)

    assert _method_names(stub_client) == ["get_scheduled_workout_by_id"]
    assert audit_log == []


def test_replace_scheduled_workout_raises_when_no_date_on_fetch(
    new_workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """Fetched payload missing both date keys → raise before any mutation."""
    stub_client.get_scheduled_response = {"workoutId": 12345}

    with pytest.raises(ValueError, match="missing a date field"):
        replace_scheduled_workout(54321, new_workout)

    assert _method_names(stub_client) == ["get_scheduled_workout_by_id"]
    assert audit_log == []


# --- unschedule_workout (standalone) ---------------------------------------


def test_unschedule_workout_happy_path(
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """One call, one audit row, success dict."""
    result = unschedule_workout(99231)

    assert result == {"scheduled_id": 99231, "success": True}
    assert _method_names(stub_client) == ["unschedule_workout"]
    assert stub_client.calls[0][1] == (99231,)

    assert len(audit_log) == 1
    tool, args, audit_result = audit_log[0]
    assert tool == "unschedule_workout"
    assert args == {"scheduled_id": 99231}
    assert audit_result == {"scheduled_id": 99231, "success": True}


def test_unschedule_workout_propagates_errors_without_audit(
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """A library error must surface unchanged and not write an audit row."""
    stub_client.unschedule_error = RuntimeError("denied")

    with pytest.raises(RuntimeError, match="denied"):
        unschedule_workout(99231)

    assert audit_log == []


# --- delete_workout (standalone) -------------------------------------------


def test_delete_workout_happy_path(
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """One call, one audit row, success dict."""
    result = delete_workout(4451)

    assert result == {"workout_id": 4451, "success": True}
    assert _method_names(stub_client) == ["delete_workout"]
    assert stub_client.calls[0][1] == (4451,)

    assert len(audit_log) == 1
    tool, args, audit_result = audit_log[0]
    assert tool == "delete_workout"
    assert args == {"workout_id": 4451}
    assert audit_result == {"workout_id": 4451, "success": True}


def test_delete_workout_propagates_errors_without_audit(
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> None:
    """A library error must surface unchanged and not write an audit row."""
    stub_client.delete_error = RuntimeError("denied")

    with pytest.raises(RuntimeError, match="denied"):
        delete_workout(4451)

    assert audit_log == []


# --- register (T14 additions) -----------------------------------------------


def test_register_adds_all_six_tools_to_fastmcp() -> None:
    """``register(mcp)`` wires both T11 + all four T14 tools."""
    mcp = FastMCP(name="test-garmin-t14")
    register(mcp)

    tool_list = asyncio.run(mcp.list_tools())
    names = sorted(tool.name for tool in tool_list)
    assert names == [
        "create_and_schedule",
        "delete_workout",
        "get_scheduled_workout",
        "list_scheduled_workouts",
        "replace_scheduled_workout",
        "unschedule_workout",
    ]


def test_register_t14_tool_docstrings_describe_args_and_return() -> None:
    """Per DESIGN.md §6, docstrings list args + return shape only."""
    mcp = FastMCP(name="test-garmin-t14-docs")
    register(mcp)
    tool_list = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tool_list}

    for tool_name in (
        "get_scheduled_workout",
        "replace_scheduled_workout",
        "unschedule_workout",
        "delete_workout",
    ):
        desc = by_name[tool_name].description or ""
        assert "Args:" in desc, f"{tool_name} docstring missing Args"
        assert "Returns:" in desc, f"{tool_name} docstring missing Returns"
