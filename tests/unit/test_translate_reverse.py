"""Unit tests for the reverse translator (T13b).

Pins the behavior described in DESIGN.md §8 for the supported-shape happy
paths plus the description-prefix sport detection. The opaque-blob fallback
is exercised end-to-end in ``tests/unit/test_round_trip.py`` against the
``power_zone.json`` external fixture; here we focus on per-shape coverage so a
regression in any single mapping (TIME, DISTANCE, HR with/without sentinel,
pace, open, all 5 step kinds, nested repeat) is caught with a minimal payload.

Reverse-translator outputs are canonical :class:`Workout` instances; we assert
on the typed fields rather than on serialized JSON so the tests don't reach
through the schema discriminators unnecessarily.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.garmin.translate_reverse import garmin_to_canonical
from src.models import (
    DistanceDuration,
    HRRangeTarget,
    OpenTarget,
    PaceTarget,
    RepeatGroup,
    Step,
    StepKind,
    TimeDuration,
)

_EXTERNAL_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "external_workouts"


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _make_executable_step(
    *,
    step_order: int = 1,
    step_type_id: int = 3,
    step_type_key: str = "interval",
    end_condition_key: str = "time",
    end_condition_value: float = 300.0,
    target_type: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal Garmin ``ExecutableStepDTO`` dict for tests.

    Garmin stores ``targetValueOne`` / ``targetValueTwo`` as siblings of
    ``targetType`` on the step. To keep call sites concise, this helper
    accepts ``target_type`` as a full dict that may include the value keys;
    the helper lifts them to step top level so the resulting shape matches
    what real Garmin returns.
    """
    if target_type is None:
        target_type = {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1,
        }
    target_type = dict(target_type)  # copy before mutation
    value_one = target_type.pop("targetValueOne", None)
    value_two = target_type.pop("targetValueTwo", None)
    end_condition_id = 2 if end_condition_key == "time" else 1
    step: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": {
            "stepTypeId": step_type_id,
            "stepTypeKey": step_type_key,
            "displayOrder": step_type_id,
        },
        "endCondition": {
            "conditionTypeId": end_condition_id,
            "conditionTypeKey": end_condition_key,
            "displayOrder": end_condition_id,
            "displayable": True,
        },
        "endConditionValue": end_condition_value,
        "targetType": target_type,
    }
    if value_one is not None:
        step["targetValueOne"] = value_one
    if value_two is not None:
        step["targetValueTwo"] = value_two
    return step


def _wrap_in_workout(
    steps: list[dict[str, Any]],
    *,
    name: str = "Test",
    description: str = "[mcp][road_run]",
) -> dict[str, Any]:
    """Wrap a list of step dicts into a full Garmin workout payload."""
    return {
        "workoutName": name,
        "description": description,
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": {
                    "sportTypeId": 1,
                    "sportTypeKey": "running",
                    "displayOrder": 1,
                },
                "workoutSteps": steps,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


def test_time_duration_parsed_to_time_duration() -> None:
    payload = _wrap_in_workout(
        [_make_executable_step(end_condition_key="time", end_condition_value=600.0)]
    )
    workout = garmin_to_canonical(payload)
    assert len(workout.steps) == 1
    step = workout.steps[0]
    assert isinstance(step, Step)
    assert isinstance(step.duration, TimeDuration)
    assert step.duration.seconds == 600


def test_distance_duration_parsed_to_distance_duration() -> None:
    payload = _wrap_in_workout(
        [_make_executable_step(end_condition_key="distance", end_condition_value=1500.0)]
    )
    workout = garmin_to_canonical(payload)
    step = workout.steps[0]
    assert isinstance(step, Step)
    assert isinstance(step.duration, DistanceDuration)
    assert step.duration.meters == 1500.0


# ---------------------------------------------------------------------------
# Target parsing
# ---------------------------------------------------------------------------


def test_hr_range_target_with_max_bpm() -> None:
    hr_target: dict[str, Any] = {
        "workoutTargetTypeId": 4,
        "workoutTargetTypeKey": "heart.rate.zone",
        "displayOrder": 4,
        "targetValueOne": 141,
        "targetValueTwo": 155,
    }
    payload = _wrap_in_workout([_make_executable_step(target_type=hr_target)])
    step = garmin_to_canonical(payload).steps[0]
    assert isinstance(step, Step)
    assert isinstance(step.target, HRRangeTarget)
    assert step.target.min_bpm == 141
    assert step.target.max_bpm == 155


