"""Unit tests for :mod:`src.strava.tools`.

The tools are thin pass-throughs to :class:`src.strava.client.StravaClient`
(exhaustively tested in T16). These tests therefore focus on the things the
tools layer actually owns:

* the three tool functions forward kwargs verbatim to the right client method,
* the published default-arg values match the documented contract
  (``limit=30``, ``weeks_back=8``),
* :func:`register` wires exactly the three Strava tools onto a fresh
  :class:`mcp.server.fastmcp.FastMCP`,
* importing :mod:`src.server` (with required env seeded) yields a server that
  carries both the Phase 1/2 Garmin tools and the new Strava tools.

The seam is :class:`StravaClient` itself: tests monkeypatch the module-level
``_client`` singleton with a :class:`types.SimpleNamespace` exposing the
methods the tools call. The wrapper never reaches its real OAuth path.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from src.strava import tools as strava_tools
from src.strava.tools import (
    get_activity_details,
    get_weekly_summary,
    list_recent_activities,
    register,
)

# --- Fixtures ----------------------------------------------------------------


@pytest.fixture
def stub_client(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace the module-level :class:`StravaClient` singleton with a stub.

    The stub exposes ``recent_activities``, ``activity_details`` and
    ``weekly_summary`` as attributes the tests assign per-case. Any other
    attribute access raises ``AttributeError`` — which is the loud failure
    mode we want if a tool reaches for the wrong method.
    """
    stub = SimpleNamespace()
    monkeypatch.setattr(strava_tools, "_client", stub)
    return stub


# --- list_recent_activities ------------------------------------------------


