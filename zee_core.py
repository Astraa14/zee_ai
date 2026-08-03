import asyncio
import importlib
import inspect
import json
import logging
import logging.handlers
import os
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

import edge_tts
import ollama
import pygame

try:
    import win_control
except ImportError as e:
    logging.getLogger("zee").warning(f"win_control not available: {e}")
    win_control = None

# ==========================================
# LOGGING
# ==========================================
log = logging.getLogger("zee")
_logging_configured = False


def setup_logging():
    """Configure the 'zee' logger once (console + rotating file handler).

    Level from ZEE_LOG_LEVEL (INFO default). Log file: ZEE_LOG_FILE or
    zee.log in the project root, rotated at 1 MB (3 backups). Safe to call
    from any module; idempotent.
    """
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True
    log.setLevel(os.getenv("ZEE_LOG_LEVEL", "INFO").upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)
    logfile = os.getenv("ZEE_LOG_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "zee.log")
    try:
        rotating = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=1_000_000, backupCount=3, encoding="utf-8",
        )
        rotating.setFormatter(fmt)
        log.addHandler(rotating)
        # Flask's request logs land in the same file.
        logging.getLogger("werkzeug").addHandler(rotating)
    except OSError as e:
        log.error(f"Cannot open log file {logfile!r}: {e}")


setup_logging()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:latest")
ZEE_VOICE = os.getenv("ZEE_VOICE", "en-US-ChristopherNeural")
ZEE_RATE = os.getenv("ZEE_RATE", "+10%")
ZEE_MAX_TOKENS = int(os.getenv("ZEE_MAX_TOKENS", "150"))
ZEE_TEMPERATURE = float(os.getenv("ZEE_TEMPERATURE", "0.7"))
ZEE_KEEP_ALIVE = os.getenv("ZEE_KEEP_ALIVE", "30m")
ZEE_NUM_CTX = int(os.getenv("ZEE_NUM_CTX", "4096"))

SYSTEM_PROMPT = (
    "You are ZEE, a helpful AI assistant. Keep your answers brief, "
    "conversational, and under 3 sentences. Do not use any special formatting "
    "or symbols like asterisks, as they will be read out loud by a text-to-speech engine. "
    "Only call a tool when the user's request clearly needs it (e.g. the time, "
    "weather, a web search, system info, or a computer action). Otherwise answer directly. "
    "Never invent tool names that do not exist; only use the tools listed. "
    "If you do not know the answer, say so instead of guessing. "
    "If a tool call was refused, never bring up that tool again — just answer the "
    "user's actual question. "
    "When the user states where they are (e.g. 'I'm in Batangas'), trust them over "
    "IP geolocation for later questions. "
    "After a tool runs, give a short summary of the result. "
    "IMPORTANT: if a tool result contains 'needs_approval', the action was NOT "
    "performed — ask the user for permission and never claim it happened."
)

# ---------------- MEMORY (conversation history + learned facts) ----------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_MEMORY_FILE = os.path.join(_BASE_DIR, "zee_memory.json")
_memory = {}
_memory_lock = threading.Lock()
_history = []  # in-memory conversation turns (user/assistant pairs)
_MAX_HISTORY = 8


def _load_memory():
    global _memory
    try:
        with open(_MEMORY_FILE, encoding="utf-8") as f:
            _memory = json.load(f)
    except (OSError, ValueError):
        _memory = {}


def _save_memory():
    try:
        with open(_MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(_memory, f, indent=2)
    except OSError as e:
        log.warning(f"Could not save memory: {e}")


def _extract_facts(text):
    """Learn facts (location, name) from what the user said."""
    low = (text or "").lower()
    m = re.search(
        r"\b(?:i'?m|i am|i live|i'?m living|i'?m based|am)\s+"
        r"(?:actually|currently|right now|now|here|just)?\s*"
        r"(?:living\s+|based\s+)?(?:at|in|from|near)\s+"
        r"([a-z][a-z .'\-]{0,40}?)\b",
        low,
    )
    if m:
        loc = m.group(1).strip(" .,")
        loc = loc.split(" not ")[0].strip()
        if loc and loc not in ("here", "there", "home", "the"):
            _memory["location"] = loc.title()
            _save_memory()
    m = re.search(r"\b(?:call me|my name is|name'?s)\s+([a-z][a-z'\-]{0,20})\b", low)
    if m:
        _memory["name"] = m.group(1).capitalize()
        _save_memory()


def _fact_block():
    with _memory_lock:
        loc = _memory.get("location")
        name = _memory.get("name")
    parts = []
    if name:
        parts.append(f"The user's name is {name}.")
    if loc:
        parts.append(f"The user's stated location is {loc}.")
    return " ".join(parts)


def _system_prompt():
    facts = _fact_block()
    return SYSTEM_PROMPT + (" " + facts if facts else "")


def _remember(user_text, reply):
    """Keep the last few turns so ZEE can follow conversations."""
    with _memory_lock:
        _history.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply},
        ])
        del _history[: -_MAX_HISTORY]


