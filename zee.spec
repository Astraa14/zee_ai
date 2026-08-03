# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the ZEE launcher. Build with:
#   pip install pyinstaller
#   pyinstaller zee.spec
# The bundle excludes the Vosk model (large); place it as "model/" next to
# the built executable — the daemon refuses to start the voice loop without it.
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = (
    collect_submodules("flask")
    + ["zee_core", "zee_voice", "zee_api", "events", "win_control", "tools.install_autostart"]
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
    excludes=["matplotlib", "PySide6", "PyQt5"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Zee",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
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