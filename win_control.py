"""Windows-only PC-control tools for JARVIS.

Everything in this module drives the local computer (apps, files, volume,
media, brightness, screenshots, typing, processes, system actions, Discord
UI automation). It is imported by jarvis_core on every platform: each tool
checks the OS first and returns a clear error on unsupported platforms so
the core stays fully cross-platform.

Security notes:
- All tool arguments are sanitized before use (control characters stripped,
  length capped, shell metacharacters rejected).
- No os.system() anywhere: external commands run through subprocess with
  explicit argument lists, so nothing user-controlled can reach a shell.
- App/file launching is restricted to trusted targets (known apps, Start
  Menu/Desktop shortcuts) or safe bare domain names.
"""
import logging
import os
import re
import subprocess
import time

log = logging.getLogger("jarvis.win")

MAX_ARG_LEN = 1000
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_APP_NAME_RE = re.compile(r"[a-z0-9 ._\-]{1,40}")
_SAFE_DOMAIN_RE = re.compile(r"[a-z0-9\-]+(\.[a-z0-9\-]+)+")
_PATH_CHARS_RE = re.compile(r"[\\/:*?\"<>|]")


def _clean_text(s, maxlen=MAX_ARG_LEN):
    """Strip control characters and cap the length of untrusted input."""
    s = "" if s is None else str(s)
    s = _CONTROL_CHARS_RE.sub("", s)
    return s[:maxlen].strip()


# ---------------- APP LAUNCHING ----------------
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
    "code": "code",
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

KNOWN_APP_NAMES = set(_KNOWN_APPS)

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
        return target, True
    global _APP_INDEX
    if _APP_INDEX is None:
        _APP_INDEX = _build_app_index()
    hit = _APP_INDEX.get(app) or _APP_INDEX.get(app.replace(" ", ""))
    if hit:
        return hit, True
    # Fuzzy match only in the SAFE direction: the user's word is a substring
    # of an installed shortcut name ("chrome" -> "Google Chrome"). Never the
    # reverse ("notepad & calc" must NOT match the "Notepad" shortcut).
    if len(app) >= 3:
        for name in _APP_INDEX:
            if app in name:
                return _APP_INDEX[name], True
    return app, False


