"""Live integration smoke test for the Garmin write surface (T15).

Runs the full happy-path round trip against a real Garmin Connect account:

1. ``create_and_schedule`` — uploads a small workout and schedules it 60 days
   out (far enough that Garmin will not sync the workout to the watch before
   the test cleans up).
2. ``list_scheduled_workouts`` — verifies the new entry appears with
   ``source == "mcp"`` (DESIGN.md §7 ``[mcp]`` description marker).
3. ``get_scheduled_workout`` — verifies the workout round-trips through the
   reverse translator with name preserved.
4. ``replace_scheduled_workout`` — replaces the entry with a renamed copy and
   asserts the listing reflects the new name.
5. ``unschedule_workout`` + ``delete_workout`` — torn down inside a
   ``try/finally`` (best-effort) so a mid-test assertion failure does not
   leak a calendar entry or library template.
6. Final ``list_scheduled_workouts`` — asserts nothing remains for the target
   date.

The test is marked ``@pytest.mark.integration`` and is **skipped by default**
via the ``-m 'not integration'`` ``addopts`` setting in ``pyproject.toml``.
Run with::

    uv run pytest tests/integration/ -m integration

Prerequisites (see ``tests/integration/README.md``):

- ``~/.garminconnect/garmin_tokens.json`` populated by
  ``scripts/garmin_bootstrap.py``.
- ``.env`` populated with ``CONNECTOR_BEARER_TOKEN``, ``STRAVA_CLIENT_ID``,
  ``STRAVA_CLIENT_SECRET``, ``STRAVA_REFRESH_TOKEN`` (required by
  ``src.config.Settings``; not exercised by this test but the module imports
  trigger validation).

Design notes
------------
- Only the happy path is exercised. The rollback / failure-injection paths
  in :func:`src.garmin.tools.replace_scheduled_workout` are covered by
  T14's stub-based unit tests; running them live risks bricking a real
  calendar entry (see STATUS.md backlog, T14 reviewer handoff to T15).
- The 60-day offset is intentional: Garmin's watch sync window is much
  shorter, so the temporary calendar entry does not propagate to the
  user's device even if the test crashes between create and cleanup.
- Cleanup uses ``contextlib.suppress(Exception)`` for the teardown calls
  so a primary assertion failure surfaces clearly rather than being
  masked by a secondary cleanup error.
"""

from __future__ import annotations

import contextlib
from datetime import date, timedelta

import pytest

from src.garmin import tools as gt
from src.garmin.translate_forward import sec_per_km_to_mps
from src.models import HRRangeTarget, PaceTarget, Step, TimeDuration, Workout

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def date_60_days_out() -> str:
    """Target date 60 days in the future, as ``YYYY-MM-DD``.

    Far enough out that Garmin will not sync the workout to the user's watch
    before this test cleans up.
    """
    return (date.today() + timedelta(days=60)).isoformat()


_HR_MIN_BPM = 141
_HR_MAX_BPM = 155
_PACE_FAST_SEC_PER_KM = 315.0  # 5:15/km
_PACE_SLOW_SEC_PER_KM = 325.0  # 5:25/km


@pytest.fixture(scope="module")
def smoke_workout() -> Workout:
    """A canonical workout exercising both HR and pace targets.

    Including pace in the smoke workout is deliberate: the original integration
    fixture used only HR, which let a wire-format bug live undetected — values
    were being nested under ``targetType`` while Garmin reads them as step
    top-level fields. The deep raw-payload assertions below pin both shapes.
    """
    return Workout(
        name="MCP integration smoke",
        sport="road_run",
        description="auto-cleaned by integration test",
        steps=[
            Step(kind="warmup", duration=TimeDuration(seconds=300)),
            Step(
                kind="active",
                duration=TimeDuration(seconds=600),
                target=HRRangeTarget(min_bpm=_HR_MIN_BPM, max_bpm=_HR_MAX_BPM),
            ),
            Step(
                kind="active",
                duration=TimeDuration(seconds=300),
                target=PaceTarget(
                    min_sec_per_km=_PACE_FAST_SEC_PER_KM,
                    max_sec_per_km=_PACE_SLOW_SEC_PER_KM,
                ),
            ),
            Step(kind="cooldown", duration=TimeDuration(seconds=300)),
        ],
    )


