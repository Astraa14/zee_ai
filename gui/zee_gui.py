"""ZEE desktop GUI.

A small PySide6 window that embeds the local web UI
(``https://127.0.0.1:5000``), stays in the system tray when hidden and
raises itself when the voice loop broadcasts a wake event over ``/events``.

Run with ``zee gui`` or ``zee start``. Requires PySide6 + PySide6-Addons
(QtWebEngine):

    pip install PySide6 PySide6-Addons
"""

import json
import os
import threading
import warnings

import requests

from PySide6.QtCore import QEvent, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
)

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".zee")
CONFIG_FILE = os.path.join(CONFIG_DIR, "zee.conf")


def _base_url():
    scheme = "https" if os.getenv("ZEE_HTTPS", "1") == "1" else "http"
    return f"{scheme}://127.0.0.1:5000"


def _read_token():
    token = os.getenv("ZEE_TOKEN")
    if token is not None:
        return token.strip() or None
    import tokenstore

    tok = tokenstore.read_token()
    if tok:
        return tok
    try:
        with open(os.path.join(CONFIG_DIR, "zee_token"), encoding="utf-8") as f:
            val = f.read().strip()
        return val or None
    except OSError:
        return None


def _request(url, data=None, timeout=5):
    token = _read_token()
    headers = {}
    if token:
        headers["X-ZEE-TOKEN"] = token
    method = "POST" if data is not None else "GET"
    return requests.request(method, url, data=data, headers=headers, timeout=timeout, verify=False)


def daemon_alive():
    try:
        resp = _request(_base_url() + "/health", timeout=1.5)
        return resp.status_code in (200, 503)
    except Exception:
        return False


class _AuthInterceptor(QWebEngineUrlRequestInterceptor):
    """Adds the ZEE token header to every request the embedded browser makes.

    The token is looked up through a mutable holder so it stays in sync
    when the user changes it in Settings (or when the daemon first
    generates one after the GUI started).
    """

    def __init__(self, token_holder):
        super().__init__()
        self._holder = token_holder

    def interceptRequest(self, info):
        token = self._holder()
        if token:
            info.setHttpHeader(b"X-ZEE-TOKEN", token.encode("utf-8"))


class _SseWorker(QThread):
    """Background thread that tails /events and forwards frames to the GUI.

    Reconnects with exponential backoff when the connection drops, and
    bounds the in-flight queue (backpressure): when the GUI cannot keep up,
    intermediate frames are coalesced — only the newest event is delivered
    — so memory stays flat and wake events are never silently lost.

    Signals:
        frame: NDJSON data line from /events
        auth_required: emitted when the SSE stream returns 401 (token expired/invalid)
    """

    frame = Signal(str)
    auth_required = Signal()
    MAX_QUEUED = 16
    _MAX_BACKOFF = 30.0

    def __init__(self, url):
        super().__init__()
        self._url = url
        self._stopped = threading.Event()
        self._depth = 0
        self._depth_lock = threading.Lock()
        self._wake_pending = threading.Event()

    def run(self):
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                with _request(self._url, timeout=(5, 600)) as resp:
                    if resp.status_code == 401:
                        self.auth_required.emit()
                        self._stopped.wait(backoff)
                        backoff = min(backoff * 2, self._MAX_BACKOFF)
                        continue
                    backoff = 1.0
                    for line in resp.iter_lines(decode_unicode=True):
                        if self._stopped.is_set():
                            return
                        if line and line.startswith("data: "):
                            self._emit(line[6:])
            except Exception:
                pass
            if self._stopped.is_set():
                return
            self._stopped.wait(min(backoff, self._MAX_BACKOFF))
            backoff = min(backoff * 2, self._MAX_BACKOFF)

    def _emit(self, data):
        with self._depth_lock:
            if self._depth >= self.MAX_QUEUED:
                self._wake_pending.set()
                return
            self._depth += 1
        self.frame.emit(data)

    def ack(self, data):
        """Called (main thread) after ``frame`` was handled."""
        try:
            payload = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return
        if payload.get("type") in ("wake", "approval"):
            self._wake_pending.clear()
        with self._depth_lock:
            self._depth = max(0, self._depth - 1)

    def take_pending_wake(self):
        """One-shot: True if a wake/approval was coalesced away under load."""
        return self._wake_pending.is_set() and (not self._wake_pending.clear() or True)

    def stop(self):
        self._stopped.set()


