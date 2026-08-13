"""Self-update helper: download a release, verify its SHA-256, apply it.

Two apply strategies (the installer path is the default):

- ``installer_apply``: runs a downloaded Inno Setup installer silently
  (``/VERYSILENT /SUPPRESSMSGBOXES /NORESTART``). Because the installer
  uses a stable AppId it upgrades in place — no uninstall required — and
  user data in ``%APPDATA%\\Zee`` is never touched.
- ``atomic_replace``: swaps the running executable (rename old -> .old,
  move new -> exe, remove .old). A running exe can be renamed but not
  overwritten on Windows, so the new version takes effect on next start.

Manifest format (plain JSON):

    {"version": "1.0.1", "url": "https://.../Zee-Setup-1.0.1.exe",
     "sha256": "<64 lowercase hex>"}

CLI::

    python -m updater --manifest https://example.com/zee/latest.json --apply
    python -m updater --file https://example.com/Zee.exe --sha256 <hex> --apply
"""
import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile

import requests

log = logging.getLogger("zee.update")

_CHUNK = 1024 * 256
_INSTALLER_FLAGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]


# ---------------- primitives ----------------
def sha256_file(path):
    """Streaming SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path, expected):
    """Raise ValueError when the file digest does not match ``expected``."""
    actual = sha256_file(path)
    if actual.lower() != expected.strip().lower():
        raise ValueError(
            f"SHA-256 mismatch: expected {expected}, got {actual}")
    return True


def download(url, dest, timeout=120):
    """Stream ``url`` to ``dest`` (raises on error/timeout). Returns dest."""
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                f.write(chunk)
    return dest


def fetch_manifest(url, timeout=30):
    """Fetch and validate the release manifest JSON."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    m = resp.json()
    for key in ("version", "url", "sha256"):
        if not m.get(key):
            raise ValueError(f"manifest missing '{key}'")
    if not m["url"].startswith(("http://", "https://")):
        raise ValueError("manifest url must be http(s)")
    return m


# ---------------- apply strategies ----------------
def installer_apply(installer_path, timeout=600):
    """Run the downloaded Inno installer silently. Returns (code, out, err)."""
    cmd = [installer_path] + _INSTALLER_FLAGS
    log.info("Running installer: %s", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, timeout=timeout,
                              capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        return None, "", f"installer timed out after {timeout}s"
    return proc.returncode, proc.stdout, proc.stderr


def atomic_replace(new_file):
    """Atomically swap the running ZEE executable for ``new_file``.

    The old exe is renamed aside first (Windows forbids overwriting a
    running image), the new one is moved in, then the stale file is
    removed. Returns the path of the replaced executable.
    """
    current = sys.executable
    if not os.path.basename(current).lower().startswith("zee"):
        # Running from source (python.exe) — no exe to swap.
        return None
    old = current + ".old"
    if os.path.exists(old):
        os.remove(old)
    if os.path.exists(current):
        os.rename(current, old)
    os.replace(new_file, current)
    try:
        os.remove(old)
    except OSError:
        log.warning("Could not remove %s (will be cleaned on next update)", old)
    log.info("Replaced %s (old kept as %s)", current, old)
    return current


# ---------------- daemon lifecycle (used by the updater) ----------------
def base_url():
    """Local daemon base URL (http/https per ZEE_HTTPS)."""
    scheme = "https" if os.getenv("ZEE_HTTPS", "1") == "1" else "http"
    return f"{scheme}://127.0.0.1:5000"


def _auth_token():
    token = os.getenv("ZEE_TOKEN")
    if token is not None:
        return token.strip() or None
    try:
        import tokenstore
        return tokenstore.read_token()
    except Exception:
        return None


def stop_daemon(timeout=10):
    """Ask the running daemon to stop cleanly via the protected /shutdown."""
    headers = {"Content-Type": "application/json"}
    token = _auth_token()
    if token:
        headers["X-ZEE-TOKEN"] = token
    try:
        resp = requests.post(base_url() + "/shutdown", data=b"{}",
                             headers=headers, timeout=timeout, verify=False)
        resp.raise_for_status()
        log.info("Daemon shutdown requested (HTTP %s)", resp.status_code)
        return True
    except Exception as e:
        log.warning("Could not stop daemon: %s", e)
        return False


def start_daemon(exe=None):
    """Restart the daemon detached (``<exe> daemon``). Returns the Popen or None."""
    target = exe or sys.executable
    args = [target, "daemon"]
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.DETACHED_PROCESS)
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            args, cwd=os.path.dirname(os.path.abspath(target)) or None,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, **kwargs)
        log.info("Restarted daemon: %s", " ".join(args))
        return proc
    except Exception as e:
        log.error("Could not restart daemon: %s", e)
        return None


# ---------------- orchestration ----------------
def run_update(url, sha256=None, apply=True, shutdown=False, restart=False):
    """Download + verify + apply an update. Returns a summary dict.

    ``url`` may be a manifest URL (auto-detected when it ends in .json) or
    a direct asset URL plus an explicit ``sha256``.

    For bare-EXE updates, pass ``shutdown=True`` to stop the daemon first
    (via POST /shutdown) and ``restart=True`` to relaunch it after the swap.
    """
    if url.endswith(".json") or "manifest" in url.lower():
        manifest = fetch_manifest(url)
        asset_url, expected = manifest["url"], manifest["sha256"]
        version = manifest.get("version", "?")
    else:
        if not sha256:
            raise ValueError("sha256 is required for direct asset URLs")
        asset_url, expected = url, sha256
        version = "?"

    staging = os.path.join(tempfile.gettempdir(), "zee-update")
    os.makedirs(staging, exist_ok=True)
    dest = os.path.join(staging, os.path.basename(asset_url.split("?")[0]) or "zee-update")

    log.info("Downloading %s (version %s)...", asset_url, version)
    download(asset_url, dest)
    log.info("Verifying SHA-256...")
    verify_sha256(dest, expected)

    applied = False
    replaced_file = None
    if apply:
        if dest.lower().endswith(".exe") and "setup" in os.path.basename(dest).lower():
            code, out, err = installer_apply(dest)
            applied = code in (0, 1)
            log.info("Installer finished (exit %s): %s", code, (err or out or "").strip())
        else:
            if shutdown:
                stop_daemon()
            replaced_file = atomic_replace(dest)
            applied = bool(replaced_file)
            if restart:
                start_daemon(replaced_file)
    return {"version": version, "file": dest, "applied": applied,
            "replaced": replaced_file}


# ---------------- CLI ----------------
def main(argv=None):
    logging.basicConfig(
        level=os.getenv("ZEE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        prog="zee-updater", description="Download, verify and apply a ZEE update")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--manifest", metavar="URL",
                     help="release manifest JSON (version/url/sha256)")
    src.add_argument("--file", metavar="URL",
                     help="direct asset URL (needs --sha256)")
    parser.add_argument("--sha256", metavar="HEX",
                        help="expected SHA-256 of the asset")
    parser.add_argument("--no-apply", action="store_true",
                        help="download + verify only")
    parser.add_argument("--shutdown", action="store_true",
                        help="POST /shutdown to stop the daemon before applying "
                             "(needed to overwrite the running exe)")
    parser.add_argument("--restart", action="store_true",
                        help="start the daemon again after an atomic replace")
    args = parser.parse_args(argv)

    url = args.manifest or args.file
    summary = run_update(url, sha256=args.sha256, apply=not args.no_apply,
                         shutdown=args.shutdown, restart=args.restart)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
