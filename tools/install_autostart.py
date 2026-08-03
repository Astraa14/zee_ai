"""Register ZEE to start at login for the current user.

Per-user autostart (not a system service) because the GUI/voice loop need an
interactive desktop session.

- Windows: a shortcut in the Startup folder that runs ``zee start``
  (daemon + GUI) via pythonw so no console window flashes at login.
- macOS: a launchd LaunchAgent plist that starts the daemon.
- Linux: a ``~/.config/autostart/zee.desktop`` entry that starts the daemon.

Run with ``python -m tools.install_autostart [--gui]`` or ``zee install-autostart [--gui]``.
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pythonw():
    """Return a python executable that runs without a console (Windows), else sys.executable."""
    if os.name == "nt":
        alt = sys.executable.replace("python.exe", "pythonw.exe")
        if alt != sys.executable and os.path.exists(alt):
            return alt
    return sys.executable


def _install_windows(with_gui):
    startup = os.path.join(os.environ.get("APPDATA", ""),
                           "Microsoft", "Windows", "Start Menu",
                           "Programs", "Startup")
    if not os.path.isdir(startup):
        raise RuntimeError(f"Startup folder not found: {startup}")
    target = _pythonw()
    args = f'"{os.path.join(ROOT, "zee.py")}" {"start" if with_gui else "daemon"}'
    shortcut = os.path.join(startup, "ZEE.lnk")
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{shortcut}'); "
        f"$sc.TargetPath = '{target}'; "
        f"$sc.Arguments = '{args}'; "
        f"$sc.WorkingDirectory = '{ROOT}'; "
        "$sc.WindowStyle = 7; "
        "$sc.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    return f"Installed autostart shortcut: {shortcut}"


def _install_macos(with_gui=False):
    launch_dir = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(launch_dir, exist_ok=True)
    plist = os.path.join(launch_dir, "com.zee.assistant.plist")
    mode = "start" if with_gui else "daemon"
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.zee.assistant</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{os.path.join(ROOT, "zee.py")}</string>
        <string>{mode}</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
</dict>
</plist>
"""
    with open(plist, "w", encoding="utf-8") as f:
        f.write(content)
    subprocess.run(["launchctl", "unload", plist], capture_output=True)
    subprocess.run(["launchctl", "load", plist], check=True)
    return plist


def _install_linux(with_gui=False):
    autostart = os.path.expanduser("~/.config/autostart")
    os.makedirs(autostart, exist_ok=True)
    mode = "start" if with_gui else "daemon"
    desktop = os.path.join(autostart, "zee.desktop")
    content = f"""[Desktop Entry]
Type=Application
Name=ZEE Assistant
Comment=Local voice assistant daemon
Exec={sys.executable} {os.path.join(ROOT, "zee.py")} {mode}
Terminal=false
X-GNOME-Autostart-enabled=true
"""
    with open(desktop, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(desktop, 0o755)
    return desktop


def install_autostart(with_gui=False):
    """Register autostart for the current platform. Returns the artefact path."""
    if os.name == "nt":
        return _install_windows(with_gui)
    if sys.platform == "darwin":
        return _install_macos(with_gui)
    return _install_linux(with_gui)


def main(argv=None):
    if shutil.which("python") is None or sys.platform == "win32":
        pass  # python is running us already
    parser = argparse.ArgumentParser(description="Register ZEE at login for the current user")
    parser.add_argument("--gui", action="store_true",
                        help="also autostart the desktop GUI window (daemon stays background)")
    args = parser.parse_args(argv)
    path = install_autostart(with_gui=args.gui)
    print(f"ZEE autostart registered: {path}")


if __name__ == "__main__":
    main()
