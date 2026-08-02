# JARVIS — Local Voice Assistant

A lightweight JARVIS-style assistant that runs **fully offline for speech** and uses a
**local LLM (Ollama)** for its brain. It ships with two interfaces:

1. **Web UI** (`app.py`) — a Stark-style browser interface with an animated arc
   reactor. Use your browser's microphone or the text box, and JARVIS answers
   out loud.
2. **Voice loop** (`jarvis.py`) — hands-free desktop mode: wake JARVIS by saying
   its name, give voice commands, and it replies through your speakers.

## Features

- 🎙️ Browser voice input (Chrome/Edge Web Speech API) *or* offline desktop
  microphone (Vosk + sounddevice)
- 🧠 Local AI brain via **Ollama** (no cloud API needed)
- ⚡ **Fast replies** — model kept warm in RAM, answers streamed to the screen
  token-by-token, audio starts playing while it's still downloading
- 🛠️ **Function calling** — JARVIS can take real actions (see [Tools](#tools))
- 🖥️ **PC control** — volume, media, brightness, screenshots, typing, folders
  (see [PC control](#pc-control))
- 🛡️ **Approval gate** — destructive actions always ask for your OK first
- 🗣️ Lifelike TTS with **edge-tts** (default: `en-US-ChristopherNeural`)
- 💬 Text fallback so it works even without a microphone
- ⌨️ Voice commands: open Notepad, Calculator, browser
- 🔧 Configurable model / voice / speed via environment variables

## Requirements

- **Python 3.9+**
- **Ollama** installed and running locally, with a **tool-capable** model.
  The default is `llama3.2` — fast and tool-capable. Prefer `llama3.1:8b`
  (set `OLLAMA_MODEL=llama3.1:8b`) for more accurate answers; `llama3` works
  but skips tools:

  ```sh
  ollama serve
  ollama pull llama3.2     # default — fastest tool-capable option
  ```

- For the desktop voice loop only: a working microphone and a **Vosk model**
  (see below).

## Installation

```sh
# 1. Clone / extract the project and enter the folder
cd zee

# 2. (Recommended) Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt
```

### Download the Vosk model (for the voice loop only)

```sh
# Download an English model from https://alphacephei.com/vosk/models
# (e.g. vosk-model-small-en-us-0.15 ~40 MB, or vosk-model-en-us-0.22 ~1.8 GB for better accuracy)
# Unpack it as a folder named "model" in the project root:
```

```
zee/
├── app.py
├── jarvis.py
└── model/          <- the unpacked Vosk model goes here
```

The Web UI does **not** need the Vosk model.

## Usage

### Web UI

```sh
python app.py
```

JARVIS serves **HTTPS by default** (self-signed cert, generated automatically)
so the microphone works from any device. Open **`https://192.168.1.4:5000`**
(use your own IP), click **Advanced → Proceed to site** once, then click
**Activate Microphone** to talk, or type a command and press Enter.

> Plain HTTP (e.g. for localhost only): `set JARVIS_HTTPS=0` then
> `python app.py`, open `http://localhost:5000`.
>
> `set FLASK_DEBUG=1` re-enables the auto-reloader for development.

#### Using the microphone from another device (or a LAN IP)

Chrome only allows microphone access on secure origins (`https://` or
`http://localhost`). HTTPS is already the default, so just use
`https://YOUR_IP:5000`. If the page hangs instead of showing a certificate
warning, an old server instance is still running — stop it (or use
`start_jarvis.bat`, which handles that automatically).

### Desktop voice loop

```sh
python jarvis.py
```

Say **"JARVIS"** to get its attention, then:

- `jarvis open notepad` / `jarvis open calculator` / `jarvis open browser`
- `jarvis what time is it` — any conversation goes to the Ollama brain
- `jarvis stop` / `exit` / `shutdown` — quit

## Tools

JARVIS can call tools through the LLM's function-calling support. It will use
them automatically when they help, then summarise the result. Requires a
tool-capable model (see [Requirements](#requirements)).

| Tool             | What it does                                        |
| ---------------- | --------------------------------------------------- |
| `get_time`       | Current date and time                               |
| `get_location`   | Where the PC is (IP geolocation, or your stated location) |
| `open_app`       | Launch **any installed app** (found via Start Menu/Desktop shortcuts: discord, steam, spotify, chrome…) or a website (youtube, google, gmail…) |
| `open_file`      | Find a file by name (Documents/Downloads/Desktop/Pictures) and open it |
| `read_notes`     | Read back your saved notes (`notes.txt`)            |
| `list_processes` | What's running right now, most CPU-hungry first     |
| `set_reminder`   | Speak a reminder after a duration ("remind me in 20 minutes…") |
| `discord_contact`| Find a Discord user and open their DM (desktop app) |
| `discord_call`   | Find a Discord user and start a voice call (desktop app) |
| `system_info`    | CPU, RAM and battery usage                          |
| `web_search`     | Web search (Wikipedia API, no key needed)           |
| `get_weather`    | Current weather for any city (Open-Meteo, no key)   |
| `create_note`    | Append a note to `notes.txt`                        |

If your model doesn't support tools, JARVIS automatically falls back to plain
chat — the tools are simply skipped. `web_search` and `get_weather` need an
internet connection.

## PC control

> Windows only. Requires `pycaw` (volume) and `Pillow` (screenshots), which are
> in `requirements.txt`.

| Tool             | What it does                                              |
| ---------------- | --------------------------------------------------------- |
| `set_volume`     | Set master volume to a percentage (0–100)                 |
| `adjust_volume`  | Volume up / down / mute / unmute                          |
| `media_control`  | Play/pause, next, previous, stop the active media         |
| `set_brightness` | Screen brightness (not supported on all monitors)         |
| `screenshot`     | Save a screenshot to `screenshots/`                       |
| `type_text`      | Type text into the focused window                         |
| `open_folder`    | Open a folder/file path in Explorer                       |
| `kill_process`   | ⚠️ Terminate an app by process name (e.g. `chrome`)       |
| `system_action`  | ⚠️ Shutdown, restart, sleep, hibernate, log off or lock   |

### Approval gate (safety)

`kill_process` and `system_action` (marked ⚠️) **never run automatically**.
JARVIS first asks for your permission and executes the action only after you
confirm:

- **Web UI** — a red-tinted *AUTHORIZATION REQUIRED* dialog with **Approve /
  Cancel** buttons appears.
- **Voice loop** — JARVIS asks *"Do you want me to…"* and waits for you to say
  *"yes"* or *"no"*.

Pending approvals expire after 2 minutes. System-critical processes (explorer,
svchost, lsass, etc.) are always refused even when approved.

Example commands to try:
- `"set the volume to 30"`
- `"pause the music"` / `"next track"`
- `"take a screenshot"`
- `"open my documents folder"` (needs the path, e.g. `C:\Users\You\Documents`)
- `"close chrome"` → asks for approval
- `"restart my computer"` → asks for approval

## Configuration

Set any of these environment variables before running:

| Variable              | Default                  | Description                                 |
| --------------------- | ------------------------ | ------------------------------------------- |
| `OLLAMA_MODEL`        | `llama3.2:latest`        | Ollama model used for answers/tools         |
| `JARVIS_VOICE`        | `en-US-ChristopherNeural`| edge-tts voice (see `edge-tts --list`)      |
| `JARVIS_RATE`         | `+10%`                   | Speech speed adjustment                     |
| `JARVIS_MAX_TOKENS`   | `150`                    | Max reply length — lower = faster answers   |
| `JARVIS_TEMPERATURE`  | `0.7`                    | Model creativity                            |
| `JARVIS_NUM_CTX`      | `4096`                   | Context window — lower = less VRAM, faster  |
| `JARVIS_KEEP_ALIVE`   | `30m`                    | How long the model stays loaded in RAM      |
| `JARVIS_HTTPS`        | `1`                      | Serve over HTTPS with a self-signed cert (`app.py`) |
| `FLASK_DEBUG`         | `0`                      | Flask auto-reloader on/off (`app.py`)       |

## How fast replies work

1. On startup, the model is **preloaded in the background** and kept in memory
   (`JARVIS_KEEP_ALIVE`), so the first question isn't a cold start.
2. While the model thinks, the web UI shows a status line; answers then
   **stream token-by-token** into the log instead of appearing all at once.
3. Once the reply is complete, the audio file is generated and played —
   so JARVIS talks right after the text appears.
4. Replies are capped at `JARVIS_MAX_TOKENS` so JARVIS gets to the point.

If your model doesn't support tools, JARVIS automatically falls back to plain
chat — the tools are simply skipped. `web_search` and `get_weather` need an
internet connection.

## Discord calls

`discord_contact` and `discord_call` drive **your own Discord desktop app**
(like you typing yourself — no bot, nothing against Discord's terms):

1. Discord must be running and logged in (JARVIS launches it if not).
2. `discord_contact` opens the quick switcher (Ctrl+K), types the name and
   opens the DM.
3. `discord_call` does the same, then clicks the **Start Voice Call** button;
   if Discord doesn't expose the button to automation, it presses a hotkey
   instead. For the most reliable calls, set a keybind in
   Discord → Settings → Keybinds → **Start/Stop Voice Call** (e.g. `Ctrl+F10`)
   and set `JARVIS_DISCORD_CALL_KEY=^F10` (SendKeys syntax) before starting.
   Voice loop: "call <name> on discord", web UI: "call <name> on discord".

## Memory

- JARVIS **remembers the last few turns** of conversation, so it can follow up
  ("you mentioned earlier that you're in Batangas…").
- Facts it learns from you — your **name** ("call me Ron", "my name is …") and
  your **stated location** ("I'm in Batangas", "I live in …") — are saved to
  `memory.json` and remembered across restarts. A stated location always wins
  over IP geolocation.
- Delete `memory.json` to wipe what it remembers.
- Greetings ("hi", "hey jarvis"…) get an instant canned reply without touching
  the model.
- JARVIS refuses to run tools you didn't ask for (e.g. it won't change the
  volume unless you mention volume), even if the model proposes it.

## Project layout

```
zee/
├── app.py            # Flask web server + /ask API
├── jarvis.py         # Offline voice loop (Vosk + sounddevice)
├── jarvis_core.py    # Shared TTS + Ollama brain (single source of truth)
├── templates/
│   └── index.html    # Browser UI
└── model/            # Vosk model (download, not included)
```

## Troubleshooting

- **"Ollama error" / "trouble connecting to my cognitive processor"** — make
  sure `ollama serve` is running and `ollama list` shows the model named in
  `OLLAMA_MODEL`.
- **No sound** — the audio device may be busy; try closing apps using the
  speakers, or set `JARVIS_VOICE`/`JARVIS_RATE` to tweak TTS.
- **Microphone not working in the browser** — Chrome blocks the mic on plain
  HTTP. Use `http://localhost:5000`, or enable HTTPS with `JARVIS_HTTPS=1`
  (see [Using the microphone](#using-the-microphone-from-another-device-or-a-lan-ip)).
- **Tools don't run (JARVIS answers normally)** — your model doesn't support
  function calling. Pull a tool-capable one: `ollama pull llama3.1` and set
  `OLLAMA_MODEL=llama3.1`.
- **`vosk.Model` not found** — download a model and place it in the `model/`
  folder as described above.

## License

Provided as-is for learning and fun. Vosk models and edge-tts voices each have
their own licenses — check the links in this README.
