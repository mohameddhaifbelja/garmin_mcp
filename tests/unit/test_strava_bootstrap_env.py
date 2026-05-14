"""Unit tests for the ``.env`` updater helper in ``scripts/strava_bootstrap.py``.

The OAuth flow itself is exercised manually with real Strava credentials (the
ticket's "smoke test" AC). These tests cover the pure file-mutation helper
that persists the refresh token, since it carries the highest risk of
accidental data loss in ``.env``.
"""

from __future__ import annotations

from pathlib import Path

from scripts.strava_bootstrap import REFRESH_TOKEN_KEY, update_env_file


def test_update_env_file_creates_file_when_absent(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"

    update_env_file(REFRESH_TOKEN_KEY, "abc123", env_path=env_path)

    assert env_path.read_text() == "STRAVA_REFRESH_TOKEN=abc123\n"


def test_update_env_file_appends_when_key_missing(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("# header comment\nSTRAVA_CLIENT_ID=42\nSTRAVA_CLIENT_SECRET=shh\n")

    update_env_file(REFRESH_TOKEN_KEY, "new-refresh", env_path=env_path)

    assert env_path.read_text() == (
        "# header comment\n"
        "STRAVA_CLIENT_ID=42\n"
        "STRAVA_CLIENT_SECRET=shh\n"
        "STRAVA_REFRESH_TOKEN=new-refresh\n"
    )


def test_update_env_file_replaces_existing_line_in_place(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "CONNECTOR_BEARER_TOKEN=keep-me\n"
        "STRAVA_REFRESH_TOKEN=old-value\n"
        "USER_TIMEZONE=Africa/Tunis\n"
    )

    update_env_file(REFRESH_TOKEN_KEY, "fresh-value", env_path=env_path)

    assert env_path.read_text() == (
        "CONNECTOR_BEARER_TOKEN=keep-me\n"
        "STRAVA_REFRESH_TOKEN=fresh-value\n"
        "USER_TIMEZONE=Africa/Tunis\n"
    )


def test_update_env_file_preserves_comments_and_blank_lines(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("# top comment\n\nSTRAVA_REFRESH_TOKEN=old\n\n# trailing comment\n")

    update_env_file(REFRESH_TOKEN_KEY, "new", env_path=env_path)

    assert env_path.read_text() == (
        "# top comment\n\nSTRAVA_REFRESH_TOKEN=new\n\n# trailing comment\n"
    )


def test_update_env_file_replaces_only_first_match(tmp_path: Path) -> None:
    # Defensive: if a user has accidentally duplicated the key, we only
    # replace the first occurrence. The duplicate is left as-is for the
    # user to clean up manually rather than silently dropping a line.
    env_path = tmp_path / ".env"
    env_path.write_text("STRAVA_REFRESH_TOKEN=first\nOTHER=x\nSTRAVA_REFRESH_TOKEN=second\n")

    update_env_file(REFRESH_TOKEN_KEY, "fresh", env_path=env_path)

    assert env_path.read_text() == (
        "STRAVA_REFRESH_TOKEN=fresh\nOTHER=x\nSTRAVA_REFRESH_TOKEN=second\n"
    )
