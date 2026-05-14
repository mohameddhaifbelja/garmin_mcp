"""Unit tests for :mod:`src.garmin.client`.

The :class:`src.garmin.client.GarminClient` is a thin pass-through. The tests
therefore care about three things, and only those:

1. **Lazy login.** The wrapper must not instantiate or call ``Garmin`` until
   the first method invocation, and must reuse the cached instance after.
2. **Auth-error wrapping.** Each of the three failure modes from the library
   (auth, connection, missing token file) must surface as
   :class:`src.garmin.client.GarminAuthError` with a hint to re-run the
   bootstrap script.
3. **Method dispatch.** Every public method must forward its arguments
   verbatim to the underlying ``Garmin`` call, and must emit a structured
   ``garmin.call`` log line whose ``method`` field matches the wrapper name.

A ``_StubGarmin`` class plays the role of ``garminconnect.Garmin`` — we
patch the symbol inside ``src.garmin.client`` rather than the upstream
module so any indirect imports still see the real class. Tests construct
:class:`GarminClient` with an explicit ``token_dir`` so they never touch
:mod:`src.config` (which would otherwise require four env vars at import
time).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

from src.garmin import client as client_module
from src.garmin.client import GarminAuthError, GarminClient


class _StubGarmin:
    """Stand-in for :class:`garminconnect.Garmin`.

    Records every call onto the per-instance ``calls`` list so tests can
    assert dispatch ordering and arguments. The ``login`` hook can be
    overridden by setting :attr:`login_side_effect` on the class before the
    constructor runs — that's how the auth-failure tests inject the three
    raise-this-instead exceptions.
    """

    login_side_effect: BaseException | None = None
    instances: list[_StubGarmin] = []

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.login_count = 0
        self.return_values: dict[str, Any] = {}
        _StubGarmin.instances.append(self)

    def login(self, tokenstore: str | None = None) -> tuple[str | None, str | None]:
        self.login_count += 1
        self.calls.append(("login", (), {"tokenstore": tokenstore}))
        if _StubGarmin.login_side_effect is not None:
            raise _StubGarmin.login_side_effect
        return ("oauth1", "oauth2")

    # --- Pass-through targets ----------------------------------------------
    def _record(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        self.calls.append((name, args, kwargs))
        return self.return_values.get(name, {"ok": name})

    def upload_running_workout(self, running_workout: Any) -> dict[str, Any]:
        return self._record("upload_running_workout", (running_workout,), {})

    def schedule_workout(self, workout_id: int | str, date_str: str) -> dict[str, Any]:
        return self._record("schedule_workout", (workout_id, date_str), {})

    def unschedule_workout(self, scheduled_workout_id: int | str) -> Any:
        return self._record("unschedule_workout", (scheduled_workout_id,), {})

    def delete_workout(self, workout_id: int | str) -> Any:
        return self._record("delete_workout", (workout_id,), {})

    def get_workouts(self, start: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self._record("get_workouts", (), {"start": start, "limit": limit})

    def get_workout_by_id(self, workout_id: int | str) -> dict[str, Any]:
        return self._record("get_workout_by_id", (workout_id,), {})

    def get_scheduled_workouts(self, year: int | str, month: int | str) -> dict[str, Any]:
        return self._record("get_scheduled_workouts", (), {"year": year, "month": month})

    def get_scheduled_workout_by_id(self, scheduled_workout_id: int | str) -> dict[str, Any]:
        return self._record("get_scheduled_workout_by_id", (scheduled_workout_id,), {})


@pytest.fixture
def stub_garmin(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_StubGarmin]]:
    """Replace ``Garmin`` inside :mod:`src.garmin.client` with :class:`_StubGarmin`.

    Resets per-test state (instance list + login side effect) so the stub
    starts clean on every test.
    """
    _StubGarmin.instances = []
    _StubGarmin.login_side_effect = None
    monkeypatch.setattr(client_module, "Garmin", _StubGarmin)
    yield _StubGarmin
    _StubGarmin.instances = []
    _StubGarmin.login_side_effect = None


@pytest.fixture
def captured_logs() -> Iterator[list[dict[str, Any]]]:
    """Capture every ``structlog`` event into a list of dicts.

    Configures structlog with a single in-memory processor for the duration
    of the test; restores the previous config on teardown.
    """
    events: list[dict[str, Any]] = []

    def _capture(_logger: Any, _method: str, event_dict: dict[str, Any]) -> str:
        """Record the event then short-circuit the chain to a plain string.

        The final processor in a structlog chain must produce something the
        wrapped stdlib/print logger can format. Returning a string covers
        the common ``PrintLogger.msg(str)`` signature without forcing the
        test config to mirror the project's full processor list.
        """
        events.append(dict(event_dict))
        return event_dict.get("event", "")

    original_config = structlog.get_config()
    structlog.configure(processors=[_capture])
    try:
        yield events
    finally:
        structlog.configure(**original_config)


# --- Lazy login --------------------------------------------------------------


def test_constructor_does_not_log_in(stub_garmin: type[_StubGarmin], tmp_path: Path) -> None:
    """Building a :class:`GarminClient` must not instantiate ``Garmin`` yet."""
    GarminClient(token_dir=tmp_path)

    assert stub_garmin.instances == []


def test_first_method_call_triggers_login(stub_garmin: type[_StubGarmin], tmp_path: Path) -> None:
    """The first wrapper call instantiates ``Garmin`` and calls ``login`` exactly once."""
    client = GarminClient(token_dir=tmp_path)

    client.get_workouts(start=0, limit=5)

    assert len(stub_garmin.instances) == 1
    stub = stub_garmin.instances[0]
    assert stub.login_count == 1
    # Login is invoked with the configured token directory as a string.
    login_call = next(call for call in stub.calls if call[0] == "login")
    assert login_call[2] == {"tokenstore": str(tmp_path)}


def test_subsequent_calls_reuse_cached_login(
    stub_garmin: type[_StubGarmin], tmp_path: Path
) -> None:
    """Multiple wrapper calls share the same underlying ``Garmin`` (no re-login)."""
    client = GarminClient(token_dir=tmp_path)

    client.get_workouts()
    client.get_workout_by_id(workout_id=42)
    client.delete_workout(workout_id=42)

    assert len(stub_garmin.instances) == 1
    assert stub_garmin.instances[0].login_count == 1


# --- Auth-error wrapping -----------------------------------------------------


@pytest.mark.parametrize(
    ("upstream_exc", "expected_fragment"),
    [
        (
            GarminConnectAuthenticationError("bad creds"),
            "Garmin authentication failed",
        ),
        (
            GarminConnectConnectionError("network down"),
            "connection error",
        ),
        (
            FileNotFoundError("no tokens"),
            "No Garmin tokens found",
        ),
    ],
)
def test_login_failures_wrap_in_garmin_auth_error(
    stub_garmin: type[_StubGarmin],
    tmp_path: Path,
    upstream_exc: BaseException,
    expected_fragment: str,
) -> None:
    """Each known auth/connection failure surfaces as ``GarminAuthError`` with a hint."""
    stub_garmin.login_side_effect = upstream_exc
    client = GarminClient(token_dir=tmp_path)

    with pytest.raises(GarminAuthError) as excinfo:
        client.get_workouts()

    message = str(excinfo.value)
    assert expected_fragment in message
    assert "garmin_bootstrap" in message
    # The original exception is preserved on the chain for debugging.
    assert excinfo.value.__cause__ is upstream_exc


def test_auth_error_keeps_client_uncached_for_retry(
    stub_garmin: type[_StubGarmin], tmp_path: Path
) -> None:
    """A failed login does not poison the cache — a subsequent call may retry login."""
    stub_garmin.login_side_effect = GarminConnectAuthenticationError("bad creds")
    client = GarminClient(token_dir=tmp_path)

    with pytest.raises(GarminAuthError):
        client.get_workouts()

    # Clear the failure and try again. A fresh Garmin instance should be created
    # and login should run a second time.
    stub_garmin.login_side_effect = None
    client.get_workouts()

    assert len(stub_garmin.instances) == 2


# --- Method dispatch ---------------------------------------------------------


def _make_dispatch_cases() -> list[
    tuple[str, Callable[[GarminClient], Any], str, tuple[Any, ...], dict[str, Any]]
]:
    """Build the dispatch parametrisation table.

    Each row: ``(method_name, invoke_fn, expected_call_name, expected_args, expected_kwargs)``.
    The ``invoke_fn`` calls the wrapper method and returns its result; the
    last three elements describe what the stub ``Garmin`` must observe.
    """
    return [
        (
            "upload_running_workout",
            lambda c: c.upload_running_workout({"workoutName": "easy"}),
            "upload_running_workout",
            ({"workoutName": "easy"},),
            {},
        ),
        (
            "schedule_workout",
            lambda c: c.schedule_workout(101, "2026-07-13"),
            "schedule_workout",
            (101, "2026-07-13"),
            {},
        ),
        (
            "unschedule_workout",
            lambda c: c.unschedule_workout(202),
            "unschedule_workout",
            (202,),
            {},
        ),
        (
            "delete_workout",
            lambda c: c.delete_workout(303),
            "delete_workout",
            (303,),
            {},
        ),
        (
            "get_workouts",
            lambda c: c.get_workouts(start=5, limit=25),
            "get_workouts",
            (),
            {"start": 5, "limit": 25},
        ),
        (
            "get_workout_by_id",
            lambda c: c.get_workout_by_id(404),
            "get_workout_by_id",
            (404,),
            {},
        ),
        (
            "get_scheduled_workouts",
            lambda c: c.get_scheduled_workouts(2026, 7),
            "get_scheduled_workouts",
            (),
            {"year": 2026, "month": 7},
        ),
        (
            "get_scheduled_workout_by_id",
            lambda c: c.get_scheduled_workout_by_id(505),
            "get_scheduled_workout_by_id",
            (505,),
            {},
        ),
    ]


@pytest.mark.parametrize(
    ("method_name", "invoke", "expected_call", "expected_args", "expected_kwargs"),
    _make_dispatch_cases(),
)
def test_method_dispatches_to_underlying_client(
    stub_garmin: type[_StubGarmin],
    tmp_path: Path,
    method_name: str,
    invoke: Callable[[GarminClient], Any],
    expected_call: str,
    expected_args: tuple[Any, ...],
    expected_kwargs: dict[str, Any],
) -> None:
    """Every wrapper method forwards its arguments to the matching ``Garmin`` method."""
    del method_name  # parametrisation key; the assertion uses ``expected_call`` instead.
    client = GarminClient(token_dir=tmp_path)

    result = invoke(client)

    assert len(stub_garmin.instances) == 1
    stub = stub_garmin.instances[0]
    # The first recorded call is always ``login``; the second is the method call.
    method_calls = [call for call in stub.calls if call[0] != "login"]
    assert len(method_calls) == 1
    name, args, kwargs = method_calls[0]
    assert name == expected_call
    assert args == expected_args
    assert kwargs == expected_kwargs
    # The stub's default response shape is ``{"ok": <method_name>}``; the
    # wrapper must surface it verbatim to confirm there's no result mangling.
    assert result == {"ok": expected_call}


# --- Structured logging ------------------------------------------------------


def test_method_emits_structured_log_line(
    stub_garmin: type[_StubGarmin],
    tmp_path: Path,
    captured_logs: list[dict[str, Any]],
) -> None:
    """Every wrapper call writes a ``garmin.call`` log event with method + args."""
    client = GarminClient(token_dir=tmp_path)

    client.schedule_workout(7, "2026-07-13")

    call_events = [evt for evt in captured_logs if evt.get("event") == "garmin.call"]
    assert len(call_events) == 1
    payload = call_events[0]
    assert payload["method"] == "schedule_workout"
    assert payload["args"] == {"workout_id": "7", "date_iso": "'2026-07-13'"}


def test_long_arguments_are_truncated_in_logs(
    stub_garmin: type[_StubGarmin],
    tmp_path: Path,
    captured_logs: list[dict[str, Any]],
) -> None:
    """Argument reprs over 100 chars are clipped to ``value[:100] + '...'``."""
    client = GarminClient(token_dir=tmp_path)
    long_description = "x" * 500
    payload = {"workoutName": "Long", "description": long_description}

    client.upload_running_workout(payload)

    call_events = [evt for evt in captured_logs if evt.get("event") == "garmin.call"]
    assert len(call_events) == 1
    logged_arg = call_events[0]["args"]["running_workout"]
    assert logged_arg.endswith("...")
    assert len(logged_arg) <= 103  # 100 chars + "..."


def test_login_emits_start_and_ok_events(
    stub_garmin: type[_StubGarmin],
    tmp_path: Path,
    captured_logs: list[dict[str, Any]],
) -> None:
    """Successful login emits a ``garmin.login.start`` then ``garmin.login.ok`` event."""
    client = GarminClient(token_dir=tmp_path)

    client.get_workouts()

    event_names = [evt.get("event") for evt in captured_logs]
    start_idx = event_names.index("garmin.login.start")
    ok_idx = event_names.index("garmin.login.ok")
    assert start_idx < ok_idx
    assert captured_logs[start_idx]["token_dir"] == str(tmp_path)
    assert captured_logs[ok_idx]["token_dir"] == str(tmp_path)


# --- Token-dir resolution ----------------------------------------------------


def test_token_dir_falls_back_to_settings(
    stub_garmin: type[_StubGarmin], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Omitting ``token_dir`` defers resolution to ``src.config.settings``.

    The ``src.config`` import is patched in-place with a tiny stand-in so the
    test does not need to set the four mandatory env vars (mirroring the
    same isolation trick used by ``tests/unit/test_audit.py``).
    """

    class _FakeSettings:
        garmin_token_dir = tmp_path / "fake-tokens"

    class _FakeConfigModule:
        settings = _FakeSettings()

    import sys

    monkeypatch.setitem(sys.modules, "src.config", _FakeConfigModule)

    client = GarminClient()

    assert client.token_dir == tmp_path / "fake-tokens"

    client.get_workouts()
    stub = stub_garmin.instances[0]
    login_call = next(call for call in stub.calls if call[0] == "login")
    assert login_call[2] == {"tokenstore": str(tmp_path / "fake-tokens")}
