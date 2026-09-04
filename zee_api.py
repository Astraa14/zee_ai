import hmac
import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, render_template, request, jsonify

import events
import zee_core
from zee_core import log

import apppaths
import tokenstore

app = Flask(__name__, template_folder=apppaths.resource_path("templates"))

# Global cap on request bodies (applies to /ask, /approve, /deny).
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("ZEE_MAX_BODY_KB", "16")) * 1024

CERT_FILE = apppaths.data_path("cert.pem")
KEY_FILE = apppaths.data_path("key.pem")

MAX_TEXT_LEN = 2000
_APPROVAL_ID_RE = re.compile(r"[0-9a-f]{8,64}")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

zee_core.setup_logging()

# ---------------- Authentication (Bearer token) ----------------
TOKEN_FILE = tokenstore.TOKEN_FILE


def _get_token():
    """Return the auth token from ZEE_TOKEN env, keyring, or the token file.

    Set ZEE_TOKEN=none to disable authentication entirely. Otherwise a fresh
    token is generated and persisted on first run.
    """
    env = os.getenv("ZEE_TOKEN")
    if env is not None:
        return env.strip() or None  # empty string → auth disabled
    tok = tokenstore.read_token()
    if tok:
        return tok
    tok = tokenstore.write_token()
    log.info(f"Generated web UI auth token -> {TOKEN_FILE} (keep this file safe)")
    return tok


_token = _get_token()


def _loopback():
    """True when the request originates from this machine (dev mode).

    Handles plain 127.*, ::1, "localhost" and IPv4-mapped IPv6
    (::ffff:127.0.0.1) via ipaddress so dual-stack clients are not
    misclassified as LAN.
    """
    addr = (request.remote_addr or "").strip().lower()
    if addr in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def _authorized():
    if not _token:
        # No token configured: dev mode only (localhost), with a warning.
        if _loopback():
            log.warning(
                "No auth token configured; allowing localhost request (dev mode). "
                "LAN access requires ZEE_TOKEN."
            )
            return True
        return False
    given = request.headers.get("X-ZEE-TOKEN", "")
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        given = header[7:]
    return bool(given) and hmac.compare_digest(given, _token)


@app.before_request
def _require_auth():
    if request.method == "POST" and request.path in (
        "/ask",
        "/approve",
        "/deny",
        "/shutdown",
        "/update",
    ):
        if not _authorized():
            return jsonify({"error": "Unauthorized. Provide a valid access token."}), 401


# ---------------- Rate limiting ----------------
class _RateLimiter:
    """Sliding-window rate limiter, one bucket per key.

    Buckets are bounded (at most ``limit + window`` entries each) and stale
    keys are pruned whenever more than ``max_keys`` accumulate, so memory
    stays flat even under token/IP churn. Keys are per authenticated token
    (hashed) with an IP fallback (see ``_client_key``).
    """

    def __init__(self, limit, window_seconds, max_keys=4096):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits = {}
        self._lock = threading.Lock()
        self._last_prune = time.monotonic()

    def allow(self, key):
        now = time.monotonic()
        with self._lock:
            if now - self._last_prune > self.window and len(self._hits) > self.limit:
                self._prune_locked(now)
            bucket = [t for t in self._hits.get(key, ()) if now - t < self.window]
            if len(bucket) >= self.limit:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            return True

    def _prune_locked(self, now):
        kept = {}
        for k, v in self._hits.items():
            recent = [t for t in v if now - t < self.window]
            if recent:
                kept[k] = recent
        self._hits = kept
        self._last_prune = now


RATE_LIMIT_PER_MIN = int(os.getenv("ZEE_RATE_LIMIT", "10"))
_ask_limiter = _RateLimiter(RATE_LIMIT_PER_MIN, 60)
# Hard per-IP cap: even a client rotating many tokens cannot exceed it.
_ip_limiter = _RateLimiter(max(RATE_LIMIT_PER_MIN * 4, 20), 60)
# /approve and /deny share a modest bucket (they mutate system state).
_approval_limiter = _RateLimiter(30, 60)


def _client_key():
    """Rate-limit bucket key: per authenticated token (hashed), else per IP."""
    given = request.headers.get("X-ZEE-TOKEN", "")
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        given = header[7:]
    if _token and given:
        return "tok:" + hashlib.sha256(given.encode("utf-8")).hexdigest()
    return f"ip:{request.remote_addr or 'local'}"