def test_hr_range_target_with_open_max_sentinel_returns_none() -> None:
    """``targetValueTwo == 220`` is the forward translator's open-ended sentinel."""
    hr_target: dict[str, Any] = {
        "workoutTargetTypeId": 4,
        "workoutTargetTypeKey": "heart.rate.zone",
        "displayOrder": 4,
        "targetValueOne": 175,
        "targetValueTwo": 220,
    }
    payload = _wrap_in_workout([_make_executable_step(target_type=hr_target)])
    step = garmin_to_canonical(payload).steps[0]
    assert isinstance(step, Step)
    assert isinstance(step.target, HRRangeTarget)
    assert step.target.min_bpm == 175
    assert step.target.max_bpm is None


def test_pace_target_converts_mps_to_sec_per_km() -> None:
    """Forward emits valueOne=mps(slow), valueTwo=mps(fast). Inverse must agree."""
    # 5:00/km == 300 sec/km == 1000/300 m/s ≈ 3.333 (slow end, smaller m/s)
    # 4:00/km == 240 sec/km == 1000/240 m/s ≈ 4.167 (fast end, larger m/s)
    pace_target: dict[str, Any] = {
        "workoutTargetTypeId": 5,
        "workoutTargetTypeKey": "pace.zone",
        "displayOrder": 5,
        "targetValueOne": 1000.0 / 300.0,  # mps of slow end
        "targetValueTwo": 1000.0 / 240.0,  # mps of fast end
    }
    payload = _wrap_in_workout([_make_executable_step(target_type=pace_target)])
    step = garmin_to_canonical(payload).steps[0]
    assert isinstance(step, Step)
    assert isinstance(step.target, PaceTarget)
    # min_sec_per_km = fast end = 240, max_sec_per_km = slow end = 300.
    # Float-conversion through m/s introduces small ULP-level error; assert
    # to within 1e-9 sec/km to confirm the formula, not bit-exact equality.
    assert step.target.min_sec_per_km == pytest.approx(240.0, abs=1e-9)
    assert step.target.max_sec_per_km == pytest.approx(300.0, abs=1e-9)


def test_open_target_maps_to_open_target() -> None:
    payload = _wrap_in_workout([_make_executable_step()])  # default target = no.target
    step = garmin_to_canonical(payload).steps[0]
    assert isinstance(step, Step)
    assert isinstance(step.target, OpenTarget)


# ---------------------------------------------------------------------------
# Step kinds (all five)
# ---------------------------------------------------------------------------


def test_all_five_step_types_map_to_correct_step_kind() -> None:
    """Garmin stepTypeId {1,2,3,4,5} -> canonical {warmup, cooldown, active, recovery, rest}."""
    expected: list[tuple[int, str, StepKind]] = [
        (1, "warmup", "warmup"),
        (2, "cooldown", "cooldown"),
        (3, "interval", "active"),
        (4, "recovery", "recovery"),
        (5, "rest", "rest"),
    ]
    for step_type_id, step_type_key, canonical_kind in expected:
        payload = _wrap_in_workout(
            [
                _make_executable_step(
                    step_type_id=step_type_id,
                    step_type_key=step_type_key,
                )
            ]
        )
        step = garmin_to_canonical(payload).steps[0]
        assert isinstance(step, Step)
        assert step.kind == canonical_kind, (
            f"stepTypeId={step_type_id} ({step_type_key}) should map to "
            f"{canonical_kind!r}, got {step.kind!r}"
        )


# ---------------------------------------------------------------------------
# RepeatGroup (including nested)
# ---------------------------------------------------------------------------


def _repeat_group(
    iterations: int,
    inner_steps: list[dict[str, Any]],
    *,
    step_order: int = 1,
) -> dict[str, Any]:
    """Build a Garmin ``RepeatGroupDTO`` dict for tests."""
    return {
        "type": "RepeatGroupDTO",
        "stepOrder": step_order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
        "numberOfIterations": iterations,
        "workoutSteps": inner_steps,
        "endCondition": {
            "conditionTypeId": 7,
            "conditionTypeKey": "iterations",
            "displayOrder": 7,
            "displayable": False,
        },
        "endConditionValue": float(iterations),
        "smartRepeat": False,
    }


def test_nested_repeat_group_parses_recursively() -> None:
    """Outer RepeatGroup(3x) containing inner RepeatGroup(5x) of a single step."""
    inner_step = _make_executable_step(
        step_order=1,
        step_type_id=3,
        step_type_key="interval",
        end_condition_value=60.0,
    )
    inner_repeat = _repeat_group(5, [inner_step], step_order=1)
    outer_repeat = _repeat_group(3, [inner_repeat], step_order=1)
    payload = _wrap_in_workout([outer_repeat])

    workout = garmin_to_canonical(payload)
    assert len(workout.steps) == 1
    outer = workout.steps[0]
    assert isinstance(outer, RepeatGroup)
    assert outer.times == 3
    assert len(outer.steps) == 1

    inner = outer.steps[0]
    assert isinstance(inner, RepeatGroup)
    assert inner.times == 5
    assert len(inner.steps) == 1

    leaf = inner.steps[0]
    assert isinstance(leaf, Step)
    assert isinstance(leaf.duration, TimeDuration)
    assert leaf.duration.seconds == 60


