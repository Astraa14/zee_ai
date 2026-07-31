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
            reply = jarvis_core.ask_ollama(text)
        except Exception as e:
            print(f"Ollama error: {e}")
            reply = "I am having trouble connecting to my cognitive processor."

    jarvis_core.speak(reply)
    return jsonify({"reply": reply})


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug)
