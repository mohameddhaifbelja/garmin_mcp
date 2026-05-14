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

## Stretch: pace-target field-ordering verification on a real watch

T08's reviewer flagged that the forward translator's pace-target field
ordering (DESIGN.md §7: `targetValueOne = sec_per_km_to_mps(max_sec_per_km)`
i.e. slow end → smaller m/s; `targetValueTwo = sec_per_km_to_mps(min_sec_per_km)`
i.e. fast end → larger m/s) cannot be verified without seeing the rendered
range on a watch. The current smoke test uses HR targets, not pace, so it
does **not** catch field-order inversion.

To cover this manually:

1. Modify the `smoke_workout` fixture locally to use `PaceTarget` instead
   of `HRRangeTarget` (e.g. `PaceTarget(min_sec_per_km=300, max_sec_per_km=360)`
   = 5:00–6:00/km).
2. Re-run the test, but **comment out the cleanup block** so the entry
   survives.
3. Open Garmin Connect → Calendar → the scheduled workout, or sync to the
   watch and inspect the pace range.
4. Confirm the displayed range reads "slow … fast" (or whichever order
   Garmin renders) consistent with `300–360 s/km`. If the watch shows it
   inverted, the fix is a 2-line swap inside `_pace_zone_target` in
   `src/garmin/translate_forward.py` (see STATUS.md, T08 handoff to T15).
5. **Delete the test workout manually afterward** (see "If the test crashes
   mid-run" above).

This stretch check is intentionally manual — automating it would require
mocking the watch display, which defeats the purpose.