def test_list_recent_activities_forwards_kwargs(stub_client: SimpleNamespace) -> None:
    """All three named args are forwarded verbatim to the client."""
    captured: dict[str, Any] = {}
    payload = [{"id": 1, "name": "Easy"}]

    def _stub(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return payload

    stub_client.recent_activities = _stub

    out = list_recent_activities(limit=5, after_iso="2026-05-01", sport_filter="Run")

    assert out == payload
    assert captured == {"limit": 5, "after_iso": "2026-05-01", "sport_filter": "Run"}


def test_list_recent_activities_uses_default_args(stub_client: SimpleNamespace) -> None:
    """Default args: ``limit=30``, ``after_iso=None``, ``sport_filter=None``."""
    captured: dict[str, Any] = {}

    def _stub(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return []

    stub_client.recent_activities = _stub

    list_recent_activities()

    assert captured == {"limit": 30, "after_iso": None, "sport_filter": None}


def test_list_recent_activities_signature_documents_defaults() -> None:
    """Pin the published default values in the function signature itself.

    Claude's tool-schema generator reads defaults from the function signature,
    so the contract lives in the signature, not just the body.
    """
    sig = inspect.signature(list_recent_activities)
    assert sig.parameters["limit"].default == 30
    assert sig.parameters["after_iso"].default is None
    assert sig.parameters["sport_filter"].default is None


# --- get_activity_details --------------------------------------------------


def test_get_activity_details_forwards_id(stub_client: SimpleNamespace) -> None:
    """The activity id is forwarded positionally to the client method."""
    captured: list[int] = []
    payload = {"id": 42, "name": "Trail long run", "sport_type": "TrailRun"}

    def _stub(activity_id: int) -> dict[str, Any]:
        captured.append(activity_id)
        return payload

    stub_client.activity_details = _stub

    out = get_activity_details(42)

    assert out == payload
    assert captured == [42]


# --- get_weekly_summary ----------------------------------------------------


def test_get_weekly_summary_forwards_weeks_back(stub_client: SimpleNamespace) -> None:
    """The ``weeks_back`` value is forwarded positionally to the client."""
    captured: list[int] = []
    payload = [{"week_start": "2026-05-04", "runs": 3}]

    def _stub(weeks_back: int) -> list[dict[str, Any]]:
        captured.append(weeks_back)
        return payload

    stub_client.weekly_summary = _stub

    out = get_weekly_summary(weeks_back=4)

    assert out == payload
    assert captured == [4]


def test_get_weekly_summary_uses_default_weeks_back(stub_client: SimpleNamespace) -> None:
    """Default ``weeks_back=8`` per the docstring contract."""
    captured: list[int] = []

    def _stub(weeks_back: int) -> list[dict[str, Any]]:
        captured.append(weeks_back)
        return []

    stub_client.weekly_summary = _stub

    get_weekly_summary()

    assert captured == [8]


def test_get_weekly_summary_signature_documents_default() -> None:
    """Pin the published default in the function signature."""
    sig = inspect.signature(get_weekly_summary)
    assert sig.parameters["weeks_back"].default == 8


# --- _get_client lazy singleton --------------------------------------------


def test_get_client_caches_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_get_client`` constructs the client once and caches it across calls."""
    monkeypatch.setattr(strava_tools, "_client", None)
    constructor_calls: list[int] = []

    class _Marker:
        def __init__(self) -> None:
            constructor_calls.append(1)

    monkeypatch.setattr(strava_tools, "StravaClient", _Marker)

    first = strava_tools._get_client()
    second = strava_tools._get_client()

    assert first is second
    assert len(constructor_calls) == 1


# --- register --------------------------------------------------------------


def test_register_adds_three_tools_to_fastmcp() -> None:
    """:func:`register` wires exactly the three Strava tools onto a FastMCP."""
    mcp = FastMCP(name="test")
    register(mcp)

    tool_names = set(mcp._tool_manager._tools.keys())
    expected = {"list_recent_activities", "get_activity_details", "get_weekly_summary"}
    assert expected.issubset(tool_names)


# --- Tool docstrings are factual (no behavioural rules) --------------------


@pytest.mark.parametrize(
    "tool",
    [list_recent_activities, get_activity_details, get_weekly_summary],
)
def test_tool_docstring_does_not_contain_behavioral_rules(tool: Any) -> None:
    """Per DESIGN.md §6 / §10, tool docstrings stay factual.

    Behavioural orchestration (when to call what, conflict detection, target
    policy, week-by-week ingest) lives in the server-level ``instructions``
    string, not in per-tool docstrings.
    """
    doc = tool.__doc__ or ""
    forbidden_phrases = (
        "ON NEW PLAN INGEST",
        "TARGET POLICY",
        "CONFLICT DETECTION",
        "MODIFICATIONS:",
        "before each ",
        "ask the user",
        "wait for user confirmation",
        "stop and tell",
    )
    lowered = doc.lower()
    for phrase in forbidden_phrases:
        assert phrase.lower() not in lowered, (
            f"Tool docstring for {tool.__name__} contains behavioural rule {phrase!r}"
        )


# --- Server-level wiring (end-to-end import) -------------------------------


def _seed_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populate the env vars :class:`src.config.Settings` requires."""
    monkeypatch.setenv("CONNECTOR_BEARER_TOKEN", "test-bearer-token")
    monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", "refresh-token")


def _fresh_server_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Force a clean re-import of :mod:`src.server` and :mod:`src.config`.

    Mirrors the pattern in ``tests/unit/test_server.py``: ``src.config``
    caches a module-level ``settings`` singleton and ``src.server`` captures
    the bearer token at construction time, so a reload of one without the
    other would leak state across tests.
    """
    _seed_required_env(monkeypatch)
    for name in ("src.server", "src.config"):
        sys.modules.pop(name, None)
    return importlib.import_module("src.server")


def test_server_module_registers_strava_tools_alongside_garmin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reloaded ``src.server`` carries both Garmin and Strava tools."""
    server_module = _fresh_server_module(monkeypatch)

    tool_names = set(server_module.mcp._tool_manager._tools.keys())

    # Phase 1 Garmin tools (T11).
    assert {"create_and_schedule", "list_scheduled_workouts"}.issubset(tool_names)
    # Phase 2 modification subset (T14).
    assert {
        "get_scheduled_workout",
        "replace_scheduled_workout",
        "unschedule_workout",
        "delete_workout",
    }.issubset(tool_names)
    # Phase 3 Strava tools (this ticket).
    assert {
        "list_recent_activities",
        "get_activity_details",
        "get_weekly_summary",
    }.issubset(tool_names)
