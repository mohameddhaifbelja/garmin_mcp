"""Reverse translator: Garmin workout payload -> canonical ``Workout``.

This module implements DESIGN.md §8. It is the inverse of
:mod:`src.garmin.translate_forward`: given the dict Garmin returns from
``get_workout_by_id`` / ``get_scheduled_workout_by_id``, it reconstructs a
canonical :class:`src.models.Workout`.

Policy
------
Best-effort. Shapes the canonical schema understands (time/distance durations;
HR-range / pace / open targets; warmup / active / recovery / cooldown / rest
step kinds; repeat groups) are parsed cleanly. Anything else (power zone,
cadence target, calorie end-condition, etc.) falls through to an *opaque
fallback*: the step is emitted with ``target=OpenTarget``, ``notes`` annotated
with ``[unsupported: <key>]``, and the original Garmin step dict stashed in
``Step.opaque`` so the forward translator can re-emit it verbatim during
:func:`src.garmin.translate_forward.to_garmin`.

Inverse mappings vs the forward translator
------------------------------------------
- Step kind: Garmin ``stepType.stepTypeId`` (1=warmup, 2=cooldown, 3=interval,
  4=recovery, 5=rest, 6=repeat) maps to canonical ``StepKind`` via
  :data:`_STEP_KIND_BY_ID`. ``"interval"`` (id 3) maps to canonical
  ``"active"``. Note: forward translator collapses ``StepKind == "rest"`` into
  ``create_recovery_step``, so a canonical ``"rest"`` round-trips as
  ``"recovery"`` through Garmin — accept this lossy direction documented in
  DESIGN.md §8.
- End-condition: ``conditionTypeKey == "time"`` -> :class:`TimeDuration`;
  ``"distance"`` -> :class:`DistanceDuration`. Anything else triggers the
  opaque fallback for the whole step.
- Target: ``workoutTargetTypeKey == "heart.rate.zone"`` -> :class:`HRRangeTarget`,
  ``"pace.zone"`` -> :class:`PaceTarget`, ``"no.target"`` -> :class:`OpenTarget`.
  Anything else triggers the opaque fallback.
- HR open-ended sentinel: the forward translator substitutes ``220`` for an
  open-ended ``HRRangeTarget.max_bpm``. We invert: ``targetValueTwo == 220``
  -> ``max_bpm = None``. This matches the constant
  ``_OPEN_HR_MAX_SENTINEL = 220`` in :mod:`translate_forward`.
- Pace: forward emits ``targetValueOne = mps(max_sec_per_km)`` (slow end,
  smaller m/s) and ``targetValueTwo = mps(min_sec_per_km)`` (fast end, larger
  m/s). Inverse: ``min_sec_per_km = 1000/targetValueTwo`` (fast end is the
  larger m/s); ``max_sec_per_km = 1000/targetValueOne``.
- Description prefix: ``"[mcp][<sport>] <rest>"`` -> sport extracted, prefix
  stripped from :attr:`Workout.description`. Missing/non-mcp prefix defaults
  to ``"road_run"`` and leaves the description verbatim (external workouts).
- ``BaseWorkout.author = {}`` and other unknown top-level fields are
  ignored (we read only what we know).
"""

from __future__ import annotations

import re
from typing import Any

from src.models import (
    DistanceDuration,
    HRRangeTarget,
    OpenTarget,
    PaceTarget,
    RepeatGroup,
    Sport,
    Step,
    StepKind,
    TimeDuration,
    Workout,
    WorkoutStep,
)
from src.models import (
    Duration as CanonicalDuration,
)
from src.models import (
    Target as CanonicalTarget,
)

# Garmin stepTypeId -> canonical StepKind. Id 6 (repeat) is handled
# structurally (RepeatGroupDTO) and is not present in this map.
_STEP_KIND_BY_ID: dict[int, StepKind] = {
    1: "warmup",
    2: "cooldown",
    3: "active",
    4: "recovery",
    5: "rest",
}

# Same mapping by stepTypeKey, as a defensive fallback for payloads where the
# id is missing but the key is present.
_STEP_KIND_BY_KEY: dict[str, StepKind] = {
    "warmup": "warmup",
    "cooldown": "cooldown",
    "interval": "active",
    "recovery": "recovery",
    "rest": "rest",
}

# Garmin sentinel value used by the forward translator when
# ``HRRangeTarget.max_bpm`` is ``None``. Inverting it back to ``None`` keeps
# round-trips exact.
_OPEN_HR_MAX_SENTINEL = 220

# Sports we recognise from the ``[mcp][<sport>]`` description prefix.
_KNOWN_SPORTS: set[Sport] = {"road_run", "trail_run", "treadmill_run"}

# ``[mcp][<sport>] optional trailing text`` matcher. Used to split the
# description into sport sub-type + clean user-facing description.
_MCP_PREFIX_RE = re.compile(r"^\[mcp\]\[(?P<sport>[a-z_]+)\](?:\s+(?P<rest>.*))?$")

