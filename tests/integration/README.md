# Integration tests

Live tests that hit real external services (Garmin Connect). They are **skipped
by default** — the project's `pyproject.toml` carries `addopts = "-m 'not
integration'"` so `uv run pytest` runs only the hermetic unit suite.

## What runs here

| File | What it exercises |
|---|---|
| `test_round_trip_live.py` | Full happy-path round trip of the six Phase 1/2 Garmin write tools: `create_and_schedule` → `list_scheduled_workouts` → `get_scheduled_workout` → `replace_scheduled_workout` → `unschedule_workout` → `delete_workout`. |

Only the happy path is exercised. Rollback / failure-injection paths in
`replace_scheduled_workout` are covered by T14's stub-based unit tests;
running them live risks bricking a real calendar entry (see STATUS.md backlog
under "T14 reviewer (handoff to T15)").

## Prerequisites

### 1. Garmin tokens

Run the one-time bootstrap to populate `~/.garminconnect/garmin_tokens.json`:

```bash
uv run python -m scripts.garmin_bootstrap
```

This prompts for the Garmin email / password / MFA code and writes a token
bundle the `garminconnect` library can refresh against until it expires
(months out).

### 2. Strava tokens (not strictly needed for T15, but the config validates them)

`src.config.Settings` requires `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, and
`STRAVA_REFRESH_TOKEN` at import time. The integration test imports
`src.garmin.tools`, which lazy-imports `src.config` during a timezone lookup,
so the env vars must be set even though the test itself does not call Strava.

Run the one-time OAuth dance to populate `STRAVA_REFRESH_TOKEN` in `.env`:

```bash
uv run python -m scripts.strava_bootstrap
```

### 3. `.env` populated

Copy `.env.example` to `.env` and fill in:

| Var | Notes |
|---|---|
| `CONNECTOR_BEARER_TOKEN` | Any 32-byte hex (`python -c "import secrets; print(secrets.token_hex(32))"`). Not exercised by this test but required for `Settings`. |
| `STRAVA_CLIENT_ID` | From <https://www.strava.com/settings/api>. |
| `STRAVA_CLIENT_SECRET` | Same page. |
| `STRAVA_REFRESH_TOKEN` | Written by `scripts/strava_bootstrap.py`. |
| `GARMIN_TOKEN_DIR` | Optional; defaults to `~/.garminconnect`. |
| `USER_TIMEZONE` | Optional; defaults to `Africa/Tunis`. |

## Running

```bash
uv run pytest tests/integration/ -m integration
```

Add `-s` for live tool output and `-v` for per-step assertions.

## What the test does to your Garmin account

For each run it creates one workout on the library and one calendar entry
60 days in the future, then deletes both before exiting. The 60-day offset
is intentional: Garmin's watch sync window is much shorter, so the temporary
entry will not propagate to your watch even if the test crashes between create
and cleanup.

## If the test crashes mid-run

The test wraps cleanup in `try/finally` with `contextlib.suppress(Exception)`,
so even an assertion failure should drop the entry. But if `pytest` is
SIGKILLed or the process is otherwise hard-killed before the `finally` block:

1. Open Garmin Connect on phone or web.
2. Navigate to the calendar, jump 60 days ahead.
3. Delete any workout named `MCP integration smoke` or `MCP integration smoke (replaced)`.
4. Under **Training → Workouts**, delete the same-named library templates.

## What this test guards against

`smoke_workout` deliberately exercises both an `HRRangeTarget` and a
`PaceTarget` step so the test catches the two regression classes that
lived undetected from T08 through the first live use (see STATUS.md
"Post-build live-test fixes"):

1. **Target value placement.** Garmin reads `targetValueOne` and
   `targetValueTwo` as siblings of `targetType` on the executable step,
   not nested inside it. The test fetches the raw Garmin payload via
   `client.get_scheduled_workout_by_id` and asserts the values are
   present at the step top level.
2. **Pace target type id.** Garmin renders `workoutTargetTypeId=5`
   (`speed.zone`) as km/h and `id=6` (`pace.zone`) as min/km. The test
   asserts the live payload comes back with `workoutTargetTypeKey ==
   "pace.zone"` and `workoutTargetTypeId == 6`, so a silent regression
   to the library's `TargetType.SPEED = 5` would fail loudly.

The canonical round-trip also pins the bpm and sec/km values exactly,
so a reverse-translator bug that fell back to `OpenTarget` (the
original failure mode) can no longer pass with matching step count.
