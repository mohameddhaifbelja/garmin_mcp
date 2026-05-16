"""Smoke tests for the forward translator (T08).

These are intentionally tight invariants — not the 12-fixture golden suite
(that lives in T08b). The goal here is to lock in the shapes that DESIGN.md §7
calls out specifically (pace m/s direction, HR open-ended sentinel, opaque
verbatim emission, sport sub-type encoding), so a future change to the
translator can't drift on those without breaking a test.
"""

from __future__ import annotations

from garminconnect.workout import RunningWorkout

from src.garmin.translate_forward import (
    _OPEN_HR_MAX_SENTINEL,
    sec_per_km_to_mps,
    to_garmin,
)
from src.models import (
    DistanceDuration,
    HRRangeTarget,
    OpenTarget,
    PaceTarget,
    RepeatGroup,
    Step,
    TimeDuration,
    Workout,
)


def _only_segment(rw: RunningWorkout) -> dict[str, object]:
    """Return the (single) ``workoutSegments[0]`` dict from a translated workout."""
    payload = rw.to_dict()
    segments = payload["workoutSegments"]
    assert isinstance(segments, list) and len(segments) == 1
    segment = segments[0]
    assert isinstance(segment, dict)
    return segment


def test_sec_per_km_to_mps_inverts_pace_correctly() -> None:
    # 5:00 /km == 300 sec/km == 1000/300 m/s == 3.333...
    assert sec_per_km_to_mps(300.0) == 1000.0 / 300.0


def test_to_garmin_returns_running_workout_for_each_sport() -> None:
    for sport in ("road_run", "trail_run", "treadmill_run"):
        w = Workout(
            name=f"sport-{sport}",
            sport=sport,
            steps=[Step(kind="warmup", duration=TimeDuration(seconds=300))],
        )
        rw = to_garmin(w)
        assert isinstance(rw, RunningWorkout)
        # All three sub-types emit the same Garmin "running" sportType.
        assert rw.sportType["sportTypeKey"] == "running"
        # Description carries the sub-type marker for later recovery.
        assert rw.description == f"[mcp][{sport}]"


def test_description_prefix_includes_user_description_when_present() -> None:
    w = Workout(
        name="with-desc",
        sport="road_run",
        description="Easy shakedown",
        steps=[Step(kind="warmup", duration=TimeDuration(seconds=60))],
    )
    rw = to_garmin(w)
    assert rw.description == "[mcp][road_run] Easy shakedown"


def test_time_duration_uses_helper_default_time_end_condition() -> None:
    w = Workout(
        name="time-step",
        sport="road_run",
        steps=[Step(kind="active", duration=TimeDuration(seconds=120))],
    )
    step = _only_segment(to_garmin(w))["workoutSteps"][0]
    assert step["endCondition"]["conditionTypeKey"] == "time"
    assert step["endConditionValue"] == 120.0


def test_distance_duration_swaps_end_condition_to_distance() -> None:
    w = Workout(
        name="dist-step",
        sport="road_run",
        steps=[Step(kind="cooldown", duration=DistanceDuration(meters=1500.0))],
    )
    step = _only_segment(to_garmin(w))["workoutSteps"][0]
    # stepType still comes from the cooldown helper.
    assert step["stepType"]["stepTypeKey"] == "cooldown"
    # End condition replaced.
    assert step["endCondition"]["conditionTypeKey"] == "distance"
    assert step["endConditionValue"] == 1500.0


