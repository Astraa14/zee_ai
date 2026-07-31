import asyncio
import os
import tempfile
import threading

import edge_tts
import ollama
import pygame

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")
JARVIS_VOICE = os.getenv("JARVIS_VOICE", "en-US-ChristopherNeural")
JARVIS_RATE = os.getenv("JARVIS_RATE", "+10%")

SYSTEM_PROMPT = (
    "You are JARVIS, a helpful AI assistant. Keep your answers brief, "
    "conversational, and under 3 sentences. Do not use any special formatting "
    "or symbols like asterisks, as they will be read out loud by a text-to-speech engine."
)

_play_lock = threading.Lock()


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


def ask_ollama(text):
    """Query the local Ollama LLM and return the text response. Raises on failure."""
    print(f"Querying Ollama model '{OLLAMA_MODEL}'...")
    response = ollama.chat(model=OLLAMA_MODEL, messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ])
    return response["message"]["content"]
