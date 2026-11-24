# Ticket Assistant v3.1.1

Windows desktop application using PySide6 and Playwright to connect to a locally running Chrome session.

## Development setup

1. Install Python (the existing project has Python 3.11/3.13 bytecode; no supported-version matrix has been verified).
2. Run `install.bat` to create `.venv` and install `requirements.txt`.
3. Obtain a valid `license.json` from the project owner and place it beside `main.py`. Licenses and signing keys are intentionally not included in Git.
4. Run `run_chrome.bat`, then sign in to the ticket website manually.
5. Run `run.bat`.

The application connects to Chrome at `http://127.0.0.1:9222`. The launcher uses a local `ChromeProfile` directory, which must never be committed.

## Project layout

- `main.py`: application entry point and license validation.
- `ui/main_window.py`: desktop UI and worker thread.
- `bots/thaiticketmajor.py`: page detection, round selection, queue handling, zone and seat selection.
- `config/settings.py`: local browser and timing settings.
- `license/`: license validation and public verification key.
- `TicketAssistant_v3_1_1.spec`, `build_exe.bat`: Windows executable packaging.

## Current reported problems

The owner reports that selecting some show rounds and clicking to receive/join a queue do not work. These problems have not yet been reproduced or fixed. See `CLAUDE.md` for investigation starting points.

## License and build notes

The original builder documentation names 2026-09-30; `generate_tester_license.bat` currently specifies 2026-10-20. Both dates precede 2026-11-24. A valid owner-issued license is required for interactive testing; do not disable validation to work around this.

The `owner_tools/` directory, signing keys, issued licenses, archives, browser profiles, and generated builds are intentionally excluded. License-generation scripts require owner-only files and cannot run from a clean clone alone. `build_exe.bat` also expects a local `license.json` for distribution.

Keep visitor-presence/CAPTCHA checks manual and preserve the stop before payment. Do not commit cookies, session tokens, private keys, or unsanitized browser captures.