_load_memory()

_play_lock = threading.Lock()
_mixer_lock = threading.Lock()


# ==========================================
# MOUTH (Text-to-Speech, streamed playback)
# ==========================================
def _ensure_mixer():
    with _mixer_lock:
        if not pygame.mixer.get_init():
            pygame.mixer.init()


def init_audio():
    """Initialize the audio mixer once, from the main thread, at startup.

    pygame's mixer can misbehave (no sound) if initialized inside a
    background thread, so always call this before spawning speech threads.
    """
    try:
        _ensure_mixer()
        pygame.mixer.music.set_volume(1.0)
        log.info("Audio ready.")
        return True
    except Exception as e:
        log.error(f"Audio init failed: {e}")
        return False


def _play_audio_file(path):
    with _play_lock:
        _ensure_mixer()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(10)
        pygame.mixer.music.unload()


def speak(text, wait=False):
    """Generate TTS audio and play it through the speakers.

    The MP3 is downloaded completely before playback starts (SDL_mixer
    hangs silently on partially-written MP3s), then played through the
    speakers. By default this runs in a background thread; pass wait=True
    to block until playback finishes.
    """
    log.info(f"ZEE: {text}")

    async def generate(path, ready):
        communicate = edge_tts.Communicate(text, ZEE_VOICE, rate=ZEE_RATE)
        with open(path, "wb") as f:
            stream = communicate.stream()
            async for chunk in stream:
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
            f.flush()
        ready.set()

    def worker():
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="zee_")
        os.close(fd)
        ready = threading.Event()
        writer = threading.Thread(
            target=lambda: asyncio.run(generate(path, ready)), daemon=True
        )
        writer.start()
        try:
            ready.wait(timeout=15)
            try:
                _play_audio_file(path)
            except Exception:
                writer.join(timeout=60)
                _play_audio_file(path)
            writer.join(timeout=60)
        except Exception as e:
            log.error(f"Audio error: {e}")
        finally:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    if wait:
        worker()
    else:
        threading.Thread(target=worker, daemon=True).start()


# ==========================================
# INPUT SANITIZATION + TOOL ARG FILTERING
# ==========================================
MAX_ARG_LEN = 1000
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# Rejected outright in subprocess-bound / sensitive inputs.
_DANGEROUS_CHARS_RE = re.compile(r"[\n\r;|`$&]")

# Desktop/browser automation (Discord, messenger search, ...) is opt-in.
AUTOMATION_ENABLED = os.getenv("ZEE_ALLOW_AUTOMATION", "0") == "1"
AUTOMATION_DISABLED_MSG = "Automation disabled; set ZEE_ALLOW_AUTOMATION=1 to enable"


def automation_enabled():
    """True when desktop/browser automation is opted in via env."""
    return os.getenv("ZEE_ALLOW_AUTOMATION", "0") == "1"


def automation_denied():
    """Error dict returned by automation tools when the opt-in flag is off."""
    return {"error": AUTOMATION_DISABLED_MSG}


def sanitize_input(text, maxlen=120):
    """Strict sanitizer for user-controlled strings bound for subprocess/URLs.

    Returns the trimmed, cleaned string, or None when the input is unsafe:
    empty, longer than maxlen, contains control characters, or any of
    ; & | ` $ and newlines.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s or len(s) > maxlen:
        return None
    if _CONTROL_CHARS_RE.search(s) or _DANGEROUS_CHARS_RE.search(s):
        return None
    return s


def clean_text(s, maxlen=MAX_ARG_LEN):
    """Soft cleaner: strip control characters and cap the length. Never rejects."""
    s = "" if s is None else str(s)
    s = _CONTROL_CHARS_RE.sub("", s)
    return s[:maxlen].strip()


def _filter_args(func, args):
    """Validate and clean tool arguments: drop unknown keys, unwrap junk
    dicts, coerce to the declared parameter types, strip control characters
    and cap lengths."""
    sig = inspect.signature(func)
    cleaned = {}
    for k, v in (args or {}).items():
        if k not in sig.parameters:
            continue
        if isinstance(v, dict) and ({"type", "value", "description"} & set(v.keys())):
            # Models sometimes wrap the value in {"type": ..., "value": ...}.
            for key in ("value", "description", "type"):
                if key in v and v[key] not in ("string", "number", "integer",
                                               "boolean", "object", "array", "null"):
                    v = v[key]
                    break
            else:
                v = ""
        p = sig.parameters[k]
        if p.annotation is str:
            v = "" if v is None else str(v)
            v = clean_text(v)
        elif isinstance(v, str) and p.annotation in (int, float):
            # Only accept cleanly convertible strings for numeric params.
            try:
                v = int(v) if p.annotation is int else float(v)
            except ValueError:
                pass  # keep original; the tool itself will reject it
        elif isinstance(v, str):
            v = clean_text(v)
        cleaned[k] = v
    return cleaned


# ==========================================
# HANDS (Tools / Actions) — cross-platform core
# ==========================================
def _fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def tool_get_time():
    from datetime import datetime
    return {"datetime": datetime.now().strftime("%A, %Y-%m-%d %H:%M:%S")}


def tool_get_location():
    """Return the user's stated location when known; otherwise IP geolocation."""
    with _memory_lock:
        stated = _memory.get("location")
    if stated:
        return {"city": stated.title(), "country": None, "source": "user-stated"}
    for url in ("http://ip-api.com/json/", "https://ipapi.co/json/"):
        try:
            data = _fetch_json(url, timeout=8)
            if not isinstance(data, dict) or data.get("error") or data.get("status") == "fail":
                continue
            city = data.get("city") or data.get("regionName") or "unknown"
            country = data.get("country_name") or data.get("country") or ""
            result = {"city": city, "country": country}
            if data.get("lat") is not None:
                result["latitude"] = data["lat"]
                result["longitude"] = data["lon"]
            elif data.get("latitude") is not None:
                result["latitude"] = data["latitude"]
                result["longitude"] = data["longitude"]
            return result
        except Exception:
            continue
    return {"error": "Location lookup failed."}


