"""
Offline Playwright harness for the ThaiTicketMajor bot.

Every request is answered from tests/fixtures through page.route(), and any
URL without a fixture is aborted, so the tests never contact a live ticketing
site and never perform a purchase.
"""

import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sync_api = pytest.importorskip("playwright.sync_api")

from bots.thaiticketmajor import ThaiTicketMajorBot  # noqa: E402


ROUTES = {
    ("event.thaiticketmajor.com", "/performance/multi.html"): "performance_multi_block.html",
    ("event.thaiticketmajor.com", "/performance/presale.html"): "performance_presale.html",
    ("event.thaiticketmajor.com", "/performance/queue-link.html"): "performance_queue_link.html",
    ("booking.thaiticketmajor.com", "/booking/3m/zones.php"): "booking_zones.html",
    ("wait.thaiticketmajor.com", "/entry"): "waiting_room_entry.html",
    ("wait.thaiticketmajor.com", "/view"): "queue_view.html",
}


class FakeSite:
    def __init__(self):
        self.requests = []
        # path -> seconds to delay the response (simulates on-sale load)
        self.delays = {}

    def handle(self, route):
        request = route.request
        parsed = urlparse(request.url)
        self.requests.append(request.url)
        fixture = ROUTES.get((parsed.hostname, parsed.path))

        if fixture is None:
            route.abort()
            return

        delay = self.delays.get(parsed.path, 0)
        if delay:
            time.sleep(delay)

        route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=(FIXTURES / fixture).read_text(encoding="utf-8"),
        )

    def count(self, fragment):
        return sum(1 for url in self.requests if fragment in url)


def _launch(playwright):
    exe = os.environ.get("TTM_TEST_CHROMIUM")
    if not exe and Path("/opt/pw-browsers/chromium").exists():
        exe = "/opt/pw-browsers/chromium"
    kwargs = {"headless": True}
    if exe:
        kwargs["executable_path"] = exe
        kwargs["args"] = ["--no-sandbox"]
    return playwright.chromium.launch(**kwargs)


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        b = _launch(p)
        yield b
        b.close()


@pytest.fixture
def site(browser):
    context = browser.new_context()
    fake = FakeSite()
    context.route("**/*", fake.handle)
    page = context.new_page()
    fake.page = page
    fake.context = context
    yield fake
    context.close()


@pytest.fixture
def make_bot():
    def factory(**kwargs):
        logs = []
        params = dict(
            event_url="",
            quantity=1,
            status_callback=logs.append,
            stop_callback=lambda: False,
            onsale_refresh_seconds=1,
        )
        params.update(kwargs)
        bot = ThaiTicketMajorBot(**params)
        bot.logs = logs
        return bot

    return factory
