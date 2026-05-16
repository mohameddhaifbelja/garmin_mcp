"""Unit tests for :mod:`src.garmin.tools`.

The tools delegate the heavy lifting to :class:`src.garmin.client.GarminClient`
(real network) and the forward translator (pure function, exhaustively tested
in T08/T08b). These tests therefore focus on the orchestration the tools
themselves own:

* date parsing and past-date rejection (in the user's timezone),
* response-envelope robustness for ``get_scheduled_workouts``,
* schedule-id key-fallback for ``schedule_workout``,
* source tagging from the ``[mcp]`` description marker,
* audit-log entries written for both Garmin write paths,
* :func:`register` actually wires both tools into a FastMCP instance.

The stubs deliberately reproduce only the bits of the ``GarminClient`` surface
the tools touch — ``upload_running_workout``, ``schedule_workout``, and
``get_scheduled_workouts``. Any other method called would raise
``AttributeError`` and fail loudly, which is what we want.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from src.garmin import tools as tools_module
from src.garmin.tools import (
    create_and_schedule,
    list_scheduled_workouts,
    register,
)
from src.models import HRRangeTarget, Step, TimeDuration, Workout

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def workout() -> Workout:
    """A minimal valid canonical workout for upload-path tests."""
    return Workout(
        name="Easy run",
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


class _StubClient:
    """Reproduces only the :class:`GarminClient` methods the tools call.

    Each method appends to ``calls`` so tests can assert call ordering. The
    ``*_response`` attributes are set by tests to control what the stub
    returns.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.upload_response: dict[str, Any] = {"workoutId": 4451}
        self.schedule_response: dict[str, Any] = {"workoutScheduleId": 99231}
        self.get_scheduled_responses: dict[tuple[int, int], Any] = {}

    def upload_running_workout(self, running_workout: Any) -> dict[str, Any]:
        self.calls.append(("upload_running_workout", (running_workout,), {}))
        return self.upload_response

    def schedule_workout(self, workout_id: int, date_iso: str) -> dict[str, Any]:
        self.calls.append(("schedule_workout", (workout_id, date_iso), {}))
        return self.schedule_response

    def get_scheduled_workouts(self, year: int, month: int) -> Any:
        self.calls.append(("get_scheduled_workouts", (year, month), {}))
        # Return an empty bare list when nothing was registered for this month;
        # tests that care register the exact payload they want.
        return self.get_scheduled_responses.get((year, month), [])


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    """Replace the module-level Garmin singleton with a fresh stub."""
    stub = _StubClient()
    monkeypatch.setattr(tools_module, "_client", stub)
    return stub


