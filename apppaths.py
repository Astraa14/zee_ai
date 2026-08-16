"""Where ZEE keeps files, frozen-build aware.

- ``data_dir()``: writable user-data folder (logs, memory, token, certs,
  approvals). When running from source this is the repo root; when frozen
  (PyInstaller) the exe lives in a possibly read-only folder like
  ``Program Files``, so data goes to ``%APPDATA%\\Zee`` (Windows) or
  ``~/.zee`` (macOS/Linux).
- ``resource_dir()``: read-only bundled files (``templates/``). When frozen
  this is the PyInstaller bundle root (``sys._MEIPASS``); from source it is
  the repo root.
"""

import os
import sys

_APP_NAME = "Zee"


def frozen():
    """True when running inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def resource_dir():
    """Read-only dir with bundled assets (templates/, model/ if shipped)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return base
    return os.path.dirname(os.path.abspath(__file__))


def data_dir():
    """Writable per-user dir for state files and generated secrets."""
    if not frozen():
        return os.path.dirname(os.path.abspath(__file__))
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, _APP_NAME)
    return os.path.join(os.path.expanduser("~"), ".zee")


def ensure_data_dir():
    """Create and return the writable data dir (safe to call repeatedly)."""
    d = data_dir()
    os.makedirs(d, exist_ok=True)
    return d


def data_path(*parts):
    """Absolute path under the writable data dir (``data_dir()/a/b``)."""
    return os.path.join(data_dir(), *parts)


def resource_path(*parts):
    """Absolute path under the bundled resources dir."""
    return os.path.join(resource_dir(), *parts)
