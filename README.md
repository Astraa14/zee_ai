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
- 🗣️ Lifelike TTS with **edge-tts** (default: `en-US-ChristopherNeural`)
- 💬 Text fallback so it works even without a microphone
- ⌨️ Voice commands: open Notepad, Calculator, browser
- 🔧 Configurable model / voice / speed via environment variables

## Requirements

- **Python 3.9+**
- **Ollama** installed and running locally, with a model pulled (e.g. `llama3`):

  ```sh
  ollama serve
  ollama pull llama3
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

## Configuration

Set any of these environment variables before running:

| Variable        | Default                    | Description                         |
| --------------- | -------------------------- | ----------------------------------- |
| `OLLAMA_MODEL`  | `llama3`                   | Ollama model used for answers       |
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
- **`vosk.Model` not found** — download a model and place it in the `model/`
  folder as described above.

## License

Provided as-is for learning and fun. Vosk models and edge-tts voices each have
their own licenses — check the links in this README.
