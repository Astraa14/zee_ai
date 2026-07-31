import os

from flask import Flask, render_template, request, jsonify

import jarvis_core

app = Flask(__name__)


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
    elif "open calculator" in text:
        os.system("start calc")
        reply = "Opening Calculator."
    else:
        try:
            result = jarvis_core.ask_ollama(text)
        except Exception as e:
            print(f"Ollama error: {e}")
            reply = "I am having trouble connecting to my cognitive processor."
            jarvis_core.speak(reply)
            return jsonify({"reply": reply})

        reply = result.text
        if result.approval:
            jarvis_core.speak(reply + " Please approve or cancel on the screen.")
            return jsonify({
                "reply": reply,
                "approval_id": result.approval["id"],
                "approval_message": result.approval["message"],
            })

    jarvis_core.speak(reply)
    return jsonify({"reply": reply})


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
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug)