def tool_read_notes():
    """Read back the last few saved notes."""
    path = os.path.join(_BASE_DIR, "zee_notes.txt")
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line for line in f.read().splitlines() if line.strip()]
    except OSError:
        return {"notes": []}
    return {"notes": lines[-10:]}


def tool_list_processes():
    """List the apps currently running, most CPU-hungry first."""
    import psutil
    procs = []
    for p in psutil.process_iter(["name", "cpu_percent"]):
        try:
            procs.append((p.info["name"] or "?", round(p.info["cpu_percent"] or 0.0, 1)))
        except Exception:
            continue
    procs.sort(key=lambda x: -x[1])
    top = [{"name": n, "cpu_percent": c} for n, c in procs[:8]]
    return {"running_apps": top, "total_processes": len(procs)}


_DURATION_RE = re.compile(
    r"(?=\d)"
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\s*)?"
    r"(?:(?P<m>\d+(?:\.\d+)?)\s*(?:minutes?|mins?)\s*)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*(?:seconds?|secs?))?",
    re.IGNORECASE,
)


def _duration_to_minutes(duration):
    """Parse '2 minutes', '1 hour 30 minutes', '45 seconds' into minutes."""
    d = clean_text(duration).lower()
    m = _DURATION_RE.search(d)
    if m and any(m.group(k) for k in ("h", "m", "s")):
        hours = float(m.group("h") or 0)
        mins = float(m.group("m") or 0)
        secs = float(m.group("s") or 0)
        return hours * 60 + mins + secs / 60
    try:
        return float(d)  # bare number = minutes
    except ValueError:
        return None


def tool_set_reminder(duration: str, message: str = None):
    """Speak a reminder after the given duration (e.g. '2 minutes')."""
    minutes = _duration_to_minutes(duration)
    if minutes is None or minutes <= 0:
        return {"error": f"Could not understand the duration: {duration!r}"}
    msg = clean_text(message or "Your reminder is due.", 500)
    def fire():
        time.sleep(minutes * 60)
        speak(f"Reminder: {msg}")
    threading.Thread(target=fire, daemon=True).start()
    return {"reminder_set_for_minutes": minutes, "message": msg}


def tool_system_info():
    import psutil
    info = {
        "cpu_percent": psutil.cpu_percent(interval=1),
        "ram_percent": psutil.virtual_memory().percent,
        "ram_used_gb": round(psutil.virtual_memory().used / 1024 ** 3, 1),
        "ram_total_gb": round(psutil.virtual_memory().total / 1024 ** 3, 1),
    }
    battery = psutil.sensors_battery()
    if battery:
        info["battery_percent"] = battery.percent
        info["battery_plugged_in"] = battery.power_plugged
    return info


def tool_web_search(query: str):
    query = clean_text(query, 300)
    if not query:
        return {"error": "No search query given."}
    params = urllib.parse.urlencode({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "format": "json",
        "srlimit": 5,
    })
    try:
        data = _fetch_json(f"https://en.wikipedia.org/w/api.php?{params}")
    except Exception as e:
        return {"error": f"Search failed: {e}"}
    results = []
    for hit in data.get("query", {}).get("search", []):
        snippet = re.sub(r"<[^>]+>", "", hit.get("snippet", ""))
        results.append({
            "title": hit.get("title"),
            "snippet": snippet,
            "url": f"https://en.wikipedia.org/?curid={hit.get('pageid')}",
        })
    return {"query": query, "results": results}


_WMO_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with light hail", 99: "Thunderstorm with heavy hail",
}