# ---------------------------------------------------------------------------
# Description prefix / sport detection
# ---------------------------------------------------------------------------


def test_sport_detection_from_mcp_prefix_road_run() -> None:
    payload = _wrap_in_workout(
        [_make_executable_step()],
        description="[mcp][road_run] Easy shakeout",
    )
    workout = garmin_to_canonical(payload)
    assert workout.sport == "road_run"
    assert workout.description == "Easy shakeout"


def test_sport_detection_from_mcp_prefix_trail_run() -> None:
    payload = _wrap_in_workout(
        [_make_executable_step()],
        description="[mcp][trail_run] Hill loop",
    )
    workout = garmin_to_canonical(payload)
    assert workout.sport == "trail_run"
    assert workout.description == "Hill loop"


def test_sport_detection_from_mcp_prefix_treadmill_run() -> None:
    payload = _wrap_in_workout(
        [_make_executable_step()],
        description="[mcp][treadmill_run]",
    )
    workout = garmin_to_canonical(payload)
    assert workout.sport == "treadmill_run"
    # No trailing text -> description is None.
    assert workout.description is None


def test_missing_mcp_prefix_defaults_to_road_run() -> None:
    """External workouts: no [mcp] prefix -> default sport=road_run."""
    payload = _wrap_in_workout(
        [_make_executable_step()],
        description="Coach Cooper's session",
    )
    workout = garmin_to_canonical(payload)
    assert workout.sport == "road_run"
    # External description preserved verbatim.
    assert workout.description == "Coach Cooper's session"


def test_empty_description_defaults_to_road_run_and_none_description() -> None:
    payload = _wrap_in_workout(
        [_make_executable_step()],
        description="",
    )
    workout = garmin_to_canonical(payload)
    assert workout.sport == "road_run"
    assert workout.description is None


def test_description_stripped_of_prefix_when_only_prefix() -> None:
    """``[mcp][road_run]`` (no trailing text) -> description is None."""
    payload = _wrap_in_workout([_make_executable_step()], description="[mcp][road_run]")
    workout = garmin_to_canonical(payload)
    assert workout.description is None


# ---------------------------------------------------------------------------
# Name fallback
# ---------------------------------------------------------------------------


def test_missing_workout_name_falls_back_to_placeholder() -> None:
    payload = _wrap_in_workout([_make_executable_step()])
    payload.pop("workoutName", None)
    workout = garmin_to_canonical(payload)
    # The placeholder is implementation-defined; we just assert it's a non-empty
    # string so the canonical schema's max_length validator doesn't reject it.
    assert isinstance(workout.name, str)
    assert workout.name != ""


def test_empty_workout_name_falls_back_to_placeholder() -> None:
    payload = _wrap_in_workout([_make_executable_step()], name="")
    workout = garmin_to_canonical(payload)
    assert isinstance(workout.name, str)
    assert workout.name != ""


# ---------------------------------------------------------------------------
# Opaque blob fallback (power.zone external fixture)
# ---------------------------------------------------------------------------


def _load_external_fixture(name: str) -> dict[str, Any]:
    """Load a fixture from ``tests/fixtures/external_workouts/``."""
    path = _EXTERNAL_FIXTURES_DIR / f"{name}.json"
    with path.open("r", encoding="utf-8") as fp:
        loaded = json.load(fp)
    assert isinstance(loaded, dict)
    return loaded


def test_power_zone_external_fixture_uses_opaque_fallback() -> None:
    """power.zone is unsupported -> step becomes an opaque fallback.

    External fixture has no ``[mcp]`` prefix, so sport defaults to ``road_run``
    and the original description is preserved unchanged.
    """
    payload = _load_external_fixture("power_zone")
    workout = garmin_to_canonical(payload)

    # No [mcp] prefix in description -> defaults to road_run + verbatim text.
    assert workout.sport == "road_run"
    assert workout.description == "Coach Cooper's power session"

    # Single power.zone step -> single opaque fallback Step.
    assert len(workout.steps) == 1
    step = workout.steps[0]
    assert isinstance(step, Step)
    assert isinstance(step.target, OpenTarget)
    assert step.notes is not None
    assert "[unsupported: power.zone]" in step.notes

    # The opaque blob is the original step dict, verbatim.
    assert step.opaque is not None
    original_step = payload["workoutSegments"][0]["workoutSteps"][0]
    assert step.opaque == original_step
