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
from src.models import HRRangeTarget, Step, TimeDuration, Workout

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def date_60_days_out() -> str:
    """Target date 60 days in the future, as ``YYYY-MM-DD``.

    Far enough out that Garmin will not sync the workout to the user's watch
    before this test cleans up.
    """
    return (date.today() + timedelta(days=60)).isoformat()


@pytest.fixture(scope="module")
def smoke_workout() -> Workout:
    """A minimal canonical workout: 5min warmup / 10min HR-zone / 5min cooldown.

    Mirrors the shape of ``scripts/tracer.py``'s tracer-bullet workout so the
    integration test and the standalone tracer exercise the same Garmin step
    types (warmup, active w/ HR target, cooldown).
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
                target=HRRangeTarget(min_bpm=141, max_bpm=155),
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
