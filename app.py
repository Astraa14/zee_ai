import json
import os
import threading

from flask import Flask, Response, render_template, request, jsonify

import jarvis_core

app = Flask(__name__)


def _stream_reply(text):
    """Generator: yields NDJSON lines (delta / approval / done) as the model replies."""
    try:
        stream = jarvis_core.StreamAsk(text)
        for delta in stream:
            yield json.dumps({"delta": delta}) + "\n"
        if stream.approval:
            jarvis_core.speak(stream.full_text + " Please approve or cancel on the screen.")
            yield json.dumps({
                "approval_id": stream.approval["id"],
                "approval_message": stream.approval["message"],
            }) + "\n"
        else:
            jarvis_core.speak(stream.full_text)
    except Exception as e:
        print(f"Ollama error: {e}")
        msg = "I am having trouble connecting to my cognitive processor."
        yield json.dumps({"delta": msg}) + "\n"
    yield json.dumps({"done": True}) + "\n"


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").lower()

    if "open notepad" in text:
        os.system("start notepad")
        reply = "Opening Notepad, sir."
        jarvis_core.speak(reply)
        return Response(
            json.dumps({"delta": reply}) + "\n" + json.dumps({"done": True}) + "\n",
            mimetype="application/x-ndjson",
        )
    elif "open calculator" in text:
        os.system("start calc")
        reply = "Opening Calculator."
        jarvis_core.speak(reply)
        return Response(
            json.dumps({"delta": reply}) + "\n" + json.dumps({"done": True}) + "\n",
            mimetype="application/x-ndjson",
        )

    return Response(_stream_reply(text), mimetype="application/x-ndjson")


@app.route("/approve", methods=["POST"])
def approve():
    data = request.get_json(silent=True) or {}
    action_id = data.get("approval_id", "")
    result = jarvis_core.approve_action(action_id)
    if "error" in result:
        return jsonify({"ok": False, "message": result["error"]}), 400
    message = _describe_result(result)
    jarvis_core.speak("Approved. " + message)
    return jsonify({"ok": True, "message": message})


@app.route("/deny", methods=["POST"])
def deny():
    data = request.get_json(silent=True) or {}
    jarvis_core.deny_action(data.get("approval_id", ""))
    jarvis_core.speak("Cancelled.")
    return jsonify({"ok": True, "message": "Action cancelled."})


def _describe_result(result):
    if "executed" in result:
        return f"{result['executed']} done."
    if "killed" in result:
        return f"Terminated: {', '.join(result['killed'])}."
    return "Done."


if __name__ == "__main__":
    # Preload the model in the background so the first question is fast.
    threading.Thread(target=jarvis_core.warmup_model, daemon=True).start()
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug)
