"""Unit tests for ``scripts.tracer``.

The tracer script is a live-Garmin smoke test by design — we cannot exercise
the real HTTP path in CI. These tests cover the two pieces of logic that *can*
fail offline:

1. ``build_tracer_workout`` constructs the right dict shape to send to Garmin
   (step types, durations, HR target). Catching regressions here is cheap and
   prevents the live script from burning a real upload to discover a typo.
2. ``_extract_workout_id`` / ``_extract_scheduled_id`` correctly pull ids from
   the response envelope, including the defensive alias keys.
3. ``_iter_calendar_items`` tolerates the plausible response shapes.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts import tracer


def test_build_tracer_workout_has_three_steps_with_expected_durations() -> None:
    workout = tracer.build_tracer_workout()
    payload = workout.to_dict()

    assert payload["workoutName"] == tracer.DEFAULT_WORKOUT_NAME
    assert payload["sportType"]["sportTypeKey"] == "running"
    assert payload["description"].startswith("[mcp]")
    assert payload["estimatedDurationInSecs"] == 20 * 60

    segments = payload["workoutSegments"]
    assert len(segments) == 1
    steps = segments[0]["workoutSteps"]
    assert len(steps) == 3

    warmup, interval, cooldown = steps
    assert warmup["stepType"]["stepTypeKey"] == "warmup"
    assert warmup["endConditionValue"] == float(tracer.WARMUP_SECONDS)
    assert warmup["targetType"]["workoutTargetTypeKey"] == "no.target"

    assert interval["stepType"]["stepTypeKey"] == "interval"
    assert interval["endConditionValue"] == float(tracer.INTERVAL_SECONDS)
    assert interval["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert interval["targetType"]["targetValueOne"] == tracer.INTERVAL_HR_MIN
    assert interval["targetType"]["targetValueTwo"] == tracer.INTERVAL_HR_MAX

    assert cooldown["stepType"]["stepTypeKey"] == "cooldown"
    assert cooldown["endConditionValue"] == float(tracer.COOLDOWN_SECONDS)


def test_build_tracer_workout_respects_custom_name() -> None:
    workout = tracer.build_tracer_workout("custom-name-123")
    assert workout.workoutName == "custom-name-123"


@pytest.mark.parametrize(
    "envelope, expected",
    [
        ({"workoutId": 4451}, 4451),
        ({"id": 9999}, 9999),
        ({"workout_id": 12}, 12),
    ],
)
def test_extract_workout_id_accepts_known_aliases(envelope: dict[str, int], expected: int) -> None:
    assert tracer._extract_workout_id(envelope) == expected


def test_extract_workout_id_raises_on_unknown_envelope() -> None:
    with pytest.raises(RuntimeError, match="workout id"):
        tracer._extract_workout_id({"unexpected": 1})


@pytest.mark.parametrize(
    "envelope, expected",
    [
        ({"workoutScheduleId": 99231}, 99231),
        ({"scheduledWorkoutId": 8}, 8),
        ({"id": 1}, 1),
    ],
)
def test_extract_scheduled_id_accepts_known_aliases(
    envelope: dict[str, int], expected: int
) -> None:
    assert tracer._extract_scheduled_id(envelope) == expected


def test_extract_scheduled_id_raises_on_unknown_envelope() -> None:
    with pytest.raises(RuntimeError, match="schedule id"):
        tracer._extract_scheduled_id({"unexpected": 1})


def test_iter_calendar_items_handles_list_envelope() -> None:
    items = [{"date": "2026-07-13", "workoutScheduleId": 1}]
    assert tracer._iter_calendar_items(items) == items


def test_iter_calendar_items_handles_dict_envelopes() -> None:
    items = [{"date": "2026-07-13", "workoutScheduleId": 1}]
    assert tracer._iter_calendar_items({"calendarItems": items}) == items
    assert tracer._iter_calendar_items({"items": items}) == items
    assert tracer._iter_calendar_items({"scheduledWorkouts": items}) == items


def test_iter_calendar_items_returns_empty_for_unknown_shapes() -> None:
    assert tracer._iter_calendar_items(None) == []
    assert tracer._iter_calendar_items({"foo": "bar"}) == []


def test_target_date_uses_offset_in_days() -> None:
    # We don't pin a specific timezone-aware "today" here because the helper
    # uses Africa/Tunis under the hood; instead we sanity-check that the offset
    # arithmetic is days-out, not e.g. weeks or seconds.
    today = tracer._today_in(tracer.USER_TIMEZONE)
    assert tracer.target_date(7) == today + timedelta(days=7)
    assert tracer.target_date(60) == today + timedelta(days=60)


def test_today_in_returns_date_instance() -> None:
    assert isinstance(tracer._today_in("Africa/Tunis"), date)
