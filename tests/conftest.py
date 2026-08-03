import os
import sys

# Project root on the import path so `import zee_core` works in CI.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402

import zee_core  # noqa: E402

# Deterministic auth token for all web tests (must be set before importing zee_api).
os.environ.setdefault("ZEE_TOKEN", "ci-test-token")


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


@pytest.fixture
def client():
    """Flask test client for API tests (auth + rate limiting)."""
    import zee_api
    zee_api.app.config["TESTING"] = True
    return zee_api.app.test_client()


@pytest.fixture(autouse=True)
def fake_brain(request, monkeypatch):
    """Stub the Ollama brain + TTS in the web/API test files only.

    Kept scoped so core unit tests still exercise the real code paths.
    """
    module = request.module.__name__
    if "test_web" not in module and "test_api_auth" not in module:
        yield
        return
    monkeypatch.setattr(zee_core, "StreamAsk", _FakeStream)
    monkeypatch.setattr(zee_core, "speak", lambda *a, **k: None)
    monkeypatch.setattr(zee_core, "run_tool", lambda *a, **k: {"error": "not used"})
    monkeypatch.setattr(zee_core, "ollama_probe", lambda: "unavailable")
    monkeypatch.setattr(zee_core, "automation_enabled", lambda: False)
    import zee_api as _api
    monkeypatch.setattr(_api, "_token", "ci-test-token")
    yield
