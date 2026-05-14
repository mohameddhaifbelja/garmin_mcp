"""Typed application settings loaded from environment variables and ``.env``.

This module is the single source of truth for runtime configuration. Every
other module in ``src/`` imports values from here instead of reaching for
``os.getenv`` directly (per ``DESIGN.md`` §4, §11, §12).

Import-time requirement
-----------------------
The module instantiates a singleton at import (``settings = Settings()``).
Pydantic's ``BaseSettings`` therefore validates the four required fields the
moment ``src.config`` is imported. In production this is fine — ``.env`` is
populated by the bootstrap scripts before the server starts. In CI / fresh
shells with no env, the import raises ``pydantic.ValidationError``. Unit
tests that exercise ``Settings`` directly should set the required env vars
via ``monkeypatch.setenv`` before importing/reloading this module.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed config sourced from environment variables and ``.env``.

    ``extra="forbid"`` rejects any unrecognized key — this catches typos in
    ``.env`` (e.g. ``STRAVA_CLINT_ID``) at startup rather than at the call
    site that needs the value.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )

    # Required — populated by the operator (`.env`) or bootstrap scripts.
    connector_bearer_token: str
    strava_client_id: str
    strava_client_secret: str
    strava_refresh_token: str

    # Optional with safe defaults — match `DESIGN.md` §4.2 and §12.
    garmin_token_dir: Path = Path.home() / ".garminconnect"
    audit_log_path: Path = Path.home() / ".fitness-mcp" / "logs" / "writes.jsonl"
    user_timezone: str = "Africa/Tunis"


settings = Settings()
"""Module-level singleton; import this rather than calling ``Settings()`` again."""
