"""Tests for the Flask web API: auth, rate limiting, size limits, endpoints.

The Ollama brain and TTS are stubbed out — these tests exercise the web
layer only.
"""
import json

import pytest

import jarvis_core
import app as app_module


class _FakeStream:
    """Drop-in for jarvis_core.StreamAsk that yields canned text."""

    def __init__(self, text):
        self._text = text
        self.full_text = "fake reply"
        self.approval = None

    def __iter__(self):
        yield "fake"
        yield " reply"


@pytest.fixture(autouse=True)
def fake_brain(monkeypatch):
    monkeypatch.setattr(jarvis_core, "StreamAsk", _FakeStream)
    monkeypatch.setattr(jarvis_core, "speak", lambda *a, **k: None)
    monkeypatch.setattr(jarvis_core, "run_tool", lambda *a, **k: {"error": "not used"})
    monkeypatch.setattr(app_module, "_token", "ci-test-token")
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
    monkeypatch.setattr(jarvis_core, "approve_action", lambda aid: {"ok": True})
    resp = client.post("/approve",
                       data=json.dumps({"approval_id": "not-hex!!"}),
                       content_type="application/json",
                       headers={"Authorization": "Bearer ci-test-token"})
    assert resp.status_code == 400


# ---------------- approval endpoints ----------------
def test_approve_success(client, monkeypatch):
    def fake_approve(aid):
        assert aid == "a" * 16
        return {"executed": "lock"}

    monkeypatch.setattr(jarvis_core, "approve_action", fake_approve)
    resp = client.post("/approve",
                       data=json.dumps({"approval_id": "a" * 16}),
                       content_type="application/json",
                       headers={"Authorization": "Bearer ci-test-token"})
    assert resp.status_code == 200
    assert resp.get_json()["message"] == "lock done."


def test_deny_success(client, monkeypatch):
    monkeypatch.setattr(jarvis_core, "deny_action", lambda aid: {"denied": True})
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


# ---------------- open fast-path ----------------
def test_open_fastpath(client, monkeypatch):
    monkeypatch.setattr(jarvis_core, "run_tool",
                        lambda name, args: {"opened": "notepad"})
    resp = _auth(client, {"text": "open notepad"})
    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Opening" in body and '"done": true' in body


def test_open_fastpath_rejects_questions(client, monkeypatch):
    monkeypatch.setattr(jarvis_core, "run_tool",
                        lambda name, args: {"error": "nope"})
    resp = _auth(client, {"text": "what is an open book"})
    assert resp.status_code == 200  # goes to the brain instead
    assert "fake" in resp.data.decode("utf-8")
