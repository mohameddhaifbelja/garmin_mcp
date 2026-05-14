"""One-time interactive Strava OAuth bootstrap.

Runs the Strava Authorization Code flow with a localhost redirect listener on
``http://localhost:8080/exchange``. Required scopes are ``read`` and
``activity:read_all`` (per ``DESIGN.md`` §4.1 / §6.1).

On success the refresh token is persisted to ``.env`` (existing
``STRAVA_REFRESH_TOKEN=`` line is replaced; other lines are preserved). The
script then prints the authenticated athlete's name.

Run with::

    uv run python -m scripts.strava_bootstrap

Prerequisites:
    - ``STRAVA_CLIENT_ID`` and ``STRAVA_CLIENT_SECRET`` set in the environment
      or in ``.env`` (read by ``python-dotenv`` if installed, otherwise this
      script parses ``.env`` directly).
    - Your Strava API application's "Authorization Callback Domain" must be
      ``localhost``.
"""

from __future__ import annotations

import http.server
import os
import socket
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import cast

from stravalib import Client
from stravalib.exc import AuthError, Fault

DEFAULT_REDIRECT_HOST = "localhost"
DEFAULT_REDIRECT_PORT = 8080
DEFAULT_REDIRECT_PATH = "/exchange"
REDIRECT_URI = f"http://{DEFAULT_REDIRECT_HOST}:{DEFAULT_REDIRECT_PORT}{DEFAULT_REDIRECT_PATH}"
REQUIRED_SCOPES = ["read", "activity:read_all"]

DEFAULT_ENV_PATH = Path(".env")
REFRESH_TOKEN_KEY = "STRAVA_REFRESH_TOKEN"

SUCCESS_HTML = b"""<!doctype html>
<html><head><meta charset='utf-8'><title>Strava bootstrap</title></head>
<body style='font-family: system-ui; padding: 2rem;'>
<h1>Strava authorization received.</h1>
<p>You can close this tab and return to the terminal.</p>
</body></html>"""

ERROR_HTML = b"""<!doctype html>
<html><head><meta charset='utf-8'><title>Strava bootstrap error</title></head>
<body style='font-family: system-ui; padding: 2rem;'>
<h1>Authorization failed.</h1>
<p>See the bootstrap script's terminal output for details.</p>
</body></html>"""


class _OAuthCallbackError(RuntimeError):
    """Raised when the user denies the OAuth grant or Strava returns an error."""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot HTTP handler that captures the OAuth ``code`` query param."""

    # Attributes are set on the server instance below.
    server: _CallbackServer

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != DEFAULT_REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        error = params.get("error", [None])[0]
        if error:
            self.server.oauth_error = error
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(ERROR_HTML)
            return

        code = params.get("code", [None])[0]
        scope = params.get("scope", [""])[0]
        if not code:
            self.server.oauth_error = "missing 'code' query parameter"
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(ERROR_HTML)
            return

        self.server.oauth_code = code
        self.server.oauth_scope = scope
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(SUCCESS_HTML)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence the default per-request stderr logging."""
        return


class _CallbackServer(http.server.HTTPServer):
    """Single-request HTTP server that holds the captured OAuth code."""

    oauth_code: str | None = None
    oauth_scope: str | None = None
    oauth_error: str | None = None


def load_dotenv_into_env(env_path: Path = DEFAULT_ENV_PATH) -> None:
    """Read ``KEY=value`` lines from ``env_path`` into ``os.environ``.

    Existing environment variables are *not* overwritten; this matches the
    behavior most ``.env`` loaders default to. Lines that are blank, comments,
    or malformed are silently skipped.
    """
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def update_env_file(
    key: str,
    value: str,
    env_path: Path = DEFAULT_ENV_PATH,
) -> None:
    """Write ``KEY=value`` into ``env_path``, replacing any prior line for ``key``.

    Creates the file if it doesn't exist. Preserves all other lines verbatim
    (including comments and blank lines). The final file always ends with a
    trailing newline.
    """
    new_line = f"{key}={value}"
    if not env_path.exists():
        env_path.write_text(new_line + "\n")
        return

    existing = env_path.read_text().splitlines()
    replaced = False
    out_lines: list[str] = []
    for line in existing:
        if not replaced and line.lstrip().startswith(f"{key}="):
            out_lines.append(new_line)
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        out_lines.append(new_line)

    env_path.write_text("\n".join(out_lines) + "\n")


