"""Unit tests for ``src.auth.BearerAuthMiddleware``.

The middleware is exercised against a tiny Starlette app via
:class:`starlette.testclient.TestClient`. Tokens are injected directly into
``BearerAuthMiddleware`` (the ``token=`` ctor arg) so these tests do not need
``src.config`` settings or any env vars — see ``DESIGN.md`` §4.3.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.auth import BearerAuthMiddleware

_TOKEN = "s3cret-test-token"


def _ok(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@pytest.fixture
def client() -> TestClient:
    """Tiny Starlette app guarded by ``BearerAuthMiddleware``."""

    app = Starlette(routes=[Route("/", _ok)])
    app.add_middleware(BearerAuthMiddleware, token=_TOKEN)
    return TestClient(app)


def test_missing_authorization_header_returns_401(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 401
    assert response.text == "unauthorized"


def test_malformed_authorization_header_returns_401(client: TestClient) -> None:
    # No "Bearer " prefix — single-token scheme should be rejected.
    response = client.get("/", headers={"Authorization": _TOKEN})
    assert response.status_code == 401
    assert response.text == "unauthorized"


def test_wrong_scheme_returns_401(client: TestClient) -> None:
    # Right shape, wrong scheme (e.g. "Token" instead of "Bearer").
    response = client.get("/", headers={"Authorization": f"Token {_TOKEN}"})
    assert response.status_code == 401
    assert response.text == "unauthorized"


def test_wrong_token_returns_401(client: TestClient) -> None:
    response = client.get("/", headers={"Authorization": "Bearer not-the-token"})
    assert response.status_code == 401
    assert response.text == "unauthorized"


def test_correct_bearer_token_returns_wrapped_response(client: TestClient) -> None:
    response = client.get("/", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert response.status_code == 200
    assert response.text == "ok"
