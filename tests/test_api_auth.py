"""API authentication: ZEE_TOKEN required for /ask, /approve, /deny, /shutdown.

Accepts the token via ``X-ZEE-TOKEN`` or ``Authorization: Bearer``; missing or
wrong tokens get a 401 JSON response. /health stays public.
"""
import json

import zee_api as api_module


def _post(client, path, body=None, token=None):
    headers = {}
    if token:
        headers["X-ZEE-TOKEN"] = token
    return client.post(path, data=json.dumps(body or {}),
                       content_type="application/json", headers=headers)


# ---------------- /ask ----------------
def test_ask_requires_token(client):
    assert _post(client, "/ask", {"text": "hi"}).status_code == 401


def test_ask_rejects_wrong_token(client):
    assert _post(client, "/ask", {"text": "hi"}, token="nope").status_code == 401


def test_ask_accepts_x_zee_token_header(client):
    resp = _post(client, "/ask", {"text": "hi"}, token="ci-test-token")
    assert resp.status_code == 200


def test_ask_accepts_bearer_header(client):
    resp = client.post("/ask", data=json.dumps({"text": "hi"}),
                       content_type="application/json",
                       headers={"Authorization": "Bearer ci-test-token"})
    assert resp.status_code == 200


def test_ask_returns_401_json(client):
    resp = _post(client, "/ask", {"text": "hi"})
    assert resp.status_code == 401
    assert "error" in resp.get_json()


def test_no_token_allows_localhost_denies_lan(client, monkeypatch):
    """Without a token, localhost (dev mode) is allowed but LAN requests 401."""
    monkeypatch.setattr(api_module, "_token", "")
    resp = _post(client, "/ask", {"text": "hi"})
    assert resp.status_code == 200
    # Simulate a request from a remote address → must be rejected.
    monkeypatch.setattr(api_module, "_loopback", lambda: False)
    resp = _post(client, "/ask", {"text": "hi"})
    assert resp.status_code == 401
    assert "error" in resp.get_json()


# ---------------- /approve / /deny ----------------
def test_approve_requires_token(client):
    resp = _post(client, "/approve", {"approval_id": "a" * 16})
    assert resp.status_code == 401


def test_deny_requires_token(client):
    resp = _post(client, "/deny", {"approval_id": "a" * 16})
    assert resp.status_code == 401


# ---------------- /shutdown ----------------
def test_shutdown_requires_token(client):
    resp = _post(client, "/shutdown", {})
    assert resp.status_code == 401


def test_shutdown_with_token(client, monkeypatch):
    monkeypatch.setattr(api_module, "_force_stop", lambda: None)
    resp = _post(client, "/shutdown", {}, token="ci-test-token")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


# ---------------- /health is public ----------------
def test_health_public(client):
    assert client.get("/health").status_code in (200, 503)
