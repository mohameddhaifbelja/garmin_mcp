"""Canonical workout schema for the Garmin MCP server.

This module is the single contract for "what a workout looks like" inside the
server. All translators (canonical -> Garmin, Garmin -> canonical) and tools
work against these models. The schema is locked by ``DESIGN.md`` §5.

Design notes
------------
- All polymorphic fields use a literal ``kind`` discriminator so pydantic v2
  can dispatch the right variant during ``model_validate_json`` without
  trying each member of the union in turn.
- ``RepeatGroup.steps`` is a recursive list that mixes ``Step`` and
  ``RepeatGroup``. The annotation uses a string forward reference and the
  class is rebuilt at module load via ``model_rebuild``.
- ``Step.opaque`` is the round-trip blob written by the reverse translator
  for Garmin step shapes that don't fit the canonical model (e.g. power
  targets). It is never set by Claude / the LLM caller — it just rides
  through unchanged.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

Sport = Literal["road_run", "trail_run", "treadmill_run"]
StepKind = Literal["warmup", "active", "recovery", "cooldown", "rest"]


class TimeDuration(BaseModel):
    """Step duration expressed in seconds (e.g. "run for 5 minutes")."""

    kind: Literal["time"] = "time"
    seconds: int = Field(gt=0)


class DistanceDuration(BaseModel):
    """Step duration expressed in meters (e.g. "run for 1 km")."""

    kind: Literal["distance"] = "distance"
    meters: float = Field(gt=0)


Duration = Annotated[TimeDuration | DistanceDuration, Field(discriminator="kind")]


class PaceTarget(BaseModel):
    """Pace band, expressed as seconds-per-km bounds.

    Numerically, a *faster* pace is a *smaller* number of seconds per km, so
    ``min_sec_per_km`` is the fast end of the band and ``max_sec_per_km`` is
    the slow end. The validator enforces ``min < max``.
    """

    kind: Literal["pace"] = "pace"
    min_sec_per_km: float
    max_sec_per_km: float

    @model_validator(mode="after")
    def _check_pace_band(self) -> PaceTarget:
        if self.min_sec_per_km >= self.max_sec_per_km:
            raise ValueError(
                "min_sec_per_km must be strictly less than max_sec_per_km "
                "(faster pace = smaller seconds-per-km value)"
            )
        return self


class HRRangeTarget(BaseModel):
    """Heart-rate band in bpm.

    ``max_bpm = None`` encodes open-ended targets like "HR > 175" — there is
    no upper bound. The forward translator substitutes a sentinel value at
    the Garmin boundary when this happens.
    """

    kind: Literal["hr_range"] = "hr_range"
    min_bpm: int
    max_bpm: int | None = None


class OpenTarget(BaseModel):
    """No target — pace and HR are unconstrained for this step."""

    kind: Literal["open"] = "open"


Target = Annotated[
    PaceTarget | HRRangeTarget | OpenTarget,
    Field(discriminator="kind"),
]


class Step(BaseModel):
    """A single executable step inside a workout.

    ``opaque`` is reserved for the reverse translator: when a Garmin step
    uses a target or end-condition we don't model (e.g. power zone, cadence
    target), the raw Garmin dict is stashed here so it round-trips intact.
    Claude / LLM callers must leave this field as ``None``.
    """

    kind: StepKind
    duration: Duration
    target: Target = Field(default_factory=OpenTarget)
    notes: str | None = None
    opaque: dict | None = None


class RepeatGroup(BaseModel):
    """A repeated block of steps (e.g. "10 x 400m fast / 90s easy").

    ``steps`` is heterogeneous: a repeat block can contain plain ``Step``
    instances and/or further nested ``RepeatGroup`` instances. The string
    annotation defers the reference; ``model_rebuild`` below resolves it
    once both classes exist.
    """

    kind: Literal["repeat"] = "repeat"
    times: int = Field(ge=1, le=99)
    steps: list[Step | RepeatGroup]


WorkoutStep = Annotated[Step | RepeatGroup, Field(discriminator="kind")]


class Workout(BaseModel):
    """A complete workout the user can schedule on Garmin Connect."""

    name: str = Field(max_length=100)
    sport: Sport = "road_run"
    description: str | None = None
    steps: list[WorkoutStep]


# Resolve the forward reference in ``RepeatGroup.steps``. Must run after
# ``Step`` is defined.
RepeatGroup.model_rebuild()
