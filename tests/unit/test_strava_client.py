"""Unit tests for :mod:`src.strava.client`.

The :class:`src.strava.client.StravaClient` is a thin pass-through around
``stravalib.Client`` plus a small OAuth-refresh state machine. The tests
therefore cover only the behaviours the wrapper actually owns:

1. **Cold-start refresh** — first call triggers a token exchange.
2. **Reuse within validity window** — second call within ~60 s of the cached
   ``expires_at`` does *not* re-exchange.
3. **Refresh when within the 60 s buffer** — second call near expiry *does*
   re-exchange.
4. **Rotated refresh tokens** — if Strava returns a new ``refresh_token``,
   the next exchange uses the rotated one (DESIGN.md §4.1).
5. **Method dispatch** — ``recent_activities``, ``activity_details``, and
   ``weekly_summary`` invoke the right ``stravalib`` method and project the
   model objects into the documented dict shape.
6. **Aggregation correctness** — ``weekly_summary`` buckets stub activities
   spanning three ISO weeks into three buckets with correct totals.

The seam to ``stravalib.Client`` is replaced with a ``_StubStravaLibClient``
patched onto :mod:`src.strava.client`. The seam to the OAuth network call
is the module-level :func:`_refresh_token_via_oauth`, replaced via
``monkeypatch.setattr``. Neither test ever touches real Strava.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from typing import Any

import pytest

from src.strava import client as strava_client_module
from src.strava.client import (
    REFRESH_BUFFER_SECONDS,
    StravaAuthError,
    StravaClient,
)

# --- Stubs -------------------------------------------------------------------


class _StubActivity:
    """Attribute bag that quacks like ``stravalib.model.SummaryActivity``.

    Only the fields the wrapper reads are populated. ``moving_time`` is a
    :class:`datetime.timedelta` to mirror ``stravalib`` v2's quantity-aware
    output; the wrapper's ``_seconds_from_duration`` normalises that.
    """

    def __init__(
        self,
        *,
        activity_id: int,
        name: str,
        sport_type: str,
        start_date: _dt.datetime | None,
        distance: float | None,
        moving_seconds: float | None,
        average_heartrate: float | None,
        description: str | None = None,
        total_elevation_gain: float | None = None,
        splits_metric: list[Any] | None = None,
        laps: list[Any] | None = None,
        perceived_exertion: float | None = None,
    ) -> None:
        self.id = activity_id
        self.name = name
        self.sport_type = sport_type
        self.type = sport_type
        self.start_date = start_date
        self.distance = distance
        self.moving_time = (
            _dt.timedelta(seconds=moving_seconds) if moving_seconds is not None else None
        )
        self.average_heartrate = average_heartrate
        self.description = description
        self.total_elevation_gain = total_elevation_gain
        self.splits_metric = splits_metric or []
        self.laps = laps or []
        self.perceived_exertion = perceived_exertion


class _StubSplit:
    """Quacks like ``stravalib.model.Split`` for splits projection tests."""

    def __init__(
        self,
        *,
        split: int,
        distance: float,
        moving_seconds: float,
        elapsed_seconds: float,
        elevation: float,
        avg_hr: float | None,
        pace_zone: int,
    ) -> None:
        self.split = split
        self.distance = distance
        self.moving_time = _dt.timedelta(seconds=moving_seconds)
        self.elapsed_time = _dt.timedelta(seconds=elapsed_seconds)
        self.elevation_difference = elevation
        self.average_heartrate = avg_hr
        self.pace_zone = pace_zone


class _StubLap:
    """Quacks like ``stravalib.model.Lap`` for laps projection tests."""

    def __init__(
        self,
        *,
        lap_id: int,
        name: str,
        lap_index: int,
        distance: float,
        moving_seconds: float,
        avg_hr: float | None,
        avg_speed: float,
    ) -> None:
        self.id = lap_id
        self.name = name
        self.lap_index = lap_index
        self.distance = distance
        self.moving_time = _dt.timedelta(seconds=moving_seconds)
        self.elapsed_time = _dt.timedelta(seconds=moving_seconds)
        self.average_heartrate = avg_hr
        self.average_speed = avg_speed


class _StubStravaLibClient:
    """Stand-in for ``stravalib.Client``.

    Records the access token it was constructed with so tests can assert
    the wrapper uses the freshly-refreshed token. ``get_activities`` and
    ``get_activity`` are populated per-test via class-level state because
    the wrapper instantiates a new ``Client`` on every call.
    """

    activities_to_return: list[_StubActivity] = []
    detail_to_return: _StubActivity | None = None
    instances: list[_StubStravaLibClient] = []
    get_activities_calls: list[dict[str, Any]] = []
    get_activity_calls: list[int] = []

    def __init__(self, access_token: str | None = None) -> None:
        self.access_token = access_token
        _StubStravaLibClient.instances.append(self)

    def get_activities(
        self,
        before: _dt.datetime | str | None = None,
        after: _dt.datetime | str | None = None,
        limit: int | None = None,
    ) -> Iterator[_StubActivity]:
        _StubStravaLibClient.get_activities_calls.append(
            {"before": before, "after": after, "limit": limit}
        )
        # Honour ``limit`` defensively so the wrapper's own bounding logic
        # is what we're testing (not the stub's leakage).
        if limit is None:
            return iter(_StubStravaLibClient.activities_to_return)
        return iter(_StubStravaLibClient.activities_to_return[:limit])

    def get_activity(self, activity_id: int, include_all_efforts: bool = False) -> _StubActivity:
        _StubStravaLibClient.get_activity_calls.append(activity_id)
        if _StubStravaLibClient.detail_to_return is None:
            raise AssertionError("test forgot to set detail_to_return")
        return _StubStravaLibClient.detail_to_return


@pytest.fixture
def stub_strava(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_StubStravaLibClient]]:
    """Replace ``StravaLibClient`` inside :mod:`src.strava.client` with the stub."""
    _StubStravaLibClient.instances = []
    _StubStravaLibClient.activities_to_return = []
    _StubStravaLibClient.detail_to_return = None
    _StubStravaLibClient.get_activities_calls = []
    _StubStravaLibClient.get_activity_calls = []
    monkeypatch.setattr(strava_client_module, "StravaLibClient", _StubStravaLibClient)
    yield _StubStravaLibClient
    _StubStravaLibClient.instances = []
    _StubStravaLibClient.activities_to_return = []
    _StubStravaLibClient.detail_to_return = None
    _StubStravaLibClient.get_activities_calls = []
    _StubStravaLibClient.get_activity_calls = []


class _OAuthStub:
    """Replacement for :func:`src.strava.client._refresh_token_via_oauth`.

    Records every call onto :attr:`calls` and returns ``responses`` in
    order. If ``responses`` is exhausted, returns the last entry repeatedly
    (this matches the ``stable token cached forever`` scenario without
    forcing the test to populate ``responses`` to match the call count).
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
        self.calls.append((client_id, client_secret, refresh_token))
        if not self.responses:
            raise AssertionError("no responses left for OAuth stub")
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