class _SettingsDialog(QDialog):
    """Edits ~/.zee/zee.conf; applied when the daemon next restarts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ZEE Settings")
        self.setMinimumWidth(380)
        self._cfg = self._load()

        form = QFormLayout(self)

        self.automation = QCheckBox("Enable desktop automation (ZEE_ALLOW_AUTOMATION)")
        self.automation.setChecked(self._cfg.get("ZEE_ALLOW_AUTOMATION") == "1")
        form.addRow(self.automation)

        # Automation warning tooltip
        if not self.automation.isChecked():
            self.automation.setToolTip(
                "⚠️ Desktop automation (opening apps, Discord calls, Messenger search) is disabled.\n"
                "Enable it for convenience, but be careful: these actions can edit your system.\n"
                "You will still get approval dialogs for dangerous actions like shutdown or kill."
            )

        self.token = QLineEdit(self._cfg.get("ZEE_TOKEN", ""))
        self.token.setPlaceholderText("(auto-generated)")
        form.addRow("Access token (ZEE_TOKEN):", self.token)

        self.voice = QComboBox()
        self.voice.setEditable(True)
        self.voice.addItems(
            [
                "en-US-ChristopherNeural",
                "en-US-JennyNeural",
                "en-GB-SoniaNeural",
                "en-AU-WilliamNeural",
            ]
        )
        self.voice.setCurrentText(self._cfg.get("ZEE_VOICE", "en-US-ChristopherNeural"))
        form.addRow("TTS voice (ZEE_VOICE):", self.voice)

        note = QLabel(
            "Settings are applied when the daemon restarts " "(tray icon → Restart Daemon)."
        )
        note.setWordWrap(True)
        form.addRow(note)

        save = QPushButton("Save")
        save.clicked.connect(self._save)
        cancel = QPushButton("Close")
        cancel.clicked.connect(self.reject)
        form.addRow(save, cancel)
        self.setLayout(form)

    @staticmethod
    def _load():
        cfg = {}
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if line and "=" in line and not line.startswith("#"):
                        k, _, v = line.partition("=")
                        cfg[k.strip()] = v.strip()
        except OSError:
            pass
        return cfg

    def _save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self._cfg["ZEE_ALLOW_AUTOMATION"] = "1" if self.automation.isChecked() else "0"
        self._cfg["ZEE_TOKEN"] = self.token.text().strip()
        self._cfg["ZEE_VOICE"] = self.voice.currentText().strip()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            for k, v in self._cfg.items():
                f.write(f"{k}={v}\n")
        if os.name != "nt":
            try:
                os.chmod(CONFIG_FILE, 0o600)  # zee.conf may hold ZEE_TOKEN
            except OSError:
                pass
        # Also persist to OS keyring if available
        import tokenstore
        tokenstore.write_token(self._cfg["ZEE_TOKEN"])
        self.accept()


class _UpdateDialog(QDialog):
    """Check for updates: POSTs a release manifest URL to /update.

    The daemon downloads, verifies (SHA-256 + optional Authenticode per
    ZEE_REQUIRE_SIGNATURE) and applies the update in the background.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ZEE Update")
        self.setMinimumWidth(420)
        form = QFormLayout(self)

        self.url = QLineEdit(os.getenv("ZEE_UPDATE_MANIFEST", ""))
        self.url.setPlaceholderText("https://host/path/latest.json")
        form.addRow("Release manifest URL:", self.url)

        note = QLabel(
            "The daemon downloads the update, verifies its SHA-256 "
            "checksum and signature, then installs it silently or "
            "swaps the executable atomically. You may need to "
            "restart ZEE afterwards."
        )
        note.setWordWrap(True)
        form.addRow(note)

        check = QPushButton("Check & Install")
        check.clicked.connect(self._run)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        form.addRow(check, cancel)

    def _run(self):
        manifest = self.url.text().strip()
        if not manifest.startswith(("http://", "https://")):
            QMessageBox.warning(self, "ZEE", "Enter an http(s) manifest URL.")
            return
        try:
            resp = _request(
                _base_url() + "/update", data=json.dumps({"manifest": manifest}), timeout=5
            )
            body = resp.json()
            if resp.status_code != 202:
                raise RuntimeError(f"HTTP {resp.status_code}: {body.get('error', '')}")
        except Exception as e:
            QMessageBox.warning(self, "ZEE", f"Update could not be started: {e}")
            return
        QMessageBox.information(
            self,
            "ZEE",
            f"{body.get('message', 'Update started.')}\n\n"
            "Watch zee.log for the verification result.",
        )
        self.accept()


