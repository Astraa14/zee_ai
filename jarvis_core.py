import asyncio
import inspect
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

import edge_tts
import ollama
import pygame

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:latest")
JARVIS_VOICE = os.getenv("JARVIS_VOICE", "en-US-ChristopherNeural")
JARVIS_RATE = os.getenv("JARVIS_RATE", "+10%")
JARVIS_MAX_TOKENS = int(os.getenv("JARVIS_MAX_TOKENS", "150"))
JARVIS_TEMPERATURE = float(os.getenv("JARVIS_TEMPERATURE", "0.7"))
JARVIS_KEEP_ALIVE = os.getenv("JARVIS_KEEP_ALIVE", "30m")
JARVIS_NUM_CTX = int(os.getenv("JARVIS_NUM_CTX", "4096"))

SYSTEM_PROMPT = (
    "You are JARVIS, a helpful AI assistant. Keep your answers brief, "
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
_MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.json")
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
    except OSError:
        pass


def _extract_facts(text):
    """Learn facts (location, name) from what the user said."""
    low = (text or "").lower()
    m = re.search(
        r"\b(?:i'?m|i am|i live|i'?m living|i'?m based|am)\s+"
        r"(?:actually|currently|right now|now|here)?\s*(?:at|in|from|near)\s+"
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
    """Keep the last few turns so JARVIS can follow conversations."""
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
        print("Audio ready.")
        return True
    except Exception as e:
        print(f"Audio init failed: {e}")
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
    print(f"JARVIS: {text}")

    async def generate(path, ready):
        communicate = edge_tts.Communicate(text, JARVIS_VOICE, rate=JARVIS_RATE)
        with open(path, "wb") as f:
            stream = communicate.stream()
            async for chunk in stream:
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
            f.flush()
        ready.set()

    def worker():
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="jarvis_")
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
            print(f"Audio error: {e}")
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
# HANDS (Tools / Actions)
# ==========================================
def _fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _filter_args(func, args):
    """Drop arguments the tool function doesn't accept (models can pass junk)."""
    sig = inspect.signature(func)
    cleaned = {}
    for k, v in (args or {}).items():
        if k not in sig.parameters:
            continue
        if isinstance(v, dict) and ({"type", "value", "description"} & set(v.keys())):
            for key in ("value", "description", "type"):
                if key in v and v[key] not in ("string", "number", "integer", "boolean", "object", "array"):
                    v = v[key]
                    break
            else:
                v = ""
        cleaned[k] = v
    return cleaned


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


_KNOWN_APPS = {
    "notepad": "notepad",
    "calculator": "calc",
    "browser": "chrome",
    "chrome": "chrome",
    "edge": "msedge",
    "file explorer": "explorer",
    "explorer": "explorer",
    "terminal": "cmd",
    "command prompt": "cmd",
    "powershell": "powershell",
    "visual studio code": "code",
    "vscode": "code",
    "paint": "mspaint",
    "word": "winword",
    "excel": "excel",
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
    "github": "https://github.com",
    "maps": "https://maps.google.com",
    "maps google": "https://maps.google.com",
    "whatsapp": "https://web.whatsapp.com",
}

_SITE_SUFFIX_RE = re.compile(r"\.(com|net|org|io|tv|me|co|ph|app|ai)$")

_APP_INDEX = None


def _build_app_index():
    """Scan Start Menu + Desktop shortcuts: {lowercase name: .lnk path}."""
    import glob
    index = {}
    dirs = [
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
        os.path.join(os.environ.get("PUBLIC", ""), "Desktop"),
    ]
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for lnk in glob.glob(os.path.join(d, "**", "*.lnk"), recursive=True):
            name = os.path.splitext(os.path.basename(lnk))[0].lower()
            index.setdefault(name, lnk)
    return index


def _resolve_app(app):
    """Map an app name to a launch target: known apps, installed shortcuts, or the name itself."""
    target = _KNOWN_APPS.get(app) or _KNOWN_APPS.get(app.replace(" ", ""))
    if target:
        return target
    global _APP_INDEX
    if _APP_INDEX is None:
        _APP_INDEX = _build_app_index()
    hit = _APP_INDEX.get(app) or _APP_INDEX.get(app.replace(" ", ""))
    if hit:
        return hit
    for name in _APP_INDEX:
        if len(app) >= 5 and (app in name or name in app):
            return _APP_INDEX[name]
    return app


def tool_open_app(app):
    if os.name != "nt":
        return {"error": "App launching is only supported on Windows."}
    app = (app or "").strip().lower()
    target = _resolve_app(app)
    if target.startswith("https://") or target.startswith("http://"):
        os.system(f'start "" "{target}"')
        return {"opened_website": target}
    if target.lower().endswith(".lnk"):
        os.system(f'start "" "{target}"')
        return {"opened": target, "via": "installed app"}
    if re.fullmatch(r"[a-z0-9 ._-]+", target):
        code = os.system(f"start {target}")
        return {"opened": target, "command_exit_code": code}
    if "." in target and not target.startswith(("cmd", "exe", "msi")) or _SITE_SUFFIX_RE.search(target):
        os.system(f'start "" "https://{target}"')
        return {"opened_website": f"https://{target}"}
    return {"error": f"Unsupported application name: {app!r}"}


# ---------------- DISCORD (drives the desktop app, like the user typing) ----------------
# This controls the user's own Discord client via UI automation — no bot, no
# self-bot API, nothing against Discord's terms. Discord must be running.


def _discord_sendkeys(keys):
    """Activate the Discord window and send keystrokes via WScript.Shell."""
    safe = keys.replace("'", "''")
    script = (
        "$p = Get-Process Discord -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1; "
        "if (-not $p) { 'notfound' } else { "
        "$ws = New-Object -ComObject WScript.Shell; "
        "$ok = $ws.AppActivate($p.Id); "
        "Start-Sleep -Milliseconds 600; "
        f"$ws.SendKeys('{safe}'); "
        "'ok' }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip()
    except Exception as e:
        return f"error: {e}"


def _uia_click(button_name):
    """Click a Discord button by its accessibility name via UI Automation."""
    script = (
        "try { Add-Type -AssemblyName UIAutomationClient; "
        "Add-Type -AssemblyName UIAutomationTypes; "
        "$root = [System.Windows.Automation.AutomationElement]::RootElement; "
        "$cond = New-Object System.Windows.Automation.PropertyCondition("
        "[System.Windows.Automation.AutomationElement]::NameProperty, " +
        f"'{button_name.replace(chr(39), chr(39) * 2)}'); "
        "$el = $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond); "
        "if ($el) { $p = $el.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern); "
        "$p.Invoke(); 'clicked' } else { 'notfound' } } catch { 'notfound' }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
        )
        return out.stdout.strip() == "clicked"
    except Exception:
        return False


def tool_discord_contact(name):
    """Find a Discord user and open their DM via the quick switcher (Ctrl+K)."""
    name = (name or "").strip()
    if not name:
        return {"error": "No Discord user given."}
    if _discord_sendkeys("^k") == "notfound":
        return {"error": "Discord is not running. Start Discord first."}
    time.sleep(1.2)
    _discord_sendkeys(name)
    time.sleep(1.5)
    _discord_sendkeys("{ENTER}")
    return {"opened_chat_with": name}


def tool_discord_call(name):
    """Open a DM with a Discord user and start a voice call."""
    res = tool_discord_contact(name)
    if "error" in res:
        return res
    time.sleep(1.5)
    if _uia_click("Start Voice Call"):
        return {"calling": name, "via": "call button"}
    hotkey = os.getenv("JARVIS_DISCORD_CALL_KEY", "^`")
    _discord_sendkeys(hotkey)
    return {"calling": name,
            "note": "If the call did not start, add the 'Start/Stop Voice Call' "
                    "keybind in Discord settings and set JARVIS_DISCORD_CALL_KEY."}


_DURATION_RE = re.compile(
    r"(?=\d)"
    r"(?:(?P<h>\d+(?:\.\d+)?)\s*(?:hours?|hrs?)\s*)?"
    r"(?:(?P<m>\d+(?:\.\d+)?)\s*(?:minutes?|mins?)\s*)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)\s*(?:seconds?|secs?))?",
    re.IGNORECASE,
)


def _duration_to_minutes(duration):
    """Parse '2 minutes', '1 hour 30 minutes', '45 seconds' into minutes."""
    d = (duration or "").strip().lower()
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


def tool_set_reminder(duration, message=None):
    """Speak a reminder after the given duration (e.g. '2 minutes')."""
    minutes = _duration_to_minutes(duration)
    if minutes is None or minutes <= 0:
        return {"error": f"Could not understand the duration: {duration!r}"}
    msg = (message or "Your reminder is due.").strip()
    def fire():
        time.sleep(minutes * 60)
        speak(f"Reminder: {msg}")
    threading.Thread(target=fire, daemon=True).start()
    return {"reminder_set_for_minutes": minutes, "message": msg}


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


_FILE_SEARCH_DIRS = ["Documents", "Downloads", "Desktop", "Pictures"]


def tool_open_file(name):
    """Find a file by name in the usual folders and open it."""
    name = (name or "").strip()
    if not name:
        return {"error": "No file name given."}
    name = name.lower()
    bases = [os.path.expanduser("~")]
    for key in _FILE_SEARCH_DIRS:
        d = os.path.join(os.path.expanduser("~"), key)
        if os.path.isdir(d):
            bases.append(d)
    scanned = 0
    for base in bases:
        for root, dirs, files in os.walk(base):
            dirs[:] = dirs[:25]
            for f in files[:300]:
                scanned += 1
                if scanned > 5000:
                    break
                if name in f.lower():
                    full = os.path.join(root, f)
                    try:
                        os.startfile(full)
                    except OSError as e:
                        return {"error": f"Could not open {full}: {e}"}
                    return {"opened_file": full}
            if scanned > 5000:
                break
        if scanned > 5000:
            break
    return {"error": f"No file matching {name!r} found in Documents, Downloads, Desktop or Pictures."}


def tool_read_notes():
    """Read back the last few saved notes."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes.txt")
    try:
        with open(path, encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
    except OSError:
        return {"notes": []}
    return {"notes": lines[-10:]}


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


def tool_web_search(query):
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


def tool_get_weather(city):
    city = (city or "").strip()
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


def tool_create_note(content):
    notes_file = os.path.join(os.getcwd(), "notes.txt")
    from datetime import datetime
    with open(notes_file, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {content}\n")
    return {"saved_to": notes_file}


# ==========================================
# HANDS (PC Control)
# ==========================================
def _press_vk(vk):
    """Press and release a virtual-key code using keybd_event."""
    import ctypes
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


def _type_unicode(text):
    """Type arbitrary text into the focused window via SendInput (KEYEVENTF_UNICODE)."""
    import ctypes
    from ctypes import wintypes

    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD = 1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

    def _input(ch, up):
        ki = KEYBDINPUT()
        ki.wScan = ord(ch)
        ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        return INPUT(type=INPUT_KEYBOARD, u=_INPUT_UNION(ki=ki))

    for ch in text:
        for up in (False, True):
            ctypes.windll.user32.SendInput(1, ctypes.byref(_input(ch, up)), ctypes.sizeof(INPUT))


def _endpoint_volume():
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def tool_set_volume(percent):
    if os.name != "nt":
        return {"error": "Volume control is only supported on Windows."}
    try:
        pct = float(percent)
    except (TypeError, ValueError):
        return {"error": f"Invalid volume: {percent!r}"}
    pct = max(0, min(100, pct))
    try:
        _endpoint_volume().SetMasterVolumeLevelScalar(pct / 100, None)
    except Exception as e:
        return {"error": f"set_volume failed: {e}"}
    return {"volume_percent": pct}


def tool_adjust_volume(direction):
    if os.name != "nt":
        return {"error": "Volume control is only supported on Windows."}
    direction = (direction or "").strip().lower()
    try:
        if direction == "mute":
            _endpoint_volume().SetMute(1, None)
            return {"adjusted": "mute"}
        if direction == "unmute":
            _endpoint_volume().SetMute(0, None)
            return {"adjusted": "unmute"}
        if direction == "up":
            _press_vk(0xAF)  # VK_VOLUME_UP
            return {"adjusted": "up"}
        if direction == "down":
            _press_vk(0xAE)  # VK_VOLUME_DOWN
            return {"adjusted": "down"}
        return {"error": f"Unknown direction: {direction!r}. Use up, down, mute or unmute."}
    except Exception as e:
        return {"error": f"adjust_volume failed: {e}"}


_MEDIA_KEYS = {
    "play/pause": 0xB3,
    "playpause": 0xB3,
    "play": 0xB3,
    "pause": 0xB3,
    "next": 0xB0,
    "next track": 0xB0,
    "previous": 0xB1,
    "previous track": 0xB1,
    "prev": 0xB1,
    "stop": 0xB2,
}


def tool_media_control(action):
    if os.name != "nt":
        return {"error": "Media control is only supported on Windows."}
    key = _MEDIA_KEYS.get((action or "").strip().lower())
    if key is None:
        return {"error": f"Unknown media action: {action!r}. Use play/pause, next, previous or stop."}
    _press_vk(key)
    return {"action": (action or "").strip().lower()}


def tool_set_brightness(percent):
    if os.name != "nt":
        return {"error": "Brightness control is only supported on Windows."}
    try:
        pct = int(percent)
    except (TypeError, ValueError):
        return {"error": f"Invalid brightness: {percent!r}"}
    pct = max(0, min(100, pct))
    cmd = ["powershell", "-NoProfile", "-Command",
           f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{pct})"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        return {"error": f"set_brightness failed: {e}"}
    if result.returncode != 0:
        return {"error": "Brightness not supported on this system."}
    return {"brightness_percent": pct}


def tool_screenshot():
    from datetime import datetime
    from PIL import ImageGrab
    folder = os.path.join(os.getcwd(), "screenshots")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    try:
        ImageGrab.grab().save(path)
    except Exception as e:
        return {"error": f"screenshot failed: {e}"}
    return {"saved_to": path}


def tool_type_text(text):
    if os.name != "nt":
        return {"error": "Typing is only supported on Windows."}
    text = text or ""
    if not text:
        return {"error": "No text to type."}
    try:
        _type_unicode(text)
    except Exception as e:
        return {"error": f"type_text failed: {e}"}
    return {"typed_chars": len(text)}


def tool_open_folder(path):
    if os.name != "nt":
        return {"error": "Opening folders is only supported on Windows."}
    path = (path or "").strip().strip('"')
    if not path:
        return {"error": "No path provided."}
    if not os.path.exists(path):
        return {"error": f"Path not found: {path}"}
    os.startfile(path)
    return {"opened": path}


_PROTECTED_PROCESSES = {
    "system", "registry", "wininit", "winlogon", "csrss", "smss", "services",
    "lsass", "explorer", "svchost", "dwm", "taskhost", "taskhostw",
    "runtimebroker", "shell", "sihost", "spoolsv",
}


def tool_kill_process(name):
    import psutil
    name = (name or "").strip()
    base = name.lower().removesuffix(".exe")
    if base in _PROTECTED_PROCESSES:
        return {"error": f"Refusing to kill protected system process: {name}"}
    killed = []
    for proc in psutil.process_iter(["name"]):
        pname = (proc.info["name"] or "").lower()
        if pname == base or pname == base + ".exe":
            try:
                proc.terminate()
                killed.append(pname)
            except Exception as e:
                return {"error": f"Could not kill {name}: {e}"}
    if not killed:
        return {"error": f"Process not found: {name}"}
    return {"killed": killed}


def tool_system_action(action):
    if os.name != "nt":
        return {"error": "System actions are only supported on Windows."}
    action = (action or "").strip().lower()
    cmds = {
        "shutdown": "shutdown /s /t 10",
        "restart": "shutdown /r /t 10",
        "hibernate": "shutdown /h",
        "logoff": "shutdown /l",
        "lock": "rundll32.exe user32.dll,LockWorkStation",
        "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
    }
    if action not in cmds:
        return {"error": f"Unknown system action: {action!r}"}
    os.system(cmds[action])
    return {"executed": action}


TOOLS = [
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
            "name": "open_app",
            "description": "Open a desktop application (e.g. notepad, calculator, browser, paint, terminal) or a website (e.g. youtube, google, gmail, facebook, netflix).",
            "parameters": {
                "type": "object",
                "properties": {"app": {"type": "string", "description": "Name of the application to open"}},
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_file",
            "description": "Find a file by name in Documents, Downloads, Desktop or Pictures and open it (e.g. resume.pdf).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "File name or part of it"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_notes",
            "description": "Read back the user's saved notes (notes.txt).",
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
            "description": "Set a reminder that JARVIS speaks out loud after a duration. Pass the duration in plain words, e.g. '2 minutes', '30 seconds' or '1 hour 30 minutes'. A bare number means minutes.",
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
            "name": "discord_contact",
            "description": "Find a Discord user and open their direct message chat. Requires the Discord desktop app to be running.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "The Discord username to find"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discord_call",
            "description": "Find a Discord user and start a voice call with them. Requires the Discord desktop app to be running.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "The Discord username to call"}},
                "required": ["name"],
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
            "description": "Save a note to a local notes.txt file.",
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string", "description": "The note text to save"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": "Set the master volume to a percentage between 0 and 100.",
            "parameters": {
                "type": "object",
                "properties": {"percent": {"type": "number", "description": "Volume percentage 0-100"}},
                "required": ["percent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "adjust_volume",
            "description": "Adjust volume up, down, mute or unmute.",
            "parameters": {
                "type": "object",
                "properties": {"direction": {"type": "string", "description": "up, down, mute or unmute"}},
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": "Control the currently playing media: play/pause, next, previous or stop.",
            "parameters": {
                "type": "object",
                "properties": {"action": {"type": "string", "description": "play/pause, next, previous or stop"}},
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_brightness",
            "description": "Set the screen brightness to a percentage between 0 and 100.",
            "parameters": {
                "type": "object",
                "properties": {"percent": {"type": "number", "description": "Brightness percentage 0-100"}},
                "required": ["percent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Take a screenshot of the screen and save it to the screenshots folder.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the currently focused window.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "The text to type"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_folder",
            "description": "Open a folder or file path in File Explorer.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "The folder or file path to open"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_process",
            "description": "Terminate a running application by its process name, e.g. notepad or chrome. This is dangerous and requires user approval.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Process name without .exe, e.g. notepad"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_action",
            "description": "Perform a system action: shutdown, restart, sleep, hibernate, logoff or lock the computer. This is dangerous and requires user approval.",
            "parameters": {
                "type": "object",
                "properties": {"action": {"type": "string", "description": "shutdown, restart, sleep, hibernate, logoff or lock"}},
                "required": ["action"],
            },
        },
    },
]

_TOOL_FUNCS = {
    "get_time": tool_get_time,
    "get_location": tool_get_location,
    "open_app": tool_open_app,
    "open_file": tool_open_file,
    "read_notes": tool_read_notes,
    "list_processes": tool_list_processes,
    "set_reminder": tool_set_reminder,
    "discord_contact": tool_discord_contact,
    "discord_call": tool_discord_call,
    "system_info": tool_system_info,
    "web_search": tool_web_search,
    "get_weather": tool_get_weather,
    "create_note": tool_create_note,
    "set_volume": tool_set_volume,
    "adjust_volume": tool_adjust_volume,
    "media_control": tool_media_control,
    "set_brightness": tool_set_brightness,
    "screenshot": tool_screenshot,
    "type_text": tool_type_text,
    "open_folder": tool_open_folder,
    "kill_process": tool_kill_process,
    "system_action": tool_system_action,
}

# Dangerous tools never run directly — they require explicit user approval.
DANGEROUS_TOOLS = {"kill_process", "system_action"}

_pending_approvals = {}
_approval_lock = threading.Lock()


def request_approval(name, args):
    """Register a dangerous action for later approval and return a needs_approval result."""
    aid = uuid.uuid4().hex[:8]
    with _approval_lock:
        _pending_approvals[aid] = {
            "name": name,
            "args": args,
            "expires": time.time() + 120,
        }
    return {
        "needs_approval": True,
        "id": aid,
        "action": name,
        "args": args,
        "message": f"Do you want me to run {name}?",
    }


def maybe_request_approval(user_text):
    """Safety net: if the user asked for a dangerous action but the model didn't
    call a tool, register the approval anyway so the flow always works."""
    t = (user_text or "").lower()
    if re.search(r"\b(shut\s?down|power\s?off|turn\s?off)\b.*\b(computer|pc|machine|laptop|system)\b", t) or \
       re.fullmatch(r"\s*(shut\s?down|power\s?off)\s*[.!]*\s*", t):
        return request_approval("system_action", {"action": "shutdown"})
    if re.search(r"\b(restart|reboot)\b", t):
        return request_approval("system_action", {"action": "restart"})
    if re.search(r"\b(hibernate|sleep)\b.*\b(computer|pc|system|laptop)\b", t):
        return request_approval("system_action", {"action": "sleep"})
    if re.search(r"\blog\s?off\b|\bsign\s?out\b", t):
        return request_approval("system_action", {"action": "logoff"})
    if re.search(r"\block\b.*\b(computer|pc|screen)\b", t):
        return request_approval("system_action", {"action": "lock"})
    m = re.search(r"\b(kill|close|terminate|stop)\s+(?:the\s+)?([a-z][a-z0-9 ._-]{1,24})", t)
    if m:
        name = m.group(2).split()[0].removesuffix(".exe")
        if name in _KNOWN_APPS or name.endswith("app") or name.endswith("application") or name.endswith("browser"):
            return request_approval("kill_process", {"name": name})
    return None


def approve_action(action_id):
    """Execute a previously requested dangerous action. Returns the result dict."""
    with _approval_lock:
        item = _pending_approvals.pop(action_id, None)
    if not item:
        return {"error": "Approval expired or not found."}
    func = _TOOL_FUNCS[item["name"]]
    try:
        result = func(**_filter_args(func, item["args"])) if item["args"] else func()
    except Exception as e:
        result = {"error": f"{item['name']} failed: {e}"}
    print(f"  [approved] {item['name']}({json.dumps(item['args'])}) -> {json.dumps(result)}")
    return result


def deny_action(action_id):
    """Cancel a previously requested dangerous action."""
    with _approval_lock:
        _pending_approvals.pop(action_id, None)
    return {"denied": True}


def run_tool(name, args):
    """Execute a tool by name. Never raises; returns a result dict."""
    func = _TOOL_FUNCS.get(name)
    if not func:
        return {"error": f"Unknown tool: {name}"}
    if name in DANGEROUS_TOOLS:
        return request_approval(name, args)
    try:
        result = func(**_filter_args(func, args)) if args else func()
    except Exception as e:
        result = {"error": f"{name} failed: {e}"}
    print(f"  [tool] {name}({json.dumps(args)}) -> {json.dumps(result)}")
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
        "keep_alive": JARVIS_KEEP_ALIVE,
        "options": {
            "num_predict": JARVIS_MAX_TOKENS,
            "temperature": JARVIS_TEMPERATURE,
            "num_ctx": JARVIS_NUM_CTX,
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
        print(f"Model '{OLLAMA_MODEL}' warmed up.")
    except Exception as e:
        print(f"Warmup failed (will retry on first request): {e}")


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
    r"^(hi|hiya|hello|hey|yo|sup|wassup|what'?s up|good\s+(morning|afternoon|evening|night))[!.?\s]*(jarvis)?[!.?\s]*$",
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


def _guarded_run(name, args, user_text):
    """Run a tool, refusing when the user never asked for it."""
    if _guard_blocked(name, user_text):
        return {"error": f"Do not use the '{name}' tool: the user did not ask for it. "
                         "Never mention this tool again — answer the user's actual "
                         "question directly instead."}
    return run_tool(name, args)


def _unknown_tool_name(content):
    """If the model emitted JSON for a tool that doesn't exist, return its name."""
    text = (content or "").strip()
    if not text.startswith("{"):
        return None
    m = re.search(r'"name"\s*:\s*"([^"]+)"', text)
    if m and m.group(1) not in _TOOL_FUNCS:
        return m.group(1)
    return None


def ask_ollama(text):
    """Query the local Ollama LLM, letting it call tools as needed. Raises on failure.

    Returns an AskResult. If the model requested a dangerous action, result.approval
    is a dict with keys: id, action, args, message — the caller should ask the user
    to approve (approve_action) or cancel (deny_action) it.
    """
    print(f"Querying Ollama model '{OLLAMA_MODEL}'...")
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
                print(f"Tool calling failed ({e}); retrying without tools.")
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
                    result = _guarded_run(name, args, text)
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
                approval = maybe_request_approval(text)
            _remember(text, response.message.content)
            return AskResult(response.message.content, approval=approval)

        tool_rounds += 1
        if tool_rounds > 5:
            raise RuntimeError("The model called tools too many times; giving up.")

        messages.append(response.message.model_dump(exclude_defaults=True))
        for call in response.message.tool_calls:
            name = call.function.name
            args = call.function.arguments or {}
            result = _guarded_run(name, args, text)
            if result.get("needs_approval"):
                approval = result
            messages.append({"role": "tool", "content": json.dumps(result)})


class StreamAsk:
    """Streaming variant of ask_ollama.

    Iterate over it to receive text deltas as the model generates them.
    When iteration finishes, .full_text holds the complete reply and
    .approval is set if a dangerous action is awaiting confirmation.
    """

    def __init__(self, text):
        self._text = text
        self.full_text = ""
        self.approval = None

    def __iter__(self):
        yield from _ask_stream(self)


def _ask_stream(sa):
    print(f"Querying Ollama model '{OLLAMA_MODEL}'...")
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
                print(f"Tool calling failed ({e}); retrying without tools.")
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
                result = _guarded_run(name, args, sa._text)
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
                sa.approval = maybe_request_approval(sa._text)
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
            result = _guarded_run(name, args, sa._text)
            if result.get("needs_approval"):
                sa.approval = result
            messages.append({"role": "tool", "content": json.dumps(result)})
