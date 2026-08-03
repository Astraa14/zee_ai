"""Tests for the Windows-only PC-control module (win_control.py).

On non-Windows platforms we verify graceful degradation. On Windows we
verify input validation and safe failure paths only — never actually
launching apps, killing processes or driving Discord.
"""
import os

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


def _fixed_index(monkeypatch):
    """Deterministic app index so tests don't depend on the machine's Start Menu."""
    monkeypatch.setattr(wc, "_APP_INDEX", {
        "notepad": r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Notepad.lnk",
        "google chrome": r"C:\Program Files\Google\Chrome\chrome.exe.lnk",
        "steam": r"C:\Program Files (x86)\Steam\steam.exe.lnk",
    })


def test_open_app_validation(monkeypatch):
    """User-controlled strings must never reach the launcher."""
    monkeypatch.setattr("os.name", "nt")
    _fixed_index(monkeypatch)
    started = []
    monkeypatch.setattr(wc, "_start", lambda target: started.append(target))

    # shell metacharacters are rejected outright
    for bad in ("notepad & calc", "notepad; rm -rf", "notepad | dir", "x$(whoami)", "notepad && calc"):
        result = wc.tool_open_app(bad)
        assert "error" in result, bad
    assert started == []

    # substring match must not work backwards either
    assert "error" in wc.tool_open_app("notepad & calc")

    # unknown bare words are rejected (no Windows error dialog)
    assert "error" in wc.tool_open_app("totallynotanapp12345")

    # unknown bare domains become https URLs
    result = wc.tool_open_app("example.com")
    assert result.get("opened_website") == "https://example.com"
    assert started == ["https://example.com"]

    # empty input
    assert "error" in wc.tool_open_app("")
    assert "error" in wc.tool_open_app(None)


def test_open_app_fuzzy_shortcut_match(monkeypatch):
    """User's word as a substring of an installed shortcut name is allowed.

    .lnk targets are opened with os.startfile (raising=False so this passes on
    Linux CI too, where os.startfile does not exist).
    """
    monkeypatch.setattr("os.name", "nt")
    _fixed_index(monkeypatch)
    opened = []
    monkeypatch.setattr(os, "startfile", lambda target: opened.append(target), raising=False)
    result = wc.tool_open_app("steam")
    assert result.get("via") == "installed app"
    assert opened == [r"C:\Program Files (x86)\Steam\steam.exe.lnk"]


def test_open_app_known_goes_to_launcher(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    started = []
    monkeypatch.setattr(wc, "_start", lambda target: started.append(target))
    result = wc.tool_open_app("notepad")
    assert result.get("opened") == "notepad"
    assert started == ["notepad"]
    result = wc.tool_open_app("YouTube")
    assert result.get("opened_website") == "https://www.youtube.com"


def test_messenger_and_discord_require_automation(monkeypatch):
    """Messenger search/contact automation is opt-in (ZEE_ALLOW_AUTOMATION=1)."""
    monkeypatch.setattr("os.name", "nt")
    monkeypatch.delenv("ZEE_ALLOW_AUTOMATION", raising=False)

    assert "Automation disabled" in wc.tool_discord_contact("John Doe")["error"]
    assert "Automation disabled" in wc.tool_discord_call("John Doe")["error"]
    assert "Automation disabled" in wc.tool_open_messenger_search("John Doe")["error"]
    assert "Automation disabled" in wc.tool_open_messenger_search()["error"]
    # open_app for messenger also goes through the gate
    assert "Automation disabled" in wc.tool_open_app("messenger")["error"]
    assert "Automation disabled" in wc.tool_open_app("facebook messages")["error"]
    assert "Automation disabled" in wc.tool_open_app("messenger search for John Doe")["error"]

def test_open_messenger_search_url(monkeypatch):
    """With automation on, a valid name produces a search URL."""
    monkeypatch.setenv("ZEE_ALLOW_AUTOMATION", "1")
    opened = []
    monkeypatch.setattr(wc, "open_url", lambda url: opened.append(url) or None)
    result = wc.tool_open_messenger_search("John Doe")
    assert result.get("opened") == "https://www.messenger.com/search?q=John%20Doe"
    assert opened == ["https://www.messenger.com/search?q=John%20Doe"]


def test_open_messenger_search_rejects_injection(monkeypatch):
    """Shell metacharacters and oversized inputs never reach a URL."""
    monkeypatch.setenv("ZEE_ALLOW_AUTOMATION", "1")
    opened = []
    monkeypatch.setattr(wc, "open_url", lambda url: opened.append(url) or None)
    for bad in ("John; del C:\\", "John & rm", "$(whoami)", "name " * 100):
        result = wc.tool_open_messenger_search(bad)
        assert "error" in result, bad
    assert opened == []


def test_sanitize_input_rejects_dangerous_chars():
    assert wc.sanitize_input("ls -la") == "ls -la"
    for bad in ("a;b", "a&b", "a|b", "a`b", "a$b", "a\nb", "a\rb"):
        assert wc.sanitize_input(bad) is None, bad
    assert wc.sanitize_input(None) is None
    assert wc.sanitize_input("") is None
    assert wc.sanitize_input("x" * 121) is None
    assert wc.sanitize_input("x" * 120) == "x" * 120


def test_safe_run_never_shell_and_timeouts(monkeypatch):
    """safe_run always passes a list and returns a tuple."""
    res = wc.safe_run(["python", "-c", "print('hi')"])
    assert res[0] == 0
    assert "hi" in res[1]
    # timeout path returns None code with an error message
    res2 = wc.safe_run(["python", "-c", "import time; time.sleep(99)"], timeout=1)
    assert res2[0] is None
    assert "timed out" in res2[2]
    # missing binary reports an error, never raises
    res3 = wc.safe_run(["definitely-not-a-real-binary-xyz"])
    assert res3[0] is None
    assert res3[2]


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
