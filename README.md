# garmin_mcp

Personal-use MCP server that lets [Claude.ai](https://claude.ai) read recent Strava activities and write structured running workouts to a Garmin Connect calendar.

**Design:** see [`DESIGN.md`](./DESIGN.md) for the locked technical design.

## Overview

`garmin_mcp` is a single-user, self-hosted [Model Context Protocol](https://modelcontextprotocol.io) server that exposes:

- **3 Strava read tools** — recent activities, activity details, weekly summaries. Used by Claude for fitness/progress context.
- **6 Garmin write tools** — create, schedule, list, get, replace, unschedule, and delete running workouts on your Garmin Connect calendar.

The intended flow is:

1. Drop or upload a training-plan PDF into a Claude.ai chat.
2. Ask Claude to schedule the plan onto your Garmin watch.
3. Claude calls this server's tools over HTTPS (via an `ngrok` tunnel) to write workouts to Garmin Connect, which then sync to your watch.

The server is stateless. The source of truth for scheduled workouts is Garmin Connect itself — there is no local database or plan cache.

**Out of scope:** multi-user / hosted SaaS, non-running sports, race-event scheduling, adaptive coaching. See `DESIGN.md` §1 for the full in/out list.

## Prerequisites

- **Python 3.12** — pinned in [`.python-version`](./.python-version).
- **[`uv`](https://docs.astral.sh/uv/)** — Python package manager. Install via `curl -LsSf https://astral.sh/uv/install.sh | sh` or your platform's equivalent.
- **Strava API app** — create one at <https://www.strava.com/settings/api>. Set the **Authorization Callback Domain** to `localhost`. You'll need the resulting Client ID + Client Secret.
- **Garmin Connect account** — email + password. Be ready to enter an MFA code if your account has MFA enabled.
- **[`ngrok`](https://ngrok.com/download)** (free tier is fine) — exposes the local server over HTTPS so Claude.ai can reach it.
- **Claude.ai account** with custom-connector access (the "Connectors" UI under Settings).

## Install

```bash
git clone git@github.com:mohameddhaifbelja/garmin_mcp.git
cd garmin_mcp
uv sync
```

`uv sync` reads `pyproject.toml` + `uv.lock` and provisions a project-local virtualenv under `.venv/`.

## One-time setup (Strava)

1. Copy the example env file:

   ```bash
   cp .env.example .env
   ```

2. Open `.env` and fill in:
   - `STRAVA_CLIENT_ID` — from <https://www.strava.com/settings/api>.
   - `STRAVA_CLIENT_SECRET` — same page.
   - `CONNECTOR_BEARER_TOKEN` — a fresh 32-byte hex token. Generate one with:

     ```bash
     python -c "import secrets; print(secrets.token_hex(32))"
     ```

   Leave `STRAVA_REFRESH_TOKEN` blank for now — the next step writes it.

3. Run the Strava OAuth bootstrap:

   ```bash
   uv run python -m scripts.strava_bootstrap
   ```

   This opens your browser to Strava's authorization page. After you approve the requested scopes (`read`, `activity:read_all`), the script captures the redirect, exchanges the code for a refresh token, writes `STRAVA_REFRESH_TOKEN=...` back into `.env`, and prints your athlete name as confirmation.

## One-time setup (Garmin)

Run the Garmin SSO bootstrap:

```bash
uv run python -m scripts.garmin_bootstrap
```

It prompts interactively for your Garmin email, password, and (if your account uses MFA) an MFA code. On success it writes a token bundle to `~/.garminconnect/garmin_tokens.json` with mode `0600`. The runtime later calls `Garmin.login(tokenstore=...)`, which auto-refreshes those tokens before each Garmin call; you only need to re-run this script if the tokens are revoked or expire (months out — see Troubleshooting).

If you want to keep the tokens somewhere other than `~/.garminconnect`, set `GARMIN_TOKEN_DIR` in `.env` first.

## Run the server

In a terminal:

```bash
uv run uvicorn src.server:app --host 127.0.0.1 --port 8000
```

The server logs a startup line and binds to `127.0.0.1:8000`. Nothing external can reach it yet — that's the next step.

## Expose via ngrok

In a **second** terminal:

```bash
ngrok http 8000
```

Copy the `https://<random>.ngrok-free.app` URL from ngrok's output. **The free tier rotates this URL every time you restart ngrok**, so expect to re-paste it after each laptop reboot.

## Add to Claude.ai

1. Open <https://claude.ai> → **Settings → Connectors → Add custom connector**.
2. Paste the `https://*.ngrok-free.app` URL from the previous step.
3. Authentication: select **Bearer token**.
4. Paste the value of `CONNECTOR_BEARER_TOKEN` from your `.env` file.
5. Save.

Claude.ai will probe the connector and surface the 9 tools (3 Strava + 6 Garmin) in any chat where the connector is enabled. Claude also reads the server's `instructions` string at session start — that's where the per-tool behavioral rules from `DESIGN.md` §10 live (one-week-at-a-time ingest, target policy per step kind, conflict detection, modification flow, cadence-as-notes). You don't need to repeat those rules in your prompts.

## First plan ingest

The recommended first run, with the watch in front of you:

1. Start a new Claude.ai chat with the `garmin_mcp` connector enabled.
2. Paste or upload your training-plan PDF.
3. Ask: **"Schedule week 1 only on my Garmin."**
4. Claude will translate week 1's rows into canonical workouts, call `create_and_schedule` for each one, and stop. Per the server's behavioral rules, Claude **must not** proceed to weeks 2–N until you confirm week 1 looks correct.
5. Open Garmin Connect (web) → Calendar. Verify each scheduled workout: name, date, steps, target type (pace vs HR), durations.
6. Once week 1 looks right, tell Claude to continue with weeks 2 through N (or however many you want at once).

Dates are interpreted in `USER_TIMEZONE` (defaults to `Africa/Tunis` — see `DESIGN.md` §11). Adjust in `.env` if you're scheduling for a different zone.

## Modification flow

To change an already-scheduled workout in a fresh chat:

> "Show me the workout scheduled on 2026-06-15 and replace the tempo step with 4×1 km at 5:00/km with 90 s recovery."

Claude follows the modification rules baked into the server's `instructions` string:

1. Calls `list_scheduled_workouts` for that date.
2. Calls `get_scheduled_workout(scheduled_id)` to read the current canonical workout.
3. Shows you the existing workout and asks for confirmation before changing anything.
4. Once confirmed, calls `replace_scheduled_workout(scheduled_id, new_workout)` — which atomically unschedules the old entry, deletes the old template, uploads the new one, and reschedules. Rollback is handled server-side if any step fails (`DESIGN.md` §6, §8).

Externally-created Garmin workouts (e.g. from Garmin Coach) round-trip via opaque blobs for any Garmin features the canonical schema doesn't model (power targets, calorie-based end conditions, etc.). Claude can safely edit *adjacent* steps without corrupting those opaque steps.

## Troubleshooting

### ngrok URL changed after restart

Free-tier ngrok rotates the public hostname on every restart. Your previously-saved connector URL is now dead.

1. Restart the tunnel: `ngrok http 8000`.
2. Copy the new `https://*.ngrok-free.app` URL.
3. In Claude.ai → **Settings → Connectors**, edit the `garmin_mcp` connector, paste the new URL, save.

The bearer token stays the same — no need to regenerate it.

### Garmin tokens expired

Symptom: tool calls fail with `GarminAuthError` and a hint in the message.

The refresh token lives for months but eventually expires (or gets revoked if you change your Garmin password). Re-bootstrap:

```bash
uv run python -m scripts.garmin_bootstrap
```

Interactive — re-enter email/password and any MFA code. The new token bundle replaces the old one at `~/.garminconnect/garmin_tokens.json`. Restart `uvicorn` so the server picks up the new tokens on its next Garmin call.

### Strava refresh token revoked

Symptom: tool calls fail with `StravaAuthError`.

Happens if you revoke API access for the app from <https://www.strava.com/settings/apps>, or if Strava expires the token. Re-run:

```bash
uv run python -m scripts.strava_bootstrap
```

The browser dance produces a fresh refresh token and writes it back into `.env`. Restart `uvicorn` so the server reads the new value.

### Watch hasn't synced new workouts

Garmin Connect only pushes roughly the **next 14 days** of scheduled workouts to your watch. Workouts further out sit on Connect but won't reach the device until they fall inside that window. This is expected behavior for long plans (the ultra-plan use case schedules 35 weeks at once).

To force a sync:

1. Open the Garmin Connect mobile app.
2. Pull-to-refresh on the dashboard, or open the Calendar tab.
3. Wait a few seconds for the phone to push to the watch (your watch must be paired and reachable).

If a workout you expected within the next 14 days still isn't on the watch after a sync, check that it's actually present on Garmin Connect web first — that rules out a failed create on the server side.

## Verification commands (optional)

After installing, you can sanity-check the project:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/unit -q
```

To run the live integration smoke test (creates and tears down a single workout 60 days out on your real Garmin account — see [`tests/integration/README.md`](./tests/integration/README.md)):

```bash
uv run pytest tests/integration/ -m integration
```

## CI

Pull requests run lint + unit tests via GitHub Actions on every push and PR to `main` (see [`.github/workflows/ci.yml`](./.github/workflows/ci.yml)). To make CI a hard merge gate, enable **Require status checks to pass before merging** for `main` under **Settings → Branches → Branch protection rules** on the GitHub repo (one-time, requires repo-admin access).

## Tech

Python 3.12 · [FastMCP](https://modelcontextprotocol.io) (Streamable HTTP transport) · [`stravalib`](https://github.com/stravalib/stravalib) · [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) · `uvicorn` · `uv` · `ruff` · `structlog`
