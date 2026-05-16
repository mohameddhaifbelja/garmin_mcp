"""Forward translator: canonical ``Workout`` -> Garmin ``RunningWorkout``.

This module implements DESIGN.md §7. It is a pure function over the canonical
schema in :mod:`src.models`; it does not touch the network, the Garmin client,
or settings.

Mapping summary
---------------
- All sports (``road_run``, ``trail_run``, ``treadmill_run``) emit a
  :class:`garminconnect.workout.RunningWorkout`. The sub-type rides on the
  description marker ``[mcp][<sport>]`` so the reverse translator and the
  ``list_scheduled_workouts`` source tag (T11) can recover it.
- Step kinds map to the typed ``create_*_step`` helpers from
  :mod:`garminconnect.workout`. There is no rest-step helper in the library
  (``StepType.REST = 5`` is defined but no constructor), so
  ``StepKind == "rest"`` is mapped through ``create_recovery_step`` — a lossy
  mapping documented in the spec. The reverse translator (T13) recovers
  ``"rest"`` from ``stepType.stepTypeKey`` in the Garmin payload, so a
  round-trip via Garmin loses the distinction on the way out and recovers it
  on the way in (Garmin's own ``stepType`` field preserves the
  ``recovery`` key, not ``rest``). Accept the loss; T13 will re-classify based
  on the stored ``stepTypeKey`` once Garmin echoes the upload back.
- Durations: ``TimeDuration`` flows through the helpers via
  ``duration_seconds``. ``DistanceDuration`` requires a hand-built
  ``endCondition`` block (``conditionTypeKey="distance"``,
  ``endConditionValue=meters``) — we let the helper build the rest of the
  ``ExecutableStep`` and then swap the end-condition fields in place so the
  ``stepType`` and default ``targetType`` blocks stay consistent with what the
  library would emit for a time-based step.
- Targets: ``OpenTarget`` defers to the helper default
  (``workoutTargetTypeKey="no.target"``). ``HRRangeTarget`` and ``PaceTarget``
  are hand-built per DESIGN.md §7. Pace is converted to m/s at this boundary
  via :func:`sec_per_km_to_mps`.
- ``HRRangeTarget.max_bpm`` may be ``None`` (open-ended like "HR > 175");
  Garmin's API needs both bounds, so we substitute the sentinel ``220`` (the
  same value DESIGN.md §7 prescribes).
- ``RepeatGroup`` is recursive and emitted via ``create_repeat_group``.
- ``Step.opaque`` (set by the reverse translator on shapes we can't model)
  bypasses translation entirely: the raw Garmin dict rides through verbatim
  so the original Garmin step round-trips losslessly.

The function returns the Pydantic ``RunningWorkout`` model; callers can call
``.to_dict()`` to obtain the upload-ready JSON dict that
``Garmin.upload_running_workout`` accepts.

Pace direction convention (DESIGN.md §7)
----------------------------------------
DESIGN.md §7 specifies, for ``PaceTarget``::

    targetValueOne corresponds to max_sec_per_km
    targetValueTwo corresponds to min_sec_per_km

Numerically: ``max_sec_per_km`` is the slow end (larger seconds per km), which
converts to the *smaller* m/s value. ``min_sec_per_km`` is the fast end,
converting to the *larger* m/s value. So ``targetValueOne < targetValueTwo``
in the emitted dict — Garmin's natural min/max ordering. We follow the
parenthetical literal mapping in DESIGN.md §7 line 173 and AC line 190.
"""

from __future__ import annotations

from typing import Any

from garminconnect.workout import (
    ConditionType,
    ExecutableStep,
    RunningWorkout,
    SportType,
    TargetType,
    WorkoutSegment,
    create_cooldown_step,
    create_interval_step,
    create_recovery_step,
    create_repeat_group,
    create_warmup_step,
)
from garminconnect.workout import (
    RepeatGroup as GarminRepeatGroup,
)

from src.models import (
    DistanceDuration,
    HRRangeTarget,
    OpenTarget,
    PaceTarget,
    RepeatGroup,
    Step,
    StepKind,
    TimeDuration,
    Workout,
    WorkoutStep,
)