def _install_oauth_stub(
    monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any]]
) -> _OAuthStub:
    stub = _OAuthStub(responses)
    monkeypatch.setattr(strava_client_module, "_refresh_token_via_oauth", stub)
    return stub


def _freeze_time(monkeypatch: pytest.MonkeyPatch, now_epoch: int) -> None:
    """Pin ``time.time`` inside :mod:`src.strava.client` to ``now_epoch``."""
    monkeypatch.setattr(strava_client_module.time, "time", lambda: now_epoch)


# --- Token refresh -----------------------------------------------------------


def test_cold_start_triggers_refresh(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """First public method call exchanges the refresh token for an access token."""
    now = 1_700_000_000
    _freeze_time(monkeypatch, now)
    oauth = _install_oauth_stub(
        monkeypatch,
        [
            {
                "access_token": "access-1",
                "expires_at": now + 6 * 3600,
                "refresh_token": "refresh-seed",
            }
        ],
    )

    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="refresh-seed")
    client.recent_activities(limit=5)

    assert oauth.calls == [("cid", "secret", "refresh-seed")]
    # The stravalib client constructed by the wrapper sees the fresh token.
    assert stub_strava.instances[-1].access_token == "access-1"


def test_token_reused_within_validity_window(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second call inside the validity window must not call OAuth again."""
    now = 1_700_000_000
    _freeze_time(monkeypatch, now)
    expires_at = now + 6 * 3600
    oauth = _install_oauth_stub(
        monkeypatch,
        [{"access_token": "access-1", "expires_at": expires_at, "refresh_token": "rt-seed"}],
    )

    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")
    client.recent_activities(limit=1)
    client.recent_activities(limit=1)

    assert len(oauth.calls) == 1


def test_refresh_triggered_when_within_buffer(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the cached token is within the 60 s buffer of expiry, refresh again."""
    now = 1_700_000_000
    expires_at = now + 6 * 3600
    # First call returns a token expiring at ``expires_at``. We jump time
    # forward to within the buffer and assert a second refresh occurs.
    oauth = _install_oauth_stub(
        monkeypatch,
        [
            {"access_token": "access-1", "expires_at": expires_at, "refresh_token": "rt-seed"},
            {"access_token": "access-2", "expires_at": expires_at + 3600, "refresh_token": "rt-2"},
        ],
    )

    current_now = now
    monkeypatch.setattr(strava_client_module.time, "time", lambda: current_now)
    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")
    client.recent_activities(limit=1)

    # Jump forward to inside the 60 s buffer (one second tighter than the
    # buffer so the freshness check returns False).
    current_now = expires_at - (REFRESH_BUFFER_SECONDS - 1)
    monkeypatch.setattr(strava_client_module.time, "time", lambda: current_now)
    client.recent_activities(limit=1)

    assert len(oauth.calls) == 2
    # The second exchange used the seed refresh token (none was rotated yet
    # since the first response returned the same one).
    assert oauth.calls[1][2] == "rt-seed"


def test_rotated_refresh_token_used_on_next_exchange(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Strava rotates the refresh token, the next exchange must use the new one."""
    now = 1_700_000_000
    expires_at = now + 6 * 3600
    oauth = _install_oauth_stub(
        monkeypatch,
        [
            {
                "access_token": "access-1",
                "expires_at": expires_at,
                # Strava issues a rotated refresh token on every exchange.
                "refresh_token": "rt-rotated",
            },
            {
                "access_token": "access-2",
                "expires_at": expires_at + 3600,
                "refresh_token": "rt-rotated-again",
            },
        ],
    )

    current_now = now
    monkeypatch.setattr(strava_client_module.time, "time", lambda: current_now)
    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")
    client.recent_activities(limit=1)

    current_now = expires_at - (REFRESH_BUFFER_SECONDS - 1)
    monkeypatch.setattr(strava_client_module.time, "time", lambda: current_now)
    client.recent_activities(limit=1)

    assert oauth.calls[0][2] == "rt-seed"
    assert oauth.calls[1][2] == "rt-rotated"


def test_oauth_failure_raises_strava_auth_error(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OAuth payload missing the required fields surfaces as ``StravaAuthError``."""
    _freeze_time(monkeypatch, 1_700_000_000)
    _install_oauth_stub(monkeypatch, [{"error": "invalid_grant"}])

    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")

    with pytest.raises(StravaAuthError) as excinfo:
        client.recent_activities(limit=1)
    assert "strava_bootstrap" in str(excinfo.value)


# --- recent_activities --------------------------------------------------------


def _seed_oauth(monkeypatch: pytest.MonkeyPatch, now: int) -> None:
    _freeze_time(monkeypatch, now)
    _install_oauth_stub(
        monkeypatch,
        [
            {
                "access_token": "access-1",
                "expires_at": now + 6 * 3600,
                "refresh_token": "rt-seed",
            }
        ],
    )


def test_recent_activities_projection_and_limit(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: limit, after_iso, and projection shape."""
    now = 1_700_000_000
    _seed_oauth(monkeypatch, now)

    stub_strava.activities_to_return = [
        _StubActivity(
            activity_id=1,
            name="Easy run",
            sport_type="Run",
            start_date=_dt.datetime(2026, 5, 1, 6, 0, tzinfo=_dt.UTC),
            distance=5000.0,
            moving_seconds=1800.0,  # 30 min → pace 360 s/km
            average_heartrate=142.0,
        ),
        _StubActivity(
            activity_id=2,
            name="Strides",
            sport_type="Run",
            start_date=_dt.datetime(2026, 5, 2, 6, 0, tzinfo=_dt.UTC),
            distance=4000.0,
            moving_seconds=1200.0,
            average_heartrate=None,
        ),
        _StubActivity(
            activity_id=3,
            name="Filler",
            sport_type="Run",
            start_date=_dt.datetime(2026, 5, 3, 6, 0, tzinfo=_dt.UTC),
            distance=3000.0,
            moving_seconds=900.0,
            average_heartrate=None,
        ),
    ]

    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")
    result = client.recent_activities(limit=2, after_iso="2026-04-30T00:00:00+00:00")

    assert len(result) == 2
    first = result[0]
    assert first["id"] == 1
    assert first["name"] == "Easy run"
    assert first["sport_type"] == "Run"
    assert first["distance_meters"] == 5000.0
    assert first["moving_time_seconds"] == 1800.0
    assert first["average_heartrate"] == 142.0
    assert first["average_pace_sec_per_km"] == pytest.approx(360.0)
    assert first["start_date"] == "2026-05-01T06:00:00+00:00"

    # ``after_iso`` is forwarded verbatim, and ``limit`` is passed through
    # because no sport filter was set.
    call = stub_strava.get_activities_calls[0]
    assert call["after"] == "2026-04-30T00:00:00+00:00"
    assert call["limit"] == 2


def test_recent_activities_sport_filter_skips_non_matching(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a sport filter, non-matching activities are dropped post-fetch."""
    now = 1_700_000_000
    _seed_oauth(monkeypatch, now)

    stub_strava.activities_to_return = [
        _StubActivity(
            activity_id=10,
            name="Ride",
            sport_type="Ride",
            start_date=_dt.datetime(2026, 5, 1, 6, 0, tzinfo=_dt.UTC),
            distance=20000.0,
            moving_seconds=3600.0,
            average_heartrate=130.0,
        ),
        _StubActivity(
            activity_id=11,
            name="Tempo run",
            sport_type="Run",
            start_date=_dt.datetime(2026, 5, 2, 6, 0, tzinfo=_dt.UTC),
            distance=8000.0,
            moving_seconds=2400.0,
            average_heartrate=155.0,
        ),
        _StubActivity(
            activity_id=12,
            name="Easy run",
            sport_type="Run",
            start_date=_dt.datetime(2026, 5, 3, 6, 0, tzinfo=_dt.UTC),
            distance=6000.0,
            moving_seconds=2100.0,
            average_heartrate=140.0,
        ),
    ]

    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")
    result = client.recent_activities(limit=2, sport_filter="Run")

    assert [a["id"] for a in result] == [11, 12]
    # With a sport filter, the wrapper requests unbounded pagination so it
    # can keep pulling until it gets ``limit`` matches.
    call = stub_strava.get_activities_calls[0]
    assert call["limit"] is None


# --- activity_details ---------------------------------------------------------


def test_activity_details_projection(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``activity_details`` returns the documented dict shape, including splits and laps."""
    now = 1_700_000_000
    _seed_oauth(monkeypatch, now)

    detail = _StubActivity(
        activity_id=99,
        name="Long run",
        sport_type="Run",
        start_date=_dt.datetime(2026, 5, 1, 6, 0, tzinfo=_dt.UTC),
        distance=20000.0,
        moving_seconds=6000.0,
        average_heartrate=150.0,
        description="LSD with strides at the end",
        total_elevation_gain=120.0,
        splits_metric=[
            _StubSplit(
                split=1,
                distance=1000.0,
                moving_seconds=300.0,
                elapsed_seconds=300.0,
                elevation=5.0,
                avg_hr=140.0,
                pace_zone=2,
            ),
            _StubSplit(
                split=2,
                distance=1000.0,
                moving_seconds=298.0,
                elapsed_seconds=300.0,
                elevation=4.0,
                avg_hr=145.0,
                pace_zone=2,
            ),
        ],
        laps=[
            _StubLap(
                lap_id=501,
                name="Lap 1",
                lap_index=1,
                distance=5000.0,
                moving_seconds=1500.0,
                avg_hr=148.0,
                avg_speed=3.33,
            )
        ],
        perceived_exertion=6.5,
    )
    stub_strava.detail_to_return = detail

    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")
    result = client.activity_details(99)

    assert stub_strava.get_activity_calls == [99]
    assert result["id"] == 99
    assert result["name"] == "Long run"
    assert result["description"] == "LSD with strides at the end"
    assert result["distance_meters"] == 20000.0
    assert result["moving_time_seconds"] == 6000.0
    assert result["total_elevation_gain"] == 120.0
    assert result["perceived_exertion"] == 6.5
    assert len(result["splits_metric"]) == 2
    assert result["splits_metric"][0] == {
        "split": 1,
        "distance_meters": 1000.0,
        "moving_time_seconds": 300.0,
        "elapsed_time_seconds": 300.0,
        "elevation_difference": 5.0,
        "average_heartrate": 140.0,
        "pace_zone": 2,
    }
    assert len(result["laps"]) == 1
    assert result["laps"][0]["id"] == 501
    assert result["laps"][0]["distance_meters"] == 5000.0


# --- weekly_summary -----------------------------------------------------------


def test_weekly_summary_aggregates_three_buckets(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activities spanning three ISO weeks produce three buckets, oldest first."""
    now = 1_700_000_000
    _seed_oauth(monkeypatch, now)

    # Week A: 2026-04-13 (Mon) → ISO week 16 of 2026. Two runs.
    # Week B: 2026-04-20 (Mon) → ISO week 17 of 2026. One run, no HR.
    # Week C: 2026-04-27 (Mon) → ISO week 18 of 2026. One run.
    stub_strava.activities_to_return = [
        _StubActivity(
            activity_id=1,
            name="A1",
            sport_type="Run",
            start_date=_dt.datetime(2026, 4, 13, 6, 0, tzinfo=_dt.UTC),
            distance=5000.0,
            moving_seconds=1500.0,
            average_heartrate=140.0,
        ),
        _StubActivity(
            activity_id=2,
            name="A2",
            sport_type="Run",
            start_date=_dt.datetime(2026, 4, 15, 6, 0, tzinfo=_dt.UTC),
            distance=10000.0,
            moving_seconds=3000.0,
            average_heartrate=150.0,
        ),
        _StubActivity(
            activity_id=3,
            name="B1",
            sport_type="Run",
            start_date=_dt.datetime(2026, 4, 22, 6, 0, tzinfo=_dt.UTC),
            distance=8000.0,
            moving_seconds=2400.0,
            average_heartrate=None,
        ),
        _StubActivity(
            activity_id=4,
            name="C1",
            sport_type="Run",
            start_date=_dt.datetime(2026, 4, 28, 6, 0, tzinfo=_dt.UTC),
            distance=12000.0,
            moving_seconds=3600.0,
            average_heartrate=145.0,
        ),
    ]

    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")
    result = client.weekly_summary(weeks_back=4)

    assert len(result) == 3
    # Oldest week first.
    assert [bucket["week_start"] for bucket in result] == [
        "2026-04-13",
        "2026-04-20",
        "2026-04-27",
    ]

    week_a = result[0]
    assert week_a["runs"] == 2
    assert week_a["total_km"] == pytest.approx(15.0)
    assert week_a["total_seconds"] == 4500
    assert week_a["avg_hr"] == pytest.approx(145.0)

    week_b = result[1]
    assert week_b["runs"] == 1
    assert week_b["total_km"] == pytest.approx(8.0)
    assert week_b["total_seconds"] == 2400
    assert week_b["avg_hr"] is None  # no HR reported

    week_c = result[2]
    assert week_c["runs"] == 1
    assert week_c["total_km"] == pytest.approx(12.0)
    assert week_c["total_seconds"] == 3600
    assert week_c["avg_hr"] == pytest.approx(145.0)


def test_weekly_summary_rejects_non_positive_weeks_back(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``weeks_back < 1`` is a programmer error and must raise."""
    _seed_oauth(monkeypatch, 1_700_000_000)
    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")
    with pytest.raises(ValueError, match="weeks_back must be >= 1"):
        client.weekly_summary(weeks_back=0)


def test_weekly_summary_passes_after_filter_to_stravalib(
    stub_strava: type[_StubStravaLibClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``after`` arg handed to stravalib is ``now - weeks_back * 7`` days."""
    # Pin the wall clock so we can compute the expected ``after`` boundary.
    now = 1_700_000_000

    class _FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz: _dt.tzinfo | None = None) -> _dt.datetime:  # type: ignore[override]
            return _dt.datetime.fromtimestamp(now, tz=tz)

    monkeypatch.setattr(strava_client_module._dt, "datetime", _FrozenDateTime)
    _seed_oauth(monkeypatch, now)

    client = StravaClient(client_id="cid", client_secret="secret", refresh_token="rt-seed")
    client.weekly_summary(weeks_back=2)

    call = stub_strava.get_activities_calls[0]
    expected_after = (
        _dt.datetime.fromtimestamp(now, tz=_dt.UTC) - _dt.timedelta(days=14)
    ).isoformat()
    assert call["after"] == expected_after
