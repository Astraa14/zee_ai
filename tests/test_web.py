"""Tests for the Flask web API: auth, rate limiting, size limits, endpoints.

The Ollama brain and TTS are stubbed out — these tests exercise the web
layer only.
"""
import json

import pytest

import zee_core
import zee_api as app_module


class _FakeStream:
    """Drop-in for zee_core.StreamAsk that yields canned text."""

    def __init__(self, text, actor="web"):
        self._text = text
        self.actor = actor
        self.full_text = "fake reply"
        self.approval = None

    def __iter__(self):
        yield "fake"
        yield " reply"


@pytest.fixture(autouse=True)
def fake_brain(monkeypatch):
    monkeypatch.setattr(zee_core, "StreamAsk", _FakeStream)
    monkeypatch.setattr(zee_core, "speak", lambda *a, **k: None)
    monkeypatch.setattr(zee_core, "run_tool", lambda *a, **k: {"error": "not used"})
    monkeypatch.setattr(zee_core, "ollama_probe", lambda: "unavailable")
    monkeypatch.setattr(zee_core, "automation_enabled", lambda: False)
    monkeypatch.setattr(app_module, "_token", "ci-test-token")
    monkeypatch.setattr(app_module, "_ask_limiter",
                        app_module._RateLimiter(1000, 60))
    yield


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _auth(client, body=None, token="ci-test-token"):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body if body is not None else {"text": "hello"})
    return client.post("/ask", data=payload, content_type="application/json",
                       headers=headers)


# ---------------- authentication ----------------
def test_ask_requires_token(client):
    resp = client.post("/ask", data=json.dumps({"text": "hi"}),
                       content_type="application/json")
    assert resp.status_code == 401


def test_ask_rejects_wrong_token(client):
    resp = _auth(client, token="wrong-token")
    assert resp.status_code == 401


def test_ask_accepts_bearer_token(client):
    resp = _auth(client)
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "fake" in body and '"done": true' in body


def test_approve_requires_token(client):
    resp = client.post("/approve", data=json.dumps({"approval_id": "a" * 16}),
                       content_type="application/json")
    assert resp.status_code == 401


# ---------------- input validation ----------------
def test_ask_empty_text(client):
    assert _auth(client, {"text": ""}).status_code == 400
    assert _auth(client, {"text": "   \n "}).status_code == 400


def test_ask_non_string_text(client):
    assert _auth(client, {"text": 123}).status_code == 400
    assert _auth(client, {"text": ["hi"]}).status_code == 400


def test_ask_text_too_long(client):
    resp = _auth(client, {"text": "x" * 2001})
    assert resp.status_code == 400


def test_ask_body_too_large(client):
    resp = client.post("/ask",
                       data=json.dumps({"text": "y" * (30 * 1024)}),
                       content_type="application/json",
                       headers={"Authorization": "Bearer ci-test-token"})
    assert resp.status_code == 413


def test_invalid_approval_id_format(client, monkeypatch):
    monkeypatch.setattr(zee_core, "approve_action", lambda aid, actor="web": {"ok": True})
    resp = client.post("/approve",
                       data=json.dumps({"approval_id": "not-hex!!"}),
                       content_type="application/json",
                       headers={"Authorization": "Bearer ci-test-token"})
    assert resp.status_code == 400


# ---------------- approval endpoints ----------------
def test_approve_success(client, monkeypatch):
    def fake_approve(aid, actor="web"):
        assert aid == "a" * 16
        assert actor == "web"
        return {"executed": "lock"}

    monkeypatch.setattr(zee_core, "approve_action", fake_approve)
    resp = client.post("/approve",
                       data=json.dumps({"approval_id": "a" * 16}),
                       content_type="application/json",
                       headers={"Authorization": "Bearer ci-test-token"})
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "lock done."


def test_deny_success(client, monkeypatch):
    monkeypatch.setattr(zee_core, "deny_action", lambda aid, actor="web": {"denied": True})
    resp = client.post("/deny",
                       data=json.dumps({"approval_id": "b" * 16}),
                       content_type="application/json",
                       headers={"Authorization": "Bearer ci-test-token"})
    assert resp.status_code == 200


# ---------------- rate limiting ----------------
def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, "_ask_limiter",
                        app_module._RateLimiter(2, 60))
    assert _auth(client).status_code == 200
    assert _auth(client).status_code == 200
    assert _auth(client).status_code == 429


def test_rate_limit_is_per_token(client, monkeypatch):
    """The bucket is keyed by the token, not just the client IP."""
    monkeypatch.setattr(app_module, "_ask_limiter",
                        app_module._RateLimiter(1, 60))
    monkeypatch.setattr(app_module, "_authorized", lambda: True)
    assert _auth(client, token="token-a").status_code == 200
    assert _auth(client, token="token-a").status_code == 429
    assert _auth(client, token="token-b").status_code == 200


# ---------------- health endpoint ----------------
def test_health_ok(client, monkeypatch):
    monkeypatch.setattr(zee_core, "ollama_probe", lambda: "ok")
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True and data["ollama"] == "ok"
    assert data["automation_enabled"] is False


def test_health_reports_automation_when_enabled(client, monkeypatch):
    monkeypatch.setattr(zee_core, "ollama_probe", lambda: "ok")
    monkeypatch.setattr(zee_core, "automation_enabled", lambda: True)
    data = client.get("/health").get_json()
    assert data["automation_enabled"] is True
    assert data["ollama"] == "ok"


def test_health_degraded_when_ollama_down(client, monkeypatch):
    monkeypatch.setattr(zee_core, "ollama_probe", lambda: "unavailable")
    resp = client.get("/health")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["ok"] is False and data["ollama"] == "unavailable"


def test_health_degraded_when_ollama_errors(client, monkeypatch):
    monkeypatch.setattr(zee_core, "ollama_probe", lambda: "connection refused")
    resp = client.get("/health")
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["ok"] is False and data["ollama"] == "connection refused"


# ---------------- open fast-path ----------------
def test_open_fastpath(client, monkeypatch):
    monkeypatch.setattr(zee_core, "run_tool",
                        lambda name, args, actor="web": {"opened": "notepad"})
    resp = _auth(client, {"text": "open notepad"})
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Opening" in body and '"done": true' in body


def test_open_fastpath_rejects_questions(client, monkeypatch):
    monkeypatch.setattr(zee_core, "run_tool",
                        lambda name, args, actor="web": {"error": "nope"})
    resp = _auth(client, {"text": "what is an open book"})
    assert resp.status_code == 200  # goes to the brain instead
    assert "fake" in resp.data.decode("utf-8")
