import ipaddress
import json
import os
import socket
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, render_template, request, jsonify

import jarvis_core

app = Flask(__name__)

CERT_FILE = os.path.join(os.path.dirname(__file__), "cert.pem")
KEY_FILE = os.path.join(os.path.dirname(__file__), "key.pem")


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def ensure_cert():
    """Create a self-signed cert covering localhost and the LAN IP, if missing."""
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "jarvis.local")])
    alt_names = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address(_lan_ip())),
    ]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(KEY_FILE, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    with open(CERT_FILE, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    print(f"Generated self-signed certificate for localhost and {_lan_ip()}")


def _stream_reply(text):
    """Generator: yields NDJSON lines (delta / approval / done) as the model replies."""
    print(f"[ask] text={text!r}", flush=True)
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
    # Audio must be initialized in the main thread before any speech threads.
    jarvis_core.init_audio()
    # Preload the model in the background so the first question is fast.
    threading.Thread(target=jarvis_core.warmup_model, daemon=True).start()
    debug = os.getenv("FLASK_DEBUG", "0") == "1"

    kwargs = {"host": "0.0.0.0", "port": 5000, "debug": debug}
    # HTTPS is on by default so the microphone works over the LAN.
    # Set JARVIS_HTTPS=0 to serve plain HTTP instead.
    if os.getenv("JARVIS_HTTPS", "1") == "1":
        try:
            ensure_cert()
            kwargs["ssl_context"] = (CERT_FILE, KEY_FILE)
            print(f"Serving at https://{_lan_ip()}:5000 "
                  f"(allow the certificate once in your browser)")
        except Exception as e:
            print(f"HTTPS unavailable ({e}); falling back to plain HTTP.")
    app.run(**kwargs)
