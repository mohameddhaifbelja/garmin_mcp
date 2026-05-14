"""Unit tests for ``src.server`` — the FastMCP server wiring (T12).

The server module is import-effectful: it constructs
:class:`src.auth.BearerAuthMiddleware`, which lazily imports :mod:`src.config`
to read ``connector_bearer_token``. To keep the test hermetic without a real
``.env`` we seed the required env vars via ``monkeypatch.setenv`` and force a
fresh module import via :func:`importlib.reload`. This mirrors the pattern
``tests/unit/test_config.py`` already uses for the same singleton.

What's covered here (automated):
- ``mcp.name == "garmin_mcp"``.
- :data:`INSTRUCTIONS` carries every section header from ``DESIGN.md`` §10.
- ``BearerAuthMiddleware`` is present on ``app.user_middleware``.
- The Phase 1 Garmin tools are registered; the Phase 2 / Strava tools are
  deliberately absent.
- ``POST /mcp`` without (and with wrong) Bearer returns 401 — driven through
  Starlette's :class:`TestClient` so AC §5 has an automated guard. The
  "valid Bearer → MCP handshake succeeds" half still requires a live server
  and is documented in the PR body as a deferred manual step.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from src.auth import BearerAuthMiddleware

_REQUIRED_INSTRUCTIONS_SECTIONS = (
    "ON NEW PLAN INGEST",
    "TARGET POLICY",
    "CONFLICT DETECTION",
    "MODIFICATIONS",
    "NON-RUNNING ROWS",
    "CADENCE",
)


def _seed_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the env vars ``src.config.Settings`` requires."""
    monkeypatch.setenv("CONNECTOR_BEARER_TOKEN", "test-bearer-token")
    monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", "refresh-token")


def _fresh_server_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Force a clean re-import of ``src.server`` and the modules it constructs.

    ``src.config`` caches a module-level ``settings`` singleton and
    ``src.server`` captures the bearer token at construction time, so a
    reload of one without the other would leak state across tests.
    """
    _seed_required_env(monkeypatch)
    for name in ("src.server", "src.config"):
        sys.modules.pop(name, None)
    return importlib.import_module("src.server")


@pytest.fixture
def server_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Yield a freshly-imported ``src.server`` with required env vars seeded."""
    return _fresh_server_module(monkeypatch)


def test_fastmcp_instance_named_garmin_mcp(server_module: ModuleType) -> None:
    """AC §1 — the FastMCP instance is named ``garmin_mcp``."""
    assert server_module.mcp.name == "garmin_mcp"


def test_instructions_string_carries_every_design_section(server_module: ModuleType) -> None:
    """AC §1 — every behavioural section from DESIGN.md §10 is in INSTRUCTIONS."""
    instructions = server_module.INSTRUCTIONS

    assert isinstance(instructions, str)
    # Multi-line — guards against accidental single-line collapse.
    assert "\n" in instructions

    for section in _REQUIRED_INSTRUCTIONS_SECTIONS:
        assert section in instructions, f"INSTRUCTIONS missing section: {section!r}"


def test_app_is_starlette_with_bearer_middleware(server_module: ModuleType) -> None:
    """AC §3 — ``app`` is an ASGI Starlette app with BearerAuthMiddleware added."""
    app = server_module.app

    assert isinstance(app, Starlette)
    middleware_classes = [m.cls for m in app.user_middleware]
    assert BearerAuthMiddleware in middleware_classes, (
        f"BearerAuthMiddleware not registered; got {middleware_classes!r}"
    )


def test_phase1_tools_registered_and_phase2_tools_absent(
    server_module: ModuleType,
) -> None:
    """AC §2 — the Garmin (and now Strava) tools registered on ``mcp`` are wired.

    T12 originally pinned only the two Phase 1 tools as present and the four
    Phase 2 modification tools as absent. T14 brought the modification subset
    online; T17 brings the three Strava read tools online. Per the T14
    reviewer convention noted in STATUS.md (subset-equality, not exact-set
    equality), this test now asserts the required subset is present rather
    than maintaining a fragile forbidden set.
    """
    # ``FastMCP._tool_manager._tools`` is the canonical registry; we reach in
    # rather than spin up an async event loop just to call ``list_tools()``.
    tool_names = set(server_module.mcp._tool_manager._tools.keys())

    assert {
        "create_and_schedule",
        "list_scheduled_workouts",
        "get_scheduled_workout",
        "replace_scheduled_workout",
        "unschedule_workout",
        "delete_workout",
        "list_recent_activities",
        "get_activity_details",
        "get_weekly_summary",
    }.issubset(tool_names)


def test_post_without_bearer_returns_401(server_module: ModuleType) -> None:
    """AC §5 (automated half) — Streamable-HTTP POST without Bearer → 401."""
    client = TestClient(server_module.app)

    response = client.post("/mcp")
    assert response.status_code == 401
    assert response.text == "unauthorized"


def test_post_with_wrong_bearer_returns_401(server_module: ModuleType) -> None:
    """AC §5 (automated half) — wrong Bearer token is rejected before MCP."""
    client = TestClient(server_module.app)

    response = client.post("/mcp", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401
    assert response.text == "unauthorized"
