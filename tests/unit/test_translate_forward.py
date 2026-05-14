"""Golden-fixture suite for the forward translator (T08b).

The companion smoke suite (``tests/unit/test_translate_forward_smoke.py``)
pins narrow invariants per DESIGN.md §7. This module pins the *full* emitted
Garmin JSON for 12 canonical workouts drawn from the user's actual ultra
training plan (DESIGN.md §14). Each fixture's expected output lives at
``tests/fixtures/workouts/<name>.json`` and is compared by exact dict equality.

The 12 fixtures collectively exercise:

- ``StepKind`` values ``warmup``, ``active``, ``recovery``, ``cooldown`` (the
  fifth, ``rest``, is covered by the smoke suite's lossy-mapping test).
- Every supported ``Duration`` shape (``TimeDuration`` and ``DistanceDuration``).
- Every supported ``Target`` shape (``HRRangeTarget`` both bounded and open-ended,
  ``PaceTarget``, ``OpenTarget``).
- Every supported ``Sport`` sub-type (``road_run``, ``trail_run``, ``treadmill_run``).
- ``RepeatGroup`` at the top level and nested two-deep.
- ``Step.opaque`` round-trip preservation (simulating a reverse-translator
  blob that must ride through verbatim).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from src.garmin.translate_forward import to_garmin
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

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "workouts"


def _easy_run() -> Workout:
    """Plan row: 40 min easy, HR 141-155, road."""
    return Workout(
        name="Easy run 40min",
        sport="road_run",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=40 * 60),
                target=HRRangeTarget(min_bpm=141, max_bpm=155),
            ),
        ],
    )


def _recovery_run() -> Workout:
    """Plan row: 30 min recovery, HR <140, road.

    The canonical schema has no half-open *low* HR target, so we use a band
    ``120-140`` per the spawn prompt.
    """
    return Workout(
        name="Recovery run 30min",
        sport="road_run",
        steps=[
            Step(
                kind="recovery",
                duration=TimeDuration(seconds=30 * 60),
                target=HRRangeTarget(min_bpm=120, max_bpm=140),
            ),
        ],
    )


def _strides_on_easy() -> Workout:
    """Plan row: 30 min easy + 4x (20s strides @ 4:00-4:20/km on grass), road.

    The strides are pace-targeted; the surrounding easy block is HR-targeted.
    """
    return Workout(
        name="Strides on easy",
        sport="road_run",
        description="Strides on grass",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=30 * 60),
                target=HRRangeTarget(min_bpm=141, max_bpm=155),
            ),
            RepeatGroup(
                times=4,
                steps=[
                    Step(
                        kind="active",
                        duration=TimeDuration(seconds=20),
                        target=PaceTarget(min_sec_per_km=240.0, max_sec_per_km=260.0),
                        notes="strides on grass",
                    ),
                ],
            ),
        ],
    )


def _flat_tempo() -> Workout:
    """Plan row: WU 15 + 20 min tempo @ 5:15-5:25/km (HR 164-175 in notes) + CD 15.

    Per DESIGN.md §10 TARGET POLICY: tempo -> PaceTarget. HR band lives in
    ``notes`` as the secondary metric.
    """
    return Workout(
        name="Flat tempo",
        sport="road_run",
        steps=[
            Step(
                kind="warmup",
                duration=TimeDuration(seconds=15 * 60),
                target=OpenTarget(),
            ),
            Step(
                kind="active",
                duration=TimeDuration(seconds=20 * 60),
                target=PaceTarget(min_sec_per_km=315.0, max_sec_per_km=325.0),
                notes="HR 164-175",
            ),
            Step(
                kind="cooldown",
                duration=TimeDuration(seconds=15 * 60),
                target=OpenTarget(),
            ),
        ],
    )


def _hill_repeats() -> Workout:
    """Plan row: WU 15 + 6x (2 min uphill HR>175 / 1 min jog) + CD 15."""
    return Workout(
        name="Hill repeats",
        sport="road_run",
        steps=[
            Step(
                kind="warmup",
                duration=TimeDuration(seconds=15 * 60),
                target=OpenTarget(),
            ),
            RepeatGroup(
                times=6,
                steps=[
                    Step(
                        kind="active",
                        duration=TimeDuration(seconds=2 * 60),
                        target=HRRangeTarget(min_bpm=175, max_bpm=None),
                        notes="uphill",
                    ),
                    Step(
                        kind="recovery",
                        duration=TimeDuration(seconds=60),
                        target=OpenTarget(),
                        notes="jog down",
                    ),
                ],
            ),
            Step(
                kind="cooldown",
                duration=TimeDuration(seconds=15 * 60),
                target=OpenTarget(),
            ),
        ],
    )


def _long_run() -> Workout:
    """Plan row: 120 min long, HR 140-155, road."""
    return Workout(
        name="Long run 2h",
        sport="road_run",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=120 * 60),
                target=HRRangeTarget(min_bpm=140, max_bpm=155),
            ),
        ],
    )


def _distance_long_run() -> Workout:
    """Plan row: 22 km long, HR 140-155, road (distance-based)."""
    return Workout(
        name="Long run 22km",
        sport="road_run",
        steps=[
            Step(
                kind="active",
                duration=DistanceDuration(meters=22000.0),
                target=HRRangeTarget(min_bpm=140, max_bpm=155),
            ),
        ],
    )


def _marathon_intervals() -> Workout:
    """Plan row: WU 15 + 4x (8 min @ 5:30-5:50/km / 2 min jog) + CD 15."""
    return Workout(
        name="Marathon-effort intervals",
        sport="road_run",
        steps=[
            Step(
                kind="warmup",
                duration=TimeDuration(seconds=15 * 60),
                target=OpenTarget(),
            ),
            RepeatGroup(
                times=4,
                steps=[
                    Step(
                        kind="active",
                        duration=TimeDuration(seconds=8 * 60),
                        target=PaceTarget(min_sec_per_km=330.0, max_sec_per_km=350.0),
                    ),
                    Step(
                        kind="recovery",
                        duration=TimeDuration(seconds=2 * 60),
                        target=OpenTarget(),
                        notes="jog",
                    ),
                ],
            ),
            Step(
                kind="cooldown",
                duration=TimeDuration(seconds=15 * 60),
                target=OpenTarget(),
            ),
        ],
    )


def _uphill_threshold() -> Workout:
    """Plan row: WU 15 + 5x (4 min HR 170-178 / 4 min jog) + CD 15, treadmill."""
    return Workout(
        name="Uphill threshold",
        sport="treadmill_run",
        steps=[
            Step(
                kind="warmup",
                duration=TimeDuration(seconds=15 * 60),
                target=OpenTarget(),
            ),
            RepeatGroup(
                times=5,
                steps=[
                    Step(
                        kind="active",
                        duration=TimeDuration(seconds=4 * 60),
                        target=HRRangeTarget(min_bpm=170, max_bpm=178),
                        notes="uphill treadmill",
                    ),
                    Step(
                        kind="recovery",
                        duration=TimeDuration(seconds=4 * 60),
                        target=OpenTarget(),
                        notes="jog",
                    ),
                ],
            ),
            Step(
                kind="cooldown",
                duration=TimeDuration(seconds=15 * 60),
                target=OpenTarget(),
            ),
        ],
    )


def _trail_long_run() -> Workout:
    """Plan row: 4h trail, HR 140-150 (no pace targets on trail per DESIGN.md §10)."""
    return Workout(
        name="Trail long run 4h",
        sport="trail_run",
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=240 * 60),
                target=HRRangeTarget(min_bpm=140, max_bpm=150),
            ),
        ],
    )


def _opaque_preserved() -> Workout:
    """Simulates a reverse-translator round-trip preserving an unmodeled
    Garmin step (here, a ``power.zone`` target) verbatim alongside an
    ordinary canonical step.

    The opaque blob carries its own ``stepOrder`` to verify the translator
    honors that value (DESIGN.md §8 round-trip guarantee).
    """
    opaque_blob: dict[str, object] = {
        "type": "ExecutableStepDTO",
        "stepOrder": 2,
        "stepType": {
            "stepTypeId": 3,
            "stepTypeKey": "interval",
            "displayOrder": 3,
        },
        "endCondition": {
            "conditionTypeId": 2,
            "conditionTypeKey": "time",
            "displayOrder": 2,
            "displayable": True,
        },
        "endConditionValue": 600.0,
        "targetType": {
            "workoutTargetTypeId": 2,
            "workoutTargetTypeKey": "power.zone",
            "displayOrder": 2,
            "targetValueOne": 250,
            "targetValueTwo": 300,
        },
    }
    return Workout(
        name="External workout with power step",
        sport="road_run",
        steps=[
            Step(
                kind="warmup",
                duration=TimeDuration(seconds=10 * 60),
                target=OpenTarget(),
            ),
            Step(
                kind="active",
                duration=TimeDuration(seconds=600),
                target=OpenTarget(),
                notes="[unsupported: power.zone Z3]",
                opaque=opaque_blob,
            ),
        ],
    )


def _nested_repeat() -> Workout:
    """Outer 3x (inner 5x (1 min hard HR 170-180 / 1 min easy) + 2 min jog).

    Verifies the canonical ``RepeatGroup`` forward-reference resolution and
    the translator's recursive ``_build_workout_step`` path.
    """
    inner = RepeatGroup(
        times=5,
        steps=[
            Step(
                kind="active",
                duration=TimeDuration(seconds=60),
                target=HRRangeTarget(min_bpm=170, max_bpm=180),
            ),
            Step(
                kind="recovery",
                duration=TimeDuration(seconds=60),
                target=OpenTarget(),
            ),
        ],
    )
    return Workout(
        name="Nested repeat",
        sport="road_run",
        steps=[
            RepeatGroup(
                times=3,
                steps=[
                    inner,
                    Step(
                        kind="recovery",
                        duration=TimeDuration(seconds=2 * 60),
                        target=OpenTarget(),
                        notes="jog",
                    ),
                ],
            ),
        ],
    )


# Type alias for the factory callable.
_WorkoutFactory = Callable[[], Workout]


# The 12 fixtures. Each entry is (fixture_name, workout_factory). Using a
# factory rather than a Workout instance avoids module-load-time side effects
# and makes regeneration of the golden JSON files straightforward.
_FIXTURES: list[tuple[str, _WorkoutFactory]] = [
    ("easy_run", _easy_run),
    ("recovery_run", _recovery_run),
    ("strides_on_easy", _strides_on_easy),
    ("flat_tempo", _flat_tempo),
    ("hill_repeats", _hill_repeats),
    ("long_run", _long_run),
    ("distance_long_run", _distance_long_run),
    ("marathon_intervals", _marathon_intervals),
    ("uphill_threshold", _uphill_threshold),
    ("trail_long_run", _trail_long_run),
    ("opaque_preserved", _opaque_preserved),
    ("nested_repeat", _nested_repeat),
]


def _translate_to_json(w: Workout) -> dict[str, object]:
    """Run the forward translator and return the upload-ready JSON dict.

    Mirrors what ``RunningWorkout.to_dict()`` would do, but spelled out so the
    serialization mode used for both sides of the equality check is identical
    to what the goldens were generated with.
    """
    rw = to_garmin(w)
    return rw.model_dump(exclude_none=True, mode="json")


def _load_golden(name: str) -> dict[str, object]:
    """Read the expected JSON for a fixture from disk."""
    path = _FIXTURES_DIR / f"{name}.json"
    with path.open("r", encoding="utf-8") as fp:
        loaded = json.load(fp)
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize(
    ("name", "factory"),
    _FIXTURES,
    ids=[name for name, _ in _FIXTURES],
)
def test_forward_translator_matches_golden(
    name: str,
    factory: _WorkoutFactory,
) -> None:
    """Exact dict-equality between translator output and the golden JSON.

    On failure pytest's default diff already points to the exact key path
    that disagrees; we also assert ``==`` so the assertion message includes
    both sides truncated.
    """
    actual = _translate_to_json(factory())
    expected = _load_golden(name)
    assert actual == expected, (
        f"Forward translator output for fixture {name!r} does not match "
        f"tests/fixtures/workouts/{name}.json. Regenerate the fixture only "
        "after auditing the translator output by hand."
    )


def test_all_12_fixtures_are_registered() -> None:
    """Defensive check: the parametrized suite must hold 12 fixtures."""
    assert len(_FIXTURES) == 12
    # Every fixture name must have a matching JSON file on disk.
    for name, _ in _FIXTURES:
        assert (_FIXTURES_DIR / f"{name}.json").exists(), (
            f"Missing golden file: tests/fixtures/workouts/{name}.json"
        )
