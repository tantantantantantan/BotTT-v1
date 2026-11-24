import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from urllib.parse import urlparse

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


# ==============================================================
# ROUND MATCHING HELPERS
# ==============================================================

# A clock time such as 18:00 / 18.00 / 8:05. Not part of a longer number
# or a dotted date like 16.08.2569.
_TIME_RE = re.compile(
    r"(?<![\d.:/])(\d{1,2})\s*[:.]\s*(\d{2})(?![\d]|[.:/]\d)"
)

_PLACEHOLDER_RE = re.compile(
    r"^[\s\-–—=*]*(เลือก|กรุณา|please|select|choose)",
    flags=re.I,
)


def normalize_round_text(value):
    """
    Normalize round text for matching:
    - lower case, collapse whitespace/newlines, commas and pipes
    - 18.00 / 08:00 -> 18:00 / 8:00
    - drop the Thai "น." or "hrs" suffix after a time
    """
    text = str(value or "").lower()
    text = _TIME_RE.sub(
        lambda m: f"{int(m.group(1))}:{m.group(2)}",
        text,
    )
    text = re.sub(
        r"(\d:\d{2})\s*(?:น\.?|hrs?\.?)(?=\s|$|\))",
        r"\1",
        text,
    )
    text = re.sub(r"[\s,|]+", " ", text)
    return text.strip()


def match_rounds(query, rounds):
    """
    Return every round that matches the user's show_round text, best tier
    first. Each round is a dict with "value" (rdId, may be empty),
    "text" (date + time) and optionally "time".

    Tiers:
      1. rdId exact (also accepts "rdId=12345")
      2. full text or visible time exact (after normalization)
      3. every query token appears as a whole token in the round text
      4. query appears in the text, but never inside another number/time
         (so "8:00" does not pick "18:00")
    """
    raw = str(query or "").strip()

    if not raw:
        return []

    raw_value = re.sub(r"^rdid\s*=\s*", "", raw, flags=re.I).lower()
    q = normalize_round_text(raw)
    q_tokens = q.split()

    def full_text(item):
        return normalize_round_text(item.get("text", ""))

    def tokens(item):
        return set(
            full_text(item).split()
            + normalize_round_text(item.get("time", "")).split()
        )

    boundary = re.compile(
        r"(?<![0-9:])" + re.escape(q) + r"(?![0-9:])"
    )

    tiers = [
        lambda item: (
            str(item.get("value") or "").strip().lower() == raw_value
        ),
        lambda item: q in (
            full_text(item),
            normalize_round_text(item.get("time", "")),
        ),
        lambda item: bool(q_tokens) and all(
            token in tokens(item) for token in q_tokens
        ),
        lambda item: bool(boundary.search(full_text(item))),
    ]

    for matches_tier in tiers:
        found = [item for item in rounds if matches_tier(item)]

        if found:
            return found

    return []


def is_placeholder_option(value, text):
    value = str(value or "").strip()

    if value in ("", "0", "-1"):
        return True

    return bool(_PLACEHOLDER_RE.match(str(text or "")))


class TTMState:
    LOGIN = "LOGIN"
    WAIT_SALE = "WAIT_SALE"
    QUEUE_ENTRY = "QUEUE_ENTRY"
    IN_QUEUE = "IN_QUEUE"
    ROUND = "ROUND"
    ZONE = "ZONE"
    SEAT = "SEAT"
    UNKNOWN = "UNKNOWN"