class _ApprovalToast(QDialog):
    """Modal approval toast: shows the requested action with Approve/Cancel.

    Shown automatically when an SSE ``approval`` event arrives. Posts
    ``/approve`` or ``/deny`` to the daemon and dismisses.
    """

    def __init__(self, payload, parent=None):
        super().__init__(parent)
        self._payload = payload
        self.setWindowTitle("ZEE needs your approval")
        self.setMinimumWidth(380)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        form = QFormLayout(self)
        action = payload.get("action", "?")
        args = payload.get("args", {}) or {}
        aid = payload.get("id", "?")
        args_text = json.dumps(args, ensure_ascii=False)
        form.addRow(QLabel(f"<b>{action}</b>({args_text})"))
        form.addRow(QLabel(f"Approval id: <code>{aid}</code>"))
        form.addRow(QLabel("Approve to let ZEE run this action. Otherwise click Cancel."))

        buttons = QFormLayout()
        approve_btn = QPushButton("Approve")
        approve_btn.setStyleSheet("background-color: #b00020; color: white; font-weight: bold;")
        approve_btn.clicked.connect(self._approve)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._deny)
        row = QFormLayout()
        row.addRow(approve_btn, cancel_btn)
        form.addRow(row)
        self.setLayout(form)

    def _post(self, endpoint):
        aid = self._payload.get("id")
        try:
            resp = _request(
                _base_url() + endpoint,
                data=json.dumps({"approval_id": aid}),
                timeout=5,
            )
            if resp.status_code not in (200, 202):
                QMessageBox.warning(self, "ZEE", f"HTTP {resp.status_code}: {resp.text[:200]}")
                return
        except Exception as e:
            QMessageBox.warning(self, "ZEE", f"Could not reach daemon: {e}")
            return
        self.accept()

    def _approve(self):
        self._post("/approve")

    def _deny(self):
        self._post("/deny")


