import asyncio
import json
import os
import re
import tempfile
import threading
import urllib.parse
import urllib.request

import edge_tts
import ollama
import pygame

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
JARVIS_VOICE = os.getenv("JARVIS_VOICE", "en-US-ChristopherNeural")
JARVIS_RATE = os.getenv("JARVIS_RATE", "+10%")

SYSTEM_PROMPT = (
    "You are JARVIS, a helpful AI assistant. Keep your answers brief, "
    "conversational, and under 3 sentences. Do not use any special formatting "
    "or symbols like asterisks, as they will be read out loud by a text-to-speech engine. "
    "You have access to tools. Use them whenever they would help the user, "
    "then give a short summary of the result."
)

_play_lock = threading.Lock()


# ==========================================
# MOUTH (Text-to-Speech)
# ==========================================
def _ensure_mixer():
    if not pygame.mixer.get_init():
        pygame.mixer.init()


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

    By default this runs in a background thread so the caller is not blocked.
    Pass wait=True to block until playback finishes (e.g. from a single-threaded loop).
    """
    print(f"JARVIS: {text}")

    async def get_audio(path):
        communicate = edge_tts.Communicate(text, JARVIS_VOICE, rate=JARVIS_RATE)
        await communicate.save(path)

    def worker():
        try:
            fd, path = tempfile.mkstemp(suffix=".mp3", prefix="jarvis_")
            os.close(fd)
            try:
                asyncio.run(get_audio(path))
                _play_audio_file(path)
            finally:
                if os.path.exists(path):
                    os.remove(path)
        except Exception as e:
            print(f"Audio error: {e}")

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


def tool_get_time():
    from datetime import datetime
    return {"datetime": datetime.now().strftime("%A, %Y-%m-%d %H:%M:%S")}


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
}


def tool_open_app(app):
    if os.name != "nt":
        return {"error": "App launching is only supported on Windows."}
    app = (app or "").strip().lower()
    target = _KNOWN_APPS.get(app, app)
    if not re.fullmatch(r"[a-z0-9 ._-]+", target):
        return {"error": f"Unsupported application name: {app!r}"}
    code = os.system(f"start {target}")
    return {"opened": target, "command_exit_code": code}


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
            "name": "open_app",
            "description": "Open a desktop application (e.g. notepad, calculator, browser, paint, terminal).",
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
]

_TOOL_FUNCS = {
    "get_time": tool_get_time,
    "open_app": tool_open_app,
    "system_info": tool_system_info,
    "web_search": tool_web_search,
    "get_weather": tool_get_weather,
    "create_note": tool_create_note,
}


def run_tool(name, args):
    """Execute a tool by name. Never raises; returns a result dict."""
    func = _TOOL_FUNCS.get(name)
    if not func:
        return {"error": f"Unknown tool: {name}"}
    try:
        result = func(**args) if args else func()
    except Exception as e:
        result = {"error": f"{name} failed: {e}"}
    print(f"  [tool] {name}({json.dumps(args)}) -> {json.dumps(result)}")
    return result


# ==========================================
# BRAIN (Ollama with function calling)
# ==========================================
def _chat(messages, tools):
    kwargs = {"model": OLLAMA_MODEL, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    return ollama.chat(**kwargs)


def ask_ollama(text):
    """Query the local Ollama LLM, letting it call tools as needed. Raises on failure."""
    print(f"Querying Ollama model '{OLLAMA_MODEL}'...")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    tools = TOOLS
    tool_rounds = 0

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
            return response.message.content

        tool_rounds += 1
        if tool_rounds > 5:
            raise RuntimeError("The model called tools too many times; giving up.")

        messages.append(response.message.model_dump(exclude_defaults=True))
        for call in response.message.tool_calls:
            name = call.function.name
            args = call.function.arguments or {}
            messages.append({"role": "tool", "content": json.dumps(run_tool(name, args))})