class ThaiTicketMajorBot:
    """
    ThaiTicketMajor v3.1 Fast Snapshot

    Flow:
        Existing Chrome/CDP
        -> Login/Queue handled by normal browser session
        -> Select round
        -> Select zone
        -> Read seat candidates from DOM
        -> Click seat in browser
        -> Read validateseat.php response
        -> Retry next candidate if validation fails
        -> Click #booknow
        -> Read bookingseats.php response
        -> STOP immediately after seat confirmation
        -> No payment automation

    Important:
        - Does NOT hard-code Cookie / PHPSESSID / WAF tokens.
        - Does NOT replay copied request headers.
        - Browser creates validateseat.php and bookingseats.php requests itself.
        - We only observe the responses and use them as truth.
    """

    CDP_URL = "http://127.0.0.1:9222"

    def __init__(
        self,
        event_url,
        quantity,
        selection_mode="AUTO",
        preferred_zone="",
        preferred_price=0,
        min_price=0,
        max_price=0,
        ticket_type="",
        show_round="AUTO",
        sale_datetime="AUTO",
        prelogin_minutes=60,
        presale_refresh_seconds=15,
        onsale_refresh_seconds=2,
        status_callback=print,
        stop_callback=lambda: False,
        progress_callback=None,
    ):
        self.event_url = (event_url or "").strip()
        self.quantity = max(1, int(quantity or 1))
        self.selection_mode = (selection_mode or "AUTO").strip().upper()
        self.preferred_zone = (preferred_zone or "").strip().upper()

        # Zone Priority รองรับหลาย Zone คั่นด้วย comma
        # เช่น: A1,A2,B1,B2
        self.preferred_zones = [
            zone.strip().upper()
            for zone in re.split(r"[,，]", self.preferred_zone)
            if zone.strip()
        ]

        try:
            self.preferred_price = int(preferred_price or 0)
        except Exception:
            self.preferred_price = 0

        # ช่วงราคาตั๋ว เช่น 2500-3500
        # 0 / ค่าว่าง = ไม่จำกัดด้านนั้น
        try:
            self.min_price = int(min_price or 0)
        except Exception:
            self.min_price = 0

        try:
            self.max_price = int(max_price or 0)
        except Exception:
            self.max_price = 0

        # กันกรอกสลับ เช่น min=3500 max=2500
        if (
            self.min_price > 0
            and self.max_price > 0
            and self.min_price > self.max_price
        ):
            self.min_price, self.max_price = (
                self.max_price,
                self.min_price,
            )

        self.ticket_type = (ticket_type or "").strip()

        # รอบการแสดง:
        # ค่าว่าง = AUTO
        raw_show_round = (show_round or "").strip()
        self.show_round = (
            raw_show_round
            if raw_show_round
            else "AUTO"
        )

        self.sale_datetime = (
            sale_datetime or "AUTO"
        ).strip()

        try:
            self.prelogin_minutes = max(
                0,
                int(prelogin_minutes or 60)
            )
        except Exception:
            self.prelogin_minutes = 60

        try:
            self.presale_refresh_seconds = max(
                5,
                int(presale_refresh_seconds or 15)
            )
        except Exception:
            self.presale_refresh_seconds = 15

        try:
            self.onsale_refresh_seconds = max(
                1,
                int(onsale_refresh_seconds or 2)
            )
        except Exception:
            self.onsale_refresh_seconds = 2

        self._presale_wait_done = False

        self.status = status_callback or print

        # Countdown / queue progress: updated in place by the UI instead
        # of appending a new log line every second. Without a progress
        # callback (CLI) it falls back to the normal log.
        self.progress = progress_callback or self.status
        self.should_stop = stop_callback or (lambda: False)

        self.selected_round = None
        self.selected_zone = None
        self.selected_seats: List[Dict] = []

        # Zone failover / popup guard
        self._attempted_zones = set()
        self._request_zone_switch = False
        self._seat_failure_reason = ""
        self._last_dialog_message = ""

        # Queue network monitor
        self._queue_status_listener_attached = False
        self._queue_last_progress = None
        self._queue_last_ahead = None
        self._queue_last_number = None
        self._queue_last_log_time = 0.0
        self._queue_redirect_seen = False
        self._queue_status_page = None

        # Round click may open a new tab; run() switches to it
        self._pending_page = None
        self._round_attempts = 0

        # Waiting room join: one click at a time, throttled logs
        self._last_join_click_time = 0.0
        self._throttle = {}

        # v3.1 Fast Snapshot telemetry / stale guard
        self._scan_cycle = 1
        self._perf_last = {}
        self._consecutive_seat_failures = 0
        self._stale_rescan_threshold = 5

    # ==========================================================
    # MAIN
    # ==========================================================

    def run(self):
        self.status("🔗 Connecting to existing Google Chrome...")

        with sync_playwright() as p:
            page = None

            try:
                browser = p.chromium.connect_over_cdp(self.CDP_URL)
                self.status("✅ Connected to Google Chrome")

                page = self.find_ttm_page(browser)

                if page is None:
                    raise Exception(
                        "ไม่พบ Tab ThaiTicketMajor ใน Chrome ที่เปิดด้วย "
                        "--remote-debugging-port=9222"
                    )

                self.status("✅ พบ Tab ThaiTicketMajor เดิม")
                self.status(f"🌐 {page.url}")

                if self.is_booking_page(page):
                    self.status("🟢 Booking Session พร้อมใช้งาน")
                    self.status("🔐 Skip Login Check")
                else:
                    self.check_login(page)

                # ถ้ามี booking tab อยู่แล้ว อย่า goto ซ้ำ
                if not self.is_booking_page(page):
                    if self.event_url and "signin" not in self.event_url.lower():
                        self.status("🌐 Opening Event...")
                        page.goto(
                            self.event_url,
                            wait_until="domcontentloaded",
                            timeout=60000,
                        )
                        page.wait_for_timeout(450)

                last_state = None
                round_done = False
                zone_done = False

                while not self.should_stop():

                    # ==================================================
                    # ROUND LINK OPENED A NEW TAB -> FOLLOW IT
                    # ==================================================
                    new_page = self.take_pending_page()

                    if new_page is not None:
                        page = new_page
                        last_state = None

                        try:
                            page.bring_to_front()
                        except Exception:
                            pass

                    # ==================================================
                    # PRE-SALE WAIT / PRE-LOGIN
                    # ==================================================
                    if any(
                        token in (page.url or "").lower()
                        for token in (
                            "/performance/",
                            "/concert/",
                        )
                    ):
                        self.wait_for_sale_if_needed(page)

                    # ==================================================
                    # PERFORMANCE PAGE ENTRY (รอบขายทั่วไป)
                    # ==================================================
                    if any(
                        token in (page.url or "").lower()
                        for token in (
                            "/performance/",
                            "/concert/",
                        )
                    ):
                        handled = self.handle_performance_page(page)

                        if handled:
                            last_state = None
                            page.wait_for_timeout(80)
                            continue

                    # ==================================================
                    # DIRECT BOOKING ENTRY
                    # ==================================================
                    self.handle_direct_booking_entry(page)

                    # ==================================================
                    # CONDITIONS / TERMS PAGE
                    # ==================================================
                    if (
                        "verify_condition.php"
                        in (page.url or "").lower()
                    ):
                        self.handle_verify_condition(page)
                        last_state = None
                        page.wait_for_timeout(80)
                        continue

                    # ==================================================
                    # ROUND MUST RUN BEFORE ZONE
                    #
                    # บางหน้า TTM มี Zone Map แสดงพร้อมกับ #rdId และ
                    # dropdown อาจมี value ถูกเลือกไว้แล้ว ทำให้
                    # detect_state() เดิมมองเป็น ZONE และข้าม Round
                    # ==================================================

                    if not round_done:
                        has_round_dropdown = False

                        try:
                            round_select = page.locator("select#rdId").first

                            if round_select.count() > 0:
                                valid_options = round_select.locator(
                                    'option[value]:not([value=""])'
                                )
                                has_round_dropdown = (
                                    valid_options.count() > 0
                                )

                        except Exception as ex:
                            self.status(
                                f"⚠️ Round pre-check: {ex}"
                            )

                        if has_round_dropdown:
                            self.status(
                                "🎭 พบ Round Dropdown → เลือกรอบก่อน Zone"
                            )

                            # round_done only after the site confirmed
                            # the round (URL rdId / zone map loaded).
                            # Errors (round not found / disabled) stop
                            # the bot instead of booking another round.
                            round_done = self._select_round_with_retry(
                                page
                            )
                            last_state = None
                            page.wait_for_timeout(150)
                            continue

                    # Direct queue landing is valid:
                    # some events redirect from round button straight to wait.thaiticketmajor.com
                    try:
                        current_url = (page.url or "").lower()

                        if (
                            "wait.thaiticketmajor.com" in current_url
                            and "/view" in current_url
                        ):
                            self._attach_queue_status_monitor(page)
                            self.status(
                                "🎟️ อยู่หน้า Queue โดยตรงหลังเลือกรอบ"
                            )
                    except Exception:
                        pass

                    state = self.detect_state(page)

                    if state != last_state:
                        self.status("────────────────────────────")
                        self.status(f"🔎 State = {state}")
                        last_state = state

                    if state == TTMState.LOGIN:
                        self.status("⚠️ พบหน้า Login")
                        self.check_login(page)
                        page.wait_for_timeout(100)
                        continue

                    if state == TTMState.ROUND:
                        # Also after a queue redirect back to a booking
                        # page with an empty dropdown.
                        round_done = self._select_round_with_retry(page)
                        zone_done = False
                        page.wait_for_timeout(150)
                        continue

                    if state == TTMState.WAIT_SALE:
                        # "State = WAIT_SALE" is already logged once;
                        # the repeating part goes to the progress line.
                        self.progress("⏰ ยังไม่เปิดขาย")

                        # fallback กรณีอ่าน sale datetime ไม่ได้:
                        # refresh เฉพาะหน้า Performance/Concert
                        current_url = (page.url or "").lower()

                        if (
                            "/performance/" in current_url
                            or "/concert/" in current_url
                        ):
                            self._sleep_with_countdown(
                                self.presale_refresh_seconds,
                                lambda left: (
                                    "⏰ ยังไม่เปิดขาย | Refresh หน้า "
                                    f"Performance ใน {int(left) + 1}s"
                                ),
                            )

                            if self._queue_detected(page):
                                self.status(
                                    "🎟️ พบ Queue → หยุด Refresh"
                                )
                                continue

                            try:
                                page.reload(
                                    wait_until="domcontentloaded",
                                    timeout=20000,
                                )
                            except Exception:
                                pass
                        else:
                            time.sleep(2)

                        continue

                    if state == TTMState.QUEUE_ENTRY:
                        self.status(
                            "🎫 พบหน้า Waiting Room Entry"
                        )

                        joined = self.handle_waiting_room_entry(
                            page
                        )

                        if joined:
                            self.status(
                                "🛑 ปิด Auto Refresh ระหว่าง Waiting Room/Queue"
                            )
                            last_state = None

                        page.wait_for_timeout(250)
                        continue

                    if state == TTMState.IN_QUEUE:
                        self._attach_queue_status_monitor(page)

                        self._log_throttled(
                            "in-queue",
                            "⏳ อยู่ใน Queue — ไม่ Refresh หน้า",
                            every=60,
                        )

                        if self._visitor_presence_visible(page):
                            now_ts = time.time()

                            if (
                                now_ts - self._queue_last_log_time
                                >= 8
                            ):
                                self._queue_last_log_time = now_ts
                                self.progress(
                                    "🔔 Still here? กรุณากด "
                                    "\"Yes, I'm here\" บน Browser"
                                )
                                self._log_throttled(
                                    "still-here",
                                    "🔔 Still here? กรุณากด "
                                    "\"Yes, I'm here\" บน Browser",
                                    every=60,
                                )

                        before_queue_url = page.url

                        page.wait_for_timeout(500)

                        current_url = page.url

                        if (
                            current_url != before_queue_url
                            and "wait.thaiticketmajor.com"
                            not in current_url.lower()
                        ):
                            self.status(
                                "🚪 Queue ปล่อยเข้าแล้ว"
                            )
                            self.status(
                                f"🌐 {current_url}"
                            )

                            last_state = None
                            continue

                        continue

                    if state == TTMState.ZONE and not zone_done:
                        self.handle_zone(page)
                        zone_done = True
                        page.wait_for_timeout(350)
                        continue

                    if state == TTMState.SEAT:
                        selected = self.select_seats_hybrid(page, self.quantity)

                        # --------------------------------------------------
                        # FAST ZONE FAILOVER
                        # ถ้า Seat แรกของ Zone นี้โดน reject / popup /
                        # click ไม่ผ่าน ให้กลับ Zone Map แล้วลอง Zone ถัดไปทันที
                        # --------------------------------------------------
                        if (
                            len(selected) != self.quantity
                            and self._request_zone_switch
                        ):
                            self.status(
                                "⚡ Seat แรกของ Zone นี้ไม่ผ่าน "
                                "→ เปลี่ยน Zone ทันที"
                            )

                            switched = self.return_to_zone_map(page)

                            if switched:
                                zone_done = False
                                last_state = None
                                self._request_zone_switch = False
                                page.wait_for_timeout(50)
                                continue

                            self.status(
                                "⚠️ กลับ Zone Map ไม่สำเร็จ "
                                "→ หยุดเพื่อป้องกันเลือกผิด Event/Zone"
                            )

                        if len(selected) != self.quantity:
                            raise Exception(
                                f"เลือกที่นั่งไม่ครบ {len(selected)}/{self.quantity}"
                                + (
                                    f" | {self._seat_failure_reason}"
                                    if self._seat_failure_reason
                                    else ""
                                )
                            )

                        self.selected_seats = selected

                        self.status(
                            f"✅ เลือกและ Validate ที่นั่งครบ {self.quantity} ที่"
                        )

                        for seat in self.selected_seats:
                            self.status(
                                f'🪑 {seat["title"]} | {seat["price"]:,} THB'
                            )

                        # กดยืนยันที่นั่ง และหยุดก่อน payment
                        confirm_result = self.confirm_seats_hybrid(page)

                        self.status("")
                        self.status("================================")
                        self.status("✅ SEAT CONFIRM COMPLETE")
                        self.status("================================")

                        if self.selected_round:
                            self.status(
                                f'🎭 Round: {self.selected_round["text"]}'
                            )

                        if self.selected_zone:
                            self.status(f"🎟️ Zone: {self.selected_zone}")

                        for seat in self.selected_seats:
                            self.status(f'🪑 Seat: {seat["title"]}')

                        self.status(
                            f"📡 bookingseats.php result: {confirm_result}"
                        )
                        self.status("🛑 STOP BEFORE PAYMENT")
                        self.status("❗ ไม่มี Automation ชำระเงิน")
                        self.status("👤 Browser เปิดค้างไว้ให้ตรวจสอบ")

                        # เปิด browser ค้างไว้จนผู้ใช้กด STOP
                        while not self.should_stop():
                            time.sleep(0.5)

                        break

                    time.sleep(0.12)

            except Exception as ex:
                if str(ex) != "BOT_STOPPED":
                    self.status("")
                    self.status("================================")
                    self.status("❌ TTM HYBRID SPEED BOT ERROR")
                    self.status("================================")
                    self.status(str(ex))
                    self.status("================================")

                    try:
                        if page:
                            page.screenshot(
                                path="ttm-hybrid-error.png",
                                full_page=True,
                            )
                            self.status("📸 ttm-hybrid-error.png")
                    except Exception:
                        pass

                    self.status("👀 Chrome เปิดค้างไว้ให้ตรวจสอบ")

                    while not self.should_stop():
                        time.sleep(0.5)

            finally:
                # ห้าม browser.close() เพราะเรา attach Chrome ที่ผู้ใช้เปิดเอง
                self.status("🔌 Ticket Bot disconnected")

    @staticmethod
    def format_remaining(seconds):
        """3725 -> '1:02:05', 125 -> '02:05'."""
        seconds = max(0, int(seconds))
        hours, rest = divmod(seconds, 3600)
        minutes, secs = divmod(rest, 60)

        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"

        return f"{minutes:02d}:{secs:02d}"

    def _sleep_with_countdown(self, seconds, message_fn):
        """
        Sleep in 1 s ticks, reporting message_fn(seconds_left) through
        the progress line and honouring STOP.
        """
        end = time.time() + max(0.0, seconds)

        while True:
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            left = end - time.time()

            if left <= 0:
                return

            self.progress(message_fn(left))
            time.sleep(min(1.0, left))

    def _select_round_with_retry(self, page, max_attempts=3):
        """
        Select the dropdown round. Returns True once confirmed.
        After max_attempts unconfirmed tries, raise rather than continue
        with a round the user did not ask for.
        """
        if self.select_show_round(page):
            self._round_attempts = 0
            return True

        self._round_attempts += 1

        if self._round_attempts >= max_attempts:
            raise Exception(
                "เลือกรอบการแสดงแล้วหน้าเว็บไม่โหลดรอบนั้น "
                f"{self._round_attempts} ครั้ง"
            )

        return False

    # ==========================================================
    # PRE-SALE / ON-SALE WAIT
    # ==========================================================

    THAI_MONTHS = {
        "มกราคม": 1,
        "กุมภาพันธ์": 2,
        "มีนาคม": 3,
        "เมษายน": 4,
        "พฤษภาคม": 5,
        "มิถุนายน": 6,
        "กรกฎาคม": 7,
        "สิงหาคม": 8,
        "กันยายน": 9,
        "ตุลาคม": 10,
        "พฤศจิกายน": 11,
        "ธันวาคม": 12,
    }

    def _parse_sale_datetime_text(self, page):
        """
        พยายามอ่านวันเปิดจำหน่ายจากหน้า Performance เช่น:
        วันอาทิตย์ที่ 16 สิงหาคม 2569, 10:00 น.

        แปลงปี พ.ศ. -> ค.ศ. โดย -543

        ถ้า UI ส่ง sale_datetime มาเอง รองรับ:
        YYYY-MM-DD HH:MM
        YYYY-MM-DDTHH:MM
        """

        # ----------------------------------------------
        # 1) USER CONFIG OVERRIDE
        # ----------------------------------------------
        if (
            self.sale_datetime
            and self.sale_datetime.upper() != "AUTO"
        ):
            value = self.sale_datetime.strip()

            for fmt in (
                "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M",
                "%Y/%m/%d %H:%M",
            ):
                try:
                    return datetime.strptime(
                        value,
                        fmt,
                    )
                except Exception:
                    pass

            raise Exception(
                "sale_datetime ไม่ถูกต้อง "
                "ใช้รูปแบบ YYYY-MM-DD HH:MM "
                "หรือกำหนด AUTO"
            )

        # ----------------------------------------------
        # 2) AUTO FROM PERFORMANCE PAGE
        # ----------------------------------------------
        try:
            body = page.locator("body").inner_text(
                timeout=2500
            )
        except Exception:
            return None

        # ตัดเฉพาะบริเวณข้อความ "วันเปิดจำหน่าย" ถ้าเจอ
        sale_match = re.search(
            r"วันเปิดจำหน่าย[\s\S]{0,160}?"
            r"(\d{1,2})\s+"
            r"(มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|"
            r"กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)"
            r"\s+(\d{4})"
            r"[\s,]*(\d{1,2}):(\d{2})",
            body,
            flags=re.I,
        )

        if not sale_match:
            return None

        day = int(sale_match.group(1))
        month = self.THAI_MONTHS[
            sale_match.group(2)
        ]
        year = int(sale_match.group(3))
        hour = int(sale_match.group(4))
        minute = int(sale_match.group(5))

        if year >= 2400:
            year -= 543

        try:
            return datetime(
                year,
                month,
                day,
                hour,
                minute,
            )
        except Exception:
            return None

    def _extract_queue_status_payload(self, data):
        """
        ดึงค่าที่มีประโยชน์จาก Queue-it status response
        โดยไม่ผูกกับ schema เดียว.
        """
        result = {
            "progress": None,
            "ahead": None,
            "queue_number": None,
            "redirect_url": None,
        }

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    low = str(key).lower()

                    if low == "progress" and result["progress"] is None:
                        result["progress"] = item
                    elif low == "usersinlineaheadofyou" and result["ahead"] is None:
                        result["ahead"] = item
                    elif low == "queuenumber" and result["queue_number"] is None:
                        result["queue_number"] = item
                    elif low == "redirecturl" and result["redirect_url"] is None:
                        result["redirect_url"] = item

                    walk(item)

            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(data)
        return result

    def _attach_queue_status_monitor(self, page):
        """
        ฟัง response ของ Queue-it /status ที่หน้าเว็บยิงเอง
        ไม่ยิง API เพิ่มเอง และไม่ refresh.
        """
        # One listener per page (a round click may move us to a new tab)
        if (
            self._queue_status_listener_attached
            and self._queue_status_page is page
        ):
            return

        def on_response(response):
            try:
                url = (response.url or "").lower()

                if "/status" not in url and "spa-api/queue/" not in url:
                    return

                try:
                    data = response.json()
                except Exception:
                    return

                info = self._extract_queue_status_payload(data)
                changed = False

                if (
                    info["progress"] is not None
                    and info["progress"] != self._queue_last_progress
                ):
                    self._queue_last_progress = info["progress"]
                    changed = True

                if (
                    info["ahead"] is not None
                    and info["ahead"] != self._queue_last_ahead
                ):
                    self._queue_last_ahead = info["ahead"]
                    changed = True

                if (
                    info["queue_number"] is not None
                    and info["queue_number"] != self._queue_last_number
                ):
                    self._queue_last_number = info["queue_number"]
                    changed = True

                if changed:
                    parts = []

                    if self._queue_last_progress is not None:
                        try:
                            progress = float(self._queue_last_progress)
                            if progress <= 1:
                                progress *= 100
                            parts.append(f"Progress {progress:.1f}%")
                        except Exception:
                            parts.append(
                                f"Progress {self._queue_last_progress}"
                            )

                    if self._queue_last_ahead is not None:
                        parts.append(
                            f"Ahead {self._queue_last_ahead}"
                        )

                    if self._queue_last_number is not None:
                        parts.append(
                            f"Queue {self._queue_last_number}"
                        )

                    if parts:
                        self.progress(
                            "📊 Queue | " + " | ".join(parts)
                        )

                if (
                    info["redirect_url"]
                    and not self._queue_redirect_seen
                ):
                    self._queue_redirect_seen = True
                    self.status(
                        "🚪 Queue แจ้งว่าพร้อม Redirect"
                    )

            except Exception:
                pass

        page.on("response", on_response)
        self._queue_status_listener_attached = True
        self._queue_status_page = page

        self.status(
            "📡 Queue Network Monitor พร้อม"
        )

    def _visitor_presence_visible(self, page):
        """
        ตรวจ popup Still here? เท่านั้น
        ไม่กดยืนยันอัตโนมัติ.
        """
        selectors = [
            "#divConfirmVisitorPresenceModal",
            "#buttonConfirmVisitorPresence",
        ]

        for selector in selectors:
            try:
                loc = page.locator(selector).first

                if loc.count() > 0 and loc.is_visible():
                    return True
            except Exception:
                pass

        return False

    JOIN_BUTTON_SELECTORS = [
        "#waitingroomentry-content-holder button.botdetect-button.btn",
        "#waitingroomentry-content-holder button",
        'button:has-text("Join waiting room")',
        'button:has-text("Join Waiting Room")',
        'button:has-text("Join queue")',
        'button:has-text("กดเพื่อรอรับคิว")',
    ]

    QUEUE_DOM_SELECTORS = [
        "#MainPart_divProgressbar",
        "#MainPart_lbQueueNumber",
        "#challenge-widget-container",
        "#divConfirmVisitorPresenceModal",
    ]

    @staticmethod
    def _host(page):
        try:
            return (urlparse(page.url or "").hostname or "").lower()
        except Exception:
            return ""

    def _is_queue_host(self, page):
        host = self._host(page)
        url = (page.url or "").lower()

        return (
            host.startswith("wait.")
            or host.startswith("queue.")
            or host.startswith("busy.")
            or "queue-it" in host
            or "queue-it" in url
        )

    def _is_ttm_content_page(self, page):
        """Event (performance/concert) or booking pages of TTM itself."""
        host = self._host(page)
        url = (page.url or "").lower()

        if not host.endswith("thaiticketmajor.com"):
            return False

        if self._is_queue_host(page):
            return False

        return (
            "/performance/" in url
            or "/concert/" in url
            or host.startswith("booking.")
            or "/booking/" in url
        )

    def _find_join_button(self, page):
        """Visible Join waiting room button in the page or any frame."""
        try:
            frames = [page.main_frame] + [
                frame
                for frame in page.frames
                if frame != page.main_frame
            ]
        except Exception:
            frames = [page]

        for frame in frames:
            for selector in self.JOIN_BUTTON_SELECTORS:
                try:
                    loc = frame.locator(selector).first

                    if loc.count() > 0 and loc.is_visible():
                        return loc
                except Exception:
                    pass

        return None

    def _queue_dom_visible(self, page):
        for selector in self.QUEUE_DOM_SELECTORS:
            try:
                loc = page.locator(selector).first

                if loc.count() > 0 and loc.is_visible():
                    return True
            except Exception:
                pass

        return False

    def _log_throttled(self, key, message, every=8.0):
        now_ts = time.time()

        if now_ts - self._throttle.get(key, 0.0) >= every:
            self._throttle[key] = now_ts
            self.status(message)

    def handle_waiting_room_entry(self, page):
        """
        Click "Join waiting room" once, with a real (trusted) mouse click,
        and confirm the page actually moved on.

        Returns True only when the join was confirmed (entry holder gone,
        navigation, queue UI or a verification challenge appeared).
        Returns False when the button is missing / not ready / the click
        was not accepted, so the main loop retries later. Never refreshes.

        If the site shows CAPTCHA / visitor verification the bot does not
        try to bypass it; the user completes it in the browser.
        """

        if self.should_stop():
            raise Exception("BOT_STOPPED")

        button = self._find_join_button(page)

        if button is None:
            self._log_throttled(
                "join-missing",
                "⏳ หน้า Waiting Room ยังไม่แสดงปุ่ม Join → รอ (ไม่ Refresh)",
            )
            return False

        # Button is often rendered disabled until the widget is ready.
        # Clicking a disabled button does nothing, so wait for it.
        enabled = False

        for _ in range(100):  # ~10 sec
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            try:
                enabled = button.is_enabled()
            except Exception:
                enabled = False

            if enabled:
                break

            self._log_throttled(
                "join-disabled",
                "⏳ ปุ่ม Join waiting room ยังกดไม่ได้ (disabled) → รอ",
            )
            page.wait_for_timeout(100)

        if not enabled:
            return False

        # Do not hammer the button: one click, then give the site time.
        if time.time() - self._last_join_click_time < 8:
            return False

        self.status("🎟️ พบปุ่ม Join waiting room")

        before_url = page.url

        try:
            button.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        try:
            button.click(timeout=5000)
            self._last_join_click_time = time.time()
            self.status("✅ Click Join waiting room แล้ว → รอยืนยัน")
        except Exception as ex:
            # A synthetic DOM click is ignored by the site's bot-detect
            # widget and used to be reported as success. Retry instead.
            self.status(
                f"⚠️ Click Join waiting room ไม่ผ่าน: {ex}"
            )
            self.status(
                "👤 ถ้ามี popup/แบนเนอร์บังปุ่ม กรุณาปิดบน Browser"
            )
            self._last_join_click_time = time.time()
            return False

        # --------------------------------------------------
        # CONFIRM: challenge / holder gone / navigation / queue UI
        # --------------------------------------------------
        for _ in range(80):  # ~8 sec
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            try:
                challenge = page.locator(
                    "#challenge-widget-container"
                ).first

                if challenge.count() > 0 and challenge.is_visible():
                    self._attach_queue_status_monitor(page)
                    self.status(
                        "🧩 พบ Visitor Verification / Challenge"
                    )
                    self.status(
                        "👤 กรุณาดำเนินการ Challenge บน Browser เอง"
                    )
                    return True
            except Exception:
                pass

            if page.url != before_url:
                self._attach_queue_status_monitor(page)
                self.status("⏳ เข้าสู่ Waiting Room / Queue แล้ว")
                self.status(f"🌐 {page.url}")
                return True

            if self._find_join_button(page) is None:
                self._attach_queue_status_monitor(page)
                self.status("⏳ เข้าสู่ Waiting Room แล้ว")
                return True

            if self._queue_dom_visible(page):
                self._attach_queue_status_monitor(page)
                self.status("⏳ อยู่ใน Queue แล้ว")
                return True

            page.wait_for_timeout(100)

        self.status(
            "⚠️ กด Join แล้วแต่หน้าเว็บยังไม่เปลี่ยน "
            "→ จะลองกดใหม่อีกครั้ง (ไม่ Refresh)"
        )
        return False

    def _queue_detected(self, page):
        """
        True when the page is a waiting room / queue.

        สำคัญ: ถ้าเข้า Queue จริงแล้ว จะไม่ refresh อัตโนมัติ
        เพื่อไม่เสี่ยงเสียลำดับคิว.

        Event/booking pages of TTM are judged only by DOM (join button,
        Queue-it widgets), never by free body text: their FAQ text often
        mentions "waiting room" / "เข้าคิว", which used to stop the
        pre-sale refresh forever.
        """

        try:
            if self._is_queue_host(page):
                return True

            if self._find_join_button(page) is not None:
                return True

            if self._queue_dom_visible(page):
                return True

            if self._is_ttm_content_page(page):
                return False

            body = (
                page.locator("body")
                .inner_text(timeout=600)
                .lower()
            )

            return any(
                token in body
                for token in [
                    "you are now in the queue",
                    "your place in the queue",
                    "queue number",
                    "your queue",
                    "หมายเลขคิว",
                    "กำลังรอคิว",
                    "อยู่ในคิว",
                ]
            )

        except Exception:
            return False

    def wait_for_sale_if_needed(self, page):
        """
        ใช้กับหน้า Performance ก่อนเปิดขาย

        หลักการ:
        - อ่านเวลาเปิดขายจากหน้าเว็บ หรือ sale_datetime จาก UI
        - ก่อน T-20m: รอแบบเบา ๆ
        - ตั้งแต่ T-20m: ตรวจว่า Login พร้อม และ refresh หน้า Performance
          เป็นช่วง ๆ เพื่อให้ session/page สด
        - ถึงเวลาเปิดขาย: refresh ถี่ขึ้นจนเห็น round link / redirect
        - เมื่อเข้า Queue จริง: STOP REFRESH ทันที แล้วปล่อย queue ทำงานเอง

        หมายเหตุ:
        ไม่ refresh หน้า Queue จริงโดยอัตโนมัติ
        """

        if self._presale_wait_done:
            return False

        current_url = (
            page.url or ""
        ).lower()

        if (
            "/performance/" not in current_url
            and "/concert/" not in current_url
        ):
            return False

        sale_dt = self._parse_sale_datetime_text(
            page
        )

        # --------------------------------------------------
        # IMPORTANT:
        # Waiting room / round buttons may open BEFORE the
        # advertised on-sale time. DOM is the source of truth.
        # If an enabled round button exists, stop waiting now.
        # --------------------------------------------------
        try:
            enabled_rounds = page.locator(
                "#section-event-round "
                ".row.row-enable "
                '.col-btn a.btn'
                '[href*="/booking/3m/zones.php"]'
                '[href*="query="]'
            ).count()
        except Exception:
            enabled_rounds = 0

        if enabled_rounds > 0:
            self.status(
                f"🔥 พบรอบเปิดให้กดแล้ว {enabled_rounds} รอบ "
                "→ หยุดรอเวลา"
            )
            self._presale_wait_done = True
            return True

        if sale_dt is None:
            return False

        now = datetime.now()

        if now >= sale_dt:
            # ถึงเวลาแล้ว ให้ performance handler ลองอ่าน round link ทันที
            self._presale_wait_done = True
            return False

        prelogin_at = sale_dt - timedelta(
            minutes=self.prelogin_minutes
        )

        self.status(
            "⏰ พบเวลาเปิดขาย: "
            f"{sale_dt:%Y-%m-%d %H:%M}"
        )

        self.status(
            f"🔐 Queue Entry Window: "
            f"T-{self.prelogin_minutes} นาที"
        )
        self.status(
            "ℹ️ ถ้าปุ่มรอบ/Waiting Room เปิดก่อนเวลาขาย "
            "Bot จะเข้าคิวทันที"
        )

        # ----------------------------------------------
        # WAIT UNTIL T-20
        # ----------------------------------------------
        while datetime.now() < prelogin_at:
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            remain = int(
                (prelogin_at - datetime.now())
                .total_seconds()
            )

            if remain <= 0:
                break

            self._sleep_with_countdown(
                min(30, max(1, remain)),
                lambda left: (
                    "⏳ รอถึงช่วง Pre-login อีก "
                    + self.format_remaining(
                        (prelogin_at - datetime.now()).total_seconds()
                    )
                    + f"  |  เปิดขาย {sale_dt:%d/%m %H:%M}"
                ),
            )

        # ----------------------------------------------
        # PRE-LOGIN WINDOW
        # ----------------------------------------------
        self.status(
            f"🔐 เข้าช่วง T-{self.prelogin_minutes} นาทีแล้ว"
        )
        self.status(
            "🎟️ เริ่มเตรียมเข้า Waiting Room / Queue"
        )

        self.check_login(page)

        # Refresh performance page periodically BEFORE sale.
        # Do not refresh if queue appears.
        while datetime.now() < sale_dt:
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            # --------------------------------------------------
            # Round/Waiting-room can open early.
            # Do NOT keep waiting for sale_dt if buttons are live.
            # --------------------------------------------------
            try:
                enabled_rounds = page.locator(
                    "#section-event-round "
                    ".row.row-enable "
                    '.col-btn a.btn'
                    '[href*="/booking/3m/zones.php"]'
                    '[href*="query="]'
                ).count()
            except Exception:
                enabled_rounds = 0

            if enabled_rounds > 0:
                self.status(
                    f"🔥 รอบเปิดก่อนเวลา พบ {enabled_rounds} รอบ"
                )
                self.status(
                    "🚀 หยุด Refresh และไปกดรอบทันที"
                )
                self._presale_wait_done = True
                return True

            if self._queue_detected(page):
                self.status(
                    "🎟️ พบ Queue → หยุด Auto Refresh"
                )
                self._presale_wait_done = True
                return True

            remain = int(
                (sale_dt - datetime.now())
                .total_seconds()
            )

            preview_refresh = self.presale_refresh_seconds

            if remain <= 3600:
                preview_refresh = min(
                    preview_refresh,
                    10,
                )

            if remain <= 900:
                preview_refresh = min(
                    preview_refresh,
                    5,
                )


            # ช่วง 1 ชั่วโมงก่อนขายให้เช็คค่อนข้างถี่
            # แต่ยังไม่ refresh ตอนอยู่ Queue จริง
            dynamic_refresh = self.presale_refresh_seconds

            if remain <= 3600:
                dynamic_refresh = min(
                    dynamic_refresh,
                    10,
                )

            if remain <= 900:
                dynamic_refresh = min(
                    dynamic_refresh,
                    5,
                )

            sleep_for = min(
                dynamic_refresh,
                max(1, remain),
            )

            self._sleep_with_countdown(
                sleep_for,
                lambda left: (
                    "⏳ รอเปิดขายอีก "
                    + self.format_remaining(
                        (sale_dt - datetime.now()).total_seconds()
                    )
                    + f"  |  เช็ค Waiting Room ทุก {preview_refresh}s"
                    + f"  |  Refresh ถัดไป {int(left) + 1}s"
                ),
            )

            if datetime.now() >= sale_dt:
                break

            # refresh เฉพาะ performance page
            if any(
                token in (page.url or "").lower()
                for token in (
                    "/performance/",
                    "/concert/",
                )
            ):
                try:
                    page.reload(
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
                except Exception:
                    pass

                # ตรวจทันทีหลัง reload
                try:
                    enabled_rounds = page.locator(
                        "#section-event-round "
                        ".row.row-enable "
                        '.col-btn a.btn'
                        '[href*="/booking/3m/zones.php"]'
                        '[href*="query="]'
                    ).count()
                except Exception:
                    enabled_rounds = 0

                if enabled_rounds > 0:
                    self.status(
                        f"🔥 หลัง Refresh พบรอบเปิดแล้ว "
                        f"{enabled_rounds} รอบ"
                    )
                    self._presale_wait_done = True
                    return True

        # ----------------------------------------------
        # ON-SALE BURST
        # ----------------------------------------------
        self.status(
            "🚀 ถึงเวลาเปิดขาย — เริ่มตรวจรอบ"
        )

        for attempt in range(1, 91):
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            if self._queue_detected(page):
                self.status(
                    "🎟️ เข้า Queue แล้ว → "
                    "หยุด Refresh และรอระบบคิว"
                )
                self._presale_wait_done = True
                return True

            try:
                round_links = page.locator(
                    "#section-event-round "
                    ".row.row-enable "
                    '.col-btn a.btn'
                    '[href*="/booking/3m/zones.php"]'
                    '[href*="query="]'
                ).count()
            except Exception:
                round_links = 0

            if round_links > 0:
                self.status(
                    f"✅ เปิดขายแล้ว พบ Round Link "
                    f"{round_links} รายการ"
                )
                self._presale_wait_done = True
                return True

            if not any(
                token in (page.url or "").lower()
                for token in (
                    "/performance/",
                    "/concert/",
                )
            ):
                self._presale_wait_done = True
                return True

            self.progress(
                f"🔄 เปิดขายแล้ว กำลังรอปุ่มรอบ | "
                f"On-sale check {attempt}/90"
            )

            self._sleep_with_countdown(
                self.onsale_refresh_seconds,
                lambda left, attempt=attempt: (
                    f"🔄 เปิดขายแล้ว กำลังรอปุ่มรอบ | "
                    f"On-sale check {attempt}/90"
                ),
            )

            try:
                page.reload(
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
            except Exception:
                pass

        self.status(
            "⚠️ ยังไม่พบ Round Link หลัง On-sale window"
        )

        self._presale_wait_done = True
        return False

    # ==========================================================
    # PERFORMANCE PAGE ROUND ENTRY
    # ==========================================================

    ROUND_LINK_SELECTOR = (
        '.col-btn a.btn'
        '[href*="/booking/3m/zones.php"]'
        '[href*="query="]'
    )

    def take_pending_page(self):
        """Return (and clear) a tab opened by a round click, if any."""
        new_page = self._pending_page
        self._pending_page = None
        return new_page

    def _read_performance_rounds(self, page):
        """
        Read every round of #section-event-round, across ALL event blocks.

        Returns (enabled, closed):
          enabled: clickable rounds (.row.row-enable) with element/href
          closed:  rows that exist but are not enabled yet / sold out
        """
        blocks = page.locator(
            "#section-event-round .event-detail-item"
        )

        enabled = []
        closed = []

        for block_index in range(blocks.count()):
            block = blocks.nth(block_index)

            try:
                block_text = (
                    block.inner_text(timeout=800) or ""
                ).strip()
            except Exception:
                block_text = ""

            links = block.locator(
                ".row.row-enable " + self.ROUND_LINK_SELECTOR
            )

            for i in range(links.count()):
                link = links.nth(i)

                try:
                    href = link.get_attribute("href") or ""

                    query_match = re.search(
                        r"[?&]query=(\d+)", href, flags=re.I
                    )

                    if not query_match:
                        continue

                    rd_match = re.search(
                        r"[?&]rdId=(\d+)", href, flags=re.I
                    )

                    time_text = ""

                    try:
                        show = link.locator(".item-show").first

                        if show.count() > 0:
                            time_text = (
                                show.inner_text(timeout=500) or ""
                            ).strip()
                    except Exception:
                        pass

                    if not time_text:
                        try:
                            time_text = (
                                link.inner_text(timeout=500) or ""
                            ).strip()
                        except Exception:
                            time_text = ""

                    date_text = ""

                    try:
                        row = link.locator(
                            "xpath=ancestor::div[contains("
                            "concat(' ', normalize-space(@class), ' '),"
                            " ' row ')][1]"
                        )

                        if row.count() > 0:
                            label = row.locator(".col-label").first

                            if label.count() > 0:
                                date_text = (
                                    label.inner_text(timeout=500)
                                    or ""
                                ).strip()
                    except Exception:
                        pass

                    enabled.append(
                        {
                            "element": link,
                            "href": href,
                            "query": query_match.group(1),
                            "value": (
                                rd_match.group(1) if rd_match else ""
                            ),
                            "time": time_text,
                            "date": date_text,
                            "text": f"{date_text} {time_text}".strip(),
                            "block_index": block_index,
                            "block_text": block_text,
                        }
                    )

                except Exception:
                    continue

            # Rounds shown but not clickable yet (not on sale / sold out)
            try:
                rows = block.locator(".row:not(.row-enable)")

                for i in range(rows.count()):
                    row = rows.nth(i)

                    if row.locator(".col-btn").count() == 0:
                        continue

                    row_text = (
                        row.inner_text(timeout=500) or ""
                    ).strip()

                    if row_text:
                        closed.append(
                            {
                                "value": "",
                                "text": row_text,
                                "block_index": block_index,
                            }
                        )
            except Exception:
                pass

        return enabled, closed

    def _wait_or_reload_performance(self, page):
        """Requested round is visible but not open yet: wait, then reload."""
        wait_s = max(1, self.onsale_refresh_seconds)
        end = time.time() + wait_s

        while time.time() < end:
            if self.should_stop():
                raise Exception("BOT_STOPPED")
            time.sleep(0.1)

        if self._queue_detected(page):
            return

        try:
            page.reload(
                wait_until="domcontentloaded",
                timeout=20000,
            )
        except Exception:
            pass

    def _click_and_follow(self, page, link, href):
        """
        Click a real round/queue link and wait until something happens.

        - URL changes (booking / conditions / queue)      -> True
        - link opens a new tab                            -> True, tab is
          handed to run() through take_pending_page()
        - a navigation was started but the site is slow   -> keep waiting,
          never re-navigate (that would abort the queue redirect)
        - nothing happened at all after ~8 s              -> page.goto(href)
        """
        state = {"nav": False, "nav_failed": False, "popup": None}
        main_frame = page.main_frame

        def on_request(request):
            try:
                if (
                    request.is_navigation_request()
                    and request.frame == main_frame
                ):
                    state["nav"] = True
            except Exception:
                pass

        def on_request_failed(request):
            try:
                if (
                    request.is_navigation_request()
                    and request.frame == main_frame
                ):
                    state["nav_failed"] = True
            except Exception:
                pass

        def on_popup(popup):
            state["popup"] = popup

        page.on("request", on_request)
        page.on("requestfailed", on_request_failed)
        page.on("popup", on_popup)

        before_url = page.url
        started = time.time()

        try:
            try:
                link.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass

            try:
                link.click(timeout=4000)
            except Exception as ex:
                if state["nav"] or state["popup"] is not None:
                    # The click went through; Playwright only timed out
                    # waiting for the (slow) navigation it started.
                    pass
                else:
                    self.status(f"⚠️ Normal round click: {ex}")

                    try:
                        # fallback DOM click - still the site's own element
                        link.evaluate("el => el.click()", timeout=3000)
                    except Exception as ex2:
                        self.status(f"⚠️ DOM click: {ex2}")

            pending_logged = False

            while True:
                if self.should_stop():
                    raise Exception("BOT_STOPPED")

                popup = state["popup"]

                if popup is not None:
                    try:
                        popup.wait_for_load_state(
                            "domcontentloaded", timeout=30000
                        )
                    except Exception:
                        pass

                    self._pending_page = popup
                    self.status("🗂️ รอบเปิดใน Tab ใหม่ → ย้ายไปทำงานที่ Tab ใหม่")
                    self.status(f"🌐 {popup.url}")
                    return True

                current_url = page.url

                if current_url != before_url:
                    self.status("✅ รอบถูกกดแล้ว")
                    self.status(f"🌐 {current_url}")
                    return True

                if self._queue_detected(page):
                    self.status("🎟️ เข้าระบบ Queue แล้ว")
                    return True

                try:
                    if (
                        page.locator("select#rdId").count() > 0
                        or page.locator(
                            'map area[onclick*="selectzone"]'
                        ).count() > 0
                        or page.locator("#tableseats").count() > 0
                    ):
                        self.status("✅ เข้าหน้า Booking แล้ว")
                        return True
                except Exception:
                    pass

                elapsed = time.time() - started

                if elapsed > 8:
                    if state["nav"] and not state["nav_failed"]:
                        if elapsed > 90:
                            self.status(
                                "⚠️ เว็บตอบช้ามาก (>90s) หลังกดรอบ"
                            )
                            return True

                        if not pending_logged:
                            pending_logged = True
                            self.status(
                                "⏳ เว็บกำลังโหลดหลังกดรอบ/รับคิว "
                                "→ รอ ไม่โหลดซ้ำ (กันเสียคิว)"
                            )
                    else:
                        break

                page.wait_for_timeout(100)

        finally:
            for event, handler in (
                ("request", on_request),
                ("requestfailed", on_request_failed),
                ("popup", on_popup),
            ):
                try:
                    page.remove_listener(event, handler)
                except Exception:
                    pass

        # Nothing happened at all: use the button's own href once.
        self.status(
            "⚠️ Click รอบไม่ทำให้หน้าเปลี่ยน "
            "→ ใช้ href ของปุ่มเป็น fallback"
        )

        try:
            page.goto(
                href,
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as ex:
            self.status(f"⚠️ เปิด href ไม่สำเร็จ: {ex}")

        self.status(f"🌐 {page.url}")
        return True

    def handle_performance_page(self, page):
        """
        Performance / On-sale handler v2.2

        รองรับ 2 รูปแบบ:
        1) รอบปกติ:
           zones.php?query=607&rdId=81814

        2) วันเปิดขาย / Queue:
           zones.php?query=695
           โดย DOM จะเป็นประมาณ:
           #section-event-round
             .event-detail-item
               .row.row-enable
                 .col-btn a.btn
                   .item-show = 18:00
                   .item-hide = รับคิวซื้อบัตร

        หลักสำคัญ:
        - ใช้เฉพาะ .row-enable ใน #section-event-round
        - อ่านรอบจากทุก event block (เดิมดูแค่ block แรก ทำให้รอบใน
          block อื่นเลือกไม่ได้) และไม่ตัดรอบที่ query ต่างกันทิ้ง
        - ticket_type ใช้กรอง block ถ้ามี block ที่ข้อความตรง
        - รอบที่ขอยังไม่เปิด -> รอแล้ว reload หน้า Performance
        - กด anchor จริงด้วย Playwright เพื่อให้ Queue/JS ของเว็บทำงาน
          ถ้าเว็บกำลังโหลดจะรอ ไม่ goto ซ้ำ (กันเสียคิว)
        - ค่าว่างของ show_round = AUTO
        """

        url = (page.url or "").lower()

        if "/performance/" not in url and "/concert/" not in url:
            return False

        rounds, closed = self._read_performance_rounds(page)

        # ยังไม่มี row-enable = ยังไม่เปิดให้กด
        if not rounds:
            return False

        # ==========================================================
        # TICKET TYPE -> EVENT BLOCK FILTER
        # ==========================================================

        pool = rounds
        ticket_query = (self.ticket_type or "").strip().lower()

        if ticket_query:
            filtered = [
                item
                for item in rounds
                if ticket_query in item["block_text"].lower()
            ]

            if filtered:
                pool = filtered
                self.status(
                    "🎫 Match Event Block จาก Ticket Type: "
                    f"{self.ticket_type}"
                )
            else:
                self.status(
                    f"⚠️ ไม่พบ Event Block ที่ตรง Ticket Type "
                    f"'{self.ticket_type}' → ใช้ทุก Block"
                )

        self.status("🔥 SALE OPEN / พบ Event Round ที่กดได้")
        self.status(f"🎭 พบรอบที่กดได้ {len(pool)} รอบ")

        for item in pool:
            rd_text = (
                f"rdId={item['value']}"
                if item["value"]
                else "Queue Link"
            )

            self.status(
                f"• {item['text']} | query={item['query']} | {rd_text}"
            )

        # ==========================================================
        # SELECT ROUND
        # ==========================================================

        requested = (self.show_round or "").strip() or "AUTO"
        target = None

        if requested.upper() != "AUTO":
            matches = match_rounds(requested, pool)

            if not matches and pool is not rounds:
                matches = match_rounds(requested, rounds)

            if not matches:
                if match_rounds(requested, closed):
                    self._log_throttled(
                        "round-not-open",
                        f"⏰ รอบ {requested} ยังไม่เปิดให้กด "
                        f"→ รอ {self.onsale_refresh_seconds}s แล้ว Refresh",
                        every=10,
                    )
                    self._wait_or_reload_performance(page)
                    return True

                available = ", ".join(
                    item["text"] or item["value"] for item in pool
                )
                raise Exception(
                    f"ไม่พบรอบการแสดง {requested} | "
                    f"รอบที่กดได้: {available}"
                )

            if len(matches) > 1:
                self.status(
                    f"⚠️ '{requested}' ตรงหลายรอบ "
                    f"({len(matches)}) → เลือกรอบแรก "
                    "ระบุวันที่เพิ่มเพื่อความแม่นยำ"
                )

            target = matches[0]

        if target is None:
            target = pool[0]

        self.selected_round = {
            "value": target["value"],
            "text": target["text"],
        }

        self.status(f"🎯 เลือกรอบ: {target['text']}")

        # ==========================================================
        # CLICK ACTUAL ON-SALE / QUEUE BUTTON
        # ==========================================================

        self.status("🚀 Click รอบ / รับคิวซื้อบัตร")

        return self._click_and_follow(
            page,
            target["element"],
            target["href"],
        )

    # ==========================================================
    # DIRECT BOOKING ENTRY
    # ==========================================================

    def handle_direct_booking_entry(self, page):
        """
        รองรับ URL ตรงเช่น:
        https://booking.thaiticketmajor.com/booking/3m/zones.php?query=607

        ไม่บังคับว่าต้องมี rdId:
        - ถ้า redirect ไป verify_condition.php -> handler Conditions จัดการต่อ
        - ถ้ามี #rdId -> Round handler เดิมจัดการ
        - ถ้ามี Zone Map -> Zone handler เดิมจัดการ
        """

        try:
            url = (page.url or "").lower()

            if (
                "booking.thaiticketmajor.com/booking/3m/zones.php"
                not in url
            ):
                return False

            self.status(
                "🎫 Direct Booking Entry detected"
            )

            # ปล่อยให้ detect_state() / verify_condition handler
            # ทำงานต่อ ไม่ navigate ซ้ำ
            return True

        except Exception:
            return False

    # ==========================================================
    # VERIFY CONDITIONS PAGE
    # ==========================================================

    def handle_verify_condition(self, page):
        """Pass the TTM Terms & Conditions page, then return to normal state flow."""
        url = (page.url or "").lower()
        if "verify_condition.php" not in url:
            return False

        self.status("📜 พบหน้า Conditions / Terms")
        before_url = page.url

        # TTM has used more than one markup variant for this page.  Search the
        # main document and child frames, and do not depend on one fixed id.
        scopes = [page] + list(page.frames)
        seen = set()
        scopes = [x for x in scopes if not (id(x) in seen or seen.add(id(x)))]

        checkbox = None
        checkbox_selectors = [
            "#check-agree", "#checkAgree", "#accept_condition", "#acceptCondition",
            'input[type="checkbox"]', 'input[name*="agree" i]',
            'input[id*="agree" i]', 'input[name*="condition" i]',
            'input[id*="condition" i]', 'input[name*="term" i]',
            'input[id*="term" i]',
        ]
        for scope in scopes:
            for selector in checkbox_selectors:
                try:
                    loc = scope.locator(selector).first
                    if loc.count() > 0:
                        checkbox = loc
                        break
                except Exception:
                    pass
            if checkbox is not None:
                break

        if checkbox is not None:
            try:
                checked = checkbox.is_checked()
            except Exception:
                checked = False
            if not checked:
                self.status("☑️ ติ๊กยอมรับ Terms & Conditions")
                try:
                    checkbox.check(force=True, timeout=1200)
                except Exception:
                    try:
                        checkbox.click(force=True, timeout=800)
                    except Exception:
                        checkbox.evaluate("""el => {
                            el.checked = true;
                            el.dispatchEvent(new Event('input', {bubbles:true}));
                            el.dispatchEvent(new Event('change', {bubbles:true}));
                            el.dispatchEvent(new Event('click', {bubbles:true}));
                        }""")
            self.status("✅ ยอมรับ Conditions แล้ว")
        else:
            # Custom checkbox fallback: click the label/text block itself.
            clicked_label = False
            for scope in scopes:
                try:
                    labels = scope.locator("label")
                    for i in range(min(labels.count(), 30)):
                        label = labels.nth(i)
                        txt = (label.inner_text(timeout=250) or "").lower()
                        if "terms & conditions" in txt or "ยอมรับข้อกำหนด" in txt or "เงื่อนไขในการใช้งาน" in txt:
                            label.click(force=True, timeout=800)
                            clicked_label = True
                            break
                except Exception:
                    pass
                if clicked_label:
                    break
            if clicked_label:
                self.status("✅ ยอมรับ Conditions แล้ว (label fallback)")
            else:
                self.status("⚠️ ไม่พบ checkbox Conditions")

        # Find the visible BUY TICKET control.  TTM may render it as button,
        # input[type=submit], anchor, or a control without #btn_verify.
        button = None
        button_selectors = [
            "#btn_verify", "#btnVerify", "#btn-buy", "#btnBuy",
            'button[type="submit"]', 'input[type="submit"]',
            'button', 'a.btn', 'a[role="button"]',
        ]
        buy_re = re.compile(r"(ซื้อ\s*บัตร|buy\s*ticket|buyticket)", re.I)

        for scope in scopes:
            # Prefer semantic role first when available.
            try:
                loc = scope.get_by_role("button", name=buy_re).first
                if loc.count() > 0 and loc.is_visible():
                    button = loc
                    break
            except Exception:
                pass

            for selector in button_selectors:
                try:
                    locs = scope.locator(selector)
                    for i in range(min(locs.count(), 40)):
                        loc = locs.nth(i)
                        try:
                            text = " ".join(filter(None, [
                                loc.inner_text(timeout=200) if selector != 'input[type="submit"]' else "",
                                loc.get_attribute("value") or "",
                                loc.get_attribute("aria-label") or "",
                                loc.get_attribute("title") or "",
                            ])).strip()
                        except Exception:
                            text = ""
                        if buy_re.search(text):
                            button = loc
                            break
                    if button is not None:
                        break
                except Exception:
                    pass
            if button is not None:
                break

        if button is None:
            raise Exception("พบหน้า Conditions แต่ไม่พบปุ่ม ซื้อบัตร / BUY TICKET")

        self.status("👉 Click ซื้อบัตร / BUY TICKET")
        try:
            button.scroll_into_view_if_needed(timeout=800)
        except Exception:
            pass
        try:
            button.click(timeout=1800)
        except Exception:
            try:
                button.click(force=True, timeout=900)
            except Exception:
                button.evaluate("el => el.click()")

        # Wait only for leaving Conditions / the next booking DOM.  This does
        # not confirm a purchase or perform any action on the payment page.
        for _ in range(40):
            if self.should_stop():
                raise Exception("BOT_STOPPED")
            current_url = page.url
            if current_url != before_url and "verify_condition.php" not in current_url.lower():
                self.status("✅ ผ่านหน้า Conditions สำเร็จ")
                self.status(f"🌐 {current_url}")
                return True
            try:
                if (page.locator("select#rdId").count() > 0
                    or page.locator('map area[onclick*="selectzone"]').count() > 0
                    or page.locator("#tableseats").count() > 0):
                    self.status("✅ ผ่านหน้า Conditions สำเร็จ")
                    return True
            except Exception:
                pass
            page.wait_for_timeout(100)

        raise Exception("กด BUY TICKET แล้วแต่ยังค้างหน้า Conditions")

    # ==========================================================
    # PAGE / SESSION
    # ==========================================================

    def find_ttm_page(self, browser):
        self.status("🔎 Searching ThaiTicketMajor Tabs...")

        pages = []

        for context_index, context in enumerate(browser.contexts):
            for page_index, page in enumerate(context.pages):
                try:
                    url = page.url.strip()
                    self.status(
                        f"   [{context_index}:{page_index}] {url}"
                    )
                    pages.append(page)
                except Exception:
                    pass

        priorities = [
            lambda url: "booking.thaiticketmajor.com" in url,
            lambda url: (
                "thaiticketmajor.com" in url and "/booking/" in url
            ),
            lambda url: (
                "event.thaiticketmajor.com" in url
                and "signin" not in url
            ),
            lambda url: (
                "thaiticketmajor.com" in url
                and "signin" not in url
            ),
        ]

        for matcher in priorities:
            for page in pages:
                try:
                    if matcher(page.url.lower()):
                        self.status("🎯 พบ Booking/TTM Tab")
                        return page
                except Exception:
                    pass

        return None

    def is_booking_page(self, page):
        try:
            url = page.url.lower()

            if "booking.thaiticketmajor.com" in url:
                return True

            if "thaiticketmajor.com" in url and "/booking/" in url:
                return True

            if page.locator("select#rdId").count() > 0:
                return True

            if (
                page.locator(
                    'map area[onclick*="selectzone"]'
                ).count()
                > 0
            ):
                return True

            if page.locator("#tableseats").count() > 0:
                return True

        except Exception:
            pass

        return False

    def check_login(self, page):
        self.status("🔐 Checking ThaiTicketMajor Login...")

        if self.is_booking_page(page):
            self.status("🟢 Booking Session พร้อมใช้งาน")
            return True

        def login_required():
            try:
                url = page.url.lower()

                if "/user/signin.php" in url:
                    return True

                if "/signin" in url:
                    return (
                        page.locator(
                            'input[type="password"]:visible'
                        ).count()
                        > 0
                    )

            except Exception:
                pass

            return False

        if not login_required():
            self.status("🟢 Login พร้อมใช้งาน")
            return True

        self.status("⚠️ ต้อง Login ThaiTicketMajor")
        self.status("👤 Login ด้วยตัวเองใน Chrome")
        self.status("🟡 Bot รอ Redirect")

        for second in range(300):
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            if self.is_booking_page(page):
                self.status("✅ Booking Session พร้อมใช้งาน")
                return True

            if not login_required():
                self.status("✅ Login สำเร็จ")
                return True

            self.progress(
                f"🔐 รอ Login บน Chrome... {self.format_remaining(second)}"
            )

            time.sleep(1)

        raise Exception("Login ไม่สำเร็จภายใน 5 นาที")

    # ==========================================================
    # STATE
    # ==========================================================

    def detect_state(self, page):
        # 1) Seat map
        try:
            if (
                page.locator(
                    "#tableseats td[title][data-info]"
                ).count()
                > 0
            ):
                return TTMState.SEAT
        except Exception:
            pass

        # 2) Round ก่อน Zone ถ้ายังไม่ได้เลือกรอบ
        try:
            rd = page.locator("select#rdId").first

            if rd.count() > 0:
                current = (rd.input_value() or "").strip()
                current_text = ""

                try:
                    current_text = rd.evaluate(
                        "el => el.selectedIndex >= 0 "
                        "? (el.options[el.selectedIndex].textContent || '') "
                        ": ''"
                    )
                except Exception:
                    pass

                if is_placeholder_option(current, current_text):
                    return TTMState.ROUND
        except Exception:
            pass

        # 3) Zone
        try:
            if (
                page.locator(
                    'map area[onclick*="selectzone"]'
                ).count()
                > 0
            ):
                return TTMState.ZONE
        except Exception:
            pass

        # 4) Login
        try:
            if "/user/signin.php" in page.url.lower():
                return TTMState.LOGIN
        except Exception:
            pass

        # --------------------------------------------------
        # WAITING ROOM ENTRY: only when a real Join button is
        # visible. Free text such as "เข้าคิว" also appears on
        # the queue page itself ("คุณได้เข้าคิวแล้ว") and on
        # performance FAQ text.
        # --------------------------------------------------
        if self._find_join_button(page) is not None:
            return TTMState.QUEUE_ENTRY

        is_content_page = self._is_ttm_content_page(page)

        try:
            body = (
                page.locator("body")
                .inner_text(timeout=1500)
                .lower()
            )
        except Exception:
            body = ""

        if self._is_queue_host(page):
            if any(
                text in body
                for text in [
                    "join waiting room",
                    "join queue",
                    "get in line",
                    "กดเพื่อรอรับคิว",
                ]
            ):
                # Entry page whose button is not rendered yet.
                return TTMState.QUEUE_ENTRY

            # Any other page on the queue host: never refresh it.
            return TTMState.IN_QUEUE

        if self._queue_dom_visible(page):
            return TTMState.IN_QUEUE

        if not is_content_page and any(
            text in body
            for text in [
                "you are now in the queue",
                "your place in the queue",
                "queue id",
                "queue number",
                "your queue",
                "estimated wait",
                "you are in line",
                "หมายเลขคิว",
                "กำลังรอคิว",
                "อยู่ในคิว",
            ]
        ):
            return TTMState.IN_QUEUE

        if any(
            text in body
            for text in [
                "coming soon",
                "ยังไม่เปิดจำหน่าย",
                "ยังไม่เปิดขาย",
            ]
        ):
            return TTMState.WAIT_SALE

        # หน้า Performance/Concert ก่อนเปิดขายบาง Event
        # ไม่มีข้อความ coming soon ใน body ที่ parser อ่านเจอ
        # แต่ยังไม่มี .row-enable ให้กด จึงถือเป็น WAIT_SALE
        try:
            current_url = (page.url or "").lower()

            if (
                "/performance/" in current_url
                or "/concert/" in current_url
            ):
                enabled_rounds = page.locator(
                    "#section-event-round .row.row-enable "
                    + self.ROUND_LINK_SELECTOR
                ).count()

                if enabled_rounds == 0:
                    return TTMState.WAIT_SALE
        except Exception:
            pass

        return TTMState.UNKNOWN

    # ==========================================================
    # ROUND
    # ==========================================================

    def read_show_rounds(self, page):
        select = page.locator("select#rdId").first

        if select.count() == 0:
            return []

        try:
            options = select.evaluate(
                """
                el => Array.from(el.options).map(o => ({
                    value: o.value,
                    text: (o.textContent || '').trim(),
                    disabled: !!o.disabled,
                }))
                """
            )
        except Exception:
            return []

        rounds = []

        for option in options:
            value = (option.get("value") or "").strip()
            text = re.sub(r"\s+", " ", option.get("text") or "").strip()

            if is_placeholder_option(value, text):
                continue

            rounds.append(
                {
                    "value": value,
                    "text": text,
                    "disabled": bool(option.get("disabled")),
                }
            )

        return rounds

    @staticmethod
    def _url_rd_id(url):
        match = re.search(r"[?&]rdId=([^&#]+)", url or "", flags=re.I)
        return match.group(1) if match else ""

    def _choose_dropdown_round(self, page, rounds):
        enabled = [r for r in rounds if not r["disabled"]]
        requested = (self.show_round or "").strip() or "AUTO"

        # 1) Round already chosen on the performance page (rdId link).
        #    Previously AUTO overrode it with the first dropdown option.
        chosen_value = (self.selected_round or {}).get("value") or ""

        if chosen_value:
            target = next(
                (r for r in enabled if r["value"] == chosen_value),
                None,
            )

            if target is not None:
                self.status("ℹ️ ใช้รอบที่เลือกจากหน้า Performance")
                return target

        # 2) Explicit show_round
        if requested.upper() != "AUTO":
            matches = match_rounds(requested, rounds)

            if not matches:
                available = ", ".join(r["text"] for r in enabled)
                raise Exception(
                    f"ไม่พบรอบการแสดง {requested} | "
                    f"รอบที่เลือกได้: {available}"
                )

            open_matches = [r for r in matches if not r["disabled"]]

            if not open_matches:
                raise Exception(
                    f"รอบการแสดง {requested} ถูกปิด/เต็ม (disabled) "
                    f"| {matches[0]['text']}"
                )

            if len(open_matches) > 1:
                self.status(
                    f"⚠️ '{requested}' ตรงหลายรอบ "
                    f"({len(open_matches)}) → เลือกรอบแรก"
                )

            return open_matches[0]

        if not enabled:
            raise Exception("ทุกรอบการแสดงถูกปิด/เต็ม (disabled)")

        # 3) AUTO: keep the round already loaded in the URL
        url_rd = self._url_rd_id(page.url)

        if url_rd:
            target = next(
                (r for r in enabled if r["value"] == url_rd),
                None,
            )

            if target is not None:
                return target

        return enabled[0]

    def _round_page_ready(self, page, value):
        """Zone map / seats loaded for the round currently in the dropdown."""
        try:
            select = page.locator("select#rdId").first

            if select.count() > 0:
                if (select.input_value() or "").strip() != value:
                    return False

            return (
                page.locator('map area[onclick*="selectzone"]').count() > 0
                or page.locator("#tableseats").count() > 0
            )
        except Exception:
            return False

    def select_show_round(self, page):
        """
        Select the round in the booking page dropdown (select#rdId) and
        confirm that the site actually loaded it.

        Returns True when confirmed, False when the page did not react
        (the caller retries). Raises when the round does not exist or is
        disabled, instead of silently continuing with another round.
        """
        rounds = self.read_show_rounds(page)

        if not rounds:
            raise Exception("ไม่พบรอบการแสดง")

        self.status(f"🎭 พบรอบการแสดง {len(rounds)} รอบ")

        for item in rounds:
            flag = " [ปิด]" if item["disabled"] else ""
            self.status(f'• {item["value"]} | {item["text"]}{flag}')

        target = self._choose_dropdown_round(page, rounds)

        self.status(f'🎯 เลือกรอบ: {target["text"]}')

        select = page.locator("select#rdId").first

        try:
            current_value = (select.input_value() or "").strip()
        except Exception:
            current_value = ""

        before_url = page.url

        # Already loaded: do not fire change again (it reloads the page).
        if current_value == target["value"] and (
            self._url_rd_id(before_url) == target["value"]
            or self._round_page_ready(page, target["value"])
        ):
            self.selected_round = target
            self.status(
                f"✅ รอบนี้ถูกโหลดอยู่แล้ว | value={target['value']}"
            )
            return True

        nav = {"started": False}
        main_frame = page.main_frame

        def on_request(request):
            try:
                if (
                    request.is_navigation_request()
                    and request.frame == main_frame
                ):
                    nav["started"] = True
            except Exception:
                pass

        page.on("request", on_request)

        try:
            if current_value != target["value"]:
                selected = False

                try:
                    if select.is_visible():
                        select.select_option(
                            value=target["value"],
                            timeout=3000,
                        )
                        selected = True
                except Exception as ex:
                    self.status(f"⚠️ select_option: {ex}")

                if not selected and not nav["started"]:
                    # Native select hidden behind a custom dropdown UI:
                    # set the value and fire the site's own change handler.
                    self.status(
                        "ℹ️ Dropdown ถูกซ่อน → ตั้งค่าและ Trigger change"
                    )
                    self._set_select_value(select, target["value"])
            else:
                # Default round already selected but not loaded:
                # browser does not fire onchange for the same value.
                self.status(
                    "ℹ️ Round นี้ถูกเลือกอยู่แล้ว → Trigger change event"
                )
                self._set_select_value(select, target["value"])

            confirmed = self._confirm_round_loaded(
                page, target, before_url, nav
            )
        finally:
            try:
                page.remove_listener("request", on_request)
            except Exception:
                pass

        if confirmed:
            self.selected_round = target
            self.status(
                f"✅ เลือกรอบการแสดงสำเร็จ | value={target['value']}"
            )
        else:
            self.status(
                "⚠️ เลือกรอบแล้วแต่หน้าเว็บยังไม่โหลดรอบนี้"
            )

        return confirmed

    @staticmethod
    def _set_select_value(select, value):
        try:
            select.evaluate(
                """
                (el, v) => {
                    el.value = v;
                    el.dispatchEvent(new Event("input", {bubbles: true}));
                    el.dispatchEvent(new Event("change", {bubbles: true}));
                }
                """,
                value,
                timeout=3000,
            )
        except Exception:
            # The change handler may navigate immediately.
            pass

    def _confirm_round_loaded(self, page, target, before_url, nav, timeout=20):
        value = target["value"]
        started = time.time()

        while time.time() - started < timeout:
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            current_url = page.url

            if self._url_rd_id(current_url) == value:
                try:
                    page.wait_for_load_state(
                        "domcontentloaded", timeout=10000
                    )
                except Exception:
                    pass
                return True

            if current_url != before_url:
                low = current_url.lower()

                # Redirected to queue / conditions after choosing round
                if (
                    self._is_queue_host(page)
                    or "verify_condition.php" in low
                ):
                    return True

                try:
                    page.wait_for_load_state(
                        "domcontentloaded", timeout=10000
                    )
                except Exception:
                    pass

                if self._round_page_ready(page, value):
                    return True

            # Same-page (AJAX) round loading
            if (
                not nav["started"]
                and time.time() - started > 2.5
                and self._round_page_ready(page, value)
            ):
                return True

            page.wait_for_timeout(100)

        return False

    # ==========================================================
    # ZONE
    # ==========================================================

    def _normalize_zone_name(self, value):
        """
        Normalize Zone โดยไม่บังคับรูปแบบชื่อ

        รองรับตัวอย่าง:
        A1, A2, A3, A4
        B1, B2, B3
        C1, C2, C3
        FF, HH
        SB, SC, SD, SE, SF, SG, SH, SI, SJ, SK, SL, SM, SN
        C, D, E, F, G, H, I, J, K, L, M, N, O, P, Q, R, S

        รวมถึงชื่อ Zone รูปแบบอื่นของ Event ในอนาคต.
        """
        value = str(value or "").strip()

        if not value:
            return ""

        # Decode simple URL fragment encoding if it appears.
        try:
            from urllib.parse import unquote
            value = unquote(value)
        except Exception:
            pass

        # Common prefixes accidentally included by DOM attributes.
        value = re.sub(
            r"^(zone[\s:_-]*)",
            "",
            value,
            flags=re.I,
        )

        # Remove fragment/query wrapper.
        value = value.split("#")[-1]
        value = value.split("?")[0]

        # Zone identifiers on TTM are normally alphanumeric.
        # Keep - and _ as fallback for future layouts.
        value = re.sub(
            r"[^A-Za-z0-9_-]",
            "",
            value,
        )

        return value.upper()

    def _extract_zone_name_from_area(self, area):
        """
        พยายามอ่านชื่อ Zone จากหลาย source ใน <area>
        เพื่อรองรับ layout ที่ไม่ได้เก็บ Zone ไว้ใน href แบบเดียว.
        """
        href = area.get_attribute("href") or ""
        onclick = area.get_attribute("onclick") or ""

        candidates = []

        # 1) href fragment เช่น #A1, #SB, #C
        if "#" in href:
            candidates.append(
                href.rsplit("#", 1)[-1]
            )

        # 2) data attributes / normal attributes
        for attr in [
            "data-zone",
            "data-zone-name",
            "data-zonecode",
            "data-zone-code",
            "title",
            "alt",
            "name",
            "id",
        ]:
            try:
                value = area.get_attribute(attr) or ""
                if value:
                    candidates.append(value)
            except Exception:
                pass

        # 3) onclick patterns:
        # selectzone(this.href,event)
        # selectzone('A1',...)
        # selectzone("#SB",...)
        for pattern in [
            r"""selectzone\s*\(\s*['"]#?([^'"]+)['"]""",
            r"""zone\s*[:=]\s*['"]([^'"]+)['"]""",
        ]:
            match = re.search(
                pattern,
                onclick,
                flags=re.I,
            )
            if match:
                candidates.append(
                    match.group(1)
                )

        for raw in candidates:
            name = self._normalize_zone_name(raw)

            # avoid element IDs like "map-area-01" being treated
            # as Zone if a better identifier does not exist.
            if (
                name
                and not name.startswith("MAPAREA")
                and not name.startswith("AREA")
            ):
                return name

        return ""

    def read_zones(self, page):
        """
        อ่าน Zone จาก Image Map แบบยืดหยุ่น

        เดิมอ่านเฉพาะ:
            map area[onclick*="selectzone"] + href #ZONE

        เวอร์ชันนี้รองรับ:
            - onclick selectzone
            - href มี #ZONE
            - data-zone / data-zone-name
            - title / alt fallback

        ทำให้รองรับ Zone แบบ:
            A1,A2,A3,A4,B1,B2,B3,C1,C2,C3,
            FF,HH,SB..SN,C..S
        โดยไม่ hard-code รายชื่อ.
        """
        areas = page.locator("map area")

        zones = []
        seen = set()

        for i in range(areas.count()):
            area = areas.nth(i)

            href = area.get_attribute("href") or ""
            onclick = area.get_attribute("onclick") or ""

            # Ignore unrelated image-map areas when there is
            # no indication that it represents a selectable zone.
            has_zone_hint = (
                "selectzone" in onclick.lower()
                or "#" in href
                or bool(area.get_attribute("data-zone"))
                or bool(area.get_attribute("data-zone-name"))
            )

            if not has_zone_hint:
                continue

            name = self._extract_zone_name_from_area(
                area
            )

            if not name:
                continue

            if name in seen:
                continue

            seen.add(name)

            zones.append(
                {
                    "name": name,
                    "href": href,
                    "index": i,
                }
            )

        return zones

    def _zone_candidates(self, zones):
        """
        คืน Zone ตาม Priority

        ZONE mode:
            รองรับ comma ทั้งอังกฤษ/ไทย เช่น:
            A1,A2,A3,A4
            SB,SC,SD,SE
            C,D,E,F
            FF,HH

        AUTO:
            ใช้ลำดับ Zone ที่หน้าเว็บส่งมา
        """

        zone_map = {
            self._normalize_zone_name(
                zone["name"]
            ): zone
            for zone in zones
        }

        if (
            self.selection_mode == "ZONE"
            and self.preferred_zones
        ):
            ordered = []

            for raw_name in self.preferred_zones:
                name = self._normalize_zone_name(
                    raw_name
                )

                zone = zone_map.get(name)

                if zone:
                    ordered.append(zone)

            return ordered

        return zones

    def handle_zone(self, page):
        zones = self.read_zones(page)

        if not zones:
            raise Exception(
                "ไม่พบ Zone ใน Image Map"
            )

        available_names = [
            self._normalize_zone_name(
                zone["name"]
            )
            for zone in zones
        ]

        self.status(
            "🗺️ Zones: "
            + ", ".join(available_names)
        )

        candidates = self._zone_candidates(
            zones
        )

        if (
            self.selection_mode == "ZONE"
            and self.preferred_zones
        ):
            normalized_priority = [
                self._normalize_zone_name(z)
                for z in self.preferred_zones
                if self._normalize_zone_name(z)
            ]

            self.status(
                "🎯 Zone Priority: "
                + " → ".join(
                    normalized_priority
                )
            )

            available_set = set(
                available_names
            )

            missing = [
                z
                for z in normalized_priority
                if z not in available_set
            ]

            for zone_name in missing:
                self.status(
                    f"⏭️ Zone {zone_name} ไม่มีในรอบนี้"
                )

        target = next(
            (
                zone
                for zone in candidates
                if self._normalize_zone_name(
                    zone["name"]
                )
                not in self._attempted_zones
            ),
            None,
        )

        if target is None:
            self.status(
                "🔁 ลอง Zone ครบทุก Zone แล้ว → เริ่มวนหา Seat ใหม่"
            )
            self._attempted_zones.clear()
            self._request_zone_switch = False
            self._seat_failure_reason = ""
            self._scan_cycle += 1
            self.status(f"🔁 SCAN CYCLE #{self._scan_cycle}")

            zones = self.read_zones(page)
            if not zones:
                raise Exception("ไม่พบ Zone หลังเริ่ม Scan Cycle ใหม่")

            candidates = self._zone_candidates(zones)
            target = next(iter(candidates), None)
            if target is None:
                raise Exception("ไม่มี Zone Candidate สำหรับ Scan Cycle ใหม่")

        self.selected_zone = (
            self._normalize_zone_name(
                target["name"]
            )
        )

        self._attempted_zones.add(
            self.selected_zone
        )

        self.status(
            f"✅ เลือก Zone: "
            f"{self.selected_zone}"
        )

        self.select_zone(
            page,
            self.selected_zone,
        )

    def select_zone(self, page, zone_name):
        """
        เลือก Zone แบบไม่ผูกกับชื่อรูปแบบ A1 เท่านั้น.

        รองรับ:
            A1 / B2 / C3
            SB / SC / ... / SN
            FF / HH
            C / D / ... / S
        """

        target = self._normalize_zone_name(
            zone_name
        )

        if not target:
            raise Exception(
                "ชื่อ Zone ไม่ถูกต้อง"
            )

        # Find target by parsing every <area>, rather than CSS
        # href suffix matching. This handles lowercase / encoded /
        # data-zone based layouts.
        areas = page.locator("map area")

        zone = None
        zone_info = None

        for i in range(areas.count()):
            area = areas.nth(i)

            current = (
                self._extract_zone_name_from_area(
                    area
                )
            )

            if current == target:
                zone = area
                zone_info = {
                    "index": i,
                    "href":
                        area.get_attribute(
                            "href"
                        )
                        or "",
                    "onclick":
                        area.get_attribute(
                            "onclick"
                        )
                        or "",
                }
                break

        if zone is None:
            raise Exception(
                f"ไม่พบ Zone {target}"
            )

        self.status(
            f"👉 Click Zone {target}"
        )

        def wait_seat_map():
            try:
                page.wait_for_selector(
                    "#tableseats td[title][data-info]",
                    timeout=8000,
                )

                return (
                    page.locator(
                        "#tableseats td[title][data-info]"
                    ).count()
                    > 0
                )
            except Exception:
                return False

        # --------------------------------------------------
        # 1) Normal Playwright Click
        # --------------------------------------------------
        try:
            zone.click(
                timeout=2200
            )

            if wait_seat_map():
                self.status(
                    "✅ Seat Map พร้อม"
                )
                return True

        except Exception:
            self.status(
                "⚠️ Normal Click Zone ไม่ผ่าน"
            )

        # --------------------------------------------------
        # 2) Call page selectzone() using actual area.href
        # --------------------------------------------------
        try:
            result = zone.evaluate(
                """
                el => {
                    try {
                        if (
                            typeof window.selectzone
                            === "function"
                        ) {
                            const ev =
                                new MouseEvent(
                                    "click",
                                    {
                                        bubbles: true,
                                        cancelable: true,
                                        view: window
                                    }
                                );

                            window.selectzone(
                                el.href,
                                ev
                            );

                            return {
                                ok: true,
                                method:
                                    "selectzone",
                                href: el.href
                            };
                        }

                        el.click();

                        return {
                            ok: true,
                            method:
                                "element.click",
                            href: el.href
                        };

                    } catch (error) {
                        return {
                            ok: false,
                            error:
                                String(error)
                        };
                    }
                }
                """
            )

            self.status(
                f"   Zone JS result={result}"
            )

            if wait_seat_map():
                self.status(
                    "✅ Seat Map พร้อม"
                )
                return True

        except Exception as ex:
            self.status(
                f"⚠️ Zone JS Error: {ex}"
            )

        # --------------------------------------------------
        # 3) Last fallback: dispatch actual click event
        # --------------------------------------------------
        try:
            zone.dispatch_event(
                "click"
            )

            if wait_seat_map():
                self.status(
                    "✅ Seat Map พร้อม"
                )
                return True
        except Exception:
            pass

        self.status(
            f"🔎 Zone info: {zone_info}"
        )

        raise Exception(
            f"เลือก Zone {target} ไม่สำเร็จ"
        )

    # ==========================================================
    # SEAT POPUP / ZONE FAILOVER
    # ==========================================================

    def _dismiss_seat_popup_if_present(self, page):
        """
        รองรับ popup แจ้ง Seat ไม่ว่างของ ThaiTicketMajor เช่น:

            #popup_alert
            "The selected seat is not available.
             Please select new seat."

        Popup นี้อยู่ใน Fancybox และต้องกด Close ก่อน
        จึงจะเลือกที่นั่งอื่นต่อได้.
        """

        popup_selectors = [
            "#popup_alert:visible",
            ".fancybox-wrap:visible #popup_alert",
            ".fancybox-overlay:visible #popup_alert",
            '[role="dialog"]:visible',
            ".fancybox-wrap:visible",
            ".fancybox-overlay:visible",
            ".swal2-container:visible",
            ".modal:visible",
            ".bootbox:visible",
        ]

        popup = None

        for selector in popup_selectors:
            try:
                loc = page.locator(selector).first

                if (
                    loc.count() > 0
                    and loc.is_visible()
                ):
                    popup = loc
                    break
            except Exception:
                pass

        if popup is None:
            return None

        try:
            message = (
                popup.inner_text(timeout=600)
                or ""
            ).strip()
        except Exception:
            message = ""

        if message:
            self.status(
                "⚠️ Seat Popup: "
                + " ".join(message.split())[:240]
            )

        # --------------------------------------------------
        # CLOSE POPUP
        # DOM ที่พบ:
        # <button ... onclick="jageClose()">Close</button>
        # และบางครั้งมี a.fancybox-close
        # --------------------------------------------------
        close_selectors = [
            '#popup_alert button:has-text("Close")',
            '#popup_alert button.btn-red',
            '.fancybox-wrap button:has-text("Close")',
            'button:has-text("Close")',
            'button:has-text("ปิด")',
            'button:has-text("ตกลง")',
            '.fancybox-item.fancybox-close',
            'a.fancybox-close',
            '.fancybox-close-small',
        ]

        closed = False

        for selector in close_selectors:
            try:
                btn = page.locator(selector).first

                if (
                    btn.count() > 0
                    and btn.is_visible()
                ):
                    btn.click(
                        timeout=1200,
                        force=True,
                    )
                    closed = True
                    self.status(
                        "✅ ปิด Seat Popup แล้ว"
                    )
                    break

            except Exception:
                pass

        # fallback: call the same page function only if the button
        # could not be clicked normally.
        if not closed:
            try:
                result = page.evaluate(
                    """
                    () => {
                        if (
                            typeof window.jageClose
                            === "function"
                        ) {
                            window.jageClose();
                            return true;
                        }
                        return false;
                    }
                    """
                )

                if result:
                    closed = True
                    self.status(
                        "✅ ปิด Seat Popup ด้วย page function แล้ว"
                    )
            except Exception:
                pass

        # รอ overlay หายก่อนเลือก Seat ใหม่
        if closed:
            for _ in range(20):
                try:
                    popup_visible = page.locator(
                        "#popup_alert:visible"
                    ).count() > 0

                    overlay_visible = page.locator(
                        ".fancybox-overlay:visible"
                    ).count() > 0

                    if (
                        not popup_visible
                        and not overlay_visible
                    ):
                        break
                except Exception:
                    break

                page.wait_for_timeout(50)

            page.wait_for_timeout(40)

        return message or "SEAT_NOT_AVAILABLE_POPUP"

    def return_to_zone_map(self, page):
        """
        จาก fixed/seat map กลับไปหน้า Zone เพื่อเลือก Priority ถัดไป

        ใช้ browser history ก่อน เพื่อรักษา session/query/round เดิม.
        ไม่สร้าง URL เองเพื่อลดความเสี่ยงไป Event ผิด.
        """

        self.status(
            "↩️ กลับ Zone Map..."
        )

        try:
            page.go_back(
                wait_until="domcontentloaded",
                timeout=8000,
            )
        except Exception as ex:
            self.status(
                f"⚠️ go_back error: {ex}"
            )

        for _ in range(30):
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            try:
                if (
                    page.locator(
                        'map area[onclick*="selectzone"]'
                    ).count()
                    > 0
                ):
                    self.status(
                        "✅ กลับ Zone Map สำเร็จ"
                    )
                    return True
            except Exception:
                pass

            page.wait_for_timeout(50)

        return False

    # ==========================================================
    # SEAT DOM
    # ==========================================================

    @staticmethod
    def _parse_price(seat_value):
        if not seat_value:
            return 0

        match = re.search(r"\*(\d+(?:\.\d+)?)", seat_value)

        if not match:
            return 0

        try:
            return int(float(match.group(1)))
        except Exception:
            return 0

    @staticmethod
    def _parse_row_number(title):
        if not title:
            return "", None

        match = re.match(r"(.+)-(\d+)$", title)

        if not match:
            return "", None

        try:
            return match.group(1), int(match.group(2))
        except Exception:
            return match.group(1), None

    def _perf(self, name, started):
        """Lightweight telemetry; never performs an extra DOM query."""
        ms = (time.perf_counter() - started) * 1000.0
        self._perf_last[name] = ms
        return ms

    def snapshot_usable_seats(self, page, excluded_titles=None):
        """v3.1: one browser round-trip for the entire seat map.

        Availability, data-info and child state are evaluated in-page once.
        Python receives only usable seat records and performs all ordering in
        memory. Server validation after click remains the final truth.
        """
        excluded_titles = excluded_titles or set()
        t0 = time.perf_counter()
        raw = page.locator("#tableseats td[title][data-info]").evaluate_all(
            """
            nodes => nodes.map(td => {
                const title = td.getAttribute('title') || '';
                const rawInfo = td.getAttribute('data-info') || '';
                const cls = (td.className || '').toString().toLowerCase();
                const check = title ? document.getElementById('checkseat-' + title) : null;
                const checkCls = check ? (check.className || '').toString().toLowerCase() : '';
                const blocked =
                    cls.includes('not-available') ||
                    cls.includes('not-allow') ||
                    !!td.querySelector('.seatnotavail,.seatsocialdis') ||
                    checkCls.includes('seatnotavail');
                return {title, rawInfo, blocked};
            }).filter(x => x.title && x.rawInfo && !x.blocked)
            """
        )

        seats = []
        for item in raw or []:
            title = item.get("title", "")
            if not title or title in excluded_titles:
                continue
            try:
                info = json.loads(item.get("rawInfo") or "{}")
            except Exception:
                continue
            seat_value = info.get("seat", "")
            row, number = self._parse_row_number(title)
            seat = {
                "title": title,
                "id": info.get("id", ""),
                "seat": seat_value,
                "seat_key": info.get("seatk", ""),
                "price": self._parse_price(seat_value),
                "row": row,
                "number": number,
            }
            if self.selection_mode == "PRICE" and self.preferred_price > 0:
                if seat["price"] != self.preferred_price:
                    continue
            if not self._seat_matches_price_range(seat):
                continue
            seats.append(seat)

        ms = self._perf("snapshot_ms", t0)
        self.status(f"⚡ Snapshot {ms:.0f}ms | Available {len(seats)}")
        return seats

    def read_seats(self, page):
        # Compatibility entry point. v3.1 intentionally uses one JS snapshot.
        return self.snapshot_usable_seats(page)

    def is_seat_usable(self, page, seat):
        """Fast single-seat check used only by compatibility/fallback paths."""
        try:
            return bool(self._seat_locator(page, seat).evaluate(
                """td => {
                    const title = td.getAttribute('title') || '';
                    const cls = (td.className || '').toString().toLowerCase();
                    const check = title ? document.getElementById('checkseat-' + title) : null;
                    const cc = check ? (check.className || '').toString().toLowerCase() : '';
                    return !cls.includes('not-available') &&
                           !cls.includes('not-allow') &&
                           !td.querySelector('.seatnotavail,.seatsocialdis') &&
                           !cc.includes('seatnotavail');
                }"""
            ))
        except Exception:
            return False

    def get_usable_seats(self, page):
        return self.snapshot_usable_seats(page)

    def _seat_matches_price_range(self, seat):
        try:
            price = int(seat.get("price") or 0)
        except Exception:
            return False
        if self.min_price > 0 and price < self.min_price:
            return False
        if self.max_price > 0 and price > self.max_price:
            return False
        return True

    def _log_price_filter(self):
        if self.min_price > 0 and self.max_price > 0:
            self.status(f"💰 ช่วงราคาที่เลือก: {self.min_price:,}-{self.max_price:,} THB")
        elif self.min_price > 0:
            self.status(f"💰 ราคาตั๋วตั้งแต่ {self.min_price:,} THB ขึ้นไป")
        elif self.max_price > 0:
            self.status(f"💰 ราคาตั๋วไม่เกิน {self.max_price:,} THB")

    def build_candidate_groups(self, page, quantity, excluded_titles=None, seats=None):
        """Build candidates entirely in Python memory from one snapshot."""
        excluded_titles = excluded_titles or set()
        if seats is None:
            seats = self.snapshot_usable_seats(page, excluded_titles)
        else:
            seats = [x for x in seats if x["title"] not in excluded_titles]

        if quantity <= 1:
            return [[seat] for seat in seats]
        if len(seats) < quantity:
            return []

        groups, seen = [], set()
        def add_group(group):
            if len(group) != quantity:
                return
            key = tuple(x["title"] for x in group)
            if key in seen:
                return
            seen.add(key); groups.append(group)

        rows = {}
        for seat in seats:
            if seat["row"] and seat["number"] is not None:
                rows.setdefault(seat["row"], []).append(seat)

        # Priority 1: adjacent, same row.
        for rs in rows.values():
            rs.sort(key=lambda x: x["number"])
            for i in range(len(rs) - quantity + 1):
                g = rs[i:i+quantity]
                nums = [x["number"] for x in g]
                if all(nums[j+1] == nums[j] + 1 for j in range(len(nums)-1)):
                    add_group(g)
        # Priority 2: same row, gaps allowed.
        for rs in rows.values():
            if len(rs) >= quantity:
                for i in range(len(rs) - quantity + 1):
                    add_group(rs[i:i+quantity])
        # Priority 3: any seats. Bounded sliding groups avoid combinations explosion.
        for i in range(len(seats) - quantity + 1):
            add_group(seats[i:i+quantity])
        return groups

    # ==========================================================
    # HYBRID: SEAT CLICK + validateseat.php RESPONSE
    # ==========================================================

    def _seat_locator(self, page, seat):
        # เว็บผูก click ที่ td แล้วหา #checkseat-{title}
        return page.locator(
            "#tableseats "
            f'td[title="{seat["title"]}"]'
        ).first

    def _is_selected_in_dom(self, page, seat):
        try:
            checkseat = page.locator(
                f'#checkseat-{seat["title"]}'
            ).first

            if checkseat.count() == 0:
                return False

            classes = (
                checkseat.get_attribute("class") or ""
            ).lower()

            hidden = page.locator(
                f'#hid-checkseat-{seat["title"]}'
            )

            return (
                "seatchecked" in classes
                or hidden.count() > 0
            )

        except Exception:
            return False

    def _click_seat_robust(self, page, seat):
        """Try several normal browser click paths for stale/awkward seat DOM."""
        td = self._seat_locator(page, seat)
        if td.count() == 0:
            raise Exception("SEAT_LOCATOR_NOT_FOUND")

        checkseat = page.locator(
            f'#checkseat-{seat["title"]}'
        ).first

        try:
            td.scroll_into_view_if_needed(timeout=800)
        except Exception:
            pass

        attempts = []
        attempts.append(("TD", lambda: td.click(timeout=900)))
        if checkseat.count() > 0:
            attempts.append(("CHECKSEAT", lambda: checkseat.click(timeout=900)))
            attempts.append(("CHECKSEAT_FORCE", lambda: checkseat.click(timeout=700, force=True)))
        attempts.append(("TD_FORCE", lambda: td.click(timeout=700, force=True)))
        attempts.append(("DOM_CLICK", lambda: td.evaluate("el => el.click()")))

        errors=[]
        for method, action in attempts:
            try:
                action()
                self.status(f"🖱️ Seat click method: {method}")
                return method
            except Exception as ex:
                errors.append(f"{method}={ex}")

        raise Exception(" | ".join(errors[-3:]))

    def click_seat_and_validate(self, page, seat):
        """
        Browser เป็นคน click และเว็บสร้าง validateseat.php เอง.

        เพิ่ม Popup Guard:
        - native alert/confirm
        - HTML modal/popup
        ถ้าเกิด popup หรือ validate fail จะส่ง reason กลับทันที.
        """

        td = self._seat_locator(page, seat)

        if td.count() == 0:
            return {
                "ok": False,
                "reason": "SEAT_LOCATOR_NOT_FOUND",
                "data": None,
            }

        self.status(
            f'👉 Click {seat["title"]} '
            f'({seat["price"]:,} THB)'
        )

        dialog_state = {
            "fired": False,
            "message": "",
        }

        def on_dialog(dialog):
            dialog_state["fired"] = True

            try:
                dialog_state["message"] = (
                    dialog.message or ""
                )
            except Exception:
                dialog_state["message"] = ""

            self._last_dialog_message = (
                dialog_state["message"]
            )

            self.status(
                "⚠️ Seat Dialog: "
                + (
                    dialog_state["message"]
                    or "Browser dialog"
                )
            )

            try:
                dialog.dismiss()
            except Exception:
                try:
                    dialog.accept()
                except Exception:
                    pass

        page.on(
            "dialog",
            on_dialog,
        )

        response = None

        try:
            with page.expect_response(
                lambda response:
                    "validateseat.php"
                    in response.url.lower(),
                timeout=1800,
            ) as response_info:

                self._click_seat_robust(
                    page,
                    seat,
                )

            response = response_info.value

        except PlaywrightTimeoutError:
            popup_message = (
                self._dismiss_seat_popup_if_present(
                    page
                )
            )

            if (
                dialog_state["fired"]
                or popup_message
            ):
                page.wait_for_timeout(60)

                return {
                    "ok": False,
                    "reason": "SEAT_POPUP",
                    "data": {
                        "message":
                            dialog_state["message"]
                            or popup_message
                    },
                }

            if self._is_selected_in_dom(page, seat):
                self.status(
                    f'✅ {seat["title"]}: DOM Selected แม้ไม่จับ XHR ทัน'
                )
                return {
                    "ok": True,
                    "reason": "DOM_SELECTED_NO_XHR",
                    "data": None,
                }

            self.status(
                f'⚠️ {seat["title"]}: '
                "ไม่พบ validateseat.php response"
            )
            return {
                "ok": False,
                "reason": "VALIDATE_TIMEOUT",
                "data": None,
            }

        except Exception as ex:
            popup_message = (
                self._dismiss_seat_popup_if_present(
                    page
                )
            )

            if (
                dialog_state["fired"]
                or popup_message
            ):
                page.wait_for_timeout(60)

                return {
                    "ok": False,
                    "reason": "SEAT_POPUP",
                    "data": {
                        "message":
                            dialog_state["message"]
                            or popup_message
                    },
                }

            self.status(
                f'⚠️ {seat["title"]}: '
                f'Click error {ex}'
            )

            return {
                "ok": False,
                "reason": "CLICK_ERROR",
                "data": None,
            }

        finally:
            try:
                page.remove_listener(
                    "dialog",
                    on_dialog,
                )
            except Exception:
                pass

        # Popup อาจเกิดพร้อม/หลัง XHR
        popup_message = (
            self._dismiss_seat_popup_if_present(
                page
            )
        )

        if (
            dialog_state["fired"]
            or popup_message
        ):
            page.wait_for_timeout(60)

            return {
                "ok": False,
                "reason": "SEAT_POPUP",
                "data": {
                    "message":
                        dialog_state["message"]
                        or popup_message
                },
            }

        try:
            data = response.json()
        except Exception:
            try:
                data = {
                    "raw": response.text()
                }
            except Exception:
                data = {}

        self.status(
            f'📡 validateseat.php '
            f'HTTP {response.status} → {data}'
        )

        result = bool(
            isinstance(data, dict)
            and data.get("result") is True
        )

        if not result:
            message = ""

            if isinstance(data, dict):
                message = (
                    data.get("message") or ""
                )

            self.status(
                f'❌ {seat["title"]} '
                "Validate ไม่ผ่าน"
                + (
                    f": {message}"
                    if message
                    else ""
                )
            )

            return {
                "ok": False,
                "reason": "SERVER_REJECTED",
                "data": data,
            }

        page.wait_for_timeout(25)

        if not self._is_selected_in_dom(
            page,
            seat,
        ):
            self.status(
                f'⚠️ {seat["title"]}: '
                "Server ผ่าน แต่ DOM ยังไม่ selected"
            )

            return {
                "ok": False,
                "reason": "DOM_NOT_SELECTED",
                "data": data,
            }

        self.status(
            f'✅ {seat["title"]} Validate ผ่าน'
        )

        return {
            "ok": True,
            "reason": "OK",
            "data": data,
        }

    def deselect_seats(self, page, seats):
        """
        Rollback กรณีเลือกกลุ่มได้บางส่วน.
        ใช้ click ตาม flow เว็บเดิมเพื่อเอา chkSeats[] ออก.
        """

        for seat in reversed(seats):
            try:
                if not self._is_selected_in_dom(page, seat):
                    continue

                td = self._seat_locator(page, seat)

                # การ deselect ยังอาจยิง validateseat.php
                try:
                    with page.expect_response(
                        lambda response:
                            "validateseat.php"
                            in response.url.lower(),
                        timeout=3000,
                    ):
                        td.click(timeout=2000)
                except Exception:
                    # บางเวอร์ชันอาจไม่ตอบตามที่คาด
                    try:
                        td.click(timeout=1500)
                    except Exception:
                        pass

                page.wait_for_timeout(40)

            except Exception:
                pass

    def select_seats_hybrid(self, page, quantity):
        """v3.1 Fast Snapshot seat engine.

        One snapshot -> candidate pool in memory -> click immediately.
        Resnapshot only when the pool is exhausted or repeated failures suggest
        that the snapshot has become stale. No per-candidate DOM re-scan.
        """
        excluded_titles = set()
        self._request_zone_switch = False
        self._seat_failure_reason = ""
        self._consecutive_seat_failures = 0
        self._log_price_filter()

        max_resnapshots = 6
        for scan_no in range(1, max_resnapshots + 1):
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            snapshot_started = time.perf_counter()
            seats = self.snapshot_usable_seats(page, excluded_titles)
            snapshot_done = time.perf_counter()

            if len(seats) < quantity:
                self.status(f"⏭️ Zone {self.selected_zone}: Available {len(seats)} < Need {quantity}")
                self._seat_failure_reason = "NOT_ENOUGH_SEATS"
                self._request_zone_switch = True
                return []

            groups = self.build_candidate_groups(
                page, quantity, excluded_titles, seats=seats
            )
            if not groups:
                self._seat_failure_reason = "NO_CANDIDATE_SEAT_LEFT"
                self._request_zone_switch = True
                return []

            first_click_logged = False
            for group in groups:
                if self.should_stop():
                    raise Exception("BOT_STOPPED")
                if any(x["title"] in excluded_titles for x in group):
                    continue

                self.status("🎯 Candidate: " + ", ".join(x["title"] for x in group))
                selected_now = []
                group_failed = False

                for seat in group:
                    if self.should_stop():
                        raise Exception("BOT_STOPPED")
                    if seat["title"] in excluded_titles:
                        group_failed = True
                        break

                    if not first_click_logged:
                        first_click_ms = (time.perf_counter() - snapshot_done) * 1000.0
                        self._perf_last["snapshot_to_first_click_ms"] = first_click_ms
                        self.status(f"⏱ Snapshot → First Click {first_click_ms:.0f}ms")
                        first_click_logged = True

                    t_click = time.perf_counter()
                    result = self.click_seat_and_validate(page, seat)
                    validation_ms = self._perf("validation_ms", t_click)

                    if not result["ok"]:
                        excluded_titles.add(seat["title"])
                        self._consecutive_seat_failures += 1
                        self._seat_failure_reason = f'{seat["title"]}: {result["reason"]}'
                        self.status(
                            f'❌ {seat["title"]} {result["reason"]} | '
                            f'Validation {validation_ms:.0f}ms'
                        )
                        group_failed = True
                        break

                    self._consecutive_seat_failures = 0
                    selected_now.append(seat)

                if not group_failed and len(selected_now) == quantity:
                    self.status(
                        f"✅ เลือกครบ {quantity} ที่ | Zone {self.selected_zone} | "
                        f"Snapshot {self._perf_last.get('snapshot_ms', 0):.0f}ms"
                    )
                    return selected_now

                if selected_now:
                    self.status("↩️ Partial selection → rollback เฉพาะ Seat ที่เลือกแล้ว")
                    self.deselect_seats(page, selected_now)

                # Snapshot is probably stale after repeated server/DOM failures.
                if self._consecutive_seat_failures >= self._stale_rescan_threshold:
                    self.status("♻️ Fail ต่อเนื่อง → Fast Resnapshot")
                    self._consecutive_seat_failures = 0
                    break
            else:
                # Candidate pool exhausted naturally.
                self.status("♻️ Candidate Pool หมด → Fast Resnapshot")
                continue

            # Broke from candidate loop due to stale threshold.
            continue

        self._seat_failure_reason = "SNAPSHOT_POOL_EXHAUSTED"
        self._request_zone_switch = True
        self.status(f"➡️ Zone {self.selected_zone} ไม่มี Candidate ที่สำเร็จ → Zone ถัดไป")
        return []

    # ==========================================================
    # ACCEPT TERMS ON PAYMENT PAGE
    # ==========================================================

    def accept_terms_if_present(self, page):
        """
        ติ๊กยอมรับข้อตกลง (#check-agree) ถ้าหน้า Payment มี checkbox นี้

        หมายเหตุ:
        - ทำเฉพาะ checkbox ยอมรับข้อตกลง
        - ไม่เลือกช่องทางชำระเงิน
        - ไม่กดปุ่มยืนยันการสั่งซื้อ
        """

        try:
            checkbox = page.locator("#check-agree").first

            if checkbox.count() == 0:
                self.status(
                    "ℹ️ ไม่พบ checkbox #check-agree — ข้าม"
                )
                return False

            # รอ element พร้อมแบบสั้น ๆ
            try:
                checkbox.wait_for(
                    state="attached",
                    timeout=2500,
                )
            except Exception:
                pass

            try:
                checked = checkbox.is_checked()
            except Exception:
                checked = False

            if checked:
                self.status(
                    "✅ ยอมรับข้อตกลงอยู่แล้ว"
                )
                return True

            self.status(
                "☑️ กำลังติ๊กยอมรับข้อตกลง..."
            )

            try:
                checkbox.check(
                    force=True,
                    timeout=2500,
                )
            except Exception:
                # fallback DOM property + events
                checkbox.evaluate(
                    """
                    el => {
                        el.checked = true;
                        el.dispatchEvent(
                            new Event("input", { bubbles: true })
                        );
                        el.dispatchEvent(
                            new Event("change", { bubbles: true })
                        );
                    }
                    """
                )

            try:
                checked = checkbox.is_checked()
            except Exception:
                checked = False

            if checked:
                self.status(
                    "✅ ติ๊กยอมรับข้อตกลงสำเร็จ"
                )
                return True

            self.status(
                "⚠️ พบ #check-agree แต่ติ๊กไม่สำเร็จ"
            )
            return False

        except Exception as ex:
            self.status(
                f"⚠️ Accept terms error: {ex}"
            )
            return False

    # ==========================================================
    # HYBRID CONFIRM: #booknow + bookingseats.php
    # ==========================================================

    def confirm_seats_hybrid(self, page):
        """Robust confirm: ใช้หน้า Payment/Step 3 เป็นหลัก ไม่ผูกกับ bookingseats.php อย่างเดียว"""

        button = page.locator("#booknow").first
        if button.count() == 0:
            for selector in [
                'a:has-text("ยืนยันที่นั่ง")',
                'button:has-text("ยืนยันที่นั่ง")',
                'a:has-text("Confirm")',
                'button:has-text("Confirm")',
            ]:
                candidate = page.locator(selector).first
                if candidate.count() > 0:
                    button = candidate
                    break

        try:
            button.wait_for(state="visible", timeout=6000)
        except PlaywrightTimeoutError:
            raise Exception("ไม่พบปุ่มยืนยันที่นั่ง #booknow")

        selected_count = page.locator("input[id^='hid-checkseat']").count()
        if selected_count != self.quantity:
            raise Exception(
                f"จำนวน Seat ก่อน Confirm ไม่ตรง: {selected_count}/{self.quantity}"
            )

        self.status(f"✅ พร้อม Confirm {selected_count} ที่")
        before_url = page.url
        seen_requests = []
        seen_responses = []
        navigation_seen = {"value": False}

        def interesting(url):
            value = (url or "").lower()
            return any(token in value for token in [
                "bookingseat", "paymentall", "payment", "booknow", "seat"
            ])

        def on_request(request):
            try:
                if interesting(request.url):
                    seen_requests.append(request.url)
            except Exception:
                pass

        def on_response(response):
            try:
                if interesting(response.url):
                    seen_responses.append((response.url, response.status))
            except Exception:
                pass

        def on_frame_navigated(frame):
            try:
                if frame == page.main_frame:
                    navigation_seen["value"] = True
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("framenavigated", on_frame_navigated)

        def is_payment_step():
            current_url = (page.url or "").lower()
            if "paymentall.php" in current_url or "/payment" in current_url:
                return True
            try:
                body_text = page.locator("body").inner_text(timeout=500).lower()
                return any(token in body_text for token in [
                    "ขั้นตอนที่ 3/4", "เลือกวิธีการชำระเงิน", "ยืนยันการสั่งซื้อ"
                ])
            except Exception:
                return False

        try:
            self.status("👉 Click ยืนยันที่นั่ง")
            try:
                button.scroll_into_view_if_needed()
            except Exception:
                pass

            try:
                button.click(timeout=2500, force=True)
            except Exception as ex:
                self.status(f"⚠️ Normal confirm click: {ex}")

            for _ in range(15):
                if self.should_stop():
                    raise Exception("BOT_STOPPED")
                if is_payment_step():
                    break
                if seen_requests or navigation_seen["value"] or page.url != before_url:
                    break
                page.wait_for_timeout(80)

            # fallback เฉพาะเมื่อ click แรกไม่ได้สร้าง request/navigation เลย
            if (not is_payment_step() and not seen_requests
                    and not navigation_seen["value"] and page.url == before_url):
                self.status("⚠️ Click แรกไม่มี Network/Navigation → Trigger DOM click fallback")
                try:
                    button.evaluate("""
                        el => {
                            for (const type of ['mousedown','mouseup','click']) {
                                el.dispatchEvent(new MouseEvent(type, {
                                    bubbles:true, cancelable:true, view:window
                                }));
                            }
                        }
                    """)
                except Exception as ex:
                    self.status(f"⚠️ DOM confirm fallback: {ex}")

            confirm_ok = False
            for _ in range(125):
                if self.should_stop():
                    raise Exception("BOT_STOPPED")
                if is_payment_step():
                    confirm_ok = True
                    break
                page.wait_for_timeout(80)

            if seen_requests:
                self.status("📤 Confirm requests: " + " | ".join(seen_requests[-3:]))
            if seen_responses:
                self.status("📥 Confirm responses: " + " | ".join(
                    f"{url} [{status}]" for url, status in seen_responses[-3:]
                ))

            if not confirm_ok:
                try:
                    info = button.evaluate("""
                        el => ({tag:el.tagName,id:el.id,href:el.getAttribute('href'),
                                onclick:el.getAttribute('onclick'),cls:el.className,
                                disabled:!!el.disabled})
                    """)
                    self.status(f"🔎 #booknow info: {info}")
                except Exception:
                    pass
                raise Exception("กดยืนยันที่นั่งแล้ว แต่หน้าเว็บไม่เข้าสู่ Payment/Step 3")

            self.status("✅ ยืนยันที่นั่งสำเร็จ")
            self.status(f"🌐 Current URL: {page.url}")
            try:
                self.accept_terms_if_present(page)
            except Exception:
                pass
            self.status("🛑 ถึงหน้า Payment แล้ว — Bot จะไม่ทำต่อ")
            self.status("❗ ไม่เลือกช่องทางชำระเงิน / ไม่กดยืนยันการสั่งซื้อ")
            return {
                "ok": True, "url_before": before_url, "url_after": page.url,
                "requests": seen_requests, "responses": seen_responses
            }
        finally:
            for event_name, handler in [("request", on_request),
                                        ("response", on_response),
                                        ("framenavigated", on_frame_navigated)]:
                try:
                    page.remove_listener(event_name, handler)
                except Exception:
                    pass
