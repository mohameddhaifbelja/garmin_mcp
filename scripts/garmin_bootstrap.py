"""One-time interactive Garmin Connect SSO bootstrap.

Prompts for email + password, handles MFA via the ``prompt_mfa`` callback,
and persists DI tokens to ``$GARMIN_TOKEN_DIR/garmin_tokens.json`` (default
``~/.garminconnect/garmin_tokens.json``) with mode ``0600``.

Subsequent runtime code uses ``Garmin().login(tokenstore=...)`` to refresh
those tokens automatically; this script is only for first login or a
post-revocation re-bootstrap.

Run with::

    uv run python -m scripts.garmin_bootstrap
"""

from __future__ import annotations

import os
import sys
from getpass import getpass
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

DEFAULT_TOKEN_DIR = Path.home() / ".garminconnect"
TOKEN_FILENAME = "garmin_tokens.json"


def resolve_token_dir() -> Path:
    """Return the directory tokens should be written to.

    Honors ``GARMIN_TOKEN_DIR`` env var; falls back to ``~/.garminconnect``.
    """
    raw = os.getenv("GARMIN_TOKEN_DIR")
    return Path(raw).expanduser() if raw else DEFAULT_TOKEN_DIR


def prompt_credentials() -> tuple[str, str]:
    """Interactively prompt for Garmin email + password."""
    email = input("Garmin email: ").strip()
    password = getpass("Garmin password: ")
    return email, password


def prompt_mfa() -> str:
    """Prompt for a one-time MFA code when Garmin requests it."""
    return input("Garmin MFA code: ").strip()


def secure_token_file(token_path: Path) -> None:
    """Restrict the persisted token file to owner read/write only."""
    token_path.chmod(0o600)


def bootstrap(token_dir: Path) -> Path:
    """Run the SSO bootstrap and return the absolute path to the token file.

    Raises:
        GarminConnectAuthenticationError: bad credentials or 401 from Garmin.
        GarminConnectTooManyRequestsError: rate-limited.
        GarminConnectConnectionError: network or transport failure.
    """
    token_dir.mkdir(parents=True, exist_ok=True)
    email, password = prompt_credentials()

    client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    # ``Garmin.login`` writes ``<token_dir>/garmin_tokens.json`` via
    # ``self.client.dump(tokenstore_path)`` after a fresh credential login.
    client.login(tokenstore=str(token_dir))

    token_path = token_dir / TOKEN_FILENAME
    if not token_path.exists():
        raise GarminConnectAuthenticationError(
            f"Login appeared to succeed but no token file was written at {token_path}"
        )
    secure_token_file(token_path)
    return token_path


def main() -> int:
    """CLI entrypoint. Returns process exit code."""
    token_dir = resolve_token_dir()
    print(f"Writing Garmin tokens to: {token_dir}")
    try:
        token_path = bootstrap(token_dir)
    except GarminConnectAuthenticationError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 1
    except GarminConnectTooManyRequestsError as exc:
        print(f"Rate-limited by Garmin: {exc}", file=sys.stderr)
        return 2
    except GarminConnectConnectionError as exc:
        print(f"Connection error talking to Garmin: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\nAborted by user.", file=sys.stderr)
        return 130

    print(f"Success. Tokens written to {token_path} (mode 0600).")
    print("Runtime code can now call Garmin().login(tokenstore=<dir>) without prompting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
