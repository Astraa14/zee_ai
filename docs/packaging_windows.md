# Packaging ZEE for Windows (EXE + installer)

Builds a single **onedir** bundle (`dist\Zee\Zee.exe`) with PyInstaller and an
optional **Inno Setup** installer (`artifacts\Zee-Setup-<version>.exe`).
The bundle includes the Flask API, the voice loop, and the PySide6 GUI/tray;
it does **not** include the Vosk model (hundreds of MB — see below).

## Build machine prerequisites

- Windows 10/11 with Python **3.12** (PySide6 wheels for other versions may
  lag behind; the CI uses 3.12)
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) (optional — needed only
  for the installer)
- `signtool.exe` from the Windows SDK (optional — needed only for signing)

```bat
pip install -r requirements.txt -r requirements-gui.txt -r requirements-dev.txt
```

## Build commands

```bat
REM 1. PyInstaller onedir bundle + (if Inno is installed) the installer:
scripts\build_windows.bat

REM ...or step by step:
pyinstaller --noconfirm --clean zee.spec
iscc scripts\make_installer.iss
```

Outputs:

- `dist\Zee\Zee.exe` — the onedir app (run `Zee.exe start` / `daemon` / `stop`)
- `artifacts\Zee-Setup-1.0.0.exe` — Inno installer (installs to
  `%ProgramFiles%\ZEE`, Start Menu shortcuts, optional desktop icon and
  registry `Run` autostart task)

## Where files live when installed

When running from source, ZEE keeps state files next to the code. When
running the **installed exe** (under `Program Files`, read-only), everything
writable moves to `%APPDATA%\Zee`:

| Item | Installed location |
| ---- | ------------------ |
| API token (`.zee_token`) | `%APPDATA%\Zee\.zee_token` (mirrored to Windows Credential Locker via python-keyring if installed) |
| Logs (`zee.log`, `zee_approvals.log`) | `%APPDATA%\Zee\` |
| Memory / notes / pending approvals | `%APPDATA%\Zee\` |
| Self-signed HTTPS cert (`cert.pem`/`key.pem`) | `%APPDATA%\Zee\` (generated on first start) |
| Settings (`~\.zee\zee.conf`) | per-user, as in source mode |
| Web UI templates | inside the bundle (read-only) |
| Vosk model | `%APPDATA%\Zee\model\` (or `model\` next to the exe) |

## Vosk model (voice loop)

The model is excluded from the installer. After installing, download one
from <https://alphacephei.com/vosk/models> (e.g.
`vosk-model-small-en-us-0.15`, ~40 MB) and unpack it to
`%APPDATA%\Zee\model` (i.e. the folder that contains `am/`, `conf/`, ...).
Without it the API/GUI still work — only the wake-word loop refuses to start.
`Zee.exe daemon` will log a clear message. Run `Zee.exe --help`/doctor for
diagnostics.

## First run

- The daemon generates an API token on first start (auto-saved as above) —
  or set `ZEE_TOKEN` / use the GUI **Settings** dialog (tray icon → Settings),
  which edits `~\.zee\zee.conf` (`ZEE_TOKEN`, `ZEE_ALLOW_AUTOMATION`,
  `ZEE_VOICE`) for the next daemon start.
- Automation stays **off** until `ZEE_ALLOW_AUTOMATION=1`.

## Self-update

`POST /update` (token-protected) kicks off a background update from either a
manifest URL or a direct asset URL + `sha256`:

```json
{"manifest": "https://example.com/zee/latest.json"}
{"url": "https://example.com/Zee-Setup-1.0.2.exe", "sha256": "<hex>"}
```

The manifest is plain JSON: `{"version": "...", "url": "...", "sha256": "..."}`.
The payload is downloaded to a temp dir, **SHA-256 verified**, then applied:
a `*Setup*.exe` runs silently (Inno `/VERYSILENT /SUPPRESSMSGBOXES` — it
upgrades in place over the same AppId), any other file is atomically swapped
for `Zee.exe` (takes effect next start). `python -m updater --manifest <url> --apply`
does the same from the command line. Keep a manifest at a stable URL and
publish new installer artifacts there when tagging releases.

## Code signing

Recommended before distribution; without a certificate Windows SmartScreen
will warn.

1. Export your code-signing cert as a PFX; base64 it:
   ```bat
   certutil -encode cert.pfx artifacts\code-sign.pfx.b64
   ```
2. Run the build with signing enabled:
   ```bat
   set ZEE_SIGNING=1
   set WINDOWS_CERT_BASE64_FILE=%cd%\artifacts\code-sign.pfx.b64
   set CERT_PASSWORD=YourPfxPassword
   scripts\build_windows.bat
   ```
   The script signs `Zee.exe` and the installer with
   `signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256`.
3. Verify:
   ```bat
   signtool verify /pa artifacts\Zee-Setup-1.0.0.exe
   ```

In CI, the same path works by setting the repo **secrets**
`WINDOWS_CERT_BASE64` (the base64 text itself), `CERT_PASSWORD`, and a
`SIGNING` toggle — see `.github/workflows/build-windows.yml`.

## GitHub Actions

Tag a release to produce and attach a Windows installer automatically:

```bat
git tag v1.0.0
git push origin v1.0.0
```

The `build-windows` workflow builds the bundle + installer, uploads them as
artifacts, and (for tags) creates a GitHub Release with the installer
attached. The build can also be triggered manually (Actions →
**build-windows** → *Run workflow*).

## Minimum system requirements

- Windows 10 64-bit or later
- **Ollama** installed and running (`ollama serve`) with a model pulled
  (`ollama pull llama3.2:latest`)
- ~500 MB free disk (bundle with Qt WebEngine) + Vosk model if voice is used
- A microphone and speaker for the voice loop

## Acceptance checklist

- [ ] `dist\Zee\Zee.exe` runs `start` from a fresh install folder
- [ ] API endpoints respond and reject without `ZEE_TOKEN`; rate limits active
- [ ] Approvals written to `%APPDATA%\Zee\zee_approvals.log`
- [ ] GUI embeds the web UI, tray icon works, close hides to tray
- [ ] Wake word raises the GUI (with the model in `%APPDATA%\Zee\model`)
- [ ] `Zee.exe stop` shuts the daemon down cleanly
- [ ] Installer installs/uninstalls cleanly, Start Menu + autostart tasks work
- [ ] CI: tests pass on ubuntu + windows; `build-windows` artifact produced
- [ ] (Distribution) EXE + installer signed and `signtool verify` passes

## Notes

- `sseclient-py` is **not** needed: the GUI implements native SSE (reads
  `data:` frames straight off the `/events` stream).
- Prefer **onedir** for development installs (faster builds, faster start);
  switch `zee.spec` to a onefile `EXE(..., a.binaries, a.zipfiles, a.datas)`
  form only if you need a single portable exe (expect slower cold start).
- If you add Windows *service* mode later, `pywin32` is already in
  `requirements-dev.txt`.
