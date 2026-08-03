"""ZEE desktop GUI.

A small PySide6 window that embeds the local web UI
(``https://127.0.0.1:5000``), stays in the system tray when hidden and
raises itself when the voice loop broadcasts a wake event over ``/events``.

Run with ``zee gui`` or ``zee start``. Requires PySide6 + PySide6-WebEngine:

    pip install PySide6 PySide6-WebEngine
"""
import json
import os
import ssl
import threading
import time
import urllib.request

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFormLayout, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton, QSystemTrayIcon,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".zee")
CONFIG_FILE = os.path.join(CONFIG_DIR, "zee.conf")


def _base_url():
    scheme = "https" if os.getenv("ZEE_HTTPS", "1") == "1" else "http"
    return f"{scheme}://127.0.0.1:5000"


def _ssl_ctx():
    # The daemon uses a self-signed cert — fine for localhost automation.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _read_token():
    token = os.getenv("ZEE_TOKEN")
    if token is not None:
        return token.strip() or None
    for path in (os.path.join(ROOT, ".zee_token"),
                 os.path.join(CONFIG_DIR, "zee_token")):
        try:
            with open(path, encoding="utf-8") as f:
                val = f.read().strip()
            if val:
                return val
        except OSError:
            continue
    return None


def _request(url, data=None, timeout=5):
    token = _read_token()
    headers = {}
    if token:
        headers["X-ZEE-TOKEN"] = token
    req = urllib.request.Request(url, data=data, headers=headers)
    if data is not None:
        req.method = "POST"
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())


def daemon_alive():
    try:
        with _request(_base_url() + "/health", timeout=1.5) as resp:
            return resp.status in (200, 503)
    except Exception:
        return False


class _AuthInterceptor(QWebEngineUrlRequestInterceptor):
    """Adds the ZEE token header to every request the embedded browser makes."""

    def __init__(self, token):
        super().__init__()
        self._token = token

    def interceptRequest(self, info):
        if self._token:
            info.setHttpHeader(b"X-ZEE-TOKEN", self._token.encode("utf-8"))


class _SseWorker(QThread):
    """Background thread that tails /events and forwards frames to the GUI."""

    frame = Signal(str)

    def __init__(self, url):
        super().__init__()
        self._url = url
        self._stopped = threading.Event()

    def run(self):
        while not self._stopped.is_set():
            try:
                with _request(self._url, timeout=15) as resp:
                    for raw in resp:
                        if self._stopped.is_set():
                            return
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("data: "):
                            self.frame.emit(line[6:])
            except Exception:
                if self._stopped.is_set():
                    return
                time.sleep(2)

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

        self.token = QLineEdit(self._cfg.get("ZEE_TOKEN", ""))
        self.token.setPlaceholderText("(auto-generated)")
        form.addRow("Access token (ZEE_TOKEN):", self.token)

        self.voice = QComboBox()
        self.voice.setEditable(True)
        self.voice.addItems(["en-US-ChristopherNeural", "en-US-JennyNeural",
                             "en-GB-SoniaNeural", "en-AU-WilliamNeural"])
        self.voice.setCurrentText(self._cfg.get("ZEE_VOICE", "en-US-ChristopherNeural"))
        form.addRow("TTS voice (ZEE_VOICE):", self.voice)

        note = QLabel("Settings are applied when the daemon restarts "
                      "(tray icon → Restart Daemon).")
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
        if self._token:
            QWebEngineProfile.defaultProfile().setUrlRequestInterceptor(
                _AuthInterceptor(self._token))

        self._tray = self._make_tray()

        self._sse = _SseWorker(_base_url() + "/events")
        self._sse.frame.connect(self._on_event)
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
        restart = QAction("Restart Daemon", self)
        restart.triggered.connect(self._restart_daemon)
        quit_a = QAction("Quit", self)
        quit_a.triggered.connect(self._quit)
        for action in (show, settings, restart, quit_a):
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

    def closeEvent(self, event):
        # Closing keeps ZEE in the tray; the daemon keeps listening.
        self.hide()
        self._tray.showMessage("ZEE", "ZEE is still running in the background.",
                               QSystemTrayIcon.Information, 2000)
        event.ignore()

    def _open_settings(self):
        _SettingsDialog(self).exec()

    def _restart_daemon(self):
        try:
            with _request(_base_url() + "/shutdown", data=b"{}", timeout=5):
                pass
        except Exception as e:
            QMessageBox.warning(self, "ZEE", f"Could not stop the daemon: {e}")

    def _quit(self):
        self._sse.stop()
        self._sse.wait(2000)
        QApplication.instance().quit()

    # ----- SSE events -----
    def _on_event(self, data):
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return
        if payload.get("type") in ("wake", "approval"):
            self._show_window()


def main():
    """Entry point (also called from ``zee gui`` / ``zee start``)."""
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
