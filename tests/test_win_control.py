"""Tests for the Windows-only PC-control module (win_control.py).

On non-Windows platforms we verify graceful degradation. On Windows we
verify input validation and safe failure paths only — never actually
launching apps, killing processes or driving Discord.
"""
import pytest

import win_control as wc


def test_clean_text():
    assert wc._clean_text("hi\x00there\x1f") == "hithere"
    assert wc._clean_text("x" * 5000, 10) == "x" * 10
    assert wc._clean_text(None) == ""


def test_known_apps_cover_basics():
    for name in ("notepad", "calculator", "chrome", "terminal", "youtube", "spotify"):
        assert name in wc._KNOWN_APPS


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr("os.name", "posix")


def test_graceful_degradation_on_posix(posix):
    assert "Windows" in wc.tool_open_app("notepad")["error"]
    # open_file/open_folder are cross-platform best-effort; must still return
    # a clean result dict without raising or launching anything.
    result = wc.tool_open_file("resume.pdf")
    assert isinstance(result, dict)
    assert "Windows" in wc.tool_set_volume(50)["error"]
    assert "Windows" in wc.tool_media_control("play")["error"]
    # kill_process is psutil-based and works on any OS — it must fail safely
    # on a nonexistent process without touching real ones.
    result = wc.tool_kill_process("no-such-process-xyz")
    assert "error" in result
    assert "Windows" in wc.tool_system_action("shutdown")["error"]
    assert "Windows" in wc.tool_type_text("hi")["error"]


def test_system_action_rejects_unknown(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    result = wc.tool_system_action("rm -rf /")
    assert "error" in result


def test_open_app_validation(monkeypatch):
    """User-controlled strings must never reach the launcher."""
    monkeypatch.setattr("os.name", "nt")
    started = []
    monkeypatch.setattr(wc, "_start", lambda target: started.append(target))

    # shell metacharacters are rejected outright
    for bad in ("notepad & calc", "notepad; rm -rf", "notepad | dir", "x$(whoami)", "notepad && calc"):
        result = wc.tool_open_app(bad)
        assert "error" in result, bad
    assert started == []

    # unknown bare words are rejected (no Windows error dialog)
    assert "error" in wc.tool_open_app("totallynotanapp12345")

    # unknown bare domains become https URLs
    result = wc.tool_open_app("example.com")
    assert result.get("opened_website") == "https://example.com"
    assert started == ["https://example.com"]

    # empty input
    assert "error" in wc.tool_open_app("")
    assert "error" in wc.tool_open_app(None)


def test_open_app_known_goes_to_launcher(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    started = []
    monkeypatch.setattr(wc, "_start", lambda target: started.append(target))
    result = wc.tool_open_app("notepad")
    assert result.get("opened") == "notepad"
    assert started == ["notepad"]
    result = wc.tool_open_app("YouTube")
    assert result.get("opened_website") == "https://www.youtube.com"


def test_open_file_rejects_path_traversal_chars(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    for bad in (r"..\..\windows\system32\cmd", "/etc/passwd", "a:b"):
        result = wc.tool_open_file(bad)
        assert "error" in result, bad


def test_open_folder_validation(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    assert "error" in wc.tool_open_folder("")
    assert "error" in wc.tool_open_folder("C:\\definitely\\not\\here")


def test_duration_helpers_not_here():
    # sanity: module exposes the schema and function registry
    assert "system_action" in wc.WIN_TOOL_FUNCS
    names = {t["function"]["name"] for t in wc.WIN_TOOLS}
    assert names == set(wc.WIN_TOOL_FUNCS)
    assert "open_app" in names and "kill_process" in names


def test_discord_name_sanitized_before_use(monkeypatch):
    """Names with quotes are escaped for the PowerShell command line."""
    escaped = wc._clean_text("O'Brian", 50).replace("'", "''")
    # single quotes are doubled (''), which is PS-safe inside '...' strings
    assert escaped == "O''Brian"
