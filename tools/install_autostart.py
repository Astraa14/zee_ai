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
import base64
import os
import subprocess
import sys

import apppaths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_PS_SHORTCUT = r"""
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($env:ZEE_SHORTCUT)
$sc.TargetPath = $env:ZEE_TARGET
$sc.Arguments = $env:ZEE_ARGS
$sc.WorkingDirectory = $env:ZEE_WORKING_DIR
$sc.WindowStyle = 7
$sc.Save()
""".strip()


def _powershell_encoded(script, env=None):
    """Run a PowerShell script via -EncodedCommand with values in env vars.

    The script text is fixed; all values travel as environment variables or
    inside the base64 payload — nothing is interpolated into a command
    string, so quotes, apostrophes or ';' in paths can never break or
    extend the script.
    """
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=True,
        capture_output=True,
        text=True,
        env=run_env,
    )


def _launch_command(mode):
    """(target, args) to launch ZEE at login: frozen exe, else python zee.py."""
    if apppaths.frozen():
        return sys.executable, mode
    return _pythonw(), f'"{os.path.join(ROOT, "zee.py")}" {mode}'


def _pythonw():
    """Return a python executable that runs without a console (Windows), else sys.executable."""
    if os.name == "nt":
        alt = sys.executable.replace("python.exe", "pythonw.exe")
        if alt != sys.executable and os.path.exists(alt):
            return alt
    return sys.executable


def _install_windows(with_gui):
    startup = os.path.join(
        os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
    )
    if not os.path.isdir(startup):
        raise RuntimeError(f"Startup folder not found: {startup}")
    target, args = _launch_command("start" if with_gui else "daemon")
    shortcut = os.path.join(startup, "ZEE.lnk")
    _powershell_encoded(
        _PS_SHORTCUT,
        {
            "ZEE_SHORTCUT": shortcut,
            "ZEE_TARGET": target,
            "ZEE_ARGS": args,
            "ZEE_WORKING_DIR": ROOT,
        },
    )
    return f"Installed autostart shortcut: {shortcut}"


def _install_macos(with_gui=False):
    launch_dir = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(launch_dir, exist_ok=True)
    plist = os.path.join(launch_dir, "com.zee.assistant.plist")
    mode = "start" if with_gui else "daemon"
    program, script = _launch_command(mode)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.zee.assistant</string>
    <key>ProgramArguments</key>
    <array>
        <string>{program}</string>
        <string>{script}</string>
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
    program, script = _launch_command(mode)
    desktop = os.path.join(autostart, "zee.desktop")
    content = f"""[Desktop Entry]
Type=Application
Name=ZEE Assistant
Comment=Local voice assistant daemon
Exec={program} {script}
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
    parser = argparse.ArgumentParser(description="Register ZEE at login for the current user")
    parser.add_argument(
        "--gui",
        action="store_true",
        help="also autostart the desktop GUI window (daemon stays background)",
    )
    args = parser.parse_args(argv)
    path = install_autostart(with_gui=args.gui)
    print(f"ZEE autostart registered: {path}")


if __name__ == "__main__":
    main()