# Default canonical sport for externally-created workouts that don't carry an
# ``[mcp]`` prefix.
_DEFAULT_SPORT: Sport = "road_run"


def _parse_description_prefix(description: str) -> tuple[Sport, str | None]:
    """Split a Garmin description into ``(sport, clean_description)``.

    - ``"[mcp][trail_run]"`` -> ``("trail_run", None)``
    - ``"[mcp][road_run] Strides on grass"`` -> ``("road_run", "Strides on grass")``
    - ``""`` or any non-``[mcp]`` text -> ``(_DEFAULT_SPORT, original)``
      (the original text is preserved so external workouts don't lose data).
    - Unknown sport inside ``[mcp][...]`` -> ``(_DEFAULT_SPORT, original)``.
    """
    match = _MCP_PREFIX_RE.match(description)
    if match is None:
        # External workout (no [mcp] marker) - preserve original description,
        # default sport.
        return _DEFAULT_SPORT, description if description else None

    sport_raw = match.group("sport")
    rest = match.group("rest")
    if sport_raw not in _KNOWN_SPORTS:
        # Unknown sport variant in a [mcp]-prefixed description: be defensive,
        # default to road_run and keep the original text so nothing is lost.
        return _DEFAULT_SPORT, description

    sport: Sport = sport_raw  # type: ignore[assignment]
    if rest is None or rest == "":
        return sport, None
    return sport, rest


def _parse_step_kind(step_dict: dict[str, Any]) -> StepKind:
    """Recover canonical ``StepKind`` from a Garmin executable-step dict.

    Prefers ``stepType.stepTypeId`` (numeric, library-emitted) and falls back
    to ``stepTypeKey`` (string). Unknown values default to ``"active"`` so a
    payload with a weird step type can still produce a usable opaque-fallback
    step rather than crashing.
    """
    step_type = step_dict.get("stepType") or {}
    type_id = step_type.get("stepTypeId")
    if isinstance(type_id, int) and type_id in _STEP_KIND_BY_ID:
        return _STEP_KIND_BY_ID[type_id]

    type_key = step_type.get("stepTypeKey")
    if isinstance(type_key, str) and type_key in _STEP_KIND_BY_KEY:
        return _STEP_KIND_BY_KEY[type_key]

    # Unknown step type - the safest canonical bucket is "active"; the
    # opaque blob (set by the caller) will preserve the original shape
    # if the duration/target also fall through.
    return "active"


def _parse_duration(step_dict: dict[str, Any]) -> CanonicalDuration | None:
    """Parse a canonical duration from a Garmin step dict.

    Returns ``None`` if the end-condition is one we don't model (calories,
    cadence, power, lap-button, etc.) — the caller treats that as a signal to
    trigger the opaque fallback.
    """
    end_condition = step_dict.get("endCondition") or {}
    condition_key = end_condition.get("conditionTypeKey")
    raw_value = step_dict.get("endConditionValue")

    if raw_value is None:
        return None

    try:
        value_float = float(raw_value)
    except (TypeError, ValueError):
        return None

    if condition_key == "time":
        seconds = int(value_float)
        if seconds <= 0:
            return None
        return TimeDuration(seconds=seconds)

    if condition_key == "distance":
        if value_float <= 0:
            return None
        return DistanceDuration(meters=value_float)

    return None


def _parse_target(step_dict: dict[str, Any]) -> CanonicalTarget | None:
    """Parse a canonical target from a Garmin step dict.

    Garmin stores ``targetValueOne`` / ``targetValueTwo`` as top-level fields
    on the step (siblings of ``targetType``), not nested inside the target
    metadata block. We read them from there. Returns ``None`` if the target
    type is one we don't model (power.zone, cadence, custom) — the caller
    treats that as a signal to trigger the opaque fallback.
    """
    target_type = step_dict.get("targetType") or {}
    target_key = target_type.get("workoutTargetTypeKey")

    if target_key == "no.target":
        return OpenTarget()

    if target_key == "heart.rate.zone":
        v_one = step_dict.get("targetValueOne")
        v_two = step_dict.get("targetValueTwo")
        if not isinstance(v_one, (int, float)):
            return None
        min_bpm = int(v_one)
        if isinstance(v_two, (int, float)):
            v_two_int = int(v_two)
            max_bpm: int | None = None if v_two_int == _OPEN_HR_MAX_SENTINEL else v_two_int
        else:
            max_bpm = None
        return HRRangeTarget(min_bpm=min_bpm, max_bpm=max_bpm)

    if target_key == "pace.zone":
        v_one = step_dict.get("targetValueOne")
        v_two = step_dict.get("targetValueTwo")
        if not isinstance(v_one, (int, float)) or not isinstance(v_two, (int, float)):
            return None
        if v_one <= 0 or v_two <= 0:
            return None
        # Forward translator emits valueOne < valueTwo (slow m/s < fast m/s):
        #   valueOne = mps(max_sec_per_km)   -> 1000/valueOne = max_sec_per_km
        #   valueTwo = mps(min_sec_per_km)   -> 1000/valueTwo = min_sec_per_km
        min_sec_per_km = 1000.0 / float(v_two)
        max_sec_per_km = 1000.0 / float(v_one)
        return PaceTarget(
            min_sec_per_km=min_sec_per_km,
            max_sec_per_km=max_sec_per_km,
        )

    return None


