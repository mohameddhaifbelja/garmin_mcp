# Running MCP Connector — Technical Design (v1)

Personal-use MCP server that lets Claude.ai read recent Strava activities and write structured running workouts to a Garmin Connect calendar.

**Status:** Locked design, ready to build
**Scope:** Single-user, self-hosted on personal laptop
**Athlete context:** 35-week ultra plan, Tunis, road + trail + treadmill running only

---

## 1. Scope

### In
- Strava read tools (3) for fitness/progress context.
- Garmin write tools (6) for workout creation, scheduling, modification, deletion.
- Canonical workout schema specialized to running (road / trail / treadmill).
- Forward + reverse translators between canonical and Garmin's JSON.
- Local deploy on personal laptop via ngrok free tier.

### Out
- Multi-user, hosted SaaS, signups, per-user OAuth UI.
- Non-running sports (cycling/swimming/walking power/swim-stroke metrics).
- Race-event scheduling — Garmin's separate Race Event type is added manually.
- Adaptive coaching, race prediction, training-load math.
- Strength, sauna, HWI — non-running rows in the plan are skipped at ingest.

---

## 2. Architecture

```
Claude.ai ──HTTPS+Bearer──▶ MCP server (laptop, uvicorn, ngrok tunnel)
                                  │
                                  ├──▶ Strava REST API   (stravalib + OAuth refresh)
                                  └──▶ Garmin Connect    (python-garminconnect, mobile SSO)
```

Server is **stateless**. Source of truth for workouts = Garmin Connect. No local DB, no plan cache.

Plan PDFs are parsed inside Claude.ai (multimodal). The server only sees structured tool calls.

---

## 3. Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| MCP framework | `mcp` SDK — `FastMCP` |
| Transport | Streamable HTTP (required by Claude.ai remote connectors) |
| Strava client | `stravalib` |
| Garmin client | `python-garminconnect` (replaces deprecated `garth`) |
| Process | `uvicorn` foreground on the laptop |
| Tunnel | ngrok free tier (URL changes on restart, accepted) |
| Logging | `structlog` to stdout + `~/.fitness-mcp/logs/writes.jsonl` |
| Dep mgmt | `uv` |

---

## 4. Authentication

### 4.1 Strava (OAuth 2.0, one-time)
- `scripts/strava_bootstrap.py` runs the redirect-loopback dance once.
- Writes `STRAVA_REFRESH_TOKEN` to `.env`.
- Runtime: exchange refresh → access on cold start, cache in memory until `expires_at`.

### 4.2 Garmin (mobile SSO, one-time)
- `scripts/garmin_bootstrap.py` prompts for email + password, calls `prompt_mfa` callback if Garmin asks.
- `Garmin.dump(...)` persists tokens at `~/.garminconnect/garmin_tokens.json` (mode 0600).
- Runtime: `Garmin.login(tokenstore=...)` auto-refreshes DI tokens before each call.
- Re-bootstrap needed only on revocation or refresh-token expiry (months).

### 4.3 Connector bearer token
- One 32-byte random token in `.env` as `CONNECTOR_BEARER_TOKEN`.
- Bearer middleware rejects any request without the matching `Authorization: Bearer …` header.

---

## 5. Canonical schema (final)

```python
# src/models.py
from pydantic import BaseModel, Field
from typing import Literal, Union

Sport = Literal["road_run", "trail_run", "treadmill_run"]
StepKind = Literal["warmup", "active", "recovery", "cooldown", "rest"]

class TimeDuration(BaseModel):
    kind: Literal["time"] = "time"
    seconds: int = Field(gt=0)

class DistanceDuration(BaseModel):
    kind: Literal["distance"] = "distance"
    meters: float = Field(gt=0)

Duration = Union[TimeDuration, DistanceDuration]

class PaceTarget(BaseModel):
    kind: Literal["pace"] = "pace"
    min_sec_per_km: float
    max_sec_per_km: float

class HRRangeTarget(BaseModel):
    kind: Literal["hr_range"] = "hr_range"
    min_bpm: int
    max_bpm: int | None = None   # None == open-ended (e.g. "HR > 175")

class OpenTarget(BaseModel):
    kind: Literal["open"] = "open"

Target = Union[PaceTarget, HRRangeTarget, OpenTarget]

class Step(BaseModel):
    kind: StepKind
    duration: Duration
    target: Target = OpenTarget()
    notes: str | None = None        # cadence hints, pace-as-secondary, RPE etc.
    opaque: dict | None = None      # round-trip blob from reverse translator; never set by Claude

class RepeatGroup(BaseModel):
    kind: Literal["repeat"] = "repeat"
    times: int = Field(ge=1, le=99)
    steps: list["Step | RepeatGroup"]

WorkoutStep = Union[Step, RepeatGroup]

class Workout(BaseModel):
    name: str = Field(max_length=100)
    sport: Sport = "road_run"
    description: str | None = None
    steps: list[WorkoutStep]
```