def _rate_limited():
    """Check token-bucket + per-IP hard cap for the current request."""
    if not _ask_limiter.allow(_client_key()):
        return True
    return not _ip_limiter.allow(f"ip:{request.remote_addr or 'local'}")


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
    apppaths.ensure_data_dir()
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "zee.local")])
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
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    with open(CERT_FILE, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    log.info(f"Generated self-signed certificate for localhost and {_lan_ip()}")


def _stream_reply(text, actor="web"):
    """Generator: yields NDJSON lines (delta / approval / done) as the model replies."""
    log.info(f"[ask] text={text!r} actor={actor}")
    try:
        stream = zee_core.StreamAsk(text, actor=actor)
        for delta in stream:
            yield json.dumps({"delta": delta}) + "\n"
        if stream.approval:
            zee_core.speak(stream.full_text + " Please approve or cancel on the screen.")
            yield json.dumps(
                {
                    "approval_id": stream.approval["id"],
                    "approval_message": stream.approval["message"],
                }
            ) + "\n"
        else:
            zee_core.speak(stream.full_text)
    except Exception as e:
        log.error(f"Ollama error: {e}")
        msg = "I am having trouble connecting to my cognitive processor."
        yield json.dumps({"delta": msg}) + "\n"
    yield json.dumps({"done": True}) + "\n"


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
def health():
    """Readiness probe: Ollama reachability + automation opt-in.

    Returns JSON ``{ok, ollama, automation_enabled}`` — ``200`` when the
    Ollama brain answers, ``503`` otherwise.
    """
    probe = zee_core.ollama_probe() or "unavailable"
    ok = probe == "ok"
    return jsonify(
        {
            "ok": ok,
            "ollama": probe,
            "automation_enabled": zee_core.automation_enabled(),
        }
    ), (200 if ok else 503)


@app.route("/events")
def events_route():
    """Server-Sent Events stream: wake events, approval requests, daemon state."""
    if _token and not _authorized():
        return jsonify({"error": "Unauthorized. Provide a valid access token."}), 401
    return events.stream_events_response()


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Stop the daemon gracefully. Protected by the same token as /ask."""
    log.warning("Shutdown requested via /shutdown")
    func = request.environ.get("werkzeug.server.shutdown")
    if func:
        func()
    else:
        threading.Thread(target=_force_stop, daemon=True).start()
    return jsonify({"ok": True, "message": "ZEE daemon stopping."})


@app.route("/update", methods=["POST"])
def update():
    """Kick off a self-update in the background (token-protected).

    Body: ``{"manifest": "https://.../latest.json"}`` or
    ``{"url": "https://.../asset.exe", "sha256": "<hex>"}``.
    Downloads + verifies + applies (silent installer, or exe swap for
    bare binaries). Returns 202 immediately; the daemon keeps running.
    """
    data = request.get_json(silent=True) or {}
    manifest = data.get("manifest")
    url = data.get("url")
    sha256 = data.get("sha256")
    if manifest:
        if not isinstance(manifest, str) or not manifest.startswith(("http://", "https://")):
            return jsonify({"error": "'manifest' must be an http(s) URL."}), 400
        target = manifest
    elif url:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return jsonify({"error": "'url' must be an http(s) URL."}), 400
        target = url
    else:
        return jsonify({"error": "Provide 'manifest' or 'url' (+ 'sha256')."}), 400

    def _run():
        import updater

        try:
            result = updater.run_update(target, sha256=sha256 or None)
            log.info("Update finished: %s", result)
        except Exception as e:  # noqa: BLE001 — report, never crash the daemon
            log.error(f"Update failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    log.info("Update started: %s", target)
    return jsonify({"ok": True, "message": "Update started in the background."}), 202


def _force_stop():
    time.sleep(0.5)
    log.info("ZEE daemon exiting.")
    os._exit(0)


@app.route("/ask", methods=["POST"])
def ask():
    if _rate_limited():
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

    # If user tries to open an app and automation is disabled, warn them.
    m = re.fullmatch(r"open\s+(?:the\s+|a\s+)?(.{1,40})", text)
    if m and not re.search(r"(where|what|how|why|when|about|with|help|door|window)", m.group(1)):
        if not zee_core.automation_enabled():
            return jsonify({
                "error": "Desktop automation is disabled. Set ZEE_ALLOW_AUTOMATION=1 to enable app opening, Discord calls, and Messenger search. "
                        "You will still need to approve each dangerous action."
            }), 403
        result = zee_core.run_tool("open_app", {"app": m.group(1)}, actor="web")
        if result.get("opened") or result.get("opened_website"):
            reply = f"Opening {m.group(1).title()}."
            zee_core.speak(reply)
            return Response(
                json.dumps({"delta": reply}) + "\n" + json.dumps({"done": True}) + "\n",
                mimetype="application/x-ndjson",
            )

    return Response(_stream_reply(text, actor="web"), mimetype="application/x-ndjson")


def _valid_approval_id(data):
    aid = data.get("approval_id")
    return aid if isinstance(aid, str) and _APPROVAL_ID_RE.fullmatch(aid) else None


@app.route("/approve", methods=["POST"])
def approve():
    if not _approval_limiter.allow(_client_key()):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429
    data = request.get_json(silent=True) or {}
    action_id = _valid_approval_id(data)
    if not action_id:
        return jsonify({"error": "Invalid approval_id."}), 400
    result = zee_core.approve_action(action_id, actor="web")
    if "error" in result:
        return jsonify({"ok": False, "message": result["error"]}), 400
    message = _describe_result(result)
    zee_core.speak("Approved. " + message)
    return jsonify({"ok": True, "message": message})


@app.route("/deny", methods=["POST"])
def deny():
    if not _approval_limiter.allow(_client_key()):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429
    data = request.get_json(silent=True) or {}
    action_id = _valid_approval_id(data)
    if not action_id:
        return jsonify({"error": "Invalid approval_id."}), 400
    zee_core.deny_action(action_id, actor="web")
    zee_core.speak("Cancelled.")
    return jsonify({"ok": True, "message": "Action cancelled."})


def _describe_result(result):
    if "executed" in result:
        return f"{result['executed']} done."
    if "killed" in result:
        return f"Terminated: {', '.join(result['killed'])}."
    return "Done."


def run_server():
    """Serve the API (blocking). Initializes audio + warmup like the web app."""
    # Logging is configured once here (RotatingFileHandler -> zee.log +
    # console). Idempotent: safe when zee_core already configured it at import.
    zee_core.setup_logging()
    report = zee_core.doctor()
    for line in zee_core.doctor_summary(report):
        log.info(f"[doctor] {line}")
    if not report["healthy"]:
        log.error("Environment problems detected — see [doctor] lines above.")

    # If Ollama is unreachable, surface a prominent early warning.
    if report.get("ollama") != "ok":
        log.error("OOPS: Ollama is unreachable. ZEE cannot answer without a brain.")
        log.error("Fix: Start the Ollama server and pull the required model:")
        log.error(f"  ollama serve")
        log.error(f"  ollama pull {os.getenv('OLLAMA_MODEL', 'llama3.2:latest')}")

    # Audio must be initialized in the main thread before any speech threads.
    zee_core.init_audio()
    # Preload the model in the background so the first question is fast.
    threading.Thread(target=zee_core.warmup_model, daemon=True).start()
    debug = os.getenv("FLASK_DEBUG", "0") == "1"

    if _token:
        log.info(
            f"Web UI auth token: {_token}  (set ZEE_TOKEN to change, " "ZEE_TOKEN=none to disable)"
        )
    else:
        log.warning(
            "No auth token configured: requests allowed from localhost only "
            "(dev mode). LAN access requires ZEE_TOKEN."
        )

    kwargs = {"host": "0.0.0.0", "port": 5000, "debug": debug}
    # HTTPS is on by default so the microphone works over the LAN.
    # Set ZEE_HTTPS=0 to serve plain HTTP instead.
    if os.getenv("ZEE_HTTPS", "1") == "1":
        try:
            ensure_cert()
            kwargs["ssl_context"] = (CERT_FILE, KEY_FILE)
            log.info(
                f"Serving at https://{_lan_ip()}:5000 "
                f"(allow the certificate once in your browser)"
            )
        except Exception as e:
            log.error(f"HTTPS unavailable ({e}); falling back to plain HTTP.")
    app.run(**kwargs)


if __name__ == "__main__":
    run_server()