# Sentinel substituted for an open-ended HR target (``max_bpm is None``)
# at the Garmin boundary. DESIGN.md §7 picks 220 (physiological hard cap).
_OPEN_HR_MAX_SENTINEL = 220

# Sport-type block emitted on the (single) ``WorkoutSegment``. All three
# canonical sport sub-types render as Garmin "running" at the segment level;
# the canonical sub-type is preserved in the workout description prefix.
_RUNNING_SPORT_TYPE: dict[str, Any] = {
    "sportTypeId": SportType.RUNNING,
    "sportTypeKey": "running",
    "displayOrder": 1,
}

# Display orders the library uses on the (default) ``no.target`` block.
_NO_TARGET_DISPLAY_ORDER = 1
_HR_TARGET_DISPLAY_ORDER = 4
_PACE_TARGET_DISPLAY_ORDER = 6

# Garmin's pace-zone target type id. The ``garminconnect`` library exposes
# ``TargetType.SPEED = 5`` (km/h display) but no ``PACE`` constant; id 6 is
# the pace-zone variant that renders as min/km on the watch and in Connect.
_PACE_ZONE_TARGET_TYPE_ID = 6

# StepKind -> the library helper that builds the right ``ExecutableStep``.
# ``"rest"`` reuses ``create_recovery_step`` — see module docstring for why.
_STEP_BUILDERS = {
    "warmup": create_warmup_step,
    "active": create_interval_step,
    "recovery": create_recovery_step,
    "cooldown": create_cooldown_step,
    "rest": create_recovery_step,  # lossy: no rest-step helper in garminconnect
}


def sec_per_km_to_mps(sec_per_km: float) -> float:
    """Convert pace in seconds-per-kilometer to meters-per-second.

    Garmin's ``pace.zone`` target stores both bounds in m/s. This is the only
    place in the codebase that crosses the units boundary.
    """
    return 1000.0 / sec_per_km


def _open_target() -> dict[str, Any]:
    """Garmin ``no.target`` block — used for ``OpenTarget``."""
    return {
        "workoutTargetTypeId": TargetType.NO_TARGET,
        "workoutTargetTypeKey": "no.target",
        "displayOrder": _NO_TARGET_DISPLAY_ORDER,
    }


def _hr_zone_target(target: HRRangeTarget) -> tuple[dict[str, Any], tuple[int, int]]:
    """Garmin ``heart.rate.zone`` target metadata + (valueOne, valueTwo).

    Garmin stores the bpm bounds as top-level fields on the ``ExecutableStep``
    (siblings of ``targetType``), not inside the target-type dict. The caller
    is responsible for attaching the returned tuple as ``targetValueOne`` /
    ``targetValueTwo`` on the step. Substitutes :data:`_OPEN_HR_MAX_SENTINEL`
    when ``max_bpm`` is ``None``; Garmin's API rejects half-open ranges.
    """
    max_bpm = target.max_bpm if target.max_bpm is not None else _OPEN_HR_MAX_SENTINEL
    type_block = {
        "workoutTargetTypeId": TargetType.HEART_RATE,
        "workoutTargetTypeKey": "heart.rate.zone",
        "displayOrder": _HR_TARGET_DISPLAY_ORDER,
    }
    return type_block, (target.min_bpm, max_bpm)


def _pace_zone_target(
    target: PaceTarget,
) -> tuple[dict[str, Any], tuple[float, float]]:
    """Garmin ``pace.zone`` target metadata + (valueOne, valueTwo) in m/s.

    Per DESIGN.md §7: ``targetValueOne`` corresponds to ``max_sec_per_km``
    (the slow end → smaller m/s), ``targetValueTwo`` to ``min_sec_per_km``
    (the fast end → larger m/s). So ``valueOne < valueTwo``, matching
    Garmin's min/max ordering. Values are attached as step top-level fields
    by the caller (Garmin reads them there, not from inside ``targetType``).
    """
    type_block = {
        "workoutTargetTypeId": _PACE_ZONE_TARGET_TYPE_ID,
        "workoutTargetTypeKey": "pace.zone",
        "displayOrder": _PACE_TARGET_DISPLAY_ORDER,
    }
    values = (
        sec_per_km_to_mps(target.max_sec_per_km),
        sec_per_km_to_mps(target.min_sec_per_km),
    )
    return type_block, values


