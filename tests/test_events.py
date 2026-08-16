"""SSE broadcaster (events.py): subscribers receive JSON payloads as data: frames."""

import json
import threading
import time

import events


def _consume_frames(generator, frames):
    """Pull frames until the stream closes or raises; appends to ``frames``."""
    try:
        for frame in generator:
            frames.append(frame)
    except StopIteration:
        pass


def _broadcast_and_wait(payload, predicate, timeout=5):
    """Subscribe, broadcast once, and return every frame the subscriber saw."""
    frames = []
    done = threading.Event()
    gen = events.stream_events()

    def consume():
        try:
            for frame in gen:
                frames.append(frame)
                if predicate(frame):
                    break
        finally:
            done.set()

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    time.sleep(0.25)  # let the subscriber register
    events.broadcast(payload)
    assert done.wait(timeout), "subscriber never received the event"
    t.join(timeout=2)
    return frames


def test_broadcast_delivers_wake_event():
    frames = _broadcast_and_wait(
        {"type": "wake", "phrase": "zee", "timestamp": 0},
        lambda f: '"type": "wake"' in f,
    )
    data = [f for f in frames if f.startswith("data: ")][-1]
    payload = json.loads(data[len("data: ") :])
    assert payload["type"] == "wake"
    assert payload["phrase"] == "zee"


def test_broadcast_delivers_approval_event():
    frames = _broadcast_and_wait(
        {"type": "approval", "id": "abc123", "action": "kill_process"},
        lambda f: '"type": "approval"' in f,
    )
    data = [f for f in frames if f.startswith("data: ")][-1]
    payload = json.loads(data[len("data: ") :])
    assert payload["action"] == "kill_process"
    assert payload["id"] == "abc123"


def test_first_frame_is_retry_hint():
    frames = _broadcast_and_wait({"type": "wake"}, lambda f: True)
    assert any(f == "retry: 2000\n\n" for f in frames)


def test_non_json_payload_does_not_crash():
    events.broadcast(object())  # must log and skip, not raise


def test_subscriber_count_tracks_connections():
    before = events.subscriber_count()
    gen = events.stream_events()
    next(gen)  # consumes the retry frame and registers the subscriber
    assert events.subscriber_count() == before + 1
    gen.close()
    assert events.subscriber_count() == before
