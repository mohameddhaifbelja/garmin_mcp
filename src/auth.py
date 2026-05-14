"""Bearer-token authentication middleware for the FastMCP ASGI app.

A single 32-byte token in ``.env`` as ``CONNECTOR_BEARER_TOKEN`` gates every
inbound request. Requests without ``Authorization: Bearer <token>`` matching
``settings.connector_bearer_token`` are rejected with HTTP 401 and body
``"unauthorized"`` (per ``DESIGN.md`` §4.3).

The token is compared with :func:`secrets.compare_digest` so the middleware
does not leak a timing oracle to a remote attacker.

Token injection
---------------
The token is accepted as a constructor argument so unit tests can supply one
without setting up env vars / ``src.config`` settings. In production
(``src.server``) the middleware is added without an explicit token; on first
request the module-level ``settings`` singleton is imported lazily and its
``connector_bearer_token`` is captured.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp

_BEARER_PREFIX = "Bearer "
_UNAUTHORIZED_BODY = "unauthorized"


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests lacking a valid ``Authorization: Bearer <token>`` header.

    Parameters
    ----------
    app:
        The ASGI app this middleware wraps.
    token:
        Expected bearer token. When ``None`` (the production default), the
        token is loaded lazily from ``src.config.settings.connector_bearer_token``
        on construction. Tests pass an explicit value to avoid touching
        ``src.config``.
    """

    def __init__(self, app: ASGIApp, token: str | None = None) -> None:
        super().__init__(app)
        if token is None:
            # Lazy import: ``src.config`` validates required env vars at import
            # time, which we want to defer past test collection.
            from src.config import settings

            token = settings.connector_bearer_token
        self._token = token

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        header = request.headers.get("Authorization")
        if header is None or not header.startswith(_BEARER_PREFIX):
            return _unauthorized()

        provided = header[len(_BEARER_PREFIX) :]
        if not secrets.compare_digest(provided, self._token):
            return _unauthorized()

        return await call_next(request)


def _unauthorized() -> PlainTextResponse:
    return PlainTextResponse(_UNAUTHORIZED_BODY, status_code=401)