### Rules baked into the schema
- One `Target` per `Step` (Garmin's API enforces this).
- Cadence is **not** a target — lives in `notes` if needed.
- Race effort = `PaceTarget` (workout-level intent), notes capture race-specific cues.
- Trail workouts: prefer `HRRangeTarget` even on tempo-ish steps.

---

## 6. MCP tool surface (final)

### 6.1 Strava (read, observational)
| Tool | Purpose |
|---|---|
| `list_recent_activities(limit, after_iso?, sport_filter?)` | Date, sport, distance, duration, avg HR, avg pace |
| `get_activity_details(activity_id)` | Splits, laps, perceived effort, description |
| `get_weekly_summary(weeks_back)` | Volume by week |

### 6.2 Garmin (write + read)
| Tool | Purpose |
|---|---|
| `create_and_schedule(workout: Workout, date_iso)` | Create on library + place on calendar. Single call. |
| `replace_scheduled_workout(scheduled_id, new_workout: Workout)` | **Atomic** unschedule → delete → create → schedule, with rollback on failure. |
| `unschedule_workout(scheduled_id)` | Remove from calendar (workout stays in library). |
| `delete_workout(workout_id)` | Remove from library. |
| `list_scheduled_workouts(start_iso, end_iso)` | Summary entries: `{scheduled_id, workout_id, date, name, total_duration_sec, source}`. `source` ∈ `"mcp" \| "external"` based on a description marker. |
| `get_scheduled_workout(scheduled_id)` | Full canonical `Workout` via reverse translator. External workouts return best-effort canonical with `opaque` blobs preserved. |

**Deliberately excluded:** `update_workout`, `create_workout` (no-schedule variant), Garmin activity reads, device queries, race scheduling.

---

## 7. Forward translator (canonical → Garmin)

`python-garminconnect`'s helpers (`create_warmup_step`, `create_interval_step`, `create_recovery_step`, `create_cooldown_step`, `create_repeat_group`, `RunningWorkout`, `WorkoutSegment`) cover time-based steps with simple targets.

**Hand-written dict construction** is required for:
- `DistanceDuration` → `{conditionTypeId: 1, conditionTypeKey: "distance", endConditionValue: meters}`
- `HRRangeTarget` → `{workoutTargetTypeId: 4, workoutTargetTypeKey: "heart.rate.zone", targetValueOne: min_bpm, targetValueTwo: max_bpm or 220}`
- `PaceTarget` → `{workoutTargetTypeId: 5, workoutTargetTypeKey: "pace.zone", targetValueOne: 1000/max_sec_per_km, targetValueTwo: 1000/min_sec_per_km}` (m/s, faster value → higher m/s)
- `"rest"` step kind → reuse `create_recovery_step` (no rest-step helper; lossy mapping documented)
- Sport sub-types → emit appropriate Garmin sport workout class (`RunningWorkout` for all three, distinguished via `description` marker and the running profile activity tag)

Pace conversion at Garmin boundary only:
```python
def sec_per_km_to_mps(sec_per_km: float) -> float:
    return 1000.0 / sec_per_km
```

**MCP marker convention.** Every Workout created via this server gets `description` prefixed with `[mcp]` so `list_scheduled_workouts` can tag `source` correctly.

---

## 8. Reverse translator (Garmin → canonical)

Best-effort policy. Parse what's known, preserve what isn't.

### Supported shapes
- Step types: `WARMUP (1)`, `COOLDOWN (2)`, `INTERVAL (3)`, `RECOVERY (4)`, `REST (5)` → maps to `StepKind`.
- End conditions: `TIME (2)` → `TimeDuration`, `DISTANCE (1)` → `DistanceDuration`.
- Targets: `HEART_RATE (4)` → `HRRangeTarget`, `SPEED (5)` (pace) → `PaceTarget`, `NO_TARGET (1)` → `OpenTarget`.
- `RepeatGroup` with nested `ExecutableStep | RepeatGroup`.

### Unsupported shapes → opaque blob fallback
- Targets `POWER (2)`, `CADENCE (3)`, custom step shapes.
- End conditions `CALORIES`, `CADENCE`, `POWER`, anything custom.
- Behavior: set `target = OpenTarget()`, annotate `notes` with e.g. `"[unsupported: power.zone Z3]"`, store the original Garmin step dict in `Step.opaque`.
- On re-upload via `replace_scheduled_workout`: `Step.opaque` is emitted verbatim if set, so the unmodified step round-trips losslessly. Claude can edit *adjacent* steps without corrupting these.

### Round-trip test (required before ship)
Build a golden fixture: an externally-created Garmin workout with a power target, run `get_scheduled_workout` → `replace_scheduled_workout` with the canonical unchanged, verify Garmin accepts the re-upload and the power step is intact.

---

## 9. Server layout

```
running_mcp/
├── pyproject.toml
├── .env.example
├── DESIGN.md                         # this file
├── src/
│   ├── server.py                     # FastMCP app + tool registrations + instructions string
│   ├── config.py                     # Settings: TZ=Africa/Tunis, paths, token paths
│   ├── auth.py                       # BearerAuthMiddleware
│   ├── audit.py                      # writes.jsonl appender
│   ├── models.py                     # canonical schema (section 5)
│   ├── strava/
│   │   ├── client.py
│   │   └── tools.py
│   └── garmin/
│       ├── client.py
│       ├── translate_forward.py      # canonical → Garmin
│       ├── translate_reverse.py      # Garmin → canonical (best-effort)
│       └── tools.py
├── scripts/
│   ├── strava_bootstrap.py
│   └── garmin_bootstrap.py
├── tests/
│   ├── unit/
│   │   ├── test_forward_translator.py    # ~12 golden fixtures from the actual plan
│   │   └── test_reverse_translator.py    # supported + unsupported (opaque) shapes
│   └── integration/
│       └── test_round_trip.py            # smoke test with workout dated 60d out
└── tokens/                              # gitignored
```

---

## 10. Behavioral rules — FastMCP `instructions` string

All orchestration + per-tool semantics live at the server level so Claude sees them once at session start:

```
You schedule running workouts on the user's Garmin Connect calendar.

ON NEW PLAN INGEST:
- Schedule Week 1 only first.
- Stop. Tell the user to verify Week 1 looks correct in Garmin Connect.
- Wait for user confirmation before scheduling weeks 2-N.

TARGET POLICY (per step):
- Tempo, steady-state, race-effort, threshold steps → PaceTarget.
- Easy, long, recovery, warmup, cooldown, interval-on-HR steps → HRRangeTarget.
- When the plan gives both pace and HR, follow this rule; put the secondary
  metric in step.notes for human reference (e.g. "5:15-5:25/km").
- Trail runs (sport=trail_run) → always HRRangeTarget, even for tempo-ish efforts.
- Pace is unreliable on trails per the user's plan.

CONFLICT DETECTION:
- Before each create_and_schedule, call list_scheduled_workouts for that date.
- If anything is already scheduled (mcp or external), surface it to the user
  and ask whether to skip, overwrite (via replace_scheduled_workout if mcp),
  or proceed and have two workouts on the same day.

MODIFICATIONS:
- Step 1: list_scheduled_workouts to find the entry.
- Step 2: get_scheduled_workout(scheduled_id) to read its content.
- Step 3: show the user what's currently scheduled, confirm they want to modify it.
- Step 4: replace_scheduled_workout(scheduled_id, new_workout) once confirmed.
- If get_scheduled_workout returns opaque blobs, you can still edit the surrounding
  steps — the opaque ones round-trip unchanged.

NON-RUNNING ROWS:
- Plan rows that are pure walking, HWI, sauna, strength, or rest days → skip.
- Race rows → skip and tell the user to add as a Race Event in Garmin manually.

CADENCE:
- Garmin only allows one target per step (already used by HR or pace).
- Put cadence hints like "cad 170+" in step.notes — watch shows them as text.
```

Per-tool docstrings stay generic — args, return shape, no behavioral rules.

---

## 11. Time zones

Hardcode `USER_TIMEZONE=Africa/Tunis` in `config.py`. All date strings interpreted in this zone. Optional `--tz` CLI override for race-week travel (Jan 2027 desert ultra).

---

## 12. Audit log

Every Garmin write appends to `~/.fitness-mcp/logs/writes.jsonl`:

```json
{"ts":"2026-05-14T09:12:01+01:00","tool":"create_and_schedule","args":{...},"result":{"workout_id":4451,"scheduled_id":99231}}
```

This is the only undo trail since the server is stateless.

---

## 13. Deployment

### Local laptop loop
```bash
uv sync
uvicorn src.server:app --host 127.0.0.1 --port 8000
ngrok http 8000     # in another terminal — copy the https URL
```

Paste the ngrok URL into Claude.ai → Settings → Connectors → Add custom connector with `Authentication: Bearer token`.

On laptop reboot: restart uvicorn + ngrok, re-paste new URL into Claude.ai connector settings.

---

## 14. Testing

| Layer | What |
|---|---|
| Unit — forward translator | ~12 golden fixtures from the actual ultra plan (warmup-tempo-cooldown, hill repeats, B2B long runs, race-effort intervals, distance-based long run, trail run with HR-only). |
| Unit — reverse translator | Supported shapes round-trip. Unsupported shape: opaque blob preserved + annotation written. |
| Integration | Smoke test: create+schedule a workout 60 days out, list it, get it back, replace it, unschedule, delete. End-to-end on real Garmin, fixture date avoids syncing to device. |
| Live | Week 1 of the actual plan, watch in front of you, verify each step renders correctly. |

Build the forward translator test suite first — that's where bugs compound silently.

---

## 15. Roadmap (ordered)

1. **Bootstraps.** Strava OAuth + Garmin SSO scripts. Verify both produce valid token files.
2. **Schema + forward translator.** Canonical models + ~12 golden fixtures from the plan. Run unit tests until green.
3. **Garmin client wrapper + `create_and_schedule`.** Push one hardcoded easy run to Garmin manually, confirm it appears on calendar.
4. **Remaining write tools.** `unschedule_workout`, `delete_workout`, `list_scheduled_workouts` (summary form with `source` tagging via `[mcp]` description marker).
5. **Reverse translator.** Supported shapes + opaque blob fallback. `get_scheduled_workout` returns canonical.
6. **`replace_scheduled_workout`.** Server-side atomic with rollback. Round-trip test required.
7. **Strava read tools** (3). Simple, low risk.
8. **FastMCP wiring.** Bearer middleware, audit log, `instructions` string with the behavioral rules from §10.
9. **Local deploy.** uvicorn + ngrok. Bearer token in `.env`. First Claude.ai connection.
10. **Smoke test.** Week 1 of the plan from Claude.ai, verify on watch.
11. **Full ingest.** Weeks 2–35 in the same conversation after Week 1 validates.

---

## 16. Residual risks

- **ngrok URL rotation.** Must re-paste connector URL on every laptop reboot. Annoying but tolerable.
- **Garmin token expiry.** Re-bootstrap is interactive (MFA). Months out, but be ready.
- **Opaque-blob round-trip.** Untested assumption that Garmin accepts unmodified opaque step dicts on re-upload. Cover in integration test before relying on it.
- **`[mcp]` description marker for source tagging.** Heuristic; if a user (or Garmin Coach) puts `[mcp]` in a description, source detection breaks. Acceptable for single-user.
- **Lap-button durations dropped.** If the plan ever needs "until I press lap" steps, this requires re-adding `LapButtonDuration` + verifying Garmin actually accepts `lap.button` end-conditions (the library doesn't enumerate it).
- **Garmin sync window (~14 days).** All 35 weeks land on Connect immediately, but only the next ~2 weeks reach the watch. User is responsible for periodically opening Garmin Connect to pull new windows onto the device.
- **Concurrent edits.** If Claude is ingesting weeks 1-2 and you simultaneously edit Garmin Connect on your phone, dedup may miss a workout added between `list_scheduled_workouts` and `create_and_schedule`. Single-user, low likelihood, no mitigation.