def _build_target_block(
    step: Step,
) -> tuple[dict[str, Any], tuple[Any, Any] | None]:
    """Dispatch ``Step.target`` to its Garmin (type-dict, values?) builder.

    Returns the target-type metadata dict and an optional ``(valueOne,
    valueTwo)`` tuple. ``None`` for open targets, where no values are
    written. The caller attaches the values as step top-level fields.
    """
    target = step.target
    if isinstance(target, OpenTarget):
        return _open_target(), None
    if isinstance(target, HRRangeTarget):
        return _hr_zone_target(target)
    if isinstance(target, PaceTarget):
        return _pace_zone_target(target)
    # Should be unreachable: ``Target`` is a closed discriminated union.
    raise TypeError(f"Unsupported canonical target type: {type(target).__name__}")


def _apply_distance_end_condition(executable: ExecutableStep, meters: float) -> None:
    """Mutate ``executable`` in place to use a distance end-condition.

    The library helpers always emit a ``time`` end-condition. For
    ``DistanceDuration`` we keep the helper-built ``stepType`` and default
    ``targetType`` (which the caller will then overwrite) and swap only the
    end-condition fields.
    """
    executable.endCondition = {
        "conditionTypeId": ConditionType.DISTANCE,
        "conditionTypeKey": "distance",
        "displayOrder": 2,
        "displayable": True,
    }
    executable.endConditionValue = float(meters)


def _build_executable_step(step: Step, step_order: int) -> ExecutableStep:
    """Translate one canonical ``Step`` into a Garmin ``ExecutableStep``.

    Builds the step via the appropriate ``create_*_step`` helper, attaches
    the target-type metadata, and lifts HR/pace values to the step's top
    level (where Garmin reads them — siblings of ``targetType``). For
    distance-based durations the end-condition block is swapped in place.
    """
    builder = _STEP_BUILDERS[step.kind]
    target_block, target_values = _build_target_block(step)

    if isinstance(step.duration, TimeDuration):
        executable = builder(
            duration_seconds=float(step.duration.seconds),
            step_order=step_order,
            target_type=target_block,
        )
    elif isinstance(step.duration, DistanceDuration):
        # ``duration_seconds`` is a required positional kwarg on every helper.
        # We pass a placeholder (the meter value) and overwrite the
        # end-condition block immediately so the placeholder never leaks.
        executable = builder(
            duration_seconds=float(step.duration.meters),
            step_order=step_order,
            target_type=target_block,
        )
        _apply_distance_end_condition(executable, step.duration.meters)
    else:
        # Unreachable: ``Duration`` is a closed discriminated union.
        raise TypeError(f"Unsupported canonical duration: {type(step.duration).__name__}")

    if target_values is not None:
        # Garmin expects ``targetValueOne`` / ``targetValueTwo`` as siblings
        # of ``targetType`` on the step, not nested inside it. ``ExecutableStep``
        # has ``model_config = ConfigDict(extra='allow')``, so ``model_copy``
        # with ``update`` attaches them as serializable extras.
        value_one, value_two = target_values
        executable = executable.model_copy(
            update={"targetValueOne": value_one, "targetValueTwo": value_two}
        )
    return executable


def _build_workout_step(
    step: WorkoutStep,
    step_order: int,
) -> ExecutableStep | GarminRepeatGroup | dict[str, Any]:
    """Translate any canonical workout step (leaf or repeat group).

    Returns one of:
    - ``ExecutableStep`` for a normal :class:`Step`
    - :class:`garminconnect.workout.RepeatGroup` for a :class:`RepeatGroup`
    - the raw opaque ``dict`` for a :class:`Step` with ``opaque`` set
      (verbatim round-trip from the reverse translator)
    """
    if isinstance(step, RepeatGroup):
        child_steps: list[ExecutableStep | GarminRepeatGroup] = []
        for child_order, child in enumerate(step.steps, start=1):
            translated = _build_workout_step(child, child_order)
            # An opaque child inside a repeat group still rides through
            # verbatim. The Garmin RepeatGroup model accepts a heterogeneous
            # list of ExecutableStep | RepeatGroup; raw dicts are also
            # accepted because ExecutableStep has ``extra="allow"`` and
            # pydantic will coerce dicts. We coerce explicitly to keep the
            # types honest.
            if isinstance(translated, dict):
                child_steps.append(ExecutableStep.model_validate(translated))
            else:
                child_steps.append(translated)
        return create_repeat_group(
            iterations=step.times,
            workout_steps=child_steps,
            step_order=step_order,
        )

    # Plain Step: honor the round-trip opaque blob if present.
    if step.opaque is not None:
        return step.opaque

    return _build_executable_step(step, step_order)