def tool_get_weather(city: str):
    city = clean_text(city, 100)
    if not city:
        return {"error": "No city provided."}
    try:
        geo = _fetch_json(
            f"https://geocoding-api.open-meteo.com/v1/search?"
            f"{urllib.parse.urlencode({'name': city, 'count': 1, 'language': 'en', 'format': 'json'})}"
        )
        place = (geo.get("results") or [None])[0]
        if not place:
            return {"error": f"City not found: {city}"}
        lat, lon = place["latitude"], place["longitude"]
        forecast = _fetch_json(
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
            f"&timezone=auto"
        )
    except Exception as e:
        return {"error": f"Weather lookup failed: {e}"}
    cur = forecast.get("current", {})
    return {
        "city": place.get("name"),
        "country": place.get("country"),
        "temp_c": cur.get("temperature_2m"),
        "condition": _WMO_CODES.get(cur.get("weather_code"), "Unknown"),
        "humidity": cur.get("relative_humidity_2m"),
        "wind_kph": cur.get("wind_speed_10m"),
    }


def tool_create_note(content: str):
    content = clean_text(content, 2000)
    if not content:
        return {"error": "No note content given."}
    notes_file = os.path.join(_BASE_DIR, "zee_notes.txt")
    from datetime import datetime
    with open(notes_file, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {content}\n")
    return {"saved_to": notes_file}


# ==========================================
# HANDS (PC Control) — Windows-only module
# ==========================================
KNOWN_APP_NAMES = set(getattr(win_control, "KNOWN_APP_NAMES", ()))


# ==========================================
# APPROVAL / AUDIT (dangerous actions)
# ==========================================
DANGEROUS_TOOLS = {"kill_process", "system_action"}

_APPROVAL_FILE = os.path.join(_BASE_DIR, "zee_pending_approvals.json")
_APPROVALS_LOG = os.path.join(_BASE_DIR, "zee_approvals.log")
_APPROVAL_TTL = int(os.getenv("ZEE_APPROVAL_TTL", "120"))

_pending_approvals = {}
_approval_lock = threading.Lock()


def _iso(ts):
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


def _append_approval(status, approval_id, name, args, expires=None, actor="unknown"):
    """Append one JSON line to approvals.log (audit trail)."""
    entry = {
        "timestamp": _iso(time.time()),
        "id": approval_id,
        "action": name,
        "args": json.dumps(args, ensure_ascii=False),
        "expires": _iso(expires) if expires else None,
        "status": status,
        "actor": actor,
    }
    try:
        with open(_APPROVALS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning(f"Could not write approvals log: {e}")


def _load_pending_approvals():
    """Restore pending approvals from disk (survives restarts)."""
    global _pending_approvals
    try:
        with open(_APPROVAL_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        for aid, item in data.items():
            if isinstance(item, dict) and "name" in item and "args" in item:
                try:
                    item["expires"] = float(item.get("expires", 0))
                except (TypeError, ValueError):
                    item["expires"] = 0.0
                _pending_approvals[aid] = item
    except OSError:
        pass
    except ValueError as e:
        log.warning(f"Corrupt approval file {_APPROVAL_FILE}: {e}")


def _save_pending_approvals():
    try:
        with _approval_lock:
            data = json.dumps(_pending_approvals, ensure_ascii=False)
        tmp = _APPROVAL_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, _APPROVAL_FILE)
    except OSError as e:
        log.warning(f"Could not save pending approvals: {e}")


def request_approval(name, args, actor="unknown"):
    """Register a dangerous action for later approval and return a needs_approval result."""
    aid = uuid.uuid4().hex[:16]
    now = time.time()
    with _approval_lock:
        for old_id, old in list(_pending_approvals.items()):
            if old.get("expires", 0) < now:
                _pending_approvals.pop(old_id, None)
                _append_approval("expired", old_id, old.get("name"),
                                 old.get("args"), old.get("expires"), actor)
        _pending_approvals[aid] = {
            "name": name,
            "args": args,
            "expires": now + _APPROVAL_TTL,
        }
    _save_pending_approvals()
    _append_approval("requested", aid, name, args, now + _APPROVAL_TTL, actor)
    log.warning(f"Approval requested: {name}({json.dumps(args)}) [id={aid}, actor={actor}]")
    try:
        import events
        events.broadcast({
            "type": "approval",
            "id": aid,
            "action": name,
            "args": args,
            "expires": round(now + _APPROVAL_TTL, 3),
            "actor": actor,
        })
    except Exception:
        pass
    return {
        "needs_approval": True,
        "id": aid,
        "action": name,
        "args": args,
        "message": f"Do you want me to run {name}?",
    }


def maybe_request_approval(user_text, actor="unknown"):
    """Safety net: if the user asked for a dangerous action but the model didn't
    call a tool, register the approval anyway so the flow always works."""
    t = (user_text or "").lower()
    if re.search(r"\b(shut\s?down|power\s?off|turn\s?off)\b.*\b(computer|pc|machine|laptop|system)\b", t) or \
       re.fullmatch(r"\s*(shut\s?down|power\s?off)\s*[.!]*\s*", t):
        return request_approval("system_action", {"action": "shutdown"}, actor=actor)
    if re.search(r"\b(restart|reboot)\b", t):
        return request_approval("system_action", {"action": "restart"}, actor=actor)
    if re.search(r"\b(hibernate|sleep)\b.*\b(computer|pc|system|laptop)\b", t):
        return request_approval("system_action", {"action": "sleep"}, actor=actor)
    if re.search(r"\blog\s?off\b|\bsign\s?out\b", t):
        return request_approval("system_action", {"action": "logoff"}, actor=actor)
    if re.search(r"\block\b.*\b(computer|pc|screen)\b", t):
        return request_approval("system_action", {"action": "lock"}, actor=actor)
    m = re.search(r"\b(kill|close|terminate|stop)\s+(?:the\s+)?([a-z][a-z0-9 ._-]{1,24})", t)
    if m:
        name = m.group(2).split()[0].removesuffix(".exe")
        if name in KNOWN_APP_NAMES or name.endswith("app") or name.endswith("application") or name.endswith("browser"):
            return request_approval("kill_process", {"name": name}, actor=actor)
    return None


def approve_action(action_id, actor="unknown"):
    """Execute a previously requested dangerous action. Returns the result dict."""
    with _approval_lock:
        item = _pending_approvals.pop(action_id, None)
    if item is None:
        _save_pending_approvals()
        _append_approval("approve_missing", action_id, None, None, None, actor)
        return {"error": "Approval expired or not found."}
    if item.get("expires", 0) < time.time():
        _save_pending_approvals()
        _append_approval("expired", action_id, item.get("name"),
                         item.get("args"), item.get("expires"), actor)
        return {"error": "Approval expired."}
    _save_pending_approvals()
    func = _TOOL_FUNCS.get(item["name"])
    if not func:
        _append_approval("approved_failed", action_id, item.get("name"),
                         item.get("args"), item.get("expires"), actor)
        return {"error": f"Unknown tool: {item['name']}"}
    try:
        result = func(**_filter_args(func, item["args"])) if item["args"] else func()
    except Exception as e:
        result = {"error": f"{item['name']} failed: {e}"}
    _append_approval("approved", action_id, item["name"], item["args"],
                     item.get("expires"), actor)
    log.info(f"[approved] {item['name']}({json.dumps(item['args'])}) -> {json.dumps(result)}")
    return result


def deny_action(action_id, actor="unknown"):
    """Cancel a previously requested dangerous action."""
    with _approval_lock:
        item = _pending_approvals.pop(action_id, None)
    if item is not None:
        _save_pending_approvals()
        _append_approval("denied", action_id, item.get("name"),
                         item.get("args"), item.get("expires"), actor)
        log.info(f"[denied] {item.get('name')}({json.dumps(item.get('args'))}) [id={action_id}]")
    return {"denied": True}


_load_pending_approvals()


# ==========================================
# TOOLS registry + dispatcher
# ==========================================
_CORE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current date and time.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_location",
            "description": "Get the approximate city and country location of this computer (IP geolocation).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_notes",
            "description": "Read back the user's saved notes (zee_notes.txt).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_processes",
            "description": "List the apps currently running on this computer, most CPU-hungry first.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a reminder that ZEE speaks out loud after a duration. Pass the duration in plain words, e.g. '2 minutes', '30 seconds' or '1 hour 30 minutes'. A bare number means minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration": {"type": "string", "description": "Time from now as words, e.g. '2 minutes'"},
                    "message": {"type": "string", "description": "What to remind about"},
                },
                "required": ["duration"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Get CPU, RAM and battery usage of this computer.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web and return the top results.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "The search query"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City name, e.g. Manila"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_note",
            "description": "Save a note to a local zee_notes.txt file.",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "The note text to save"}},
                "required": ["content"],
            },
        },
    },
]

