"""Unit tests for ``src.config``.

The module declares a top-level ``settings = Settings()`` singleton, so any
import of ``src.config`` requires the four mandatory env vars to be set in
the process environment (or in a ``.env`` file pointed at by the working
directory). These tests deliberately scrub the environment, point pydantic
at a per-test ``.env`` written under ``tmp_path``, and reload the module via
``importlib.reload`` to exercise both the singleton bootstrap and the
``extra="forbid"`` rejection path.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

_REQUIRED_ENV_VARS = (
    "CONNECTOR_BEARER_TOKEN",
    "STRAVA_CLIENT_ID",
    "STRAVA_CLIENT_SECRET",
    "STRAVA_REFRESH_TOKEN",
)

_OPTIONAL_ENV_VARS = (
    "GARMIN_TOKEN_DIR",
    "AUDIT_LOG_PATH",
    "USER_TIMEZONE",
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every env var that ``Settings`` reads, so the test starts clean."""
    for name in (*_REQUIRED_ENV_VARS, *_OPTIONAL_ENV_VARS):
        monkeypatch.delenv(name, raising=False)


def _reload_config_module():
    """Force a fresh import of ``src.config`` so the singleton is reconstructed."""
    if "src.config" in sys.modules:
        return importlib.reload(sys.modules["src.config"])
    return importlib.import_module("src.config")


def test_settings_reads_required_and_default_fields_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """All required fields load from env; defaults apply when optional fields are unset."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # no real .env in CWD

    monkeypatch.setenv("CONNECTOR_BEARER_TOKEN", "bearer-test-token")
    monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", "refresh-token")

    config = _reload_config_module()

    fresh = config.Settings()

    assert fresh.connector_bearer_token == "bearer-test-token"
    assert fresh.strava_client_id == "12345"
    assert fresh.strava_client_secret == "client-secret"
    assert fresh.strava_refresh_token == "refresh-token"
    assert fresh.garmin_token_dir == Path.home() / ".garminconnect"
    assert fresh.audit_log_path == Path.home() / ".fitness-mcp" / "logs" / "writes.jsonl"
    assert fresh.user_timezone == "Africa/Tunis"
    # Module-level singleton bootstrapped successfully.
    assert config.settings.connector_bearer_token == "bearer-test-token"


def test_settings_loads_overrides_from_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``Settings`` reads values from a ``.env`` in the current working directory."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "CONNECTOR_BEARER_TOKEN=from-file\n"
        "STRAVA_CLIENT_ID=999\n"
        "STRAVA_CLIENT_SECRET=secret-from-file\n"
        "STRAVA_REFRESH_TOKEN=refresh-from-file\n"
        "GARMIN_TOKEN_DIR=/tmp/garmin-tokens\n"
        "AUDIT_LOG_PATH=/tmp/audit.jsonl\n"
        "USER_TIMEZONE=Europe/Paris\n"
    )

    config = _reload_config_module()
    fresh = config.Settings()

    assert fresh.connector_bearer_token == "from-file"
    assert fresh.strava_client_id == "999"
    assert fresh.strava_client_secret == "secret-from-file"
    assert fresh.strava_refresh_token == "refresh-from-file"
    assert fresh.garmin_token_dir == Path("/tmp/garmin-tokens")
    assert fresh.audit_log_path == Path("/tmp/audit.jsonl")
    assert fresh.user_timezone == "Europe/Paris"


def test_settings_rejects_unknown_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``extra='forbid'`` causes unknown fields passed at init time to raise."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    monkeypatch.setenv("CONNECTOR_BEARER_TOKEN", "bearer-test-token")
    monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("STRAVA_REFRESH_TOKEN", "refresh-token")

    config = _reload_config_module()

    with pytest.raises(ValidationError) as excinfo:
        config.Settings(unexpected_field="boom")  # type: ignore[call-arg]

    assert "unexpected_field" in str(excinfo.value)


def test_settings_raises_when_required_env_var_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A required field with no env var and no ``.env`` entry raises ``ValidationError``."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    # Provide three of four required fields; leave STRAVA_REFRESH_TOKEN missing.
    monkeypatch.setenv("CONNECTOR_BEARER_TOKEN", "bearer-test-token")
    monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "client-secret")

    # Drop any previously-imported version so the reload sees the cleared env.
    sys.modules.pop("src.config", None)

    with pytest.raises(ValidationError) as excinfo:
        importlib.import_module("src.config")

    assert "strava_refresh_token" in str(excinfo.value).lower()
