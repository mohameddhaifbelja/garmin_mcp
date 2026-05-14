"""FastMCP server entry point for ``garmin_mcp``.

Wires together the locked design pieces from earlier phases into a single
ASGI app suitable for ``uvicorn src.server:app``:

* :class:`mcp.server.fastmcp.FastMCP` instance ``mcp`` constructed with the
  behavioral-rules :data:`INSTRUCTIONS` string from ``DESIGN.md`` §10. Claude
  sees these once at session start so per-tool docstrings can stay generic.
* Phase 1 tool registration via :func:`src.garmin.tools.register`. Strava and
  the Phase 2 modification tools (``replace_scheduled_workout``,
  ``get_scheduled_workout``, ``unschedule_workout``, ``delete_workout``) are
  deliberately not wired here — they land in T14 / T17.
* :class:`src.auth.BearerAuthMiddleware` is added on the Streamable-HTTP ASGI
  app produced by :meth:`FastMCP.streamable_http_app`, gating every inbound
  request on the ``CONNECTOR_BEARER_TOKEN`` from ``.env`` (DESIGN.md §4.3).

Module-import side effects
--------------------------
Importing this module constructs :class:`BearerAuthMiddleware`, which lazily
loads :mod:`src.config` to read ``connector_bearer_token``. That import will
raise :class:`pydantic.ValidationError` if the required env vars are absent,
which is the intended fail-loudly behaviour for production starts. Tests that
need to import this module must seed the env vars first (see
``tests/unit/test_server.py``).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.auth import BearerAuthMiddleware
from src.garmin import tools as garmin_tools

INSTRUCTIONS = """\
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
"""


mcp = FastMCP(name="garmin_mcp", instructions=INSTRUCTIONS)
garmin_tools.register(mcp)

app = mcp.streamable_http_app()
app.add_middleware(BearerAuthMiddleware)
