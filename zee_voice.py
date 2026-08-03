"""ZEE offline voice loop (Vosk + sounddevice).

Run standalone (``python zee_voice.py``) or as part of the daemon via
:func:`run_voice_loop` (``zee daemon`` / ``zee start``).

When the wake word "zee" is heard the loop broadcasts a ``wake`` event on the
SSE channel (``/events``) so the desktop GUI can raise itself.
"""
import json
import queue
import re
import sys
import threading

import sounddevice as sd

import events
import zee_core
from zee_core import log

q = queue.Queue()
is_speaking = False

# Wake words: "zee" (and the truncated "zi" that Vosk often transcribes).
_WAKE_RE = re.compile(r"\b(zee|zi|z)\b", re.IGNORECASE)
_APPROVAL_WORDS = ("yes", "approve", "ok", "okay", "sure",
                   "go ahead", "do it", "confirm", "allowed")


def speak(text):
    global is_speaking

    # Cover ears so the microphone ignores our own voice
    is_speaking = True
    try:
        zee_core.speak(text, wait=True)
    finally:
        # Empty the queue of any echoes
        while not q.empty():
            q.get()
        is_speaking = False


def is_wake_word(text):
    """True when the user said the assistant's name (case-insensitive)."""
    return bool(_WAKE_RE.search(text or ""))


def open_application(command):
    """Open any installed app or website from a voice command. Returns True if handled."""
    m = re.match(
        r"^(?:please\s+)?(?:open|start|launch|run)\s+(?:the\s+)?(.{1,40})$",
        command,
    )
    name = m.group(1).strip() if m else None
    if not name:
        return False
    result = zee_core.run_tool("open_app", {"app": name}, actor="voice")
    if result.get("opened") or result.get("opened_website"):
        speak(f"Opening {name}.")
        return True
    return False


def handle_brain(text):
    """Ask the Ollama brain. Returns a pending approval dict, or None."""
    try:
        answer = zee_core.ask_ollama(text, actor="voice")
    except Exception as e:
        speak("I'm sorry, I am having trouble connecting "
              "to my cognitive processor.")
        log.error(f"Brain error: {e}")
        return None

    if answer.approval is not None:
        speak(answer.text + " Say yes to approve, or no to cancel.")
        return answer.approval
    speak(answer.text)
    return None


def _broadcast_wake(text):
    import time
    events.broadcast({
        "type": "wake",
        "phrase": text,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })


def audio_callback(indata, frames, time, status):
    global is_speaking
    if status:
        log.warning(f"Audio status: {status}")

    # Only save the audio if ZEE is NOT currently speaking
    if not is_speaking:
        q.put(bytes(indata))


def _check_vosk_model():
    """Fail fast with clear instructions when the Vosk model is missing."""
    if not zee_core.find_vosk_model():
        log.error(
            "Vosk model not found. Download one from "
            "https://alphacephei.com/vosk/models and unpack it as 'model' "
            "in the project folder. The web UI (zee_api.py) does NOT need it."
        )
        sys.exit(1)


def _check_sound_device():
    """Warn early when no microphone input device is available."""
    try:
        sd.query_devices()
        return True
    except Exception as e:
        log.error(f"No audio input device detected: {e}. "
                  "The voice loop cannot listen without a microphone.")
        return False


def run_voice_loop():
    """Background-able entry point: blocks listening for the wake word.

    Broadcasts a ``wake`` SSE event whenever "zee" is heard, so a connected
    GUI can raise itself. Returns when the user says stop/exit/shutdown.
    """
    log.info("Starting ZEE voice loop...")

    _check_vosk_model()
    import vosk

    if not _check_sound_device():
        sys.exit(1)

    # Diagnostics: warn (don't stop) if Ollama or the model is missing —
    # the brain fails gracefully per request, but the user should know.
    if not zee_core.check_ollama():
        log.error("Ollama server is not reachable. Start it with 'ollama serve' "
                  "and pull the model first: ollama pull " + zee_core.OLLAMA_MODEL)

    model = vosk.Model(zee_core.find_vosk_model())
    samplerate = 16000
    pending_approval = None

    # Audio must be initialized in the main thread before any speech threads.
    zee_core.init_audio()
    # Preload the model so the first question isn't slow.
    threading.Thread(target=zee_core.warmup_model, daemon=True).start()

    with sd.RawInputStream(samplerate=samplerate, blocksize=8000, dtype='int16',
                           channels=1, callback=audio_callback):
        rec = vosk.KaldiRecognizer(model, samplerate)

        speak("I am online and ready.")
        log.info("Listening... (Press Ctrl+C to stop)")

        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                text = result.get("text", "")

                if not text:
                    continue
                log.info(f"You said: {text}")

                # A dangerous action is awaiting a yes/no answer
                if pending_approval is not None:
                    if any(word in text for word in _APPROVAL_WORDS):
                        zee_core.approve_action(pending_approval["id"], actor="voice")
                        speak("Approved.")
                    else:
                        zee_core.deny_action(pending_approval["id"], actor="voice")
                        speak("Cancelled.")
                    pending_approval = None
                    continue

                if is_wake_word(text):
                    _broadcast_wake(text)
                    speak("Yes, sir?")
                elif "open" in text:
                    if not open_application(text):
                        pending_approval = handle_brain(text)
                elif "stop" in text or "exit" in text or "shutdown" in text:
                    speak("Shutting down. Goodbye.")
                    break
                else:
                    pending_approval = handle_brain(text)


def main():
    try:
        run_voice_loop()
    except KeyboardInterrupt:
        log.info("Program stopped manually.")


if __name__ == "__main__":
    main()
