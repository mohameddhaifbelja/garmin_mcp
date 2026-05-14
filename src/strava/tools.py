"""MCP tools for the Strava read surface (Phase 3).

This module registers the three Strava observational tools defined in
``DESIGN.md`` §6.1 on a :class:`mcp.server.fastmcp.FastMCP` instance:

* ``list_recent_activities`` — summary view of recent runs.
* ``get_activity_details`` — full splits / laps / RPE for one activity.
* ``get_weekly_summary`` — running volume aggregated by ISO calendar week.

Per DESIGN.md §6 / §10, per-tool docstrings stay factual (args + return
shape + a one-line "use this when…" hint). Behavioural orchestration
rules live in the server-level ``instructions`` string set up by T12.

Read-only — no audit log
------------------------
Strava tools never mutate Strava state, so :mod:`src.audit` is not invoked.
This is the documented contract in the T17 ticket; mirror that decision if
ever adding new Strava tools.

Design notes
------------
- A single module-level :class:`StravaClient` is reused across calls. The
  client itself does lazy OAuth refresh on the first public-method call,
  so instantiating it once avoids redundant token exchanges.
- The pattern mirrors :mod:`src.garmin.tools` exactly: module-level
  ``_client`` singleton, ``_get_client()`` accessor, plain tool functions
  decorated inside :func:`register`. Keeping the callables importable at
  module level lets unit tests bypass the FastMCP machinery entirely.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from src.strava.client import StravaClient

# Singleton Strava client. Constructed lazily on first call so importing
# this module does not require ``.env`` to be populated.
_client: StravaClient | None = None


def _get_client() -> StravaClient:
    """Return the module-level :class:`StravaClient`, creating it on demand."""
    global _client
    if _client is None:
        _client = StravaClient()
    return _client


def list_recent_activities(
    limit: int = 30,
    after_iso: str | None = None,
    sport_filter: str | None = None,
) -> list[dict[str, Any]]:
    """List the user's recent Strava activities.

    Use this to understand the user's recent fitness before adjusting pace
    targets or scheduling new workouts. Returns one summary dict per activity.

    Args:
        limit: Maximum number of activities to return (default 30).
        after_iso: Optional ISO-8601 date; only activities at or after this
            date are returned.
        sport_filter: Optional Strava ``sport_type`` literal
            (e.g. ``"Run"``, ``"TrailRun"``).

    Returns:
        A list of dicts. Each has: ``id``, ``name``, ``sport_type``,
        ``start_date``, ``distance_meters``, ``moving_time_seconds``,
        ``average_heartrate``, ``average_pace_sec_per_km``.
    """
    return _get_client().recent_activities(
        limit=limit, after_iso=after_iso, sport_filter=sport_filter
    )


def get_activity_details(activity_id: int) -> dict[str, Any]:
    """Fetch full details for one Strava activity.

    Use this when you need splits, laps, or perceived effort beyond what
    :func:`list_recent_activities` returns.

    Args:
        activity_id: Strava activity id.

    Returns:
        A dict with ``id``, ``name``, ``sport_type``, ``start_date``,
        ``description``, ``distance_meters``, ``moving_time_seconds``,
        ``total_elevation_gain``, ``average_heartrate``, ``splits_metric``,
        ``laps``, ``perceived_exertion``.
    """
    return _get_client().activity_details(activity_id)


def get_weekly_summary(weeks_back: int = 8) -> list[dict[str, Any]]:
    """Aggregate the user's running volume by ISO calendar week.

    Use this to spot recent overload, taper effectiveness, or training
    consistency before adjusting an upcoming workout.

    Args:
        weeks_back: How many calendar weeks of history to aggregate
            (default 8). Must be ``>= 1``.

    Returns:
        A list of dicts, oldest first. Each: ``week_start`` (``YYYY-MM-DD``),
        ``runs``, ``total_km``, ``total_seconds``, ``avg_hr``.
    """
    return _get_client().weekly_summary(weeks_back)


def register(mcp: FastMCP) -> None:
    """Register the three Strava read tools on the supplied FastMCP app.

    Decorates the module-level functions in place via ``mcp.tool()`` so the
    callables remain importable for unit tests that bypass the FastMCP
    machinery entirely. Mirrors :func:`src.garmin.tools.register`.
    """
    mcp.tool()(list_recent_activities)
    mcp.tool()(get_activity_details)
    mcp.tool()(get_weekly_summary)