def _format_description(sport: str, description: str | None) -> str:
    """Build the ``[mcp][<sport>]`` prefixed description.

    The reverse translator (T13) parses this prefix to recover the sub-type;
    ``list_scheduled_workouts`` (T11) uses the leading ``[mcp]`` marker for
    source tagging.
    """
    prefix = f"[mcp][{sport}]"
    if description is None or description == "":
        return prefix
    return f"{prefix} {description}"


def _estimated_total_seconds(steps: list[WorkoutStep]) -> int:
    """Best-effort total duration in seconds across all steps.

    Garmin's ``estimatedDurationInSecs`` is informational on upload; the watch
    re-derives totals from the steps. We compute a lower-bound estimate by
    summing time-based durations and zero-ing out distance-based ones (we
    have no pace target on every step, so a true estimate is not possible).
    Distance-based steps and opaque steps contribute 0.
    """
    total = 0
    for step in steps:
        if isinstance(step, RepeatGroup):
            total += step.times * _estimated_total_seconds(list(step.steps))
        elif isinstance(step, Step):
            if step.opaque is not None:
                continue
            if isinstance(step.duration, TimeDuration):
                total += step.duration.seconds
    return total


def to_garmin(w: Workout) -> RunningWorkout:
    """Translate a canonical :class:`Workout` to Garmin's ``RunningWorkout``.

    Sport sub-types all emit a ``RunningWorkout``; the canonical sub-type is
    encoded into the description as ``[mcp][<sport>]``. The returned model
    can be uploaded via ``Garmin.upload_running_workout`` directly, or
    ``.to_dict()``-ed for tests and round-trip checks.
    """
    translated_steps: list[ExecutableStep | GarminRepeatGroup] = []
    for order, step in enumerate(w.steps, start=1):
        result = _build_workout_step(step, order)
        if isinstance(result, dict):
            # Opaque blob at the top level: re-hydrate into an ExecutableStep
            # so the WorkoutSegment's typed list stays well-formed. The blob's
            # own ``stepOrder`` is preserved if present; otherwise we use our
            # ordinal so Garmin orders the steps correctly.
            blob = dict(result)
            blob.setdefault("stepOrder", order)
            translated_steps.append(ExecutableStep.model_validate(blob))
        else:
            translated_steps.append(result)

    segment = WorkoutSegment(
        segmentOrder=1,
        sportType=_RUNNING_SPORT_TYPE,
        workoutSteps=translated_steps,
    )

    return RunningWorkout(
        workoutName=_format_name(w.name),
        estimatedDurationInSecs=_estimated_total_seconds(list(w.steps)),
        workoutSegments=[segment],
        description=_format_description(w.sport, w.description),
    )


# Marker prefix on the workout name so ``list_scheduled_workouts`` can classify
# source from the calendar payload's ``title`` field alone. Garmin's calendar
# list response does not include the workout description, so the description
# marker (``[mcp][<sport>]``) is invisible to the list endpoint — see
# ``list_scheduled_workouts`` and ``DESIGN.md`` §7.
_NAME_MARKER = "[mcp] "


def _format_name(name: str) -> str:
    """Prepend the ``[mcp] `` marker to the workout name if not already present.

    Idempotent: if the caller already passes a prefixed name (e.g. via
    ``replace_scheduled_workout`` after a round-trip through the reverse
    translator that retained the prefix), no second prefix is added.
    """
    return name if name.startswith(_NAME_MARKER) else f"{_NAME_MARKER}{name}"


# Step kind set is exported as a sanity check for downstream tickets that may
# want to iterate over the supported translations.
SUPPORTED_STEP_KINDS: tuple[StepKind, ...] = (
    "warmup",
    "active",
    "recovery",
    "cooldown",
    "rest",
)