def test_open_target_emits_no_target_block() -> None:
    w = Workout(
        name="open",
        sport="road_run",
        steps=[
            Step(kind="active", duration=TimeDuration(seconds=60), target=OpenTarget()),
        ],
    )
    step = _only_segment(to_garmin(w))["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "no.target"


def test_hr_range_target_with_both_bounds_passes_through_unchanged() -> None:
    w = Workout(
        name="hr-both",
        sport="road_run",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=60),
                target=HRRangeTarget(min_bpm=160, max_bpm=175),
            ),
        ],
    )
    step = _only_segment(to_garmin(w))["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert step["targetValueOne"] == 160
    assert step["targetValueTwo"] == 175


def test_hr_range_target_open_ended_substitutes_220_sentinel() -> None:
    w = Workout(
        name="hr-open",
        sport="road_run",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=60),
                target=HRRangeTarget(min_bpm=175),  # max_bpm omitted => None
            ),
        ],
    )
    step = _only_segment(to_garmin(w))["workoutSteps"][0]
    assert step["targetValueOne"] == 175
    assert step["targetValueTwo"] == _OPEN_HR_MAX_SENTINEL == 220


def test_pace_target_emits_mps_in_min_max_garmin_ordering() -> None:
    # 5:00-5:30 /km == 300-330 sec/km.
    # Per DESIGN.md §7 parenthetical: targetValueOne == sec_per_km_to_mps(max_sec_per_km)
    # (the slow end -> smaller m/s), targetValueTwo == sec_per_km_to_mps(min_sec_per_km)
    # (the fast end -> larger m/s). So targetValueOne < targetValueTwo in m/s.
    w = Workout(
        name="pace",
        sport="road_run",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=60),
                target=PaceTarget(min_sec_per_km=300.0, max_sec_per_km=330.0),
            ),
        ],
    )
    step = _only_segment(to_garmin(w))["workoutSteps"][0]
    assert step["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert step["targetValueOne"] == sec_per_km_to_mps(330.0)
    assert step["targetValueTwo"] == sec_per_km_to_mps(300.0)
    assert step["targetValueOne"] < step["targetValueTwo"]


def test_rest_step_kind_maps_through_recovery_helper_lossy() -> None:
    # Documented lossy mapping: garminconnect has no rest-step helper, so we
    # use create_recovery_step. The emitted stepTypeKey is "recovery".
    w = Workout(
        name="rest",
        sport="road_run",
        steps=[Step(kind="rest", duration=TimeDuration(seconds=60))],
    )
    step = _only_segment(to_garmin(w))["workoutSteps"][0]
    assert step["stepType"]["stepTypeKey"] == "recovery"
    assert step["stepType"]["stepTypeId"] == 4


def test_step_with_opaque_blob_emits_blob_verbatim() -> None:
    blob: dict[str, object] = {
        "type": "ExecutableStepDTO",
        "stepOrder": 7,
        "someUnknownGarminField": "preserved",
        "anotherField": [1, 2, 3],
    }
    w = Workout(
        name="opaque",
        sport="road_run",
        steps=[
            Step(kind="active", duration=TimeDuration(seconds=60), opaque=blob),
        ],
    )
    step = _only_segment(to_garmin(w))["workoutSteps"][0]
    assert step["someUnknownGarminField"] == "preserved"
    assert step["anotherField"] == [1, 2, 3]
    # The blob's own stepOrder is preserved (verbatim emission).
    assert step["stepOrder"] == 7


def test_repeat_group_round_trips_nested_structure() -> None:
    inner = RepeatGroup(
        times=5,
        steps=[
            Step(kind="active", duration=TimeDuration(seconds=60)),
            Step(kind="recovery", duration=TimeDuration(seconds=60)),
        ],
    )
    outer = RepeatGroup(
        times=3,
        steps=[inner, Step(kind="recovery", duration=TimeDuration(seconds=120))],
    )
    w = Workout(name="nested", sport="road_run", steps=[outer])
    payload = _only_segment(to_garmin(w))
    top = payload["workoutSteps"][0]
    assert top["type"] == "RepeatGroupDTO"
    assert top["numberOfIterations"] == 3
    nested = top["workoutSteps"][0]
    assert nested["type"] == "RepeatGroupDTO"
    assert nested["numberOfIterations"] == 5
    # Total time: 3 * (5 * (60+60) + 120) = 3 * (600 + 120) = 2160
    assert to_garmin(w).estimatedDurationInSecs == 2160
