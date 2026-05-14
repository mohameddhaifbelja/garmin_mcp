"""Thin wrapper over ``stravalib.Client`` for the MCP server's Strava tools.

This module is the seam between the untyped ``stravalib`` library + Strava's
OAuth surface and the typed code in the rest of the project. Responsibilities:

* **OAuth refresh on cold start.** Strava issues short-lived access tokens
  (~6h). On every public method call we check the in-memory
  ``(access_token, expires_at)`` cache. If empty or within 60 s of expiry,
  we ``POST https://www.strava.com/oauth/token`` with ``grant_type=
  refresh_token`` to swap the long-lived refresh token for a fresh access
  token. Strava rotates refresh tokens on every exchange (per DESIGN.md
  §4.1), so we always capture the response's ``refresh_token`` even when it
  matches the inbound one.
* **Lazy settings resolution.** Constructor args may be ``None`` — those
  fields are resolved from :mod:`src.config` on the first call, so tests can
  instantiate :class:`StravaClient` without populating four env vars.
  Mirrors the lazy pattern in :class:`src.garmin.client.GarminClient`.
* **Projection to plain dicts.** ``stravalib`` returns Pydantic-v2 model
  objects (``SummaryActivity``, ``DetailedActivity``). We project them into
  ``dict[str, Any]`` containing only the fields the MCP tools (T17) need —
  no full model echoing, no leaking ``stravalib`` types upward.

**Return-type note (deliberate exception to CLAUDE.md).** Like
:mod:`src.garmin.client`, this wrapper is an intentionally thin pass-through
at a library seam. The MCP tools layer (T17) projects these dicts into
Pydantic models where the schema is known. ``dict[str, Any]`` is acceptable
*here only*; flag in code review if any future module repeats this pattern.

**HTTP library choice.** We call ``POST /oauth/token`` directly via
``httpx`` (transitively available — ``mcp`` and ``fastapi`` both depend on
it) rather than via ``stravalib.Client.refresh_access_token`` so the
network seam is trivial to stub in unit tests
(``monkeypatch.setattr(strava_client_module, "_refresh_token_via_oauth",
stub)``).
"""

from __future__ import annotations

import datetime as _dt
import time
from collections import defaultdict
from typing import Any

import httpx
import structlog
from stravalib import Client as StravaLibClient

logger = structlog.get_logger(__name__)

STRAVA_OAUTH_TOKEN_URL = "https://www.strava.com/oauth/token"
"""Strava's OAuth token endpoint (DESIGN.md §4.1)."""

REFRESH_BUFFER_SECONDS = 60
"""Refresh the access token if it expires within this many seconds."""

OAUTH_HTTP_TIMEOUT_SECONDS = 10.0
"""Timeout for the OAuth refresh call. Strava is normally fast — 10 s is a
generous ceiling that still keeps a stuck MCP tool from hanging the session."""


class StravaAuthError(RuntimeError):
    """Raised when the Strava OAuth refresh fails.

    Message includes a hint to re-run ``scripts/strava_bootstrap.py`` so the
    operator reading the logs knows the remediation path without consulting
    DESIGN.md.
    """


