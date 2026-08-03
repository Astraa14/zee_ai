# Contributing to ZEE

Thanks for helping with ZEE. This file covers the workflow; architecture
notes live in the README, packaging in `docs/packaging_windows.md`.

## Project layout

- `zee_core.py` — brain glue: memory, notes, approvals, TTS, Ollama streaming,
  `sanitize_input`, `safe_run`, `--doctor`
- `zee_api.py` — Flask API: token auth, rate limits, `/health`, `/events`,
  `/shutdown`, `/update`
- `zee_voice.py` — wake-word ("zee") voice loop, emits SSE wake events
- `zee.py` — CLI (`daemon` / `gui` / `start` / `stop` / `install-autostart`)
- `events.py` — SSE broadcaster; `tokenstore.py` — keyring-backed token store;
  `apppaths.py` — frozen-aware data dirs; `updater.py` — self-update helper
- `win_control.py` — desktop tools (Windows/macOS/Linux), automation gated
- `gui/zee_gui.py` — PySide6 tray app; `tools/install_autostart.py` — login items
- `tests/` — pytest suite; `.github/workflows/` — CI + Windows packaging

## Setting up

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pip install -r requirements-gui.txt   # only if you touch the GUI
```

## Rules of thumb

1. **No shell strings.** External commands go through `safe_run` /
   `win_control.safe_run` as argument **lists** with a `timeout`. Never use
   `os.system`, `shell=True`, or string interpolation into a command.
2. **Sanitize every input** (`sanitize_input`) before it reaches a
   subprocess, URL, or file path.
3. **Automation stays opt-in.** Anything that touches the desktop/Discord/
   Messenger must return an error unless `ZEE_ALLOW_AUTOMATION=1` and be
   covered by the approval flow for destructive actions.
4. **Log, don't print.** Use the `logging` module (`log = logging.getLogger(...)`);
   CLI-facing *output* (doctor report, autostart) may use `print`.
5. **Tokens never go in logs or code.** Auth is `X-ZEE-TOKEN` /
   `Authorization: Bearer`; storage is keyring-first with file fallback.
6. **Data goes in `%APPDATA%\Zee` when installed** — never `Program Files`.
   New state files must route through `apppaths.data_path()`.

## Running checks

```sh
python -m pytest tests -q          # tests
python -m ruff check .             # lint
python -m compileall -q . -x "model"   # syntax check (approx. CI exclusion)
python zee_core.py --doctor        # environment health
```

CI runs the first three on Ubuntu **and** Windows — keep tests OS-agnostic;
Windows-only behavior lives behind `os.name` checks (see `win_control.py`).

## Submitting changes

- Keep commits small and descriptive (`Short summary in the imperative`).
- Run the full test suite + ruff before pushing.
- For Windows packaging changes, verify with the `build-windows` workflow
  (Actions → build-windows → Run workflow) or push a `v*` tag to produce a
  release with installer artifacts.
- Update `CHANGELOG.md` under `[Unreleased]` for user-visible changes.

## Security issues

Report vulnerabilities privately via GitHub issues (do not open a public
issue containing the exploit). Key security surface: auth, rate limits,
sanitization, approval flow, updater verification (SHA-256 before apply).