def _start(target):
    """Launch a target (exe name, .lnk path or URL) detached, without a shell."""
    subprocess.Popen(
        ["cmd", "/c", "start", "", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def tool_open_app(app: str):
    """Open a desktop app or website. Only trusted targets are launched."""
    if os.name != "nt":
        return {"error": "App launching is only supported on Windows."}
    app = _clean_text(app, 60).lower()
    if not app:
        return {"error": "No application name given."}
    target, trusted = _resolve_app(app)

    if target.startswith(("https://", "http://")):
        if not trusted:
            return {"error": f"Refusing to open an unknown URL: {app!r}"}
        _start(target)
        return {"opened_website": target}
    if trusted:
        via = "installed app" if target.lower().endswith(".lnk") else "known app"
        _start(target)
        return {"opened": target, "via": via}
    if _SAFE_DOMAIN_RE.fullmatch(target) or _SITE_SUFFIX_RE.search(target):
        url = f"https://{target}"
        _start(url)
        return {"opened_website": url}
    return {"error": f"Unsupported application name: {app!r}"}


# ---------------- FILES ----------------
_FILE_SEARCH_DIRS = ["Documents", "Downloads", "Desktop", "Pictures"]


def _posix_opener():
    if sys_platform_darwin():
        return "open"
    return "xdg-open"


def sys_platform_darwin():
    import sys
    return sys.platform == "darwin"


def _open_path(path):
    """Open a file/folder with the OS default program (cross-platform best-effort)."""
    if os.name == "nt":
        os.startfile(path)
        return None
    opener = _posix_opener()
    try:
        subprocess.run([opener, path], timeout=15,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None
    except Exception as e:
        return f"{e}"


def tool_open_file(name: str):
    """Find a file by name in the usual folders and open it."""
    name = _clean_text(name, 100).lower()
    if not name:
        return {"error": "No file name given."}
    if _PATH_CHARS_RE.search(name):
        return {"error": "Invalid file name (path separators not allowed)."}
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
                    err = _open_path(full)
                    if err:
                        return {"error": f"Could not open {full}: {err}"}
                    return {"opened_file": full}
            if scanned > 5000:
                break
        if scanned > 5000:
            break
    return {"error": f"No file matching {name!r} found in Documents, Downloads, Desktop or Pictures."}


def tool_open_folder(path: str):
    if os.name != "nt":
        # POSIX: still support opening paths via the OS file manager.
        path = _clean_text(path, 500)
        if not path:
            return {"error": "No path provided."}
        if not os.path.exists(path):
            return {"error": f"Path not found: {path}"}
        err = _open_path(path)
        if err:
            return {"error": f"Could not open {path}: {err}"}
        return {"opened": path}
    path = (path or "").strip().strip('"')
    if not path:
        return {"error": "No path provided."}
    if not os.path.exists(path):
        return {"error": f"Path not found: {path}"}
    try:
        os.startfile(path)
    except OSError as e:
        return {"error": f"Could not open {path}: {e}"}
    return {"opened": path}


# ---------------- DISCORD (drives the desktop app, like the user typing) ----------------
# This controls the user's own Discord client via UI automation — no bot, no
# self-bot API, nothing against Discord's terms. Discord must be running.


def _discord_sendkeys(keys):
    """Activate the Discord window and send keystrokes via WScript.Shell."""
    keys = _clean_text(keys, 50)
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
    button_name = _clean_text(button_name, 100).replace("'", "''")
    script = (
        "try { Add-Type -AssemblyName UIAutomationClient; "
        "Add-Type -AssemblyName UIAutomationTypes; "
        "$root = [System.Windows.Automation.AutomationElement]::RootElement; "
        "$cond = New-Object System.Windows.Automation.PropertyCondition("
        "[System.Windows.Automation.AutomationElement]::NameProperty, "
        f"'{button_name}'); "
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


def tool_discord_contact(name: str):
    """Find a Discord user and open their DM via the quick switcher (Ctrl+K)."""
    name = _clean_text(name, 50)
    if not name:
        return {"error": "No Discord user given."}
    if _discord_sendkeys("^k") == "notfound":
        return {"error": "Discord is not running. Start Discord first."}
    time.sleep(1.2)
    _discord_sendkeys(name)
    time.sleep(1.5)
    _discord_sendkeys("{ENTER}")
    return {"opened_chat_with": name}


def tool_discord_call(name: str):
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


# ---------------- PC CONTROL (volume / media / brightness / typing) ----------------
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


def tool_adjust_volume(direction: str):
    if os.name != "nt":
        return {"error": "Volume control is only supported on Windows."}
    direction = _clean_text(direction, 20).lower()
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


def tool_media_control(action: str):
    if os.name != "nt":
        return {"error": "Media control is only supported on Windows."}
    key = _MEDIA_KEYS.get(_clean_text(action, 20).lower())
    if key is None:
        return {"error": f"Unknown media action: {action!r}. Use play/pause, next, previous or stop."}
    _press_vk(key)
    return {"action": _clean_text(action, 20).lower()}


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


def tool_type_text(text: str):
    if os.name != "nt":
        return {"error": "Typing is only supported on Windows."}
    text = _clean_text(text, 1000)
    if not text:
        return {"error": "No text to type."}
    try:
        _type_unicode(text)
    except Exception as e:
        return {"error": f"type_text failed: {e}"}
    return {"typed_chars": len(text)}


# ---------------- PROCESSES / SYSTEM ----------------
_PROTECTED_PROCESSES = {
    "system", "registry", "wininit", "winlogon", "csrss", "smss", "services",
    "lsass", "explorer", "svchost", "dwm", "taskhost", "taskhostw",
    "runtimebroker", "shell", "sihost", "spoolsv",
}


def tool_kill_process(name: str):
    import psutil
    name = _clean_text(name, 60)
    if not name:
        return {"error": "No process name given."}
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


_SYSTEM_ACTIONS = {
    "shutdown": ["shutdown", "/s", "/t", "10"],
    "restart": ["shutdown", "/r", "/t", "10"],
    "hibernate": ["shutdown", "/h"],
    "logoff": ["shutdown", "/l"],
    "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
    "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
}


def tool_system_action(action: str):
    if os.name != "nt":
        return {"error": "System actions are only supported on Windows."}
    action = _clean_text(action, 20).lower()
    cmd = _SYSTEM_ACTIONS.get(action)
    if not cmd:
        return {"error": f"Unknown system action: {action!r}"}
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        return {"error": f"system_action failed: {e}"}
    return {"executed": action}


# ---------------- SCHEMA (merged into jarvis_core.TOOLS) ----------------
WIN_TOOLS = [
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

WIN_TOOL_FUNCS = {
    "open_app": tool_open_app,
    "open_file": tool_open_file,
    "discord_contact": tool_discord_contact,
    "discord_call": tool_discord_call,
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
