"""Thin wrapper over ``garminconnect.Garmin`` for the MCP server's Garmin tools.

This module is the seam between the untyped ``garminconnect`` library and the
typed code in the rest of the project. Responsibilities:

* Lazy login: instantiate :class:`garminconnect.Garmin` on the first call and
  cache it. ``Garmin.login(tokenstore=...)`` performs the DI-token refresh
  using the token file written by ``scripts/garmin_bootstrap.py`` (DESIGN.md
  §4.2). Subsequent method calls reuse the cached client.
* Auth-error translation: any failure during the lazy login is wrapped in
  :class:`GarminAuthError` with an actionable hint pointing at the bootstrap
  script. The MCP tools layer (T11) can catch this single type rather than
  the three concrete ``GarminConnect*Error`` flavours.
* Structured logging: every public method emits ``logger.info("garmin.call",
  method=..., args=...)`` via :mod:`structlog`. Argument values are
  truncated to 100 chars to keep the audit-friendly logs bounded (CLAUDE.md
  logging convention).

**Return-type note (deliberate exception to CLAUDE.md).**
``garminconnect``'s public surface returns ``dict[str, Any]`` for most
methods and bare ``Any`` for the delete/unschedule calls. We are an
intentionally-thin pass-through layer, so we propagate those types verbatim
rather than introducing a leaky typed wrapper. Higher layers (T11 tools)
project these dicts into Pydantic models where the schema is known. This is
the single boundary in the project where ``dict[str, Any]`` returns are
acceptable; flag in code review if any future module repeats this pattern.

**Method coverage (T07 scope).**
The MCP tools tickets (T11, T14) only need the workout/calendar surface:

* ``upload_running_workout(running_workout)``
* ``schedule_workout(workout_id, date_iso)``
* ``unschedule_workout(scheduled_id)``
* ``delete_workout(workout_id)``
* ``get_workouts(start=0, limit=100)``
* ``get_workout_by_id(workout_id)``
* ``get_scheduled_workouts(year, month)`` — month is 1-indexed at the API
  boundary; the library internally subtracts 1 to hit Garmin's 0-indexed
  URL (T04 reviewer note).
* ``get_scheduled_workout_by_id(scheduled_id)``

Anything not listed here is intentionally absent — add it explicitly when a
tools ticket needs it, rather than exposing the whole ``Garmin`` surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

logger = structlog.get_logger(__name__)

_MAX_ARG_REPR_LEN = 100
_TRUNCATE_SUFFIX = "..."


class GarminAuthError(RuntimeError):
    """Raised when Garmin token loading or login fails.

    The message always includes a hint to re-run the bootstrap script so the
    caller (or the operator reading the logs) knows the remediation path
    without consulting DESIGN.md.
    """


def _short_repr(value: Any) -> str:
    """Return a short ``repr`` of ``value`` capped at :data:`_MAX_ARG_REPR_LEN`.

    Garmin workout payloads can be tens of kilobytes; logging them verbatim
    pollutes stdout and risks leaking PII (HR ranges, descriptions). The
    truncation is best-effort — we trim the ``repr`` string itself, so a
    long ``RunningWorkout`` instance becomes e.g.
    ``"RunningWorkout(workoutName='Easy run', estimatedDurati..."``.
    """
    text = repr(value)
    if len(text) <= _MAX_ARG_REPR_LEN:
        return text
    return text[:_MAX_ARG_REPR_LEN] + _TRUNCATE_SUFFIX


def _log_call(method: str, **call_args: Any) -> None:
    """Emit a structured ``garmin.call`` log line with truncated arg reprs."""
    safe_args = {name: _short_repr(value) for name, value in call_args.items()}
    logger.info("garmin.call", method=method, args=safe_args)


class GarminClient:
    """Lazy-logging-in pass-through to :class:`garminconnect.Garmin`.

    Construct with an explicit ``token_dir`` for tests (no settings import)
    or omit it in production to fall back to ``settings.garmin_token_dir``.
    The actual ``Garmin`` instance is created on the first method call and
    cached for the lifetime of this object.

    Example::

        client = GarminClient()
        workouts = client.get_workouts(limit=10)  # triggers login here
        client.delete_workout(workouts[0]["workoutId"])  # reuses cached login

    The wrapper does not retry, rate-limit, or interpret responses — that
    belongs in the tools layer or the library itself.
    """

    def __init__(self, token_dir: Path | None = None) -> None:
        """Store the token directory; defer settings lookup until first use.

        Passing ``token_dir`` directly lets tests construct a client without
        importing :mod:`src.config` (which requires four mandatory env vars).
        """
        self._token_dir_override = token_dir
        self._client: Garmin | None = None

    @property
    def token_dir(self) -> Path:
        """Resolve the token directory, falling back to ``settings.garmin_token_dir``.

        Lazy so tests can construct a :class:`GarminClient` without setting
        the env vars that ``src.config`` requires at import time.
        """
        if self._token_dir_override is not None:
            return self._token_dir_override
        # Imported lazily so a bare ``import src.garmin.client`` does not
        # crash in a fresh shell with no ``.env`` present.
        from src.config import settings

        return settings.garmin_token_dir

    def _ensure_logged_in(self) -> Garmin:
        """Return the cached ``Garmin`` client, logging in on first call.

        Wraps every concrete auth/connection exception from the library in
        :class:`GarminAuthError` so callers only need to catch one type.
        """
        if self._client is not None:
            return self._client

        client = Garmin()
        token_dir = self.token_dir
        logger.info("garmin.login.start", token_dir=str(token_dir))
        try:
            client.login(tokenstore=str(token_dir))
        except GarminConnectAuthenticationError as exc:
            raise GarminAuthError(
                f"Garmin authentication failed using tokens at {token_dir}. "
                "Re-run `uv run python -m scripts.garmin_bootstrap` to refresh."
            ) from exc
        except GarminConnectConnectionError as exc:
            raise GarminAuthError(
                f"Garmin login failed (connection error) using tokens at {token_dir}. "
                "If this persists, re-run `uv run python -m scripts.garmin_bootstrap`."
            ) from exc
        except FileNotFoundError as exc:
            raise GarminAuthError(
                f"No Garmin tokens found at {token_dir}. "
                "Run `uv run python -m scripts.garmin_bootstrap` first."
            ) from exc

        self._client = client
        logger.info("garmin.login.ok", token_dir=str(token_dir))
        return client

    # --- Workout library --------------------------------------------------

    def upload_running_workout(self, running_workout: Any) -> dict[str, Any]:
        """Create a workout template in the Garmin library.

        ``running_workout`` is a ``garminconnect.workout.RunningWorkout``
        instance (constructed by the forward translator in T08). Returns the
        library response dict, which contains ``workoutId`` at the top level
        plus various other metadata Garmin echoes back.
        """
        client = self._ensure_logged_in()
        _log_call("upload_running_workout", running_workout=running_workout)
        return client.upload_running_workout(running_workout)

    def schedule_workout(self, workout_id: int | str, date_iso: str) -> dict[str, Any]:
        """Place a library workout on the calendar for ``date_iso`` (``YYYY-MM-DD``).

        Returns the schedule response dict. The id of the new calendar entry
        is exposed under ``workoutScheduleId`` (per T04's live shakedown);
        ``scheduledWorkoutId`` / ``id`` are defensively accepted by the
        higher tools layer.
        """
        client = self._ensure_logged_in()
        _log_call("schedule_workout", workout_id=workout_id, date_iso=date_iso)
        return client.schedule_workout(workout_id, date_iso)

    def unschedule_workout(self, scheduled_id: int | str) -> Any:
        """Remove a calendar entry. Library workout template stays intact.

        Returns whatever the underlying library returns (the upstream type
        hint is bare ``Any`` because the response body is empty on success).
        """
        client = self._ensure_logged_in()
        _log_call("unschedule_workout", scheduled_id=scheduled_id)
        return client.unschedule_workout(scheduled_id)

    def delete_workout(self, workout_id: int | str) -> Any:
        """Delete a workout template from the library.

        Returns whatever the underlying library returns — same ``Any`` shape
        as :meth:`unschedule_workout`.
        """
        client = self._ensure_logged_in()
        _log_call("delete_workout", workout_id=workout_id)
        return client.delete_workout(workout_id)

    def get_workouts(self, start: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """List workout templates in the library (paginated)."""
        client = self._ensure_logged_in()
        _log_call("get_workouts", start=start, limit=limit)
        return client.get_workouts(start=start, limit=limit)

    def get_workout_by_id(self, workout_id: int | str) -> dict[str, Any]:
        """Fetch one workout template by id."""
        client = self._ensure_logged_in()
        _log_call("get_workout_by_id", workout_id=workout_id)
        return client.get_workout_by_id(workout_id)

    # --- Calendar / scheduled workouts ------------------------------------

    def get_scheduled_workouts(self, year: int, month: int) -> dict[str, Any]:
        """Return calendar items for ``year``/``month`` (month is 1-indexed).

        The library subtracts 1 internally to hit Garmin's 0-indexed URL —
        pass ``date.month`` directly here. The response envelope is not
        pinned in ``garminconnect`` 0.2.x; the T04 tracer documents that
        items live under ``calendarItems`` / ``items`` / ``scheduledWorkouts``
        or a bare list, so the tools layer handles that defensively.
        """
        client = self._ensure_logged_in()
        _log_call("get_scheduled_workouts", year=year, month=month)
        return client.get_scheduled_workouts(year=year, month=month)

    def get_scheduled_workout_by_id(self, scheduled_id: int | str) -> dict[str, Any]:
        """Fetch one calendar entry by its scheduled-id."""
        client = self._ensure_logged_in()
        _log_call("get_scheduled_workout_by_id", scheduled_id=scheduled_id)
        return client.get_scheduled_workout_by_id(scheduled_id)
