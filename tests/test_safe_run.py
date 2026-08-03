"""safe_run: subprocess wrapper — list arguments only, timeout enforced,
no shell, errors returned as tuples instead of raised."""
import subprocess

import pytest

import win_control


def test_safe_run_returns_tuple():
    code, out, err = win_control.safe_run([sys_python(), "-c", "print('hi')"])
    assert code == 0
    assert "hi" in out
    assert err == "" or err is None


def test_safe_run_never_uses_shell(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(win_control.subprocess, "run", fake_run)
    code, out, _ = win_control.safe_run(["echo", "hello"], timeout=3)
    assert code == 0
    assert out == "ok"
    assert calls and all(isinstance(c, list) for c in calls)


def test_safe_run_timeout_returns_error(monkeypatch):
    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout", 10))

    monkeypatch.setattr(win_control.subprocess, "run", slow)
    code, out, err = win_control.safe_run(["sleep", "99"], timeout=1)
    assert code is None
    assert err and "timed out" in err


def test_safe_run_missing_binary_returns_error(monkeypatch):
    def missing(cmd, **kwargs):
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(win_control.subprocess, "run", missing)
    code, out, err = win_control.safe_run(["definitely-not-real-xyz"])
    assert code is None
    assert err


def sys_python():
    import sys
    return sys.executable
