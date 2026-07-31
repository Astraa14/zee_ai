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
  The default is `llama3.1:8b` (supports function calling). `llama3`, `llama3.2`,
  `qwen3` and `gemma3` also work — `llama3` will simply skip tools:

  ```sh
  ollama serve
  ollama pull llama3.1:8b     # default, recommended for full tool support
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

Then open `http://localhost:5000` in Chrome or Edge. Click **Activate
Microphone** to talk, or type a command and press Enter. Audio plays through
your speakers.

> `FLASK_DEBUG=0 python app.py` disables the auto-reloader.

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
| `open_app`       | Launch desktop apps (notepad, calculator, browser…) |
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

| Variable        | Default                    | Description                         |
| --------------- | -------------------------- | ----------------------------------- |
| `OLLAMA_MODEL`  | `llama3.1:8b`               | Ollama model used for answers/tools |
| `JARVIS_VOICE`  | `en-US-ChristopherNeural`  | edge-tts voice (see `edge-tts --list`) |
| `JARVIS_RATE`   | `+10%`                     | Speech speed adjustment             |
| `FLASK_DEBUG`   | `1`                        | Flask auto-reloader on/off (`app.py`) |

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
- **Microphone not working in the browser** — use Chrome or Edge, and grant
  microphone permission to `localhost`.
- **Tools don't run (JARVIS answers normally)** — your model doesn't support
  function calling. Pull a tool-capable one: `ollama pull llama3.1` and set
  `OLLAMA_MODEL=llama3.1`.
- **`vosk.Model` not found** — download a model and place it in the `model/`
  folder as described above.

## License

Provided as-is for learning and fun. Vosk models and edge-tts voices each have
their own licenses — check the links in this README.
