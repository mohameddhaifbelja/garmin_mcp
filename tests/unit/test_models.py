"""Unit tests for the canonical workout schema (``src/models.py``).

Covers the acceptance criteria from TICKETS.md T06:

(a) Valid ``Workout`` round-trips through ``model_dump_json`` /
    ``model_validate_json``.
(b) Every ``Target`` variant: ``PaceTarget``, ``HRRangeTarget`` with and
    without ``max_bpm``, and ``OpenTarget``.
(c) Nested ``RepeatGroup``.
(d) Rejection of bad inputs: negative duration, ``RepeatGroup.times = 0``,
    ``Workout.name`` > 100 chars, ``PaceTarget`` with ``min >= max``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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

# --------------------------------------------------------------------------- #
# (a) round-trip
# --------------------------------------------------------------------------- #


def test_workout_round_trips_through_json() -> None:
    """A non-trivial Workout survives dump_json -> validate_json unchanged."""

    workout = Workout(
        name="Tempo with hill repeats",
        sport="road_run",
        description="Warmup, 3x hills, cooldown",
        steps=[
            Step(
                kind="warmup",
                duration=TimeDuration(seconds=600),
                target=OpenTarget(),
                notes="easy jog",
            ),
            RepeatGroup(
                times=3,
                steps=[
                    Step(
                        kind="active",
                        duration=DistanceDuration(meters=400),
                        target=PaceTarget(min_sec_per_km=200, max_sec_per_km=220),
                    ),
                    Step(
                        kind="recovery",
                        duration=TimeDuration(seconds=90),
                        target=HRRangeTarget(min_bpm=120, max_bpm=140),
                    ),
                ],
            ),
            Step(
                kind="cooldown",
                duration=TimeDuration(seconds=600),
                target=OpenTarget(),
            ),
        ],
    )

    serialized = workout.model_dump_json()
    restored = Workout.model_validate_json(serialized)

    assert restored == workout


def test_step_target_defaults_to_open_target() -> None:
    """``Step.target`` is ``OpenTarget()`` when not supplied."""

    step = Step(kind="warmup", duration=TimeDuration(seconds=300))

    assert isinstance(step.target, OpenTarget)
    assert step.target.kind == "open"


def test_step_opaque_defaults_to_none() -> None:
    """``Step.opaque`` is ``None`` unless the reverse translator sets it."""

    step = Step(kind="active", duration=TimeDuration(seconds=60))

    assert step.opaque is None


def test_workout_sport_defaults_to_road_run() -> None:
    """``Workout.sport`` defaults to ``road_run`` per DESIGN.md §5."""

    workout = Workout(
        name="Default sport",
        steps=[Step(kind="warmup", duration=TimeDuration(seconds=300))],
    )

    assert workout.sport == "road_run"


# --------------------------------------------------------------------------- #
# (b) every Target variant
# --------------------------------------------------------------------------- #


def test_pace_target_round_trips() -> None:
    workout = Workout(
        name="Pace",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=600),
                target=PaceTarget(min_sec_per_km=270, max_sec_per_km=300),
            )
        ],
    )

    restored = Workout.model_validate_json(workout.model_dump_json())

    assert isinstance(restored.steps[0].target, PaceTarget)
    assert restored.steps[0].target.min_sec_per_km == 270
    assert restored.steps[0].target.max_sec_per_km == 300


def test_hr_range_target_with_max_bpm_round_trips() -> None:
    workout = Workout(
        name="HR closed",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=600),
                target=HRRangeTarget(min_bpm=140, max_bpm=160),
            )
        ],
    )

    restored = Workout.model_validate_json(workout.model_dump_json())

    assert isinstance(restored.steps[0].target, HRRangeTarget)
    assert restored.steps[0].target.min_bpm == 140
    assert restored.steps[0].target.max_bpm == 160


def test_hr_range_target_without_max_bpm_round_trips() -> None:
    """Open-ended HR target ("HR > 175") allows ``max_bpm = None``."""

    workout = Workout(
        name="HR open-ended",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=300),
                target=HRRangeTarget(min_bpm=175),
            )
        ],
    )

    restored = Workout.model_validate_json(workout.model_dump_json())

    assert isinstance(restored.steps[0].target, HRRangeTarget)
    assert restored.steps[0].target.min_bpm == 175
    assert restored.steps[0].target.max_bpm is None


def test_open_target_round_trips() -> None:
    workout = Workout(
        name="Open",
        steps=[
            Step(
                kind="warmup",
                duration=TimeDuration(seconds=300),
                target=OpenTarget(),
            )
        ],
    )

    restored = Workout.model_validate_json(workout.model_dump_json())

    assert isinstance(restored.steps[0].target, OpenTarget)


# --------------------------------------------------------------------------- #
# (c) nested RepeatGroup
# --------------------------------------------------------------------------- #


def test_nested_repeat_group_round_trips() -> None:
    """A RepeatGroup whose ``steps`` contains another RepeatGroup works."""

    workout = Workout(
        name="Nested intervals",
        steps=[
            RepeatGroup(
                times=2,
                steps=[
                    Step(kind="active", duration=TimeDuration(seconds=60)),
                    RepeatGroup(
                        times=4,
                        steps=[
                            Step(
                                kind="active",
                                duration=DistanceDuration(meters=200),
                            ),
                            Step(
                                kind="recovery",
                                duration=TimeDuration(seconds=30),
                            ),
                        ],
                    ),
                ],
            )
        ],
    )

    restored = Workout.model_validate_json(workout.model_dump_json())

    outer = restored.steps[0]
    assert isinstance(outer, RepeatGroup)
    inner = outer.steps[1]
    assert isinstance(inner, RepeatGroup)
    assert inner.times == 4
    assert isinstance(inner.steps[0], Step)
    assert isinstance(inner.steps[0].duration, DistanceDuration)
    assert inner.steps[0].duration.meters == 200


# --------------------------------------------------------------------------- #
# (d) rejection of bad inputs
# --------------------------------------------------------------------------- #


def test_negative_time_duration_rejected() -> None:
    with pytest.raises(ValidationError):
        TimeDuration(seconds=-1)


def test_zero_time_duration_rejected() -> None:
    with pytest.raises(ValidationError):
        TimeDuration(seconds=0)


def test_negative_distance_duration_rejected() -> None:
    with pytest.raises(ValidationError):
        DistanceDuration(meters=-10.0)


def test_zero_distance_duration_rejected() -> None:
    with pytest.raises(ValidationError):
        DistanceDuration(meters=0.0)


def test_repeat_group_times_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        RepeatGroup(
            times=0,
            steps=[Step(kind="active", duration=TimeDuration(seconds=60))],
        )


def test_repeat_group_times_above_99_rejected() -> None:
    with pytest.raises(ValidationError):
        RepeatGroup(
            times=100,
            steps=[Step(kind="active", duration=TimeDuration(seconds=60))],
        )


def test_workout_name_over_100_chars_rejected() -> None:
    with pytest.raises(ValidationError):
        Workout(
            name="x" * 101,
            steps=[Step(kind="warmup", duration=TimeDuration(seconds=60))],
        )


def test_workout_name_at_100_chars_accepted() -> None:
    """Boundary: exactly 100 chars must validate."""

    workout = Workout(
        name="x" * 100,
        steps=[Step(kind="warmup", duration=TimeDuration(seconds=60))],
    )

    assert len(workout.name) == 100


def test_pace_target_min_equals_max_rejected() -> None:
    with pytest.raises(ValidationError):
        PaceTarget(min_sec_per_km=240, max_sec_per_km=240)


def test_pace_target_min_greater_than_max_rejected() -> None:
    with pytest.raises(ValidationError):
        PaceTarget(min_sec_per_km=300, max_sec_per_km=240)


def test_unknown_step_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        Step(kind="sprint", duration=TimeDuration(seconds=60))  # type: ignore[arg-type]


def test_unknown_sport_rejected() -> None:
    with pytest.raises(ValidationError):
        Workout(
            name="bad sport",
            sport="cycling",  # type: ignore[arg-type]
            steps=[Step(kind="warmup", duration=TimeDuration(seconds=60))],
        )


# --------------------------------------------------------------------------- #
# Discriminator dispatch on validation from JSON
# --------------------------------------------------------------------------- #


def test_target_discriminator_dispatches_correct_variant_from_json() -> None:
    """``model_validate_json`` must pick the correct Target variant via ``kind``."""

    raw = """
    {
        "name": "discrim",
        "sport": "road_run",
        "description": null,
        "steps": [
            {
                "kind": "active",
                "duration": {"kind": "time", "seconds": 600},
                "target": {"kind": "hr_range", "min_bpm": 150, "max_bpm": 170},
                "notes": null,
                "opaque": null
            },
            {
                "kind": "recovery",
                "duration": {"kind": "distance", "meters": 200.0},
                "target": {"kind": "pace", "min_sec_per_km": 300.0, "max_sec_per_km": 360.0},
                "notes": null,
                "opaque": null
            }
        ]
    }
    """

    workout = Workout.model_validate_json(raw)

    assert isinstance(workout.steps[0].target, HRRangeTarget)
    assert isinstance(workout.steps[0].duration, TimeDuration)
    assert isinstance(workout.steps[1].target, PaceTarget)
    assert isinstance(workout.steps[1].duration, DistanceDuration)


def test_opaque_blob_round_trips() -> None:
    """A reverse-translator-supplied opaque dict survives a round-trip."""

    opaque_blob = {"workoutTargetTypeKey": "power.zone", "targetValueOne": 250}
    step = Step(
        kind="active",
        duration=TimeDuration(seconds=60),
        target=OpenTarget(),
        notes="[unsupported: power.zone]",
        opaque=opaque_blob,
    )

    workout = Workout(name="opaque", steps=[step])
    restored = Workout.model_validate_json(workout.model_dump_json())

    assert restored.steps[0].opaque == opaque_blob
    assert restored.steps[0].notes == "[unsupported: power.zone]"
