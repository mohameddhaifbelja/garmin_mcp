---
name: Garmin Connect Wire Format Quirks
description: Hard-won facts about Garmin Connect's workout/calendar API: target value placement, pace vs speed type ids, response envelope shapes, calendar-list field set.
source: garmin_mcp (post-build live-test fixes, 2026-05-16)
date: 2026-05-16
---

Garmin's API doesn't have a public schema and `python-garminconnect` types most responses as `dict[str, Any]`. The facts below were each found by uploading something, watching it fail on the watch, and inspecting the raw `get_scheduled_workout_by_id` payload.

## 1. Target values live at the step top level, not inside `targetType`

For an `ExecutableStepDTO` with an HR or pace target, Garmin reads `targetValueOne` and `targetValueTwo` as **siblings of** `targetType`, NOT nested inside it.

✅ Correct wire shape:
```json
{
  "type": "ExecutableStepDTO",
  "stepOrder": 1,
  "stepType": {...},
  "endCondition": {...},
  "targetType": {
    "workoutTargetTypeId": 4,
    "workoutTargetTypeKey": "heart.rate.zone"
  },
  "targetValueOne": 141,
  "targetValueTwo": 155
}
```

❌ Silently dropped (server stores `null`):
```json
{
  "targetType": {
    "workoutTargetTypeKey": "heart.rate.zone",
    "targetValueOne": 141,
    "targetValueTwo": 155
  }
}
```

The `garminconnect.workout.ExecutableStep` model has `extra="allow"`, so we attach values via `model_copy(update={"targetValueOne": ..., "targetValueTwo": ...})` in `_build_executable_step`.

## 2. Pace target uses `workoutTargetTypeId = 6`, not the library's `TargetType.SPEED = 5`

`garminconnect.workout.TargetType` exposes:
```python
NO_TARGET = 1
POWER = 2
CADENCE = 3
HEART_RATE = 4
SPEED = 5        # renders as km/h
OPEN = 6         # MISLEADING — this is actually pace.zone id, renders as min/km
```

`TargetType.OPEN = 6` is misnamed. Garmin uses id 6 for `pace.zone` (min/km display). The library's "no target" is `NO_TARGET = 1` (`workoutTargetTypeKey = "no.target"`).

If you upload with `id=5` + key `pace.zone`, Garmin normalizes to `speed.zone` (km/h on the watch) and the user sees their tempo step in km/h instead of min/km. Use the literal `6` with key `pace.zone`.

## 3. `get_scheduled_workout_by_id` returns a calendar-entry envelope, not a bare workout

The response shape is the calendar item wrapping the workout:

```json
{
  "calendarDate": "2026-05-28",
  "workoutScheduleId": ...,
  "workout": {
    "workoutId": ...,
    "workoutName": "...",
    "workoutSegments": [...]
  },
  ...
}
```

The workout's own fields (`workoutId`, `workoutName`, etc.) are **nested under `payload["workout"]`**, NOT at the top level. Helpers that pull from the response must check the nested path first (or both paths if test stubs use a flatter shape).

## 4. Calendar-list endpoint drops the workout description entirely

`client.get_scheduled_workouts(year, month)` returns calendar items with only these useful fields:

```json
{
  "id": ...,
  "itemType": "workout",
  "title": "...",
  "date": "YYYY-MM-DD",
  "sportTypeKey": "running",
  "workoutId": ...
}
```

No `description`, no `workoutDescription`, no field that carries through any prose marker from the underlying workout template. Source-classification (mcp vs external) cannot rely on the description here; we prefix `workoutName` with `[mcp] ` so the marker rides on `title`. The forward translator does this idempotently in `_format_name`; the reverse path strips it.

Trade-off accepted: the `[mcp] ` prefix is visible in the Garmin Connect UI and on the watch. The alternative was N+1 fetches per list item to read the template description.

## 5. Distance-based steps don't carry an estimated duration

`_estimated_total_seconds` returns 0 for `DistanceDuration` steps because we have no per-step pace target to estimate from. A purely distance-based workout appears in Garmin Connect with no time estimate at all. For long runs where the plan specifies both a target distance AND a target duration, prefer `TimeDuration` so the watch shows a planned time; embed the distance in the description as prose.
