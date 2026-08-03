# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the ZEE desktop app (onedir).
#
#   pip install -r requirements-dev.txt
#   pip install -r requirements-gui.txt      # PySide6, needed to bundle the GUI
#   pyinstaller --noconfirm --clean zee.spec
#
# Output: dist/Zee/Zee.exe (onedir). The installer (scripts/make_installer.iss)
# packs that folder. The Vosk model is NOT bundled (hundreds of MB) — after
# install, drop it in %APPDATA%\Zee\model (see docs/packaging_windows.md).
#
# PySide6 + QtWebEngine ship their own PyInstaller hooks, which collect
# QtWebEngineProcess.exe and its resources automatically.
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = (
    collect_submodules("flask")
    + ["zee_core", "zee_voice", "zee_api", "events", "win_control",
       "apppaths", "tokenstore", "updater", "tools.install_autostart"]
)

datas = [
    ("templates", "templates"),
]

a = Analysis(
    ["zee.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "matplotlib", "tkinter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Zee",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # daemon/GUI run without a console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Zee",
)