_CORE_TOOL_FUNCS = {
    "get_time": tool_get_time,
    "get_location": tool_get_location,
    "read_notes": tool_read_notes,
    "list_processes": tool_list_processes,
    "set_reminder": tool_set_reminder,
    "system_info": tool_system_info,
    "web_search": tool_web_search,
    "get_weather": tool_get_weather,
    "create_note": tool_create_note,
}

if win_control is not None:
    _CORE_TOOL_FUNCS.update(win_control.WIN_TOOL_FUNCS)

_TOOL_FUNCS = _CORE_TOOL_FUNCS
TOOLS = _CORE_TOOLS + (win_control.WIN_TOOLS if win_control is not None else [])


def run_tool(name, args, actor="unknown"):
    """Execute a tool by name. Never raises; returns a result dict."""
    func = _TOOL_FUNCS.get(name)
    if not func:
        return {"error": f"Unknown tool: {name}"}
    if name in DANGEROUS_TOOLS:
        return request_approval(name, args, actor=actor)
    try:
        result = func(**_filter_args(func, args)) if args else func()
    except Exception as e:
        result = {"error": f"{name} failed: {e}"}
    log.info(f"[tool] {name}({json.dumps(args)}) -> {json.dumps(result)}")
    return result


# ==========================================
# BRAIN (Ollama, kept warm and capped)
# ==========================================
class AskResult:
    """Result of a conversation turn. approval is set when a dangerous action
    is waiting for the user to confirm or cancel it."""

    def __init__(self, text, approval=None):
        self.text = text
        self.approval = approval