def _opaque_step(step_dict: dict[str, Any]) -> Step:
    """Build an opaque-fallback ``Step`` from a Garmin step dict.

    Used when end-condition or target is unsupported. The opaque blob carries
    the verbatim Garmin shape; the canonical fields (``kind``, ``duration``,
    ``target``) are placeholders that the forward translator will skip — it
    sees ``opaque is not None`` and emits the blob unchanged.

    The placeholder ``duration`` is a 1-second ``TimeDuration`` (the canonical
    schema requires ``seconds > 0``); it is never used downstream because the
    forward translator bypasses the canonical fields entirely when ``opaque``
    is set.
    """
    kind = _parse_step_kind(step_dict)
    target_type = step_dict.get("targetType") or {}
    end_condition = step_dict.get("endCondition") or {}
    unsupported_key = (
        target_type.get("workoutTargetTypeKey")
        or end_condition.get("conditionTypeKey")
        or "unknown"
    )
    return Step(
        kind=kind,
        duration=TimeDuration(seconds=1),
        target=OpenTarget(),
        notes=f"[unsupported: {unsupported_key}]",
        opaque=dict(step_dict),
    )


def _parse_executable_step(step_dict: dict[str, Any]) -> Step:
    """Parse one Garmin executable step into a canonical :class:`Step`.

    Falls back to an opaque blob when the duration or target is unsupported.
    """
    duration = _parse_duration(step_dict)
    target = _parse_target(step_dict)
    if duration is None or target is None:
        return _opaque_step(step_dict)

    return Step(
        kind=_parse_step_kind(step_dict),
        duration=duration,
        target=target,
    )


def _parse_repeat_group(group_dict: dict[str, Any]) -> RepeatGroup:
    """Parse a Garmin ``RepeatGroupDTO`` recursively into a canonical group."""
    iterations_raw = group_dict.get("numberOfIterations", 1)
    try:
        iterations = int(iterations_raw)
    except (TypeError, ValueError):
        iterations = 1
    # Clamp to the canonical schema's bounds (1-99).
    iterations = max(1, min(iterations, 99))

    inner_steps = group_dict.get("workoutSteps") or []
    children: list[WorkoutStep] = _parse_segment_steps(inner_steps)
    return RepeatGroup(times=iterations, steps=children)


def _is_repeat_group(step_dict: dict[str, Any]) -> bool:
    """Decide whether a step dict is a RepeatGroup vs an ExecutableStep.

    Prefers the ``type`` discriminator emitted by the library
    (``"RepeatGroupDTO"`` vs ``"ExecutableStepDTO"``); falls back to
    ``stepType.stepTypeKey == "repeat"`` or the presence of
    ``numberOfIterations`` for resilience.
    """
    dto_type = step_dict.get("type")
    if dto_type == "RepeatGroupDTO":
        return True
    if dto_type == "ExecutableStepDTO":
        return False
    # Fallbacks for payloads that omit the DTO type discriminator.
    step_type = step_dict.get("stepType") or {}
    if step_type.get("stepTypeKey") == "repeat":
        return True
    return "numberOfIterations" in step_dict


def _parse_segment_steps(steps_list: list[Any]) -> list[WorkoutStep]:
    """Parse a list of Garmin step dicts (executable or repeat) recursively."""
    out: list[WorkoutStep] = []
    for raw in steps_list:
        if not isinstance(raw, dict):
            # Garmin payload shape is dicts all the way down; anything else is
            # unexpected. Skip rather than crash.
            continue
        if _is_repeat_group(raw):
            out.append(_parse_repeat_group(raw))
        else:
            out.append(_parse_executable_step(raw))
    return out


def garmin_to_canonical(payload: dict[str, Any]) -> Workout:
    """Translate a Garmin workout payload into a canonical :class:`Workout`.

    Reads the first ``workoutSegments`` entry (the forward translator emits a
    single segment) and walks its ``workoutSteps`` recursively. Description
    prefix ``[mcp][<sport>]`` is parsed to recover the sport sub-type and is
    stripped from the returned :attr:`Workout.description`. Unknown top-level
    fields (e.g. ``author``) are ignored.

    External workouts that lack the ``[mcp]`` marker default to
    ``sport="road_run"`` and preserve the original description as-is.
    """
    name = str(payload.get("workoutName") or "Untitled")

    description_raw = payload.get("description") or ""
    if not isinstance(description_raw, str):
        description_raw = ""
    sport, clean_description = _parse_description_prefix(description_raw)

    segments = payload.get("workoutSegments") or []
    first_segment: dict[str, Any] = segments[0] if segments else {}
    steps_raw = first_segment.get("workoutSteps") or []
    steps = _parse_segment_steps(steps_raw)

    return Workout(
        name=name,
        sport=sport,
        description=clean_description,
        steps=steps,
    )
