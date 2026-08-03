"""ZEE command-line entry point.

Subcommands:
    zee daemon           run the background daemon (API + voice loop)
    zee gui              run the desktop GUI only (connects to the daemon)
    zee start            launch the daemon in the background, then open the GUI
    zee stop             ask the daemon to stop (POST /shutdown, token-protected)
    zee install-autostart  register ZEE to start at login (per user)

The daemon reads ``~/.zee/zee.conf`` (simple ``KEY=VALUE`` lines) into the
environment before importing the core, so GUI settings (automation toggle,
token, voice) take effect on the next daemon start.
"""
import argparse
import os
import subprocess
import sys
import threading
import time
import urllib.request
import ssl

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from zee_core import log  # noqa: E402

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".zee")
CONFIG_FILE = os.path.join(CONFIG_DIR, "zee.conf")
TOKEN_FILE = os.path.join(ROOT, ".zee_token")


def load_config():
    """Apply ~/.zee/zee.conf over the environment (without overriding real env)."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    except OSError:
        pass


def _get_token():
    token = os.getenv("ZEE_TOKEN")
    if token is not None:
        return token.strip() or None
    for path in (TOKEN_FILE, os.path.join(CONFIG_DIR, "zee_token")):
        try:
            with open(path, encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            continue
    return None


def _base_url():
    scheme = "https" if os.getenv("ZEE_HTTPS", "1") == "1" else "http"
    return f"{scheme}://127.0.0.1:5000"


def _ssl_context():
    # The daemon uses a self-signed cert — fine for localhost automation.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _daemon_running():
    try:
        with urllib.request.urlopen(_base_url() + "/health", timeout=1.5,
                                    context=_ssl_context()) as resp:
            return resp.status == 200 or resp.status == 503
    except Exception:
        return False


def _wait_for_daemon(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _daemon_running():
            log.info("Daemon is up.")
            return True
        time.sleep(0.75)
    return False


def cmd_daemon():
    """Background service: API + voice loop in one process."""
    import zee_api
    import zee_core
    import zee_voice

    log.info("ZEE daemon starting (API + voice loop)...")
    # Audio must be initialized in the main thread before any speech threads.
    zee_core.init_audio()
    threading.Thread(target=zee_voice.run_voice_loop, daemon=True,
                     name="voice-loop").start()
    zee_api.run_server()


def _spawn_detached(args):
    """Launch a subprocess that survives this one closing (daemon)."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        args, cwd=ROOT,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, **kwargs)


def cmd_start():
    """Start the daemon in the background, then bring up the GUI."""
    from zee_core import log

    if not _daemon_running():
        log.info("Launching ZEE daemon in the background...")
        _spawn_detached([sys.executable, "-u", os.path.join(ROOT, "zee.py"), "daemon"])
        if not _wait_for_daemon():
            log.error("Daemon did not become ready in time. Check zee.log.")
    try:
        from gui import zee_gui
        zee_gui.main()
    except ImportError as e:
        log.error(f"GUI unavailable ({e}); the daemon keeps running at {_base_url()}.")


def cmd_gui():
    """Run only the desktop GUI (daemon must already be running)."""
    from gui import zee_gui
    zee_gui.main()


def cmd_stop():
    """Ask the running daemon to shut down via the protected /shutdown endpoint."""
    import json
    from zee_core import log

    token = _get_token()
    headers = {}
    if token:
        headers["X-ZEE-TOKEN"] = token
    req = urllib.request.Request(
        _base_url() + "/shutdown", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=5, context=_ssl_context()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            log.info(f"Daemon: {body.get('message', 'stopping')}")
            return 0
    except urllib.error.HTTPError as e:
        log.error(f"Could not stop daemon (HTTP {e.code}): check ZEE_TOKEN.")
        return 1
    except Exception as e:
        log.error(f"Could not reach daemon: {e}")
        return 1


def cmd_install_autostart(args):
    """Register ZEE to start at login for the current user."""
    from tools.install_autostart import install_autostart
    return install_autostart(with_gui=args.gui)


def main(argv=None):
    load_config()
    parser = argparse.ArgumentParser(prog="zee", description="ZEE local assistant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon", help="run the background daemon (API + voice loop)")
    sub.add_parser("gui", help="run only the desktop GUI")
    sub.add_parser("start", help="start daemon in background, then open GUI")
    sub.add_parser("stop", help="stop the running daemon")
    autostart = sub.add_parser("install-autostart", help="register ZEE at login")
    autostart.add_argument("--gui", action="store_true",
                           help="also autostart the desktop GUI window")
    args = parser.parse_args(argv)

    if args.command == "daemon":
        cmd_daemon()
    elif args.command == "gui":
        cmd_gui()
    elif args.command == "start":
        cmd_start()
    elif args.command == "stop":
        sys.exit(cmd_stop())
    elif args.command == "install-autostart":
        sys.exit(cmd_install_autostart(args) or 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