class _TokenPromptDialog(QDialog):
    """Prompts the user to enter / update the ZEE access token.

    Shown when the SSE stream returns 401. Saves the new token via
    tokenstore + .zee.conf so the GUI's auth interceptor picks it up.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ZEE auth required")
        self.setMinimumWidth(380)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        form = QFormLayout(self)
        form.addRow(QLabel("ZEE's stream is rejecting requests. Enter the access token."))
        self._entry = QLineEdit(_read_token() or "")
        self._entry.setEchoMode(QLineEdit.EchoMode.Password)
        self._entry.setPlaceholderText("paste the token from ~/.zee/zee.conf or zee_token")
        form.addRow("Token:", self._entry)
        save = QPushButton("Save")
        save.clicked.connect(self._save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        form.addRow(save, cancel)
        self.setLayout(form)

    def _save(self):
        new = self._entry.text().strip()
        if not new:
            QMessageBox.warning(self, "ZEE", "Token cannot be empty.")
            return
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    for raw in f:
                        line = raw.strip()
                        if line and "=" in line and not line.startswith("#"):
                            k, _, v = line.partition("=")
                            cfg[k.strip()] = v.strip()
            except OSError:
                pass
        cfg["ZEE_TOKEN"] = new
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            for k, v in cfg.items():
                f.write(f"{k}={v}\n")
        if os.name != "nt":
            try:
                os.chmod(CONFIG_FILE, 0o600)
            except OSError:
                pass
        import tokenstore

        tokenstore.write_token(new)
        self.accept()


class MainWindow(QMainWindow):
    """Embedded browser + system tray + SSE wake handling."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ZEE")
        self.resize(900, 640)
        self._token = _read_token()

        self.view = QWebEngineView(self)
        self.setCentralWidget(self.view)

        try:
            QWebEngineProfile.defaultProfile().setIgnoreCertificateErrors(True)
        except Exception:
            pass
        QWebEngineProfile.defaultProfile().setUrlRequestInterceptor(
            _AuthInterceptor(lambda: self._token)
        )

        self._tray = self._make_tray()

        self._sse = _SseWorker(_base_url() + "/events")
        self._sse.frame.connect(self._on_event)
        self._sse.auth_required.connect(self._on_auth_required)
        self._sse.start()

        self._retry = QTimer(self)
        self._retry.setInterval(3000)
        self._retry.timeout.connect(self._try_load)
        self._retry.start()
        self._try_load()

    # ----- embedded webview -----
    def _try_load(self):
        if daemon_alive():
            self._retry.stop()
            self.view.load(_base_url() + "/")

    # ----- tray -----
    def _make_tray(self):
        pix = QPixmap(64, 64)
        pix.fill(Qt.transparent)
        tray = QSystemTrayIcon(QIcon(pix), self)
        menu = QMenu()
        show = QAction("Show", self)
        show.triggered.connect(self._show_window)
        settings = QAction("Settings", self)
        settings.triggered.connect(self._open_settings)
        update = QAction("Check for Updates...", self)
        update.triggered.connect(self._open_update)
        restart = QAction("Restart Daemon", self)
        restart.triggered.connect(self._restart_daemon)
        quit_a = QAction("Quit", self)
        quit_a.triggered.connect(self._quit)
        for action in (show, settings, update, restart, quit_a):
            menu.addAction(action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        return tray

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:  # left click toggles show/hide
            if self.isVisible():
                self.hide()
            else:
                self._show_window()

    def _show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def show_and_raise(self):
        """Raise + focus the window (wired to SSE wake events and the tray)."""
        self._show_window()

    def changeEvent(self, event):
        # Minimizing hides to the tray instead of the taskbar.
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def closeEvent(self, event):
        # Closing keeps ZEE in the tray; the daemon keeps listening.
        self.hide()
        self._tray.showMessage(
            "ZEE", "ZEE is still running in the background.", QSystemTrayIcon.Information, 2000
        )
        event.ignore()

    def _open_settings(self):
        if _SettingsDialog(self).exec():
            self._token = _read_token()
            self._retry.start()

    def _open_update(self):
        _UpdateDialog(self).exec()

    def _restart_daemon(self):
        try:
            resp = _request(_base_url() + "/shutdown", data=b"{}", timeout=5)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
        except Exception as e:
            QMessageBox.warning(self, "ZEE", f"Could not stop the daemon: {e}")

    def _quit(self):
        self._sse.stop()
        self._sse.wait(6000)
        QApplication.instance().quit()

    # ----- SSE events -----
    def _on_event(self, data):
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return
        if payload.get("type") in ("wake", "approval"):
            self.show_and_raise()
        self._sse.ack(data)
        if self._sse.take_pending_wake():
            # A wake/approval was coalesced under load — still raise the window.
            self.show_and_raise()
        # Show approval toast for approval events
        if payload.get("type") == "approval":
            # We need the parent reference; since _on_event is a slot,
            # we create the toast as a child of self.
            toast = _ApprovalToast(payload, self)
            toast.show()

    def _on_auth_required(self):
        """Called when SSE stream returns 401 — prompt the user for a valid token."""
        dlg = _TokenPromptDialog(self)
        if dlg.exec():
            # Token was saved; restart the SSE worker to re-connect with new token
            self._sse.stop()
            self._sse.wait(3000)
            # Force re-read of token from environment/config
            self._token = _read_token()
            # The _AuthInterceptor lambda will pick up the new value on next request;
            # we also need to restart the SSE worker so it uses the fresh token.
            self._sse = _SseWorker(_base_url() + "/events")
            self._sse.frame.connect(self._on_event)
            self._sse.auth_required.connect(self._on_auth_required)
            self._sse.start()


def main():
    """Entry point (also called from ``zee gui`` / ``zee start``)."""
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