def test_full_smoke_loop(date_60_days_out: str, smoke_workout: Workout) -> None:
    """Walk the full create -> list -> get -> replace -> unschedule -> delete loop.

    All six write tools are exercised against a live Garmin account. Asserts
    each public side effect, then verifies the final calendar state is clean.
    """
    # 1. create_and_schedule -------------------------------------------------
    create_result = gt.create_and_schedule(smoke_workout, date_60_days_out)
    workout_id = create_result["workout_id"]
    scheduled_id = create_result["scheduled_id"]
    assert workout_id, "create_and_schedule returned no workout_id"
    assert scheduled_id, "create_and_schedule returned no scheduled_id"
    assert create_result["date"] == date_60_days_out

    try:
        # 2. list_scheduled_workouts ----------------------------------------
        items = gt.list_scheduled_workouts(date_60_days_out, date_60_days_out)
        matched = [item for item in items if item["scheduled_id"] == scheduled_id]
        assert len(matched) == 1, (
            f"Expected exactly one scheduled entry for {scheduled_id} on "
            f"{date_60_days_out}, found {len(matched)}: {matched!r}"
        )
        assert matched[0]["source"] == "mcp", (
            f"Expected source='mcp' (via [mcp] description marker), got {matched[0]['source']!r}"
        )

        # 3. get_scheduled_workout ------------------------------------------
        canonical = gt.get_scheduled_workout(scheduled_id)
        assert canonical["name"] == smoke_workout.name, (
            f"Round-trip name mismatch: sent {smoke_workout.name!r}, got {canonical['name']!r}"
        )
        assert canonical.get("sport") == smoke_workout.sport
        assert isinstance(canonical.get("steps"), list)
        assert len(canonical["steps"]) == len(smoke_workout.steps)

        # 3b. Target round-trip values --------------------------------------
        # The canonical reverse-translated step must surface the same target
        # bounds we sent. Pre-T15+: the reverse translator silently fell back
        # to ``OpenTarget`` when values weren't found, so step count matched
        # but every target turned into "open". Pin actual values here so
        # that regression cannot pass quietly again.
        hr_step = canonical["steps"][1]
        assert hr_step["target"]["kind"] == "hr_range", hr_step
        assert hr_step["target"]["min_bpm"] == _HR_MIN_BPM
        assert hr_step["target"]["max_bpm"] == _HR_MAX_BPM
        pace_step = canonical["steps"][2]
        assert pace_step["target"]["kind"] == "pace", pace_step
        assert pace_step["target"]["min_sec_per_km"] == pytest.approx(
            _PACE_FAST_SEC_PER_KM, rel=1e-4
        )
        assert pace_step["target"]["max_sec_per_km"] == pytest.approx(
            _PACE_SLOW_SEC_PER_KM, rel=1e-4
        )

        # 3c. Raw-payload wire format ---------------------------------------
        # Pin the exact Garmin wire shape: ``targetValueOne`` / ``targetValueTwo``
        # are siblings of ``targetType`` on the executable step (NOT nested
        # inside it), and pace uses target-type id 6 (``pace.zone``, min/km
        # display) not 5 (``speed.zone``, km/h). Both were live bugs once.
        raw = gt._get_client().get_scheduled_workout_by_id(scheduled_id)
        raw_steps = [
            step
            for seg in raw.get("workout", raw).get("workoutSegments", [])
            for step in seg.get("workoutSteps", [])
            if step.get("type") == "ExecutableStepDTO"
        ]
        assert len(raw_steps) == len(smoke_workout.steps)
        raw_hr = raw_steps[1]
        assert raw_hr["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
        assert raw_hr.get("targetValueOne") == pytest.approx(_HR_MIN_BPM, rel=1e-6), (
            "HR targetValueOne missing from step top-level — likely nested under "
            "targetType, which Garmin silently drops."
        )
        assert raw_hr.get("targetValueTwo") == pytest.approx(_HR_MAX_BPM, rel=1e-6)
        raw_pace = raw_steps[2]
        assert raw_pace["targetType"]["workoutTargetTypeKey"] == "pace.zone", (
            "Pace target stored as something other than pace.zone — was the "
            "target-type id swapped from 6 back to SPEED (5)? Watch would "
            "render this in km/h instead of min/km."
        )
        assert raw_pace["targetType"]["workoutTargetTypeId"] == 6
        assert raw_pace.get("targetValueOne") == pytest.approx(
            sec_per_km_to_mps(_PACE_SLOW_SEC_PER_KM), rel=1e-4
        )
        assert raw_pace.get("targetValueTwo") == pytest.approx(
            sec_per_km_to_mps(_PACE_FAST_SEC_PER_KM), rel=1e-4
        )

        # 4. replace_scheduled_workout --------------------------------------
        replacement = smoke_workout.model_copy(update={"name": "MCP integration smoke (replaced)"})
        replace_result = gt.replace_scheduled_workout(scheduled_id, replacement)
        assert replace_result["original_workout_id"] == workout_id
        assert replace_result["date"] == date_60_days_out
        # The replacement takes over as the live identifiers — point the
        # cleanup at the new ids so we delete the right rows.
        workout_id = replace_result["new_workout_id"]
        scheduled_id = replace_result["new_scheduled_id"]
        assert workout_id, "replace_scheduled_workout returned no new_workout_id"
        assert scheduled_id, "replace_scheduled_workout returned no new_scheduled_id"

        # Verify the replacement is what we now see in the listing.
        items_after_replace = gt.list_scheduled_workouts(date_60_days_out, date_60_days_out)
        matched_after_replace = [
            item for item in items_after_replace if item["scheduled_id"] == scheduled_id
        ]
        assert len(matched_after_replace) == 1
        assert matched_after_replace[0]["name"] == replacement.name
        assert matched_after_replace[0]["source"] == "mcp"
    finally:
        # 5 + 6. Always clean up, even on assertion failure. Suppress
        # secondary failures so the primary assertion error is the one the
        # operator sees.
        with contextlib.suppress(Exception):
            gt.unschedule_workout(scheduled_id)
        with contextlib.suppress(Exception):
            gt.delete_workout(workout_id)

    # 7. Final calendar state -----------------------------------------------
    final_items = gt.list_scheduled_workouts(date_60_days_out, date_60_days_out)
    leftover = [item for item in final_items if item["scheduled_id"] == scheduled_id]
    assert not leftover, (
        f"Cleanup incomplete: scheduled_id {scheduled_id} still present on "
        f"{date_60_days_out}: {leftover!r}"
    )
