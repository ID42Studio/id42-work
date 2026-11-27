"""Shared browser-less client for the operator UI tests."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Protocol, cast

from starlette.testclient import TestClient

from id42_work.web.app import create_app
from id42_work.web.config import OperatorSecret

ORIGIN = "http://testserver"
OPERATOR = "id42-operator"
_UNSET = object()


class _Headers(Protocol):
    def get(self, key: str, default: str = "") -> str: ...

    def __getitem__(self, key: str) -> str: ...


class _Response(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def text(self) -> str: ...

    @property
    def headers(self) -> _Headers: ...


def secret(value: str = "operator-secret") -> OperatorSecret:
    """Build a token without keeping a module-level secret constant."""
    return OperatorSecret(value)


def app_client(
    server: object,
    *,
    phrase: str = "operator-secret",
    token: object = _UNSET,
    credential_auth: bool = False,
    clock: Callable[[], datetime] | None = None,
    session_ttl: timedelta | None = None,
) -> TestClient:
    """A client whose default origin is the test server."""
    chosen = secret(phrase) if token is _UNSET else token
    if session_ttl is None:
        application = create_app(
            server,
            token=chosen,
            credential_auth=credential_auth,
            clock=clock,
        )
    else:
        application = create_app(
            server,
            token=chosen,
            credential_auth=credential_auth,
            clock=clock,
            session_ttl=session_ttl,
        )
    return TestClient(application, base_url=ORIGIN)


def csrf(html: str) -> str:
    """Read the first synchronizer token from a page."""
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    if match is None:
        msg = "csrf field missing"
        raise AssertionError(msg)
    return match.group(1)


def sign_in(
    client: TestClient,
    actor_id: str = OPERATOR,
    *,
    phrase: str = "operator-secret",
    credential: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> _Response:
    """POST the login form. Redirects are not followed."""
    page = client.get("/login")
    data: dict[str, str] = {"csrf": csrf(page.text), "token": phrase, "actor_id": actor_id}
    if credential is not None:
        data["credential"] = credential
    if extra is not None:
        data.update(extra)
    return cast(
        "_Response",
        client.post(
            "/login",
            data=data,
            headers={"origin": ORIGIN},
            follow_redirects=False,
        ),
    )


def signed_in(client: TestClient, actor_id: str = OPERATOR) -> str:
    """Sign in and return the CSRF token from the decision list."""
    response = sign_in(client, actor_id)
    assert response.status_code == 303
    home = client.get("/")
    assert home.status_code == 200
    return csrf(home.text)