def _refresh_token_via_oauth(
    client_id: str, client_secret: str, refresh_token: str
) -> dict[str, Any]:
    """POST to Strava's OAuth endpoint and return the parsed JSON body.

    Module-level (not a method) so tests can stub it with
    ``monkeypatch.setattr(strava_client_module, "_refresh_token_via_oauth",
    stub)`` without instantiating an httpx mock transport.

    On HTTP error, raises :class:`StravaAuthError` with the upstream
    response status + body fragment attached for diagnostics.
    """
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    try:
        response = httpx.post(
            STRAVA_OAUTH_TOKEN_URL,
            data=payload,
            timeout=OAUTH_HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise StravaAuthError(
            "Strava OAuth refresh network error. "
            "Re-run `uv run python -m scripts.strava_bootstrap` if this persists."
        ) from exc

    if response.status_code != 200:
        body_excerpt = response.text[:200]
        raise StravaAuthError(
            f"Strava OAuth refresh failed: HTTP {response.status_code} {body_excerpt!r}. "
            "Re-run `uv run python -m scripts.strava_bootstrap` to mint a new refresh token."
        )

    return response.json()


def _activity_avg_pace_sec_per_km(
    distance_meters: float | None, moving_time_seconds: float | None
) -> float | None:
    """Compute average pace (sec / km) from distance + moving time.

    Returns ``None`` if either input is missing or distance is zero — pace
    is undefined for zero-distance manual entries.
    """
    if not distance_meters or not moving_time_seconds:
        return None
    if distance_meters <= 0:
        return None
    return float(moving_time_seconds) / (float(distance_meters) / 1000.0)


def _coerce_float(value: Any) -> float | None:
    """Return ``float(value)`` or ``None`` if ``value`` is missing/non-numeric.

    ``stravalib`` returns ``pint.Quantity`` for some fields when its unit
    handling kicks in; ``float()`` works on both raw numbers and quantities
    that implement ``__float__``. Anything else returns ``None`` instead of
    crashing the projection.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    """Return ``int(value)`` or ``None`` if conversion fails."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds_from_duration(value: Any) -> float | None:
    """Return seconds from a value that may be a ``timedelta`` or a number.

    ``stravalib`` v2 surfaces durations as ``datetime.timedelta`` instances
    (e.g. ``moving_time``); raw numerics still occur in nested split dicts.
    Anything we can't convert returns ``None``.
    """
    if value is None:
        return None
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    return _coerce_float(value)


def _start_date_iso(value: Any) -> str | None:
    """Return an ISO-8601 string for an activity's start date.

    ``stravalib`` returns ``datetime.datetime`` for ``start_date``; the
    weekly-summary helper also needs to bucket activities by ISO week, so we
    normalise to a stable string here and let callers parse if needed.
    """
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)


def _summary_activity_to_dict(activity: Any) -> dict[str, Any]:
    """Project a ``stravalib`` ``SummaryActivity`` (or attr bag) into a dict.

    Output shape (consumed by T17's ``list_recent_activities`` tool):

    ``id``                            — Strava activity id (int)
    ``name``                          — User-set title (str)
    ``sport_type``                    — e.g. ``"Run"``, ``"TrailRun"`` (str)
    ``start_date``                    — ISO-8601 UTC start (str)
    ``distance_meters``               — float or None
    ``moving_time_seconds``           — float or None
    ``average_heartrate``             — float or None (bpm)
    ``average_pace_sec_per_km``       — float or None, computed from
                                        distance + moving time
    """
    distance_m = _coerce_float(getattr(activity, "distance", None))
    moving_s = _seconds_from_duration(getattr(activity, "moving_time", None))
    sport_type = getattr(activity, "sport_type", None) or getattr(activity, "type", None)
    return {
        "id": _coerce_int(getattr(activity, "id", None)),
        "name": getattr(activity, "name", None),
        "sport_type": str(sport_type) if sport_type is not None else None,
        "start_date": _start_date_iso(getattr(activity, "start_date", None)),
        "distance_meters": distance_m,
        "moving_time_seconds": moving_s,
        "average_heartrate": _coerce_float(getattr(activity, "average_heartrate", None)),
        "average_pace_sec_per_km": _activity_avg_pace_sec_per_km(distance_m, moving_s),
    }


def _split_to_dict(split: Any) -> dict[str, Any]:
    """Project a ``stravalib`` ``Split`` model (or attr bag) into a dict."""
    return {
        "split": _coerce_int(getattr(split, "split", None)),
        "distance_meters": _coerce_float(getattr(split, "distance", None)),
        "moving_time_seconds": _seconds_from_duration(getattr(split, "moving_time", None)),
        "elapsed_time_seconds": _seconds_from_duration(getattr(split, "elapsed_time", None)),
        "elevation_difference": _coerce_float(getattr(split, "elevation_difference", None)),
        "average_heartrate": _coerce_float(getattr(split, "average_heartrate", None)),
        "pace_zone": _coerce_int(getattr(split, "pace_zone", None)),
    }