def _chat(messages, tools, stream=False):
    kwargs = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "keep_alive": ZEE_KEEP_ALIVE,
        "options": {
            "num_predict": ZEE_MAX_TOKENS,
            "temperature": ZEE_TEMPERATURE,
            "num_ctx": ZEE_NUM_CTX,
        },
    }
    if tools:
        kwargs["tools"] = tools
    if stream:
        kwargs["stream"] = True
    return ollama.chat(**kwargs)


def warmup_model():
    """Preload the model so the first request isn't slow. Call in a thread."""
    try:
        _chat([{"role": "user", "content": "hi"}], tools=None)
        log.info(f"Model '{OLLAMA_MODEL}' warmed up.")
    except Exception as e:
        log.warning(f"Warmup failed (will retry on first request): {e}")


def _parse_inline_tool_call(content):
    """Some models emit a tool call as raw (sometimes malformed) JSON text
    instead of a structured tool_calls field. Detect it and return (name, args)
    or None. Only matches known tool names."""
    text = (content or "").strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None
    m = re.search(r'"name"\s*:\s*"([^"]+)"', text)
    if not m or m.group(1) not in _TOOL_FUNCS:
        return None
    args = {}
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            fn = data["function"] if isinstance(data.get("function"), dict) else data
            raw = fn.get("arguments") or data.get("parameters") or {}
            if isinstance(raw, dict):
                args = raw
    except (json.JSONDecodeError, AttributeError):
        pass
    return m.group(1), args


_GREETING_RE = re.compile(
    r"^(hi|hiya|hello|hey|yo|sup|wassup|what'?s up|good\s+(morning|afternoon|evening|night))[!.?\s]*(zee)?[!.?\s]*$",
    re.IGNORECASE,
)


def _quick_reply(text):
    """Canned reply for greeting-only messages, so the model doesn't call an
    unrelated tool (like get_time) in response to 'hey'."""
    m = _GREETING_RE.match((text or "").strip())
    if m:
        return f"{m.group(1).capitalize()}! How can I help you?"
    return None


# Tools the model must NOT run unless the user's message mentions a related
# keyword. Stops the model from acting on its own (e.g. changing the volume
# while answering an unrelated question).
_GUARDS = {
    "get_time": ["time", "date", "day", "clock", "today", "calendar"],
    "get_weather": ["weather", "temperature", "forecast", "rain", "snow", "hot", "cold", "degrees", "warm"],
    "set_volume": ["volume", "louder", "quieter", "mute", "unmute", "sound", "turn it up", "turn it down", "turn up", "turn down", "lower", "raise"],
    "adjust_volume": ["volume", "louder", "quieter", "mute", "unmute", "sound", "turn it up", "turn it down", "turn up", "turn down", "lower", "raise"],
    "media_control": ["play", "pause", "stop", "music", "song", "video", "media", "resume", "skip", "next", "previous"],
    "set_brightness": ["brightness", "brighter", "dimmer", "dim", "screen", "display", "turn it up", "turn it down"],
    "type_text": ["type", "write", "keyboard"],
    "kill_process": ["kill", "close", "end task", "exit", "terminate", "quit"],
    "system_action": ["shutdown", "shut down", "restart", "reboot", "sleep", "hibernate", "lock", "logoff", "sign out", "turn off", "power off"],
}


def _guard_blocked(name, user_text):
    """Return True if the model must not run this tool for this message."""
    keywords = _GUARDS.get(name)
    if not keywords:
        return False
    low = (user_text or "").lower()
    return not any(k in low for k in keywords)


def _guarded_run(name, args, user_text, actor="unknown"):
    """Run a tool, refusing when the user never asked for it."""
    if _guard_blocked(name, user_text):
        return {"error": f"Do not use the '{name}' tool: the user did not ask for it. "
                         "Never mention this tool again — answer the user's actual "
                         "question directly instead."}
    return run_tool(name, args, actor=actor)


def _unknown_tool_name(content):
    """If the model emitted JSON for a tool that doesn't exist, return its name."""
    text = (content or "").strip()
    if not text.startswith("{"):
        return None
    m = re.search(r'"name"\s*:\s*"([^"]+)"', text)
    if m and m.group(1) not in _TOOL_FUNCS:
        return m.group(1)
    return None


