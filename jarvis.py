import json
import os
import queue
import sys

import sounddevice as sd
import vosk

import jarvis_core

q = queue.Queue()
is_speaking = False


def speak(text):
    global is_speaking

    # Cover ears so the microphone ignores our own voice
    is_speaking = True
    try:
        jarvis_core.speak(text, wait=True)
    finally:
        # Empty the queue of any echoes
        while not q.empty():
            q.get()
        is_speaking = False


def open_application(command):
    command = command.lower()

    if "notepad" in command:
        speak("Opening Notepad, sir.")
        os.system("start notepad")
    elif "calculator" in command:
        speak("Opening Calculator.")
        os.system("start calc")
    elif "browser" in command or "chrome" in command:
        speak("Opening your browser.")
        os.system("start chrome")
    else:
        return False
    return True


def handle_brain(text):
    """Ask the Ollama brain. Returns a pending approval dict, or None."""
    try:
        answer = jarvis_core.ask_ollama(text)
    except Exception as e:
        speak("I'm sorry, I am having trouble connecting "
              "to my cognitive processor.")
        print(f"Error: {e}")
        return None

    if answer.approval is not None:
        speak(answer.text + " Say yes to approve, or no to cancel.")
        return answer.approval
    speak(answer.text)
    return None


def audio_callback(indata, frames, time, status):
    global is_speaking
    if status:
        print(status, file=sys.stderr)

    # Only save the audio if JARVIS is NOT currently speaking
    if not is_speaking:
        q.put(bytes(indata))


def main():
    if not os.path.exists("model"):
        print("Vosk model not found. Download one from "
              "https://alphacephei.com/vosk/models and unpack it as 'model' "
              "in the current folder.")
        sys.exit(1)

    model = vosk.Model("model")
    samplerate = 16000
    pending_approval = None

    # Preload the model so the first question isn't slow.
    import threading
    threading.Thread(target=jarvis_core.warmup_model, daemon=True).start()

    with sd.RawInputStream(samplerate=samplerate, blocksize=8000, dtype='int16',
                           channels=1, callback=audio_callback):
        rec = vosk.KaldiRecognizer(model, samplerate)

        speak("I am online and ready.")
        print("Listening... (Press Ctrl+C to stop)")

        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                result = json.loads(rec.Result())
                text = result.get("text", "")

                if not text:
                    continue
                print(f"You said: {text}")

                # A dangerous action is awaiting a yes/no answer
                if pending_approval is not None:
                    if any(word in text for word in
                           ("yes", "approve", "ok", "okay", "sure", "go ahead", "do it", "confirm", "allowed")):
                        jarvis_core.approve_action(pending_approval["id"])
                        speak("Approved.")
                    else:
                        jarvis_core.deny_action(pending_approval["id"])
                        speak("Cancelled.")
                    pending_approval = None
                    continue

                if "jarvis" in text:
                    speak("Yes, sir?")
                elif "open" in text:
                    if not open_application(text):
                        pending_approval = handle_brain(text)
                elif "stop" in text or "exit" in text or "shutdown" in text:
                    speak("Shutting down. Goodbye.")
                    break
                else:
                    pending_approval = handle_brain(text)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nProgram stopped manually.")
