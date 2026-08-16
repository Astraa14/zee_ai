"""Unit tests for zee_core: parsers, sanitization, guards, approvals.

These never touch the network, the audio device or a real LLM.
"""

import json
import time

import pytest

import zee_core as jc


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Point all persistence (memory, approvals, audit) at a temp dir."""
    monkeypatch.setattr(jc, "_MEMORY_FILE", str(tmp_path / "zee_memory.json"))
    monkeypatch.setattr(jc, "_APPROVAL_FILE", str(tmp_path / "zee_pending_approvals.json"))
    monkeypatch.setattr(jc, "_APPROVALS_LOG", str(tmp_path / "zee_approvals.log"))
    jc._load_memory()
    jc._pending_approvals.clear()
    yield


# ---------------- memory / facts ----------------
def test_extract_facts_location(monkeypatch):
    calls = []
    monkeypatch.setattr(jc, "_save_memory", lambda: calls.append(1))
    jc._extract_facts("I'm in Batangas right now")
    assert jc._memory.get("location") == "Batangas"
    jc._extract_facts("I am actually living near Makati")
    assert jc._memory.get("location") == "Makati"
    jc._extract_facts("hello world, what is 2+2?")
    assert jc._memory.get("location") == "Makati"  # unchanged


def test_extract_facts_name(monkeypatch):
    monkeypatch.setattr(jc, "_save_memory", lambda: None)
    jc._extract_facts("please call me Ron")
    assert jc._memory.get("name") == "Ron"
    jc._extract_facts("my name's zee test")
    assert jc._memory.get("name") == "Zee"


def test_fact_block_roundtrip():
    jc._memory = {"location": "Imus", "name": "Ron"}
    block = jc._fact_block()
    assert "Ron" in block and "Imus" in block


# ---------------- quick replies ----------------
@pytest.mark.parametrize("msg", ["hi", "hey zee", "Hello!", "good morning", "what's up"])
def test_quick_reply_greetings(msg):
    assert jc._quick_reply(msg)


@pytest.mark.parametrize("msg", ["what time is it", "open notepad", "tell me a joke"])
def test_quick_reply_non_greetings(msg):
    assert jc._quick_reply(msg) is None


# ---------------- duration parser ----------------
@pytest.mark.parametrize(
    "duration,expected",
    [
        ("2 minutes", 2.0),
        ("in 10 minutes", 10.0),
        ("1 hour 30 minutes", 90.0),
        ("45 seconds", 0.75),
        ("30 sec", 0.5),
        ("5", 5.0),
        ("2 minutes 30 seconds", 2.5),
    ],
)
def test_duration_to_minutes(duration, expected):
    assert jc._duration_to_minutes(duration) == expected


@pytest.mark.parametrize("duration", ["soon", "tomorrow", "asap", "", None])
def test_duration_invalid(duration):
    assert jc._duration_to_minutes(duration) is None


def test_set_reminder_invalid_duration():
    result = jc.tool_set_reminder("nonsense")
    assert "error" in result


# ---------------- arg filtering / sanitization ----------------
def test_filter_args_drops_unknown_keys():
    cleaned = jc._filter_args(jc.tool_get_weather, {"city": "Manila", "junk": "x"})
    assert cleaned == {"city": "Manila"}


def test_filter_args_unwraps_dict_wrapper():
    cleaned = jc._filter_args(
        jc.tool_set_reminder,
        {
            "duration": {"type": "string", "description": "time", "value": "10 minutes"},
            "message": "hello",
        },
    )
    assert cleaned["duration"] == "10 minutes"


def test_filter_args_unwraps_junk_dict():
    cleaned = jc._filter_args(jc.tool_set_reminder, {"duration": {"type": "string"}})
    assert cleaned["duration"] == ""


def test_filter_args_strips_control_chars_and_caps():
    cleaned = jc._filter_args(jc.tool_get_weather, {"city": "Manila\x00\x1fELETE" * 200})
    assert "\x00" not in cleaned["city"] and "\x1f" not in cleaned["city"]
    assert len(cleaned["city"]) <= jc.MAX_ARG_LEN


def test_filter_args_string_coercion():
    assert jc._filter_args(jc.tool_get_weather, {"city": 123}) == {"city": "123"}


# ---------------- guards ----------------
def test_guard_blocked():
    assert jc._guard_blocked("set_volume", "what is the weather today")
    assert not jc._guard_blocked("set_volume", "turn the volume down")
    assert not jc._guard_blocked("get_time", "what time is it")
    assert not jc._guard_blocked("web_search", "anything")  # no guard entry


# ---------------- unknown / inline tool call detection ----------------
def test_unknown_tool_name():
    assert jc._unknown_tool_name('{"name": "fake_tool", "arguments": {}}') == "fake_tool"
    assert jc._unknown_tool_name('{"name": "get_time"}') is None
    assert jc._unknown_tool_name("just a normal answer") is None


def test_parse_inline_tool_call():
    call = json.dumps({"function": {"name": "get_weather", "arguments": {"city": "Manila"}}})
    assert jc._parse_inline_tool_call(call) == ("get_weather", {"city": "Manila"})
    assert jc._parse_inline_tool_call("no tool here") is None
    assert jc._parse_inline_tool_call('{"name": "nonexistent_tool"}') is None


# ---------------- tool dispatch ----------------
def test_run_tool_unknown():
    assert jc.run_tool("definitely_not_a_tool", {})["error"]


def test_run_tool_dangerous_needs_approval():
    result = jc.run_tool("kill_process", {"name": "notepad"})
    assert result.get("needs_approval") is True
    assert result.get("action") == "kill_process"


def test_run_tool_get_time():
    result = jc.run_tool("get_time", {})
    assert "datetime" in result


def test_run_tool_error_is_caught(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(jc._TOOL_FUNCS, "get_time", boom)
    result = jc.run_tool("get_time", {})
    assert "error" in result


# ---------------- approval lifecycle ----------------
def test_approval_approve_deny_flow(tmp_path, monkeypatch):
    calls = []

    def fake_kill(name, **kwargs):
        calls.append({"name": name})
        return {"killed": ["notepad"]}

    monkeypatch.setitem(jc._TOOL_FUNCS, "kill_process", fake_kill)
    jc._APPROVAL_TTL = 120

    pending = jc.request_approval("kill_process", {"name": "notepad", "junk": 1})
    assert pending["needs_approval"] is True
    # persisted to disk
    assert json.loads((tmp_path / "zee_pending_approvals.json").read_text(encoding="utf-8"))

    result = jc.approve_action(pending["id"])
    assert result == {"killed": ["notepad"]}
    assert calls == [{"name": "notepad"}]  # junk key was filtered

    # replay protection: the same id can never be approved twice
    second = jc.approve_action(pending["id"])
    assert "error" in second

    # deny flow
    pending2 = jc.request_approval("kill_process", {"name": "calc"})
    assert jc.deny_action(pending2["id"]) == {"denied": True}
    assert "error" in jc.approve_action(pending2["id"])


def test_approval_expired():
    pending = jc.request_approval("system_action", {"action": "lock"})
    jc._pending_approvals[pending["id"]]["expires"] = time.time() - 10
    result = jc.approve_action(pending["id"])
    assert "error" in result


def test_approval_broadcasts_result(monkeypatch):
    """approve/deny push an approval_result SSE event (best-effort)."""
    seen = []
    monkeypatch.setattr(jc, "_broadcast_approval", seen.append)
    monkeypatch.setitem(jc._TOOL_FUNCS, "system_action", lambda **kw: {"executed": "lock"})

    pending = jc.request_approval("system_action", {"action": "lock"}, actor="web")
    jc.approve_action(pending["id"], actor="web")
    statuses = [p["status"] for p in seen if p["type"] == "approval_result"]
    assert "approved" in statuses

    denied = jc.request_approval("system_action", {"action": "lock"}, actor="voice")
    jc.deny_action(denied["id"], actor="voice")
    statuses = [p["status"] for p in seen if p["type"] == "approval_result"]
    assert "denied" in statuses
    assert set(seen[-1]) == {"type", "id", "status", "action", "result"}


def test_approval_audit_log(tmp_path):
    """Approval lifecycle events land in zee_approvals.log as JSON lines."""
    pending = jc.request_approval("kill_process", {"name": "notepad"}, actor="user1")
    jc.deny_action(pending["id"], actor="user1")
    audit_file = tmp_path / "zee_approvals.log"
    lines = [line for line in audit_file.read_text(encoding="utf-8").strip().splitlines() if line]
    assert lines, "zee_approvals.log should have at least one line"
    events = [json.loads(entry) for entry in lines]
    statuses = [ev["status"] for ev in events]
    assert "requested" in statuses and "denied" in statuses
    for ev in events:
        assert set(ev) >= {
            "timestamp",
            "id",
            "action",
            "args",
            "expires",
            "status",
            "actor",
            "result",
        }
        assert ev["action"] == "kill_process"
        assert json.loads(ev["args"]) == {"name": "notepad"}
    assert events[-1]["actor"] == "user1"
    denied = [ev for ev in events if ev["status"] == "denied"][0]
    assert denied["result"] == {"denied": True}


def test_approve_no_replay(monkeypatch):
    """A pending approval can only be approved once (pop-then-execute)."""
    calls = []
    monkeypatch.setitem(
        jc._TOOL_FUNCS, "system_action", lambda **kw: calls.append(kw) or {"executed": kw}
    )

    pending = jc.request_approval("system_action", {"action": "lock"})
    jc.approve_action(pending["id"])
    second = jc.approve_action(pending["id"])
    assert "error" in second and "not found" in second["error"]
    assert len(calls) == 1

    denied = jc.request_approval("system_action", {"action": "lock"})
    jc.deny_action(denied["id"])
    assert "error" in jc.approve_action(denied["id"])


def test_approve_tool_exception_is_audited(monkeypatch):
    """A crashing tool is audited as approved_failed, never 'approved'."""
    seen = []

    def boom(**kw):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(jc._TOOL_FUNCS, "system_action", boom)
    monkeypatch.setattr(jc, "_append_approval", lambda *a, **k: seen.append(a[0]))
    monkeypatch.setattr(jc, "_broadcast_approval", lambda *a, **k: None)

    pending = jc.request_approval("system_action", {"action": "lock"})
    result = jc.approve_action(pending["id"])
    assert "error" in result and "failed" in result["error"]
    assert "approved_failed" in seen


def test_deny_missing_is_audited(monkeypatch):
    """Denying an unknown/expired id is audited as deny_missing."""
    seen = []
    monkeypatch.setattr(jc, "_append_approval", lambda *a, **k: seen.append(a[0]))
    jc.deny_action("nonexistent-id")
    assert "deny_missing" in seen


def test_maybe_request_approval_phrases():
    assert jc.maybe_request_approval("shut down the computer")["action"] == "system_action"
    assert jc.maybe_request_approval("restart please")["action"] == "system_action"
    assert jc.maybe_request_approval("lock my pc")["action"] == "system_action"
    assert jc.maybe_request_approval("close chrome")["action"] == "kill_process"
    assert jc.maybe_request_approval("what is the weather") is None


# ---------------- web/weather tools (mocked HTTP) ----------------
def test_tool_get_weather_mocked(monkeypatch):
    def fake_fetch(url, timeout=10):
        if "/search?" in url:
            return {
                "results": [
                    {
                        "name": "Manila",
                        "country": "Philippines",
                        "latitude": 14.6,
                        "longitude": 121.0,
                    }
                ]
            }
        return {
            "current": {
                "temperature_2m": 30.5,
                "relative_humidity_2m": 70,
                "wind_speed_10m": 12.3,
                "weather_code": 1,
            }
        }

    monkeypatch.setattr(jc, "_fetch_json", fake_fetch)
    result = jc.tool_get_weather("Manila")
    assert result["city"] == "Manila"
    assert result["country"] == "Philippines"
    assert result["temp_c"] == 30.5
    assert result["condition"] == "Mainly clear"


def test_tool_get_weather_city_not_found(monkeypatch):
    monkeypatch.setattr(jc, "_fetch_json", lambda url, timeout=10: {"results": []})
    assert "error" in jc.tool_get_weather("Nowhereville")


def test_tool_get_weather_network_error(monkeypatch):
    def boom(url, timeout=10):
        raise OSError("no network")

    monkeypatch.setattr(jc, "_fetch_json", boom)
    assert "error" in jc.tool_get_weather("Manila")


def test_tool_get_weather_empty_city():
    assert "error" in jc.tool_get_weather("   ")


def test_tool_web_search_mocked(monkeypatch):
    def fake_fetch(url, timeout=10):
        return {
            "query": {
                "search": [
                    {"title": "Manila", "snippet": "<b>Manila</b> is the capital", "pageid": 42},
                ]
            }
        }

    monkeypatch.setattr(jc, "_fetch_json", fake_fetch)
    result = jc.tool_web_search("manila")
    assert result["results"][0]["title"] == "Manila"
    assert "<b>" not in result["results"][0]["snippet"]  # HTML stripped


def test_tool_web_search_network_error(monkeypatch):
    monkeypatch.setattr(
        jc, "_fetch_json", lambda url, timeout=10: (_ for _ in ()).throw(OSError("down"))
    )
    assert "error" in jc.tool_web_search("anything")


def test_tool_web_search_empty_query():
    assert "error" in jc.tool_web_search("")


# ---------------- doctor / environment ----------------
def test_doctor_structure():
    report = jc.doctor()
    assert "platform" in report
    assert "dependencies" in report
    for label in ("ollama", "edge_tts", "pygame"):
        assert label in report["dependencies"]
    assert "audio" in report and "vosk_model" in report
    assert "healthy" in report


def test_doctor_summary_lines():
    report = jc.doctor()
    lines = jc.doctor_summary(report)
    assert any("Platform:" in line for line in lines)


def test_check_ollama_never_raises():
    # Local server may or may not be running; must not raise either way.
    assert isinstance(jc.check_ollama(), bool)