@pytest.fixture
def audit_log(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """Capture every :func:`audit.record` call without touching the filesystem."""
    captured: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def _capture(tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        captured.append((tool, args, result))

    monkeypatch.setattr(tools_module, "audit_record", _capture)
    return captured


@pytest.fixture
def fixed_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the user timezone helper so tests don't depend on ``settings``.

    Importing :mod:`src.config` would require four mandatory env vars; instead
    we override the helper directly with a Tunis ZoneInfo.
    """
    from zoneinfo import ZoneInfo

    monkeypatch.setattr(tools_module, "_user_timezone", lambda: ZoneInfo("Africa/Tunis"))


@pytest.fixture
def today_dec_15(monkeypatch: pytest.MonkeyPatch, fixed_tz: None) -> date:
    """Pin ``_today_in_user_tz`` to a fixed date so date math is deterministic."""
    pinned = date(2026, 5, 14)
    monkeypatch.setattr(tools_module, "_today_in_user_tz", lambda: pinned)
    return pinned


# --- create_and_schedule ---------------------------------------------------


def test_create_and_schedule_happy_path(
    workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
    today_dec_15: date,
) -> None:
    """Happy path: upload + schedule + audit log + correct return shape."""
    target_iso = (today_dec_15 + timedelta(days=60)).isoformat()

    result = create_and_schedule(workout, target_iso)

    assert result == {"workout_id": 4451, "scheduled_id": 99231, "date": target_iso}

    # Both Garmin calls must be made, upload first, schedule second.
    methods = [name for name, _args, _kwargs in stub_client.calls]
    assert methods == ["upload_running_workout", "schedule_workout"]

    # The schedule call must reference the workout id returned by upload.
    _name, schedule_args, _kwargs = stub_client.calls[1]
    assert schedule_args == (4451, target_iso)


def test_create_and_schedule_audit_log_records_both_ids(
    workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
    today_dec_15: date,
) -> None:
    """The audit log captures one row for the whole tool call.

    The args dict carries the workout *name* + ``date_iso`` (we deliberately
    do not dump the full Workout — it can be many KB), and the result dict
    carries both Garmin-returned ids.
    """
    target_iso = (today_dec_15 + timedelta(days=10)).isoformat()
    create_and_schedule(workout, target_iso)

    assert len(audit_log) == 1
    tool, args, result = audit_log[0]
    assert tool == "create_and_schedule"
    assert args == {"workout_name": "Easy run", "date_iso": target_iso}
    assert result == {"workout_id": 4451, "scheduled_id": 99231, "date": target_iso}


def test_create_and_schedule_rejects_past_date(
    workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
    today_dec_15: date,
) -> None:
    """A date strictly earlier than today (in user TZ) must raise.

    No Garmin call must happen, no audit-log row must be written.
    """
    yesterday = (today_dec_15 - timedelta(days=1)).isoformat()

    with pytest.raises(ValueError, match="in the past"):
        create_and_schedule(workout, yesterday)

    assert stub_client.calls == []
    assert audit_log == []


def test_create_and_schedule_accepts_today(
    workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
    today_dec_15: date,
) -> None:
    """Today (same date as ``_today_in_user_tz``) must be accepted."""
    result = create_and_schedule(workout, today_dec_15.isoformat())
    assert result["date"] == today_dec_15.isoformat()
    assert len(audit_log) == 1


def test_create_and_schedule_rejects_malformed_date(
    workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
    today_dec_15: date,
) -> None:
    """Garbage in must raise, not silently parse via fromisoformat extras."""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        create_and_schedule(workout, "2026/05/14")
    assert stub_client.calls == []


def test_create_and_schedule_schedule_id_fallback_to_id_key(
    workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
    today_dec_15: date,
) -> None:
    """If Garmin returns only ``id`` (not ``workoutScheduleId``), still work.

    Pins the documented multi-key fallback described in STATUS.md backlog
    observations for T04/T07 handoff to T11.
    """
    stub_client.schedule_response = {"id": 77777}
    target_iso = (today_dec_15 + timedelta(days=1)).isoformat()

    result = create_and_schedule(workout, target_iso)

    assert result["scheduled_id"] == 77777


def test_create_and_schedule_schedule_id_fallback_to_scheduledworkoutid(
    workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
    today_dec_15: date,
) -> None:
    """The middle key (``scheduledWorkoutId``) is also accepted."""
    stub_client.schedule_response = {"scheduledWorkoutId": 88888}
    target_iso = (today_dec_15 + timedelta(days=1)).isoformat()

    result = create_and_schedule(workout, target_iso)

    assert result["scheduled_id"] == 88888


def test_create_and_schedule_raises_when_no_schedule_id_present(
    workout: Workout,
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
    today_dec_15: date,
) -> None:
    """All three keys missing must raise a useful error, not return ``None``."""
    stub_client.schedule_response = {"someOtherKey": 1}
    target_iso = (today_dec_15 + timedelta(days=1)).isoformat()

    with pytest.raises(ValueError, match="missing schedule id"):
        create_and_schedule(workout, target_iso)


# --- list_scheduled_workouts -----------------------------------------------


def test_list_scheduled_workouts_envelope_calendar_items(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """Items under the ``calendarItems`` envelope key are extracted."""
    stub_client.get_scheduled_responses[(2026, 5)] = {
        "calendarItems": [
            {
                "workoutScheduleId": 1,
                "workoutId": 100,
                "calendarDate": "2026-05-14",
                "title": "[mcp] Easy run",
                "estimatedDurationInSecs": 1800,
            },
        ],
    }

    out = list_scheduled_workouts("2026-05-14", "2026-05-20")
    assert len(out) == 1
    assert out[0]["scheduled_id"] == 1
    assert out[0]["workout_id"] == 100
    assert out[0]["date"] == "2026-05-14"
    assert out[0]["name"] == "Easy run"
    assert out[0]["total_duration_sec"] == 1800.0
    assert out[0]["source"] == "mcp"


def test_list_scheduled_workouts_envelope_items(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """Items under the alternate ``items`` envelope key are extracted."""
    stub_client.get_scheduled_responses[(2026, 5)] = {
        "items": [
            {
                "scheduledWorkoutId": 2,
                "workoutId": 200,
                "date": "2026-05-15",
                "name": "External workout",
                "estimatedDurationInSecs": 3600,
                "description": "Coach Bob says go hard",
            },
        ],
    }

    out = list_scheduled_workouts("2026-05-14", "2026-05-20")
    assert len(out) == 1
    assert out[0]["scheduled_id"] == 2
    assert out[0]["source"] == "external"


def test_list_scheduled_workouts_envelope_scheduledworkouts(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """Items under ``scheduledWorkouts`` are extracted too."""
    stub_client.get_scheduled_responses[(2026, 5)] = {
        "scheduledWorkouts": [
            {
                "id": 3,
                "workoutId": 300,
                "calendarDate": "2026-05-16",
                "workoutName": "From the third envelope key",
            },
        ],
    }

    out = list_scheduled_workouts("2026-05-14", "2026-05-20")
    assert len(out) == 1
    assert out[0]["scheduled_id"] == 3
    assert out[0]["name"] == "From the third envelope key"


def test_list_scheduled_workouts_bare_list_envelope(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """If the response is itself a list, items are extracted directly."""
    stub_client.get_scheduled_responses[(2026, 5)] = [
        {
            "workoutScheduleId": 4,
            "workoutId": 400,
            "calendarDate": "2026-05-17",
            "title": "Bare-list response",
        },
    ]

    out = list_scheduled_workouts("2026-05-14", "2026-05-20")
    assert len(out) == 1
    assert out[0]["scheduled_id"] == 4


def test_list_scheduled_workouts_source_tagging_both_branches(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """One [mcp]-prefixed and one bare item — both source flags must be set.

    Source classification reads the ``title`` field because Garmin's calendar
    list payload does not echo the workout ``description`` (see
    ``_project_summary`` for the rationale).
    """
    stub_client.get_scheduled_responses[(2026, 5)] = {
        "calendarItems": [
            {
                "workoutScheduleId": 10,
                "workoutId": 1000,
                "calendarDate": "2026-05-14",
                "title": "[mcp] Easy run",
            },
            {
                "workoutScheduleId": 11,
                "workoutId": 1100,
                "calendarDate": "2026-05-15",
                "title": "Race week tune-up — added on the phone",
            },
        ],
    }

    out = list_scheduled_workouts("2026-05-14", "2026-05-20")
    assert [item["source"] for item in out] == ["mcp", "external"]


def test_list_scheduled_workouts_filters_outside_range(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """Items in the queried months but outside ``[start, end]`` are dropped."""
    stub_client.get_scheduled_responses[(2026, 5)] = {
        "calendarItems": [
            {"workoutScheduleId": 1, "workoutId": 1, "calendarDate": "2026-05-01"},
            {"workoutScheduleId": 2, "workoutId": 2, "calendarDate": "2026-05-15"},
            {"workoutScheduleId": 3, "workoutId": 3, "calendarDate": "2026-05-31"},
        ],
    }

    out = list_scheduled_workouts("2026-05-10", "2026-05-20")
    assert [item["scheduled_id"] for item in out] == [2]


def test_list_scheduled_workouts_walks_each_month_in_range(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """A range spanning three months hits three ``get_scheduled_workouts`` calls."""
    list_scheduled_workouts("2026-05-31", "2026-07-01")

    months_called = [
        args[:2] for name, args, _kwargs in stub_client.calls if name == "get_scheduled_workouts"
    ]
    assert months_called == [(2026, 5), (2026, 6), (2026, 7)]


def test_list_scheduled_workouts_handles_year_rollover(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """December 2026 → January 2027 must produce two month-calls in order."""
    list_scheduled_workouts("2026-12-20", "2027-01-10")

    months_called = [
        args[:2] for name, args, _kwargs in stub_client.calls if name == "get_scheduled_workouts"
    ]
    assert months_called == [(2026, 12), (2027, 1)]


def test_list_scheduled_workouts_rejects_inverted_range(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """``end_iso < start_iso`` is a programmer error and must raise."""
    with pytest.raises(ValueError, match="precedes"):
        list_scheduled_workouts("2026-05-20", "2026-05-10")


def test_list_scheduled_workouts_rejects_malformed_date(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """Same strict ``YYYY-MM-DD`` parsing as ``create_and_schedule``."""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        list_scheduled_workouts("2026-05-01", "not-a-date")


def test_list_scheduled_workouts_skips_items_without_date(
    stub_client: _StubClient,
    fixed_tz: None,
) -> None:
    """An item lacking a date (which we can't filter) is dropped."""
    stub_client.get_scheduled_responses[(2026, 5)] = {
        "calendarItems": [
            {"workoutScheduleId": 1, "workoutId": 100},  # no date
            {
                "workoutScheduleId": 2,
                "workoutId": 200,
                "calendarDate": "2026-05-14",
            },
        ],
    }

    out = list_scheduled_workouts("2026-05-14", "2026-05-20")
    assert [item["scheduled_id"] for item in out] == [2]


def test_list_scheduled_workouts_does_not_call_audit_log(
    stub_client: _StubClient,
    audit_log: list[tuple[str, dict[str, Any], dict[str, Any]]],
    fixed_tz: None,
) -> None:
    """Reads must not write to the audit log (which is for writes only)."""
    list_scheduled_workouts("2026-05-14", "2026-05-20")
    assert audit_log == []


# --- register --------------------------------------------------------------


def test_register_adds_both_tools_to_fastmcp() -> None:
    """``register(mcp)`` must wire both T11 tools so they appear in ``list_tools``.

    Asserted as a subset rather than equality so later tickets (T14) can add
    more tools to the same ``register`` call without breaking this test —
    T14's own register test pins the full exact list.
    """
    mcp = FastMCP(name="test-garmin")
    register(mcp)

    tool_list = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tool_list}
    assert {"create_and_schedule", "list_scheduled_workouts"}.issubset(names)


def test_register_tool_docstrings_describe_args_and_return() -> None:
    """Per DESIGN.md §6, docstrings list args + return shape, no behavioral rules."""
    mcp = FastMCP(name="test-garmin")
    register(mcp)
    tool_list = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tool_list}

    create_desc = by_name["create_and_schedule"].description or ""
    list_desc = by_name["list_scheduled_workouts"].description or ""

    assert "Args:" in create_desc
    assert "Returns:" in create_desc
    assert "Args:" in list_desc
    assert "Returns:" in list_desc
