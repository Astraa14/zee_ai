# Changelog

All notable changes to ZEE. Format loosely based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Self-update: `POST /update` (token-protected) and `updater.py`
  (manifest fetch, SHA-256 verification, silent-installer apply / atomic
  exe swap). Upgrades no longer require uninstalling.
- `zee doctor` CLI command; `build_windows.bat` now smoke-tests the bundle
  with `Zee.exe doctor` before packaging and supports an optional onefile
  build (`ZEE_ONEFILE=1`).
- `updater.py --shutdown/--restart`: stops the running daemon via the
  protected `POST /shutdown` before an atomic exe swap and relaunches it
  afterward, so bare-EXE updates apply immediately.
- `zee.spec` declares PySide6 QtWebEngine hidden imports + data files
  explicitly (belt-and-braces alongside PyInstaller's own hooks).
- `zee install-model` (`tools/download_vosk_model.py`): optional post-install
  Vosk model installer — downloads + safely unpacks a model into the data dir,
  so large speech models stay out of the EXE. CI `packaging` job builds a
  headless onedir bundle to smoke-test the spec; `requirements-dev.txt` now
  includes PySide6 for GUI tests.

## [1.0.0] - 2026-08-03

Jarvis is renamed **ZEE** and becomes a desktop app.

### Added
- Desktop app: background daemon (`zee.py daemon`), PySide6 tray GUI
  (`gui/zee_gui.py`, embed of the web UI + native SSE `/events` client),
  `zee.py start/gui/stop/install-autostart` CLI.
- SSE `/events` broadcaster (`events.py`); voice loop emits `wake` events,
  approval flow emits `approval` events (GUI raises on both).
- `POST /shutdown` endpoint (token-protected) with graceful stop.
- Packaging: `zee.spec` (PyInstaller, onedir), Inno `scripts/make_installer.iss`,
  `scripts/build_windows.bat` (incl. optional signtool signing),
  `packaging/` templates (systemd / LaunchAgent / desktop), `requirements-dev.txt`.
- `build-windows` GitHub Actions workflow producing an installer + artifacts
  (and a GitHub Release on `v*` tags).
- Per-user data dir for installed builds (`%APPDATA%\Zee`) via `apppaths.py`;
  settings in `~/.zee/zee.conf`; autostart installer task (registry Run /
  Startup shortcut).
- `python-keyring`-backed token storage (`tokenstore.py`) with file fallback.

### Changed
- Files/identifiers renamed Jarvis → ZEE (`zee_core.py`, `zee_api.py`,
  `zee_voice.py`, `zee.py`, `start_zee.bat`); data files `zee_memory.json`,
  `zee_notes.txt`, `zee_pending_approvals.json`, `zee_approvals.log`, `zee.log`.
- Env vars now `ZEE_*` (`ZEE_TOKEN`, `ZEE_ALLOW_AUTOMATION`, `ZEE_HTTPS`, ...).
- Auth header accepted as `X-ZEE-TOKEN` or `Authorization: Bearer`.
- GUI HTTP switched to `requests`; `PySide6-Addons` replaces the discontinued
  standalone `PySide6-WebEngine` package.

### Security
- Automation tools strictly opt-in (`ZEE_ALLOW_AUTOMATION`, default off).
- Payload size cap (16 KB default) and per-token `/ask` rate limits.
- Update payloads verified by SHA-256 before application.

## [0.1.0] - 2026-08-03

First hardened Jarvis release (pre-rename).

### Added
- Token-based auth (`X-ZEE-TOKEN`/Bearer) on `/ask`, `/approve`, `/deny`.
- `sanitize_input` + `safe_run`; all `os.system` uses replaced; approval
  audit to `approvals.log`; approvals single-use and expire.
- Cell size guards, rotating logs, `/health`, per-token rate limiting,
  CI (tests on Ubuntu + Windows), pytest suite, ruff linting.