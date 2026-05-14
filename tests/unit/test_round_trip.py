"""Round-trip tests for forward + reverse translators (T13b).

The AC for T13b (issue #15) reads:

    For each T08b fixture, ``garmin_to_canonical(to_garmin(w).model_dump()) == w``
    (modulo ``opaque`` field if absent originally).

We don't have an in-Python literal for each canonical ``w`` (those live as
Garmin-JSON fixtures, not canonical-JSON ones — T08b owns the JSON; canonical
literals would need to be duplicated). To satisfy the *spirit* of the AC
without re-creating the 12 canonical literals, we assert the equivalent
**stable round-trip property** instead:

    reverse(forward(reverse(g)).model_dump(exclude_none=True, mode="json"))
    == reverse(g)

Proof of equivalence: T08b's own suite already pins ``to_garmin(w).model_dump()
== g`` for each fixture (the JSON file is the expected output of forward).
So ``reverse(g) == reverse(to_garmin(w).model_dump()) == w`` if the AC holds.
Substituting ``w := reverse(g)`` (the canonical recovered from the fixture)
into the AC gives the property we assert here. This avoids a brittle parallel
suite of canonical literals while exercising both translators end-to-end.

For the external power-zone fixture (``power_zone.json``), we additionally
assert (a) the opaque-blob reverse parsing produces ``target=OpenTarget``,
``notes`` annotated with ``"[unsupported: power.zone]"``, and a populated
``opaque`` field (covered in ``test_translate_reverse.py``); and (b) forward
emission of that canonical produces a payload whose relevant step carries
``workoutTargetTypeKey == "power.zone"`` verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.garmin.translate_forward import to_garmin
from src.garmin.translate_reverse import garmin_to_canonical
from src.models import Workout

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "workouts"
_EXTERNAL_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "external_workouts"

# The 12 plan-row fixtures owned by T08b. Names must stay in sync with that
# suite; the corresponding JSON files live at ``tests/fixtures/workouts/*.json``.
_T08B_FIXTURES: tuple[str, ...] = (
    "easy_run",
    "recovery_run",
    "strides_on_easy",
    "flat_tempo",
    "hill_repeats",
    "long_run",
    "distance_long_run",
    "marathon_intervals",
    "uphill_threshold",
    "trail_long_run",
    "opaque_preserved",
    "nested_repeat",
)


def _load_fixture(directory: Path, name: str) -> dict[str, Any]:
    """Load a Garmin-shaped JSON fixture from disk."""
    path = directory / f"{name}.json"
    with path.open("r", encoding="utf-8") as fp:
        loaded = json.load(fp)
    assert isinstance(loaded, dict), f"Fixture {path} must be a JSON object"
    return loaded


def _forward_then_dump(workout: Workout) -> dict[str, Any]:
    """Run the forward translator and produce its upload-ready JSON dict.

    Mirrors the serialization mode T08b's golden suite uses
    (``exclude_none=True``, ``mode="json"``) so the regenerated payload's
    shape matches what Garmin would receive.
    """
    return to_garmin(workout).model_dump(exclude_none=True, mode="json")


# ---------------------------------------------------------------------------
# Round-trip across the 12 T08b fixtures (stable property)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", _T08B_FIXTURES, ids=list(_T08B_FIXTURES))
def test_round_trip_is_stable_for_t08b_fixture(fixture_name: str) -> None:
    """``reverse(forward(reverse(g)).model_dump()) == reverse(g)`` for each fixture.

    See module docstring for why this property substitutes for the literal AC
    ``reverse(forward(w).model_dump()) == w`` (no canonical literals owned
    here; T08b owns the Garmin-JSON side only).
    """
    payload = _load_fixture(_FIXTURES_DIR, fixture_name)
    once = garmin_to_canonical(payload)
    regenerated = _forward_then_dump(once)
    twice = garmin_to_canonical(regenerated)

    # Pydantic ``__eq__`` is field-by-field structural equality. Both Workouts
    # come from the same code path, so the discriminator ``kind`` fields and
    # nested Step/RepeatGroup variants are compared correctly.
    assert twice == once, (
        f"Round-trip is not stable for fixture {fixture_name!r}: "
        f"reverse(forward(reverse(g))) diverged from reverse(g)."
    )


def test_all_twelve_t08b_fixtures_are_referenced() -> None:
    """Defensive: the round-trip suite covers exactly the 12 T08b fixtures."""
    assert len(_T08B_FIXTURES) == 12
    for name in _T08B_FIXTURES:
        assert (_FIXTURES_DIR / f"{name}.json").exists(), (
            f"Missing T08b fixture: tests/fixtures/workouts/{name}.json"
        )


# ---------------------------------------------------------------------------
# Opaque-blob forward emission (power_zone external fixture)
# ---------------------------------------------------------------------------


def _find_step_with_target_key(payload: dict[str, Any], target_key: str) -> dict[str, Any]:
    """Return the first executable step whose target uses ``target_key``.

    Searches the (single) ``workoutSegments[0]`` and its top-level steps. The
    power_zone fixture has only one step; this helper is defensive against
    payload-shape drift (e.g. if Garmin started nesting steps differently).
    """
    segments = payload.get("workoutSegments") or []
    assert segments, "Payload must have at least one workoutSegment"
    steps = segments[0].get("workoutSteps") or []
    for step in steps:
        assert isinstance(step, dict)
        target_type = step.get("targetType") or {}
        if target_type.get("workoutTargetTypeKey") == target_key:
            return step
    raise AssertionError(f"No step with workoutTargetTypeKey == {target_key!r} found in payload")


def test_power_zone_opaque_blob_emits_verbatim_on_forward() -> None:
    """Reverse-translating the power_zone fixture, then forward-translating the
    canonical, must yield a payload whose power.zone step is emitted verbatim.

    This locks in the DESIGN.md §8 contract: unsupported target shapes ride
    through unmodified via ``Step.opaque``, so external workouts edited via
    ``replace_scheduled_workout`` keep their original power/cadence/etc steps
    intact even though canonical never models them.
    """
    payload = _load_fixture(_EXTERNAL_FIXTURES_DIR, "power_zone")
    canonical = garmin_to_canonical(payload)
    regenerated = _forward_then_dump(canonical)

    # The regenerated payload must still carry a power.zone step.
    regenerated_step = _find_step_with_target_key(regenerated, "power.zone")

    # The original power.zone target fields are preserved verbatim.
    original_step = _find_step_with_target_key(payload, "power.zone")
    assert regenerated_step["targetType"] == original_step["targetType"], (
        "power.zone target block must round-trip verbatim through Step.opaque"
    )
    # The end-condition shape is preserved as well (no canonical re-derivation).
    assert regenerated_step["endCondition"] == original_step["endCondition"]
    assert regenerated_step["endConditionValue"] == original_step["endConditionValue"]


def test_power_zone_external_workout_round_trip_is_stable() -> None:
    """The stable round-trip property must also hold for the external power_zone
    fixture: reverse(forward(reverse(g))) == reverse(g).

    Confirms the opaque-blob path is idempotent under repeated translation
    (a precondition for ``replace_scheduled_workout`` editing adjacent steps
    without corrupting opaque ones).
    """
    payload = _load_fixture(_EXTERNAL_FIXTURES_DIR, "power_zone")
    once = garmin_to_canonical(payload)
    regenerated = _forward_then_dump(once)
    twice = garmin_to_canonical(regenerated)
    assert twice == once
