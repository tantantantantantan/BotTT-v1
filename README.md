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

## Round selection and queue entry fixes

The owner's reports ("some show rounds cannot be selected", "clicking to receive/join the queue does not work") were traced to these causes in `bots/thaiticketmajor.py`, reproduced with offline fixtures and fixed:

- Performance page: only the first event block was read, and rounds whose `query` differed from the block's majority were dropped, so those rounds raised "ไม่พบรอบการแสดง". All blocks are now read; `ticket_type` filters blocks when it matches.
- Round text matching: whitespace/newlines, `18.00` vs `18:00` and the `น.` suffix broke matches, while a partial match let `8:00` pick `18:00`. Matching is now normalized and token based (`match_rounds`).
- A requested round that is visible but not open yet used to stop the bot; it now waits and reloads the performance page.
- Booking dropdown `select#rdId`: AUTO replaced the round chosen on the performance page with the first option; disabled/placeholder options were not handled; a hidden native select timed out; `round_done` was set without checking the page loaded the round. The round is now confirmed (URL `rdId` / zone map) and retried at most 3 times; a missing or disabled round raises instead of continuing with another round.
- Queue click: under on-sale load the round/queue click waited for a slow navigation, then the fallback `el.click()`/`goto` aborted or crashed it. A started navigation is now awaited without re-navigating; links that open a new tab are followed.
- Queue detection used free page text: FAQ text on the performance page ("waiting room", "เข้าคิว") stopped the pre-sale refresh, and the queue page text "เข้าคิวแล้ว" was treated as the entry page. Detection now uses the queue host and real DOM (Join button, Queue-it widgets).
- Join waiting room: a disabled button was "clicked" with a synthetic event that bot-detect ignores, and success was reported anyway. The bot now waits for the button to be enabled, clicks once with a real click, confirms the page changed, and otherwise retries later. CAPTCHA/visitor verification stays manual.


## Speed and accuracy changes

Measured with the offline fixtures (local headless Chromium; over CDP to a real Chrome each saved round-trip is worth more):

| Step | Before | After |
|---|---|---|
| Read rounds on the performance page | 107 ms | 3 ms |
| Read a 40-zone image map | 957 ms | ~45 ms |
| Click zone until the seat map is ready | 3.6 s | ~0.25 s |

- Rounds and zones are read with one in-page JavaScript call instead of one call per element/attribute.
- Zones are clicked with a real mouse click on their position in the map image. The old Playwright click on `<area>` always timed out (2.2 s) and fell back to a synthetic JS call.
- A zone that does not open (sold out / popup) now fails over to the next zone in priority instead of stopping the bot; a site popup ends the wait immediately. When all zones fail, a new scan cycle starts.
- While waiting for the sale, the bot watches the local DOM every 0.2 s, so a round button enabled by the page's own JavaScript is clicked without waiting for the next reload. This sends no extra requests to the site.
- Removed a fixed 350 ms delay after the zone step.

### Tests

```
pip install playwright pytest
python -m pytest -q tests
```

The tests use sanitized HTML fixtures in `tests/fixtures/` served through `page.route()`; every other request is aborted, so no live site is contacted. Set `TTM_TEST_CHROMIUM` to a Chromium/Chrome executable if Playwright's bundled browser is not installed. The fixtures model the selectors the bot uses; they are not captures of the live site, so live verification on a real event is still required.

## License and build notes

The original builder documentation names 2026-09-30; `generate_tester_license.bat` currently specifies 2026-10-20. Both dates precede 2026-11-24. A valid owner-issued license is required for interactive testing; do not disable validation to work around this.

The `owner_tools/` directory, signing keys, issued licenses, archives, browser profiles, and generated builds are intentionally excluded. License-generation scripts require owner-only files and cannot run from a clean clone alone. `build_exe.bat` also expects a local `license.json` for distribution.

Keep visitor-presence/CAPTCHA checks manual and preserve the stop before payment. Do not commit cookies, session tokens, private keys, or unsanitized browser captures.