def ask_ollama(text, actor="voice"):
    """Query the local Ollama LLM, letting it call tools as needed. Raises on failure.

    Returns an AskResult. If the model requested a dangerous action, result.approval
    is a dict with keys: id, action, args, message — the caller should ask the user
    to approve (approve_action) or cancel (deny_action) it.
    """
    log.info(f"Querying Ollama model '{OLLAMA_MODEL}'...")
    _extract_facts(text)
    quick = _quick_reply(text)
    if quick:
        _remember(text, quick)
        return AskResult(quick)
    messages = [
        {"role": "system", "content": _system_prompt()},
    ]
    with _memory_lock:
        messages.extend(_history)
    messages.append({"role": "user", "content": text})
    tools = TOOLS
    tool_rounds = 0
    approval = None

    while True:
        try:
            response = _chat(messages, tools)
        except Exception as e:
            if tools:
                # Model probably doesn't support function calling — retry without tools.
                log.warning(f"Tool calling failed ({e}); retrying without tools.")
                tools = None
                continue
            raise

        if not response.message.tool_calls:
            inline = _parse_inline_tool_call(response.message.content)
            if inline is not None:
                name, args = inline
                if name in _TOOL_FUNCS:
                    tool_rounds += 1
                    if tool_rounds > 5:
                        raise RuntimeError("The model called tools too many times; giving up.")
                    messages.append({"role": "assistant", "content": response.message.content})
                    result = _guarded_run(name, args, text, actor)
                    if result.get("needs_approval"):
                        approval = result
                    messages.append({"role": "tool", "content": json.dumps(result)})
                    continue
            unknown = _unknown_tool_name(response.message.content)
            if unknown:
                tool_rounds += 1
                if tool_rounds > 5:
                    raise RuntimeError("The model called tools too many times; giving up.")
                messages.append({"role": "assistant", "content": response.message.content})
                messages.append({
                    "role": "tool",
                    "content": json.dumps({"error": f"Unknown tool '{unknown}'. Only use the "
                                               "tools listed; otherwise answer directly."}),
                })
                continue
            if approval is None:
                approval = maybe_request_approval(text, actor)
            _remember(text, response.message.content)
            return AskResult(response.message.content, approval=approval)

        tool_rounds += 1
        if tool_rounds > 5:
            raise RuntimeError("The model called tools too many times; giving up.")

        messages.append(response.message.model_dump(exclude_defaults=True))
        for call in response.message.tool_calls:
            name = call.function.name
            args = call.function.arguments or {}
            result = _guarded_run(name, args, text, actor)
            if result.get("needs_approval"):
                approval = result
            messages.append({"role": "tool", "content": json.dumps(result)})


class StreamAsk:
    """Streaming variant of ask_ollama.

    Iterate over it to receive text deltas as the model generates them.
    When iteration finishes, .full_text holds the complete reply and
    .approval is set if a dangerous action is awaiting confirmation.
    """

    def __init__(self, text, actor="web"):
        self._text = text
        self.actor = actor
        self.full_text = ""
        self.approval = None

    def __iter__(self):
        yield from _ask_stream(self)


def _ask_stream(sa):
    log.info(f"Querying Ollama model '{OLLAMA_MODEL}'...")
    _extract_facts(sa._text)
    quick = _quick_reply(sa._text)
    if quick:
        sa.full_text = quick
        _remember(sa._text, quick)
        yield quick
        return
    messages = [
        {"role": "system", "content": _system_prompt()},
    ]
    with _memory_lock:
        messages.extend(_history)
    messages.append({"role": "user", "content": sa._text})
    tools = TOOLS
    tool_rounds = 0

    while True:
        try:
            response = _chat(messages, tools, stream=True)
        except Exception as e:
            if tools:
                # Model probably doesn't support function calling — retry without tools.
                log.warning(f"Tool calling failed ({e}); retrying without tools.")
                tools = None
                continue
            raise

        buffered = []
        tool_calls = []
        for chunk in response:
            if chunk.message.content:
                buffered.append(chunk.message.content)
            if chunk.message.tool_calls:
                tool_calls.extend(chunk.message.tool_calls)

        if not tool_calls:
            # Maybe the model emitted a tool call as inline JSON text instead.
            content = "".join(buffered)
            inline = _parse_inline_tool_call(content)
            if inline is not None and inline[0] in _TOOL_FUNCS:
                tool_rounds += 1
                if tool_rounds > 5:
                    raise RuntimeError("The model called tools too many times; giving up.")
                messages.append({"role": "assistant", "content": content})
                name, args = inline
                result = _guarded_run(name, args, sa._text, sa.actor)
                if result.get("needs_approval"):
                    sa.approval = result
                messages.append({"role": "tool", "content": json.dumps(result)})
                continue
            unknown = _unknown_tool_name(content)
            if unknown:
                tool_rounds += 1
                if tool_rounds > 5:
                    raise RuntimeError("The model called tools too many times; giving up.")
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "tool",
                    "content": json.dumps({"error": f"Unknown tool '{unknown}'. Only use the "
                                               "tools listed; otherwise answer directly."}),
                })
                continue
            # Final answer — stream it.
            if sa.approval is None:
                sa.approval = maybe_request_approval(sa._text, sa.actor)
            for piece in buffered:
                sa.full_text += piece
                yield piece
            _remember(sa._text, sa.full_text)
            return

        # The model wants to call tools — execute them, then continue streaming.
        tool_rounds += 1
        if tool_rounds > 5:
            raise RuntimeError("The model called tools too many times; giving up.")

        messages.append({
            "role": "assistant",
            "content": "".join(buffered) or None,
            "tool_calls": [
                {"function": {"name": tc.function.name, "arguments": tc.function.arguments or {}}}
                for tc in tool_calls
            ],
        })
        for call in tool_calls:
            name = call.function.name
            args = call.function.arguments or {}
            result = _guarded_run(name, args, sa._text, sa.actor)
            if result.get("needs_approval"):
                sa.approval = result
            messages.append({"role": "tool", "content": json.dumps(result)})


