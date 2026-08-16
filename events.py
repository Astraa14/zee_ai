"""Thread-safe Server-Sent Events (SSE) broadcaster.

Background code (the voice loop, the approval flow) calls :func:`broadcast`
to push JSON payloads to every connected client. The API server serves
them via :func:`stream_events` on ``GET /events``.

Usage::

    import events

    # from a background thread:
    events.broadcast({"type": "wake", "phrase": "zee", "timestamp": ...})

    # from Flask:
    @app.route("/events")
    def events_route():
        return events.stream_events_response()
"""

import json
import logging
import queue
import threading

log = logging.getLogger("zee.events")

_subscribers = set()
_lock = threading.Lock()


def broadcast(payload):
    """Push a JSON-serializable payload to every connected client.

    Payloads are encoded to a single SSE ``data:`` line; the GUI subscribes
    and reacts (wake events raise the window, approvals open the dialog).
    """
    try:
        data = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        log.error(f"Event payload not JSON-serializable: {e}")
        return
    event = f"data: {data}\n\n"
    with _lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            _subscribers.discard(q)


def stream_events():
    """Generator yielding SSE frames until the client leaves."""
    q = queue.Queue(maxsize=64)
    with _lock:
        _subscribers.add(q)
    try:
        yield "retry: 2000\n\n"
        while True:
            frame = q.get()
            if frame is None:
                break
            yield frame
    finally:
        with _lock:
            _subscribers.discard(q)


def stream_events_response():
    """Return a Flask ``Response`` (text/event-stream) for ``/events``."""
    from flask import Response, stream_with_context

    return Response(stream_with_context(stream_events()), mimetype="text/event-stream")


def subscriber_count():
    """Number of currently connected SSE clients (used by tests/tools)."""
    with _lock:
        return len(_subscribers)
