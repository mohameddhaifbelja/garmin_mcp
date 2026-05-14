"""Unit tests for ``scripts.garmin_bootstrap``.

The real Garmin SSO flow is interactive and network-dependent; these tests
mock the ``Garmin`` client and the ``input``/``getpass`` prompts to exercise
the script's I/O, file-mode, and error-handling logic in isolation.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from scripts import garmin_bootstrap


class _FakeGarmin:
    """Stand-in for ``garminconnect.Garmin`` that writes a token file on login."""

    last_instance: _FakeGarmin | None = None

    def __init__(
        self,
        *,
        email: str,
        password: str,
        prompt_mfa: Any = None,
    ) -> None:
        self.email = email
        self.password = password
        self.prompt_mfa = prompt_mfa
        self.login_called_with: str | None = None
        _FakeGarmin.last_instance = self

    def login(self, tokenstore: str) -> tuple[None, None]:
        self.login_called_with = tokenstore
        # Emulate the real client.dump() side effect.
        token_path = Path(tokenstore) / garmin_bootstrap.TOKEN_FILENAME
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text('{"fake":"tokens"}')
        return None, None


def _patch_prompts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    email: str = "runner@example.com",
    password: str = "hunter2",
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": email)
    monkeypatch.setattr(garmin_bootstrap, "getpass", lambda _prompt="": password)


def test_resolve_token_dir_uses_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "custom"))
    assert garmin_bootstrap.resolve_token_dir() == tmp_path / "custom"


def test_resolve_token_dir_defaults_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GARMIN_TOKEN_DIR", raising=False)
    assert garmin_bootstrap.resolve_token_dir() == Path.home() / ".garminconnect"


def test_bootstrap_writes_token_file_with_mode_0600(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_prompts(monkeypatch)
    monkeypatch.setattr(garmin_bootstrap, "Garmin", _FakeGarmin)

    token_path = garmin_bootstrap.bootstrap(tmp_path)

    assert token_path == tmp_path / garmin_bootstrap.TOKEN_FILENAME
    assert token_path.exists()
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"

    fake = _FakeGarmin.last_instance
    assert fake is not None
    assert fake.email == "runner@example.com"
    assert fake.password == "hunter2"
    assert fake.prompt_mfa is garmin_bootstrap.prompt_mfa
    assert fake.login_called_with == str(tmp_path)


def test_bootstrap_raises_if_login_did_not_write_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_prompts(monkeypatch)

    class _SilentGarmin(_FakeGarmin):
        def login(self, tokenstore: str) -> tuple[None, None]:
            self.login_called_with = tokenstore
            return None, None  # no file written

    monkeypatch.setattr(garmin_bootstrap, "Garmin", _SilentGarmin)

    with pytest.raises(GarminConnectAuthenticationError):
        garmin_bootstrap.bootstrap(tmp_path)


@pytest.mark.parametrize(
    "exc_cls, expected_exit",
    [
        (GarminConnectAuthenticationError, 1),
        (GarminConnectTooManyRequestsError, 2),
        (GarminConnectConnectionError, 3),
    ],
)
def test_main_returns_nonzero_on_known_garmin_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exc_cls: type[Exception],
    expected_exit: int,
) -> None:
    _patch_prompts(monkeypatch)
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path))

    class _ExplodingGarmin(_FakeGarmin):
        def login(self, tokenstore: str) -> tuple[None, None]:
            raise exc_cls("boom")

    monkeypatch.setattr(garmin_bootstrap, "Garmin", _ExplodingGarmin)

    assert garmin_bootstrap.main() == expected_exit


def test_main_happy_path_returns_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_prompts(monkeypatch)
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path))
    monkeypatch.setattr(garmin_bootstrap, "Garmin", _FakeGarmin)

    assert garmin_bootstrap.main() == 0
    assert (tmp_path / garmin_bootstrap.TOKEN_FILENAME).exists()


def test_main_returns_130_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path))

    def _raise(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", _raise)

    assert garmin_bootstrap.main() == 130