def _lap_to_dict(lap: Any) -> dict[str, Any]:
    """Project a ``stravalib`` ``Lap`` (or attr bag) into a dict."""
    return {
        "id": _coerce_int(getattr(lap, "id", None)),
        "name": getattr(lap, "name", None),
        "lap_index": _coerce_int(getattr(lap, "lap_index", None)),
        "distance_meters": _coerce_float(getattr(lap, "distance", None)),
        "moving_time_seconds": _seconds_from_duration(getattr(lap, "moving_time", None)),
        "elapsed_time_seconds": _seconds_from_duration(getattr(lap, "elapsed_time", None)),
        "average_heartrate": _coerce_float(getattr(lap, "average_heartrate", None)),
        "average_speed": _coerce_float(getattr(lap, "average_speed", None)),
    }


def _detailed_activity_to_dict(activity: Any) -> dict[str, Any]:
    """Project a ``stravalib`` ``DetailedActivity`` (or attr bag) into a dict.

    Output shape (consumed by T17's ``get_activity_details`` tool):

    ``id``                       — Strava activity id (int)
    ``name``                     — User-set title (str)
    ``sport_type``               — e.g. ``"Run"`` (str)
    ``start_date``               — ISO-8601 UTC start (str)
    ``description``              — Athlete-written notes (str | None)
    ``distance_meters``          — float | None
    ``moving_time_seconds``      — float | None
    ``total_elevation_gain``     — meters, float | None
    ``average_heartrate``        — float | None (bpm)
    ``splits_metric``            — list[dict] of km-splits (see
                                   :func:`_split_to_dict`)
    ``laps``                     — list[dict] of laps (see
                                   :func:`_lap_to_dict`)
    ``perceived_exertion``       — float | None (athlete-reported 1–10)
    """
    splits_raw = getattr(activity, "splits_metric", None) or []
    laps_raw = getattr(activity, "laps", None) or []
    sport_type = getattr(activity, "sport_type", None) or getattr(activity, "type", None)
    return {
        "id": _coerce_int(getattr(activity, "id", None)),
        "name": getattr(activity, "name", None),
        "sport_type": str(sport_type) if sport_type is not None else None,
        "start_date": _start_date_iso(getattr(activity, "start_date", None)),
        "description": getattr(activity, "description", None),
        "distance_meters": _coerce_float(getattr(activity, "distance", None)),
        "moving_time_seconds": _seconds_from_duration(getattr(activity, "moving_time", None)),
        "total_elevation_gain": _coerce_float(getattr(activity, "total_elevation_gain", None)),
        "average_heartrate": _coerce_float(getattr(activity, "average_heartrate", None)),
        "splits_metric": [_split_to_dict(s) for s in splits_raw],
        "laps": [_lap_to_dict(lap) for lap in laps_raw],
        "perceived_exertion": _coerce_float(getattr(activity, "perceived_exertion", None)),
    }


