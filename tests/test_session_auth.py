"""Regression tests for cookie-based media auth (web/auth.py session cookie).

<video>/<img> tags cannot send an Authorization header, so clip/thumb media
endpoints authenticate via the ce_session cookie set by POST /api/auth/session.
Without this, the queue rendered blank panels (every media request 401'd).

Note: the cookie is Secure, so tests must use an https:// base_url — httpx
refuses to transmit Secure cookies over plain http.
"""

from __future__ import annotations

import pytest

from web.auth import SESSION_COOKIE, session_token


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "testpw123")
    # The module caches the password at import; force the env fallback path.
    import web.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_WEB_ADMIN_PASSWORD", "")

    from fastapi.testclient import TestClient
    import web.api as w
    return TestClient(w.app, base_url="https://testserver")


def test_no_credentials_is_401(client):
    assert client.get("/api/runs/fitness/log").status_code == 401


def test_create_session_sets_cookie(client):
    r = client.post(
        "/api/auth/session", headers={"Authorization": "Bearer testpw123"}
    )
    assert r.status_code == 200
    assert SESSION_COOKIE in r.cookies
    assert r.cookies[SESSION_COOKIE] == session_token()
    # Cookie must never be the raw password.
    assert r.cookies[SESSION_COOKIE] != "testpw123"


def test_cookie_only_request_is_authorized(client):
    client.post("/api/auth/session", headers={"Authorization": "Bearer testpw123"})
    # Any non-401 status proves the cookie authenticated (404 = no log file yet).
    assert client.get("/api/runs/fitness/log").status_code != 401


def test_bogus_cookie_is_401(client):
    client.cookies.set(SESSION_COOKIE, "bogus")
    assert client.get("/api/runs/fitness/log").status_code == 401


def test_wrong_bearer_is_401_even_with_valid_cookie_format(client):
    r = client.get(
        "/api/runs/fitness/log", headers={"Authorization": "Bearer wrong"}
    )
    assert r.status_code == 401


def test_destroy_session_clears_cookie(client):
    client.post("/api/auth/session", headers={"Authorization": "Bearer testpw123"})
    r = client.delete(
        "/api/auth/session", headers={"Authorization": "Bearer testpw123"}
    )
    assert r.status_code == 200
    # After deletion the jar no longer carries a usable session cookie.
    assert not client.cookies.get(SESSION_COOKIE, None)
