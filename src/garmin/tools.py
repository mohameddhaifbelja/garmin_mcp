"""MCP tools for the Garmin write surface (Phase 1 subset).

This module owns the FastMCP tool layer over :mod:`src.garmin.client` and
:mod:`src.garmin.translate_forward`. It registers two tools:

* ``create_and_schedule(workout, date_iso)`` — uploads a canonical
  :class:`src.models.Workout` to the Garmin library and schedules it on the
  calendar in a single call.
* ``list_scheduled_workouts(start_iso, end_iso)`` — returns a summary view of
  scheduled workouts in a date range, tagging each entry as ``"mcp"`` or
  ``"external"`` based on the ``[mcp]`` description marker (DESIGN.md §7).

Per DESIGN.md §6 / §10, per-tool docstrings stay generic (args + return
shape). Behavioral orchestration rules (conflict detection, week-by-week
ingest, target policy) live in the server-level ``instructions`` string set
up by T12.

Design notes
------------
- Date strings are interpreted in ``settings.user_timezone`` (Africa/Tunis by
  default) so "today" matches the calendar the user lives by, not UTC.
- A single module-level :class:`GarminClient` is reused across calls. The
  client itself does lazy login on first method invocation; instantiating it
  once avoids redundant tokenstore lookups.
- The Garmin response shapes are not pinned in ``garminconnect`` 0.2.x —
  :func:`schedule_workout` returns the schedule id under one of three keys
  (``workoutScheduleId`` / ``scheduledWorkoutId`` / ``id``) and
  :func:`get_scheduled_workouts` returns the items list under one of three
  envelope keys (``calendarItems`` / ``items`` / ``scheduledWorkouts``) or a
  bare list. Helpers in this module handle that defensively until a live
  smoke run locks the actual keys (see STATUS.md backlog observations).
- ``audit.record`` is exposed as the module attribute :data:`audit_record`
  so unit tests can monkeypatch it without touching the audit module.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from src import audit
from src.garmin.client import GarminClient
from src.garmin.translate_forward import to_garmin
from src.models import Workout

# Re-exported so unit tests can monkeypatch ``tools.audit_record`` instead of
# poking at ``src.audit`` directly.
audit_record = audit.record


class CreateAndScheduleResult(TypedDict):
    """Return shape for :func:`create_and_schedule`."""

    workout_id: int
    scheduled_id: int
    date: str


class ScheduledWorkoutSummary(TypedDict):
    """One row returned by :func:`list_scheduled_workouts`."""

    scheduled_id: int | None
    workout_id: int | None
    date: str | None
    name: str | None
    total_duration_sec: float | None
    source: str


# Singleton Garmin client. Constructed lazily on first call so importing this
# module does not require ``.env`` to be populated.
_client: GarminClient | None = None


def _get_client() -> GarminClient:
    """Return the module-level :class:`GarminClient`, creating it on demand."""
    global _client
    if _client is None:
        _client = GarminClient()
    return _client


# --- date helpers -----------------------------------------------------------


def _user_timezone() -> ZoneInfo:
    """Resolve the user's timezone via :mod:`src.config`.

    Lazy so a bare ``import src.garmin.tools`` does not require ``.env``.
    """
    from src.config import settings

    return ZoneInfo(settings.user_timezone)


def _parse_iso_date(date_iso: str, field_name: str) -> date:
    """Parse ``YYYY-MM-DD`` strictly. Raises :class:`ValueError` on bad input.

    ``datetime.fromisoformat`` accepts richer shapes (with time, with offsets);
    we want only the calendar-date form for tool inputs to keep semantics
    obvious.
    """
    try:
        parsed = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be a calendar date in YYYY-MM-DD format, got {date_iso!r}"
        ) from exc
    return parsed


def _today_in_user_tz() -> date:
    """Today's calendar date in the user's configured timezone."""
    return datetime.now(_user_timezone()).date()


def _iter_year_months(start: date, end: date) -> Iterator[tuple[int, int]]:
    """Yield each ``(year, month)`` covered by ``[start, end]`` inclusive.

    Months are 1-indexed at the wrapper boundary (DESIGN.md / STATUS.md note
    on T04 reviewer handoff to T11).
    """
    cursor = date(start.year, start.month, 1)
    end_marker = date(end.year, end.month, 1)
    while cursor <= end_marker:
        yield cursor.year, cursor.month
        # Advance one month.
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)


# --- response-envelope helpers ---------------------------------------------


_CALENDAR_ITEM_KEYS = ("calendarItems", "items", "scheduledWorkouts")
_SCHEDULE_ID_KEYS = ("workoutScheduleId", "scheduledWorkoutId", "id")
_WORKOUT_ID_KEYS = ("workoutId",)
_ITEM_DATE_KEYS = ("date", "calendarDate")
_ITEM_NAME_KEYS = ("name", "title", "workoutName")
_ITEM_DURATION_KEYS = ("total_duration_sec", "estimatedDurationInSecs")
_ITEM_DESCRIPTION_KEYS = ("description", "workoutDescription")


def _iter_calendar_items(payload: Any) -> list[dict[str, Any]]:
    """Best-effort extraction of the items list from a get-scheduled response.

    Tries the three known envelope keys in order, then falls back to treating
    ``payload`` itself as a bare list. Returns an empty list when nothing
    matches so callers can iterate without a guard.
    """
    if isinstance(payload, dict):
        for key in _CALENDAR_ITEM_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first value found under any of ``keys`` in ``payload``."""
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _extract_scheduled_id(response: dict[str, Any]) -> int:
    """Pull the schedule id from a ``schedule_workout`` response.

    Tries ``workoutScheduleId``, then ``scheduledWorkoutId``, then ``id``.
    Raises :class:`ValueError` if none are present so a silent ``None`` does
    not propagate into the audit log or the tool's return value.
    """
    for key in _SCHEDULE_ID_KEYS:
        value = response.get(key)
        if value is not None:
            return int(value)
    raise ValueError(
        f"schedule_workout response missing schedule id; tried {_SCHEDULE_ID_KEYS} "
        f"in {sorted(response.keys())!r}"
    )


