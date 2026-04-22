"""Test the MCP BasicAuthMiddleware against a mock downstream."""
from __future__ import annotations

import base64

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.mcp_server import BasicAuthMiddleware, _current_user


async def _echo(request):
    return JSONResponse({"user": _current_user.get()})


@pytest.fixture
def client():
    app = Starlette(routes=[Route("/", _echo, methods=["GET"])],
                    middleware=[Middleware(BasicAuthMiddleware)])
    return TestClient(app)


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_rejects_missing_auth(client):
    r = client.get("/")
    assert r.status_code == 401
    assert "www-authenticate" in {k.lower() for k in r.headers.keys()}


def test_rejects_bad_password(client):
    r = client.get("/", headers={"Authorization": _basic("tobisako", "wrongpw")})
    assert r.status_code == 401


def test_accepts_seeded_user(client):
    # tobisako/admin is seeded by startup. If this test runs before a daemon
    # has ever booted, it will fail — but in practice the API/daemon have
    # both seeded by now.
    r = client.get("/", headers={"Authorization": _basic("tobisako", "admin")})
    assert r.status_code == 200
    assert r.json()["user"] == "tobisako"
