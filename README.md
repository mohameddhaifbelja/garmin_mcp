# garmin_mcp

Personal MCP server bridging Claude.ai with Strava (read) and Garmin Connect (write) — attach a training plan in chat and have the workouts scheduled on your watch.

**Status:** under construction
**Design:** see [`DESIGN.md`](./DESIGN.md)

## What it does

- Reads recent Strava activities so Claude can reason about your current fitness.
- Writes structured running workouts to your Garmin Connect calendar, where they sync to your watch.
- Lets Claude modify scheduled workouts in a fresh chat (read the existing workout via reverse translator, confirm with you, atomically replace).

## What it doesn't do

- Multi-user / hosted SaaS.
- Sports other than running (road / trail / treadmill).
- Race-event scheduling (use Garmin Connect's Race Event type directly).
- Adaptive coaching, training-load math, race prediction.

## Quick start

(filled in as Phase 0–1 ship)

```bash
uv sync
uv run python scripts/strava_bootstrap.py    # one-time
uv run python scripts/garmin_bootstrap.py    # one-time
uv run uvicorn src.server:app --host 127.0.0.1 --port 8000
ngrok http 8000                               # paste URL into Claude.ai connector settings
```

## Tech

Python 3.11+ · FastMCP · stravalib · python-garminconnect · uvicorn · uv · ruff