def _port_available(host: str, port: int) -> bool:
    """Return True iff we can bind a TCP socket on ``(host, port)`` right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def wait_for_callback(
    host: str = DEFAULT_REDIRECT_HOST,
    port: int = DEFAULT_REDIRECT_PORT,
) -> str:
    """Block until Strava redirects to the listener, then return the OAuth code.

    Raises:
        _OAuthCallbackError: the redirect carried an ``error`` parameter or
            no ``code`` parameter (e.g. user denied the grant).
    """
    # Bind to ``host`` (typically "localhost") so we don't accept connections
    # from the rest of the network during the brief OAuth dance.
    server = _CallbackServer((host, port), _CallbackHandler)
    try:
        # Handle requests until we observe either a code or an error. Strava
        # sometimes fires a favicon request alongside the redirect, so we keep
        # looping until the OAuth state on the server is decided.
        while server.oauth_code is None and server.oauth_error is None:
            server.handle_request()
    finally:
        server.server_close()

    if server.oauth_error:
        raise _OAuthCallbackError(server.oauth_error)
    # mypy/ty: we just asserted code is not None via the loop condition.
    return cast(str, server.oauth_code)


def resolve_credentials() -> tuple[int, str]:
    """Return ``(client_id, client_secret)`` from env vars; raise if missing."""
    raw_id = os.getenv("STRAVA_CLIENT_ID", "").strip()
    secret = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
    if not raw_id or not secret:
        raise RuntimeError(
            "STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set in the environment "
            "or in .env before running this script."
        )
    try:
        client_id = int(raw_id)
    except ValueError as exc:
        raise RuntimeError(f"STRAVA_CLIENT_ID must be an integer; got {raw_id!r}.") from exc
    return client_id, secret


def _format_athlete_name(athlete: object) -> str:
    """Best-effort full name from a ``SummaryAthlete``-like object."""
    first = getattr(athlete, "firstname", None) or ""
    last = getattr(athlete, "lastname", None) or ""
    full = f"{first} {last}".strip()
    return full or "<unknown athlete>"


def run_oauth_dance(client_id: int, client_secret: str) -> tuple[str, str]:
    """Run the full Authorization Code flow; return ``(refresh_token, athlete_name)``."""
    if not _port_available(DEFAULT_REDIRECT_HOST, DEFAULT_REDIRECT_PORT):
        raise RuntimeError(
            f"Port {DEFAULT_REDIRECT_PORT} on {DEFAULT_REDIRECT_HOST} is already in use; "
            "stop the process holding it before re-running this script."
        )

    client = Client()
    authorize_url = client.authorization_url(
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        scope=REQUIRED_SCOPES,
        approval_prompt="auto",
    )

    print("Open this URL in your browser to authorize the app:")
    print(f"  {authorize_url}")
    # webbrowser.open returns False on headless boxes; that's fine.
    webbrowser.open(authorize_url)

    print(f"Waiting for Strava to redirect to {REDIRECT_URI} ...")
    code = wait_for_callback()

    result = client.exchange_code_for_token(
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        return_athlete=True,
    )
    # With ``return_athlete=True`` stravalib returns ``(AccessInfo, SummaryAthlete)``.
    if isinstance(result, tuple):
        access_info, athlete = result
    else:  # pragma: no cover — defensive; stravalib >=2.0 returns a tuple here.
        access_info, athlete = result, None

    refresh_token = access_info["refresh_token"]
    athlete_name = _format_athlete_name(athlete) if athlete is not None else "<unknown athlete>"
    return refresh_token, athlete_name


def main() -> int:
    """CLI entrypoint. Returns process exit code."""
    load_dotenv_into_env()
    try:
        client_id, client_secret = resolve_credentials()
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        refresh_token, athlete_name = run_oauth_dance(client_id, client_secret)
    except _OAuthCallbackError as exc:
        print(f"Strava authorization failed: {exc}", file=sys.stderr)
        return 2
    except AuthError as exc:
        print(f"Strava rejected the token exchange: {exc}", file=sys.stderr)
        return 3
    except Fault as exc:
        print(f"Strava API fault during token exchange: {exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        # Covers socket bind failures and network errors raised through stravalib.
        print(f"Network/transport error: {exc}", file=sys.stderr)
        return 5
    except KeyboardInterrupt:
        print("\nAborted by user.", file=sys.stderr)
        return 130

    update_env_file(REFRESH_TOKEN_KEY, refresh_token)
    print(f"Success. Authorized athlete: {athlete_name}.")
    print(f"Wrote {REFRESH_TOKEN_KEY} to {DEFAULT_ENV_PATH.resolve()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
