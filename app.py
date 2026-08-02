import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, render_template, request, jsonify

import jarvis_core
from jarvis_core import log

app = Flask(__name__)

# Global cap on request bodies (applies to /ask, /approve, /deny).
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("JARVIS_MAX_BODY_KB", "16")) * 1024

CERT_FILE = os.path.join(os.path.dirname(__file__), "cert.pem")
KEY_FILE = os.path.join(os.path.dirname(__file__), "key.pem")

MAX_TEXT_LEN = 2000
_APPROVAL_ID_RE = re.compile(r"[0-9a-f]{8,64}")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

jarvis_core.setup_logging()

# ---------------- Authentication (Bearer token) ----------------
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".jarvis_token")


def _get_token():
    """Return the auth token from JARVIS_TOKEN env, or the token file.

    If neither exists, generate one, persist it to .jarvis_token and
    return it. Set JARVIS_TOKEN=none to disable authentication entirely.
    """
    env = os.getenv("JARVIS_TOKEN")
    if env is not None:
        return env.strip() or None  # empty string → auth disabled
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok)
        log.info(f"Generated web UI auth token -> {TOKEN_FILE} (keep this file safe)")
    except OSError as e:
        log.error(f"Could not persist auth token: {e}")
    return tok


_token = _get_token()


def _authorized():
    if not _token:
        return True
    given = request.headers.get("X-JARVIS-Token", "")
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        given = header[7:]
    return bool(given) and hmac.compare_digest(given, _token)


@app.before_request
def _require_auth():
    if request.method == "POST" and request.path in ("/ask", "/approve", "/deny"):
        if not _authorized():
            return jsonify({"error": "Unauthorized. Provide a valid access token."}), 401


# ---------------- Rate limiting ----------------
class _RateLimiter:
    """Sliding-window rate limiter, one bucket per key (client IP)."""

    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window = window_seconds
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self._lock:
            bucket = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(bucket) >= self.limit:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            return True


RATE_LIMIT_PER_MIN = int(os.getenv("JARVIS_RATE_LIMIT", "30"))
_ask_limiter = _RateLimiter(RATE_LIMIT_PER_MIN, 60)


def _client_key():
    return request.remote_addr or "local"


def _clean_text(text):
    return _CONTROL_CHARS_RE.sub("", text).strip()


# ---------------- Routes ----------------
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
    log.info(f"Generated self-signed certificate for localhost and {_lan_ip()}")


def _stream_reply(text):
    """Generator: yields NDJSON lines (delta / approval / done) as the model replies."""
    log.info(f"[ask] text={text!r}")
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
        log.error(f"Ollama error: {e}")
        msg = "I am having trouble connecting to my cognitive processor."
        yield json.dumps({"delta": msg}) + "\n"
    yield json.dumps({"done": True}) + "\n"


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/ask", methods=["POST"])
def ask():
    if not _ask_limiter.allow(_client_key()):
        log.warning(f"Rate limit hit for {_client_key()}")
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429

    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"error": "'text' must be a string."}), 400
    text = _clean_text(text)
    if not text:
        return jsonify({"error": "'text' must not be empty."}), 400
    if len(text) > MAX_TEXT_LEN:
        return jsonify({"error": f"'text' is too long (max {MAX_TEXT_LEN} characters)."}), 400

    m = re.fullmatch(r"open\s+(?:the\s+|a\s+)?(.{1,40})", text)
    if m and not re.search(r"(where|what|how|why|when|about|with|help|door|window)", m.group(1)):
        result = jarvis_core.run_tool("open_app", {"app": m.group(1)})
        if result.get("opened") or result.get("opened_website"):
            reply = f"Opening {m.group(1).title()}."
            jarvis_core.speak(reply)
            return Response(
                json.dumps({"delta": reply}) + "\n" + json.dumps({"done": True}) + "\n",
                mimetype="application/x-ndjson",
            )

    return Response(_stream_reply(text), mimetype="application/x-ndjson")


def _valid_approval_id(data):
    aid = data.get("approval_id")
    return aid if isinstance(aid, str) and _APPROVAL_ID_RE.fullmatch(aid) else None


@app.route("/approve", methods=["POST"])
def approve():
    data = request.get_json(silent=True) or {}
    action_id = _valid_approval_id(data)
    if not action_id:
        return jsonify({"error": "Invalid approval_id."}), 400
    result = jarvis_core.approve_action(action_id)
    if "error" in result:
        return jsonify({"ok": False, "message": result["error"]}), 400
    message = _describe_result(result)
    jarvis_core.speak("Approved. " + message)
    return jsonify({"ok": True, "message": message})


@app.route("/deny", methods=["POST"])
def deny():
    data = request.get_json(silent=True) or {}
    action_id = _valid_approval_id(data)
    if not action_id:
        return jsonify({"error": "Invalid approval_id."}), 400
    jarvis_core.deny_action(action_id)
    jarvis_core.speak("Cancelled.")
    return jsonify({"ok": True, "message": "Action cancelled."})


def _describe_result(result):
    if "executed" in result:
        return f"{result['executed']} done."
    if "killed" in result:
        return f"Terminated: {', '.join(result['killed'])}."
    return "Done."


if __name__ == "__main__":
    report = jarvis_core.doctor()
    for line in jarvis_core.doctor_summary(report):
        log.info(f"[doctor] {line}")
    if not report["healthy"]:
        log.error("Environment problems detected — see [doctor] lines above.")

    # Audio must be initialized in the main thread before any speech threads.
    jarvis_core.init_audio()
    # Preload the model in the background so the first question is fast.
    threading.Thread(target=jarvis_core.warmup_model, daemon=True).start()
    debug = os.getenv("FLASK_DEBUG", "0") == "1"

    if _token:
        log.info(f"Web UI auth token: {_token}  (set JARVIS_TOKEN to change, "
                 "JARVIS_TOKEN=none to disable)")
    else:
        log.warning("Authentication is DISABLED (JARVIS_TOKEN=none).")

    kwargs = {"host": "0.0.0.0", "port": 5000, "debug": debug}
    # HTTPS is on by default so the microphone works over the LAN.
    # Set JARVIS_HTTPS=0 to serve plain HTTP instead.
    if os.getenv("JARVIS_HTTPS", "1") == "1":
        try:
            ensure_cert()
            kwargs["ssl_context"] = (CERT_FILE, KEY_FILE)
            log.info(f"Serving at https://{_lan_ip()}:5000 "
                     f"(allow the certificate once in your browser)")
        except Exception as e:
            log.error(f"HTTPS unavailable ({e}); falling back to plain HTTP.")
    app.run(**kwargs)
