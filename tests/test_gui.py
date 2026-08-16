"""Tests for the PySide6 GUI: token injection, SSE reconnect/backpressure,
wake window-raise, and daemon liveness checks.

Qt is only imported lazily inside the tests that need it; these run headless
(no QApplication) by exercising the plain-Python parts.
"""

import sys
import time

import pytest


class FakeResponse:
    def __init__(self, lines, status_code=200):
        self._lines = list(lines)
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


def _import_gui():
    import gui.zee_gui as g

    return g


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])
    return app


def _wait_until(cond, qt_app, timeout=8):
    """Pump the Qt event loop so queued frame signals are delivered."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        qt_app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    qt_app.processEvents()
    return cond()


def test_auth_interceptor_injects_token():
    g = _import_gui()

    class FakeInfo:
        def __init__(self):
            self.headers = {}

        def setHttpHeader(self, name, value):
            self.headers[name] = value

    interceptor = g._AuthInterceptor(lambda: "abc123")
    info = FakeInfo()
    interceptor.interceptRequest(info)
    assert info.headers.get(b"X-ZEE-TOKEN") == b"abc123"

    bare = g._AuthInterceptor(lambda: None)
    info2 = FakeInfo()
    bare.interceptRequest(info2)
    assert not info2.headers


def test_sse_worker_delivers_frames(monkeypatch, qt_app):
    g = _import_gui()
    lines = ['data: {"type": "wake", "text": "hi"}\n', 'data: {"type": "heartbeat"}\n']

    def fake_request(url, data=None, timeout=5):
        return FakeResponse(lines)

    monkeypatch.setattr(g, "_request", fake_request)
    worker = g._SseWorker("http://example/events")
    got = []
    worker.frame.connect(got.append)
    worker.start()
    assert _wait_until(lambda: len(got) >= 2, qt_app)
    worker.stop()
    worker.wait(2000)
    assert got[0].strip() == '{"type": "wake", "text": "hi"}'
    assert got[1].strip() == '{"type": "heartbeat"}'


def test_sse_worker_retries_after_exception(monkeypatch, qt_app):
    g = _import_gui()
    calls = {"n": 0}

    def fake_request(url, data=None, timeout=5):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return FakeResponse(['data: {"type": "wake"}\n'])

    monkeypatch.setattr(g, "_request", fake_request)
    worker = g._SseWorker("http://example/events")
    got = []
    worker.frame.connect(got.append)
    worker.start()
    assert _wait_until(lambda: len(got) >= 1, qt_app)
    worker.stop()
    worker.wait(2000)
    assert got
    assert calls["n"] >= 2


def test_sse_worker_backpressure_coalesces(monkeypatch, qt_app):
    g = _import_gui()
    wake = 'data: {"type": "wake"}\n'
    other = 'data: {"type": "x"}\n'
    lines = [other] * 200 + [wake]

    def fake_request(url, data=None, timeout=5):
        return FakeResponse(lines)

    monkeypatch.setattr(g, "_request", fake_request)
    worker = g._SseWorker("http://example/events")
    got = []
    worker.frame.connect(got.append)
    worker.start()
    assert _wait_until(lambda: len(got) >= g._SseWorker.MAX_QUEUED, qt_app)
    worker.stop()
    worker.wait(2000)
    # Unacked queue is bounded at MAX_QUEUED, and the dropped wake is
    # recorded as a pending wake (one-shot).
    assert len(got) <= g._SseWorker.MAX_QUEUED
    assert worker.take_pending_wake()
    assert not worker.take_pending_wake()


def test_ack_releases_depth(monkeypatch):
    g = _import_gui()
    worker = g._SseWorker("http://example/events")
    worker._depth = 5
    worker.ack('{"type": "wake"}')
    assert worker._depth == 4
    worker.ack("not json")
    assert worker._depth == 4
    worker._wake_pending.set()
    worker.ack('{"type": "other"}')
    assert worker._wake_pending.is_set()


def test_main_window_wake_raises_window(monkeypatch):
    g = _import_gui()
    raised = []

    class FakeMain:
        def __init__(self):
            self._sse = g._SseWorker("http://example/events")

        def show_and_raise(self):
            raised.append(1)

    win = FakeMain()
    g.MainWindow._on_event(win, '{"type": "wake"}')
    assert raised
    win._sse._wake_pending.set()
    g.MainWindow._on_event(win, '{"type": "approval", "id": "x"}')
    assert len(raised) >= 2


def test_daemon_alive_statuses(monkeypatch):
    g = _import_gui()
    state = {"code": 200}

    def fake_request(url, data=None, timeout=5):
        class R:
            status_code = state["code"]

        return R()

    monkeypatch.setattr(g, "_request", fake_request)
    assert g.daemon_alive()
    state["code"] = 503
    assert g.daemon_alive()
    state["code"] = 500
    assert not g.daemon_alive()