class StravaClient:
    """Lazy-refresh pass-through to :class:`stravalib.Client`.

    Construct with explicit OAuth credentials for tests (no settings
    import) or omit them in production to fall back to
    :attr:`src.config.settings`. The access token is fetched on the first
    public-method call and cached in memory until ~60 s before expiry.

    Example::

        client = StravaClient()
        runs = client.recent_activities(limit=20, sport_filter="Run")
        details = client.activity_details(runs[0]["id"])

    The wrapper does not retry HTTP errors from ``stravalib`` or paginate
    beyond the requested ``limit`` — that's the tools layer's responsibility.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        """Store OAuth credential overrides; defer settings lookup until first use.

        Passing all three credentials lets tests construct a client without
        importing :mod:`src.config` (which requires four mandatory env vars).
        """
        self._client_id_override = client_id
        self._client_secret_override = client_secret
        self._refresh_token_override = refresh_token
        self._access_token: str | None = None
        self._expires_at: int | None = None
        # Strava rotates refresh tokens on every exchange; cache the latest
        # rotation in memory so the next refresh uses the freshest token.
        self._current_refresh_token: str | None = None

    # --- Credential resolution --------------------------------------------

    def _resolve_credentials(self) -> tuple[str, str, str]:
        """Return ``(client_id, client_secret, refresh_token)``.

        Falls back to :mod:`src.config` for any field not provided to the
        constructor. The refresh token specifically prefers the most-recent
        rotation captured by :meth:`_refresh` over the constructor seed —
        once Strava rotates a token, the old one is no longer valid.
        """
        client_id = self._client_id_override
        client_secret = self._client_secret_override
        refresh_token = self._current_refresh_token or self._refresh_token_override

        if client_id is None or client_secret is None or refresh_token is None:
            # Imported lazily so ``import src.strava.client`` does not crash
            # in a fresh shell with no ``.env`` present.
            from src.config import settings

            if client_id is None:
                client_id = settings.strava_client_id
            if client_secret is None:
                client_secret = settings.strava_client_secret
            if refresh_token is None:
                refresh_token = settings.strava_refresh_token

        return client_id, client_secret, refresh_token

    # --- Token refresh ----------------------------------------------------

    def _token_is_fresh(self, now: int) -> bool:
        """True if the cached access token is non-empty and >60 s from expiry."""
        if self._access_token is None or self._expires_at is None:
            return False
        return self._expires_at - now > REFRESH_BUFFER_SECONDS

    def _refresh(self) -> None:
        """Exchange the refresh token for a fresh access token.

        Updates ``_access_token`` / ``_expires_at`` and captures any rotated
        ``refresh_token`` for the next exchange. Raises
        :class:`StravaAuthError` if the response is missing required fields.
        """
        client_id, client_secret, refresh_token = self._resolve_credentials()
        logger.info("strava.oauth.refresh.start")
        body = _refresh_token_via_oauth(client_id, client_secret, refresh_token)

        access_token = body.get("access_token")
        expires_at = body.get("expires_at")
        if not isinstance(access_token, str) or not isinstance(expires_at, int):
            raise StravaAuthError(
                "Strava OAuth refresh returned an unexpected payload "
                f"(missing access_token or expires_at): keys={sorted(body)}. "
                "Re-run `uv run python -m scripts.strava_bootstrap` if this persists."
            )

        self._access_token = access_token
        self._expires_at = expires_at
        rotated = body.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            self._current_refresh_token = rotated
        logger.info("strava.oauth.refresh.ok", expires_at=expires_at)

    def _ensure_access_token(self) -> str:
        """Return a valid access token, refreshing if cold or near-expiry."""
        now = int(time.time())
        if not self._token_is_fresh(now):
            self._refresh()
        # ``_refresh`` either sets ``_access_token`` or raises, so this
        # cast-by-assertion is safe.
        assert self._access_token is not None  # noqa: S101  # narrow for type checker
        return self._access_token

    def _build_stravalib_client(self) -> StravaLibClient:
        """Construct a ``stravalib.Client`` bound to the current access token.

        Strava access tokens are short-lived (~6h) and we want every call to
        use the freshest one; constructing per-call avoids stale-token bugs
        if the wrapper outlives a single token's lifetime.
        """
        return StravaLibClient(access_token=self._ensure_access_token())

    # --- Public methods ---------------------------------------------------

    def recent_activities(
        self,
        limit: int,
        after_iso: str | None = None,
        sport_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the athlete's recent activities, newest first.

        Args:
            limit: Maximum activities to return. ``stravalib`` paginates the
                underlying API; the iterator is consumed until ``limit`` is
                reached or it's exhausted.
            after_iso: ISO-8601 timestamp. Activities whose
                ``start_date`` is at or after this value are returned.
                ``None`` (default) means "no lower bound".
            sport_filter: Optional Strava ``sport_type`` literal
                (e.g. ``"Run"``, ``"TrailRun"``, ``"VirtualRun"``). When
                set, activities whose ``sport_type`` (or legacy ``type``)
                does not match are skipped. Filtering is done in this
                wrapper after the API call because the Strava REST surface
                does not accept a sport filter on the list endpoint.

        Returns:
            List of dicts (see :func:`_summary_activity_to_dict` for shape).
        """
        client = self._build_stravalib_client()
        # When ``sport_filter`` is set we may discard activities, so ask
        # ``stravalib`` for unbounded pagination and stop ourselves once we
        # have enough matches. Without the filter, hand ``limit`` to the
        # library so it can short-circuit pagination.
        api_limit: int | None = None if sport_filter else limit
        iterator = client.get_activities(after=after_iso, limit=api_limit)

        results: list[dict[str, Any]] = []
        for activity in iterator:
            projected = _summary_activity_to_dict(activity)
            if sport_filter is not None and projected.get("sport_type") != sport_filter:
                continue
            results.append(projected)
            if len(results) >= limit:
                break
        return results

    def activity_details(self, activity_id: int) -> dict[str, Any]:
        """Fetch full detail for one activity by Strava id.

        Returns a dict (see :func:`_detailed_activity_to_dict` for shape).
        Splits and laps are pre-projected so the tools layer does not need
        to traverse ``stravalib`` model objects.
        """
        client = self._build_stravalib_client()
        activity = client.get_activity(activity_id)
        return _detailed_activity_to_dict(activity)

    def weekly_summary(self, weeks_back: int) -> list[dict[str, Any]]:
        """Aggregate the last ``weeks_back`` weeks of activities into buckets.

        Buckets by ISO calendar week (``(year, week_num)``). Each bucket's
        ``week_start`` is the Monday of that ISO week (``YYYY-MM-DD``).

        Args:
            weeks_back: Number of full weeks of history to summarise. Must
                be ``>= 1``. We fetch activities whose ``start_date`` is at
                or after ``today - weeks_back * 7`` days, UTC.

        Returns:
            List of dicts, oldest week first. Shape per item::

                {
                    "week_start": "YYYY-MM-DD",       # Monday of the ISO week
                    "runs": int,                       # activity count
                    "total_km": float,                 # sum of distance_meters / 1000
                    "total_seconds": int,              # sum of moving_time_seconds
                    "avg_hr": float | None,            # mean of per-activity averages,
                                                       # None if no activity in the week
                                                       # reported heart rate
                }
        """
        if weeks_back < 1:
            raise ValueError(f"weeks_back must be >= 1, got {weeks_back}")

        after_dt = _dt.datetime.now(tz=_dt.UTC) - _dt.timedelta(days=weeks_back * 7)
        client = self._build_stravalib_client()
        iterator = client.get_activities(after=after_dt.isoformat())

        # Bucket key = (year, iso_week_num); value = running totals + hr accumulator.
        buckets: dict[tuple[int, int], dict[str, Any]] = defaultdict(
            lambda: {
                "distance_meters": 0.0,
                "moving_seconds": 0.0,
                "runs": 0,
                "hr_sum": 0.0,
                "hr_count": 0,
            }
        )

        for activity in iterator:
            start_date = getattr(activity, "start_date", None)
            if start_date is None:
                continue
            # ``stravalib`` v2 returns ``datetime.datetime`` here; if we ever
            # receive an ISO string (defensive for stubs), parse it.
            if isinstance(start_date, str):
                start_date = _dt.datetime.fromisoformat(start_date)
            iso_year, iso_week, _ = start_date.isocalendar()
            bucket = buckets[(iso_year, iso_week)]

            distance = _coerce_float(getattr(activity, "distance", None)) or 0.0
            moving = _seconds_from_duration(getattr(activity, "moving_time", None)) or 0.0
            bucket["distance_meters"] += distance
            bucket["moving_seconds"] += moving
            bucket["runs"] += 1

            hr = _coerce_float(getattr(activity, "average_heartrate", None))
            if hr is not None:
                bucket["hr_sum"] += hr
                bucket["hr_count"] += 1

        # Render oldest → newest. ISO weeks sort lexicographically by
        # ``(year, week)`` so a plain tuple sort suffices.
        out: list[dict[str, Any]] = []
        for (iso_year, iso_week), totals in sorted(buckets.items()):
            week_start = _dt.date.fromisocalendar(iso_year, iso_week, 1)  # Monday
            hr_count = totals["hr_count"]
            avg_hr: float | None = totals["hr_sum"] / hr_count if hr_count else None
            out.append(
                {
                    "week_start": week_start.isoformat(),
                    "runs": totals["runs"],
                    "total_km": totals["distance_meters"] / 1000.0,
                    "total_seconds": int(totals["moving_seconds"]),
                    "avg_hr": avg_hr,
                }
            )
        return out
