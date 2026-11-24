# Development handoff for Claude

## Requested work

Investigate and fix the owner's reports:
1. Some show rounds cannot be selected.
2. Clicking to receive/join the queue does not work in some cases.

These are user reports, not reproduced diagnoses. No live ticketing flow was tested during repository preparation.

## Read first

- `README.md` for setup and license limitations.
- `ui/main_window.py`: `show_round` is a free-text QLineEdit; `start_bot` passes the value or AUTO.
- `bots/thaiticketmajor.py`: `run`, `handle_performance_page`, `read_show_rounds`, `select_show_round`, `detect_state`, `handle_waiting_room_entry`, `_queue_detected`, and `_attach_queue_status_monitor`.

## Investigation plan

- Obtain an affected event URL, requested date/time, actual result, and sanitized logs from the owner.
- Distinguish performance-page round links from the booking-page `select#rdId` dropdown.
- Check matching of date/time, disabled options, dynamic rendering, redirects, and new tabs after clicking.
- Check that round_done is only set after selection is confirmed and the intended page transition has occurred.
- Distinguish waiting-room entry, manual verification, an active queue, and the booking page. Avoid refreshing an active queue or losing its session.
- Reproduce using sanitized HTML fixtures or mocked Playwright pages where feasible, then add regression checks for the confirmed bug.
- Report the root cause, changed behavior, tests performed, and any remaining live verification.

## Boundaries

Keep license validation intact. Signing tools and keys remain owner-only. Never commit license.json, Chrome profiles, credentials, cookies, or session tokens. Keep CAPTCHA/visitor verification manual and stop before payment. Do not initiate live purchases while investigating.