def _extract_workout_id(response: dict[str, Any]) -> int:
    """Pull the workout id from an ``upload_running_workout`` response."""
    for key in _WORKOUT_ID_KEYS:
        value = response.get(key)
        if value is not None:
            return int(value)
    raise ValueError(
        f"upload_running_workout response missing workoutId; got {sorted(response.keys())!r}"
    )


# --- tool implementations --------------------------------------------------


def create_and_schedule(workout: Workout, date_iso: str) -> CreateAndScheduleResult:
    """Create a workout on Garmin Connect and schedule it for a specific date.

    Args:
        workout: Canonical :class:`src.models.Workout` to upload.
        date_iso: Target date as ``YYYY-MM-DD``, interpreted in the user's
            configured timezone.

    Returns:
        A dict with keys ``workout_id``, ``scheduled_id``, and ``date``.

    Raises:
        ValueError: If ``date_iso`` is not a valid calendar date or refers to
            a date in the past (strictly earlier than today in the user's
            timezone).
    """
    target_date = _parse_iso_date(date_iso, "date_iso")
    today = _today_in_user_tz()
    if target_date < today:
        raise ValueError(
            f"date_iso={date_iso} is in the past (today is {today.isoformat()} "
            f"in the user's timezone); refusing to schedule a workout."
        )

    client = _get_client()

    garmin_workout = to_garmin(workout)
    upload_response = client.upload_running_workout(garmin_workout)
    workout_id = _extract_workout_id(upload_response)

    schedule_response = client.schedule_workout(workout_id, date_iso)
    scheduled_id = _extract_scheduled_id(schedule_response)

    result: CreateAndScheduleResult = {
        "workout_id": workout_id,
        "scheduled_id": scheduled_id,
        "date": date_iso,
    }
    audit_record(
        "create_and_schedule",
        {"workout_name": workout.name, "date_iso": date_iso},
        dict(result),
    )
    return result


def list_scheduled_workouts(start_iso: str, end_iso: str) -> list[ScheduledWorkoutSummary]:
    """List scheduled workouts in the given inclusive date range.

    Args:
        start_iso: Inclusive start date as ``YYYY-MM-DD``.
        end_iso: Inclusive end date as ``YYYY-MM-DD``.

    Returns:
        A list of summary dicts: ``{scheduled_id, workout_id, date, name,
        total_duration_sec, source}``. ``source`` is ``"mcp"`` if the
        workout description starts with ``[mcp]``, else ``"external"``.

    Raises:
        ValueError: If either date is malformed or ``end_iso`` precedes
            ``start_iso``.
    """
    start = _parse_iso_date(start_iso, "start_iso")
    end = _parse_iso_date(end_iso, "end_iso")
    if end < start:
        raise ValueError(f"end_iso={end_iso} precedes start_iso={start_iso}")

    client = _get_client()

    summaries: list[ScheduledWorkoutSummary] = []
    for year, month in _iter_year_months(start, end):
        payload = client.get_scheduled_workouts(year, month)
        for item in _iter_calendar_items(payload):
            summary = _project_summary(item)
            item_date = summary["date"]
            if item_date is None:
                # Without a date we can't filter; skip rather than guess.
                continue
            if start_iso <= item_date <= end_iso:
                summaries.append(summary)
    return summaries


def _project_summary(item: dict[str, Any]) -> ScheduledWorkoutSummary:
    """Project a raw Garmin calendar item into the public summary shape."""
    description = _first_present(item, _ITEM_DESCRIPTION_KEYS)
    source = (
        "mcp" if isinstance(description, str) and description.startswith("[mcp]") else "external"
    )
    return {
        "scheduled_id": _coerce_optional_int(_first_present(item, _SCHEDULE_ID_KEYS)),
        "workout_id": _coerce_optional_int(_first_present(item, _WORKOUT_ID_KEYS)),
        "date": _coerce_optional_str(_first_present(item, _ITEM_DATE_KEYS)),
        "name": _coerce_optional_str(_first_present(item, _ITEM_NAME_KEYS)),
        "total_duration_sec": _coerce_optional_float(_first_present(item, _ITEM_DURATION_KEYS)),
        "source": source,
    }


def _coerce_optional_int(value: Any) -> int | None:
    """Coerce to ``int`` if possible, else ``None``. Never raises."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(value: Any) -> float | None:
    """Coerce to ``float`` if possible, else ``None``. Never raises."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_optional_str(value: Any) -> str | None:
    """Return ``value`` as a string, or ``None`` if it is ``None``."""
    if value is None:
        return None
    return str(value)


def register(mcp: FastMCP) -> None:
    """Register the Phase 1 Garmin tools on the supplied FastMCP app.

    Decorates the module-level functions in place via ``mcp.tool()`` so the
    callables remain importable for unit tests that bypass the FastMCP
    machinery entirely.
    """
    mcp.tool()(create_and_schedule)
    mcp.tool()(list_scheduled_workouts)