# ==========================================
# ENVIRONMENT DIAGNOSTICS
# ==========================================
_CRITICAL_DEPS = {
    "ollama": "ollama",
    "edge_tts": "edge_tts",
    "pygame": "pygame",
}
_OPTIONAL_DEPS = {
    "psutil": "psutil",
    "flask": "flask",
    "vosk": "vosk",
    "sounddevice": "sounddevice",
    "Pillow": "PIL",
    "cryptography": "cryptography",
}


def check_ollama():
    """True if the Ollama server answers on the local API."""
    try:
        ollama.list()
        return True
    except Exception:
        return False


def ollama_probe():
    """Health probe string: 'ok', 'unavailable', or the underlying error."""
    try:
        ollama.list()
        return "ok"
    except Exception as e:
        msg = str(e).strip()
        return msg or "unavailable"


def doctor():
    """Diagnose the environment: platform, dependencies, Ollama, model, audio.

    Never raises. Returns a dict report; use --doctor on the CLI for a
    human-readable summary with a nonzero exit code when critical pieces
    are missing.
    """
    import platform as _platform
    rep = {
        "platform": {
            "system": _platform.system(),
            "release": _platform.release(),
            "machine": _platform.machine(),
            "python": sys.version.split()[0],
        },
    }
    deps = {}
    for label, mod in {**_CRITICAL_DEPS, **_OPTIONAL_DEPS}.items():
        try:
            importlib.import_module(mod)
            deps[label] = "ok"
        except Exception as e:
            # ImportError for missing packages, OSError for import-time
            # failures (e.g. sounddevice without a PortAudio system lib).
            deps[label] = f"UNAVAILABLE: {e}"
    if os.name == "nt":
        try:
            importlib.import_module("pycaw")
            deps["pycaw"] = "ok"
        except Exception as e:
            deps["pycaw"] = f"UNAVAILABLE: {e}"
    rep["dependencies"] = deps

    if check_ollama():
        rep["ollama"] = "ok"
        try:
            ollama.show(OLLAMA_MODEL)
            rep["model"] = "ok"
        except Exception as e:
            rep["model"] = f"ERROR: {e} (pull it with: ollama pull {OLLAMA_MODEL})"
    else:
        rep["ollama"] = "ERROR: Ollama server unreachable (start 'ollama serve')"
        rep["model"] = "skipped (Ollama unreachable)"

    if init_audio():
        rep["audio"] = "ok"
    else:
        rep["audio"] = "ERROR: audio device unavailable (TTS playback disabled)"

    model_dir = os.path.join(os.getcwd(), "model")
    rep["vosk_model"] = ("ok" if os.path.isdir(model_dir)
                         else "missing (only needed for zee.py voice loop)")
    rep["automation_enabled"] = automation_enabled()

    critical_ok = (
        all(v == "ok" for k, v in deps.items() if k in _CRITICAL_DEPS)
        and rep.get("ollama") == "ok"
        and rep.get("model") == "ok"
    )
    rep["healthy"] = critical_ok
    return rep


def doctor_summary(rep):
    """Format a doctor() report for console output."""
    lines = [f"Platform: {rep['platform']['system']} ({rep['platform']['machine']}), "
             f"Python {rep['platform']['python']}"]
    for label in sorted(rep["dependencies"]):
        lines.append(f"  dep  {label:15} {rep['dependencies'][label]}")
    lines.append(f"  ollama        {rep.get('ollama')}")
    lines.append(f"  model         {rep.get('model')}")
    lines.append(f"  audio         {rep.get('audio')}")
    lines.append(f"  vosk model    {rep.get('vosk_model')}")
    lines.append(f"  automation    {'enabled' if rep.get('automation_enabled') else 'DISABLED (ZEE_ALLOW_AUTOMATION=1)'}")
    return lines


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--doctor":
        report = doctor()
        for line in doctor_summary(report):
            print(line)
        print("HEALTHY" if report["healthy"] else "PROBLEMS DETECTED")
        sys.exit(0 if report["healthy"] else 1)
