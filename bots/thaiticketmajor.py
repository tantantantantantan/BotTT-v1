import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


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
                        try:
                            round_select = page.locator("select#rdId").first

                            if round_select.count() > 0:
                                valid_options = round_select.locator(
                                    'option[value]:not([value=""])'
                                )

                                if valid_options.count() > 0:
                                    self.status(
                                        "🎭 พบ Round Dropdown → เลือกรอบก่อน Zone"
                                    )

                                    self.select_show_round(page)
                                    round_done = True
                                    last_state = None

                                    page.wait_for_timeout(450)
                                    continue

                        except Exception as ex:
                            self.status(
                                f"⚠️ Round pre-check: {ex}"
                            )

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

                    if state == TTMState.ROUND and not round_done:
                        self.select_show_round(page)
                        round_done = True
                        page.wait_for_timeout(350)
                        continue

                    if state == TTMState.WAIT_SALE:
                        self.status("⏰ ยังไม่เปิดขาย")

                        # fallback กรณีอ่าน sale datetime ไม่ได้:
                        # refresh เฉพาะหน้า Performance/Concert
                        current_url = (page.url or "").lower()

                        if (
                            "/performance/" in current_url
                            or "/concert/" in current_url
                        ):
                            time.sleep(
                                self.presale_refresh_seconds
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

                        page.wait_for_timeout(250)
                        last_state = None
                        continue

                    if state == TTMState.IN_QUEUE:
                        self._attach_queue_status_monitor(page)

                        if last_state != TTMState.IN_QUEUE:
                            self.status(
                                "⏳ อยู่ใน Queue — ไม่ Refresh หน้า"
                            )

                        if self._visitor_presence_visible(page):
                            now_ts = time.time()

                            if (
                                now_ts - self._queue_last_log_time
                                >= 8
                            ):
                                self._queue_last_log_time = now_ts
                                self.status(
                                    "🔔 Still here? กรุณากด "
                                    "\"Yes, I'm here\" บน Browser"
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
        if self._queue_status_listener_attached:
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
                        self.status(
                            "📊 Queue Status | " + " | ".join(parts)
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

    def handle_waiting_room_entry(self, page):
        """
        รองรับหน้า Entry Zone / Waiting Room ที่มีปุ่ม:

            #waitingroomentry-content-holder
            button.botdetect-button.btn
            "Join waiting room"

        Bot จะกดปุ่ม Join waiting room ให้เพียงครั้งเดียว แล้วหยุด
        auto-refresh ทันที

        ถ้าหลังจากกดเว็บแสดง challenge/CAPTCHA/visitor verification
        Bot จะไม่พยายาม bypass และจะรอให้ผู้ใช้ดำเนินการบน Browser เอง.
        """

        # --------------------------------------------------
        # FIND JOIN BUTTON
        # --------------------------------------------------
        selectors = [
            "#waitingroomentry-content-holder "
            "button.botdetect-button.btn",
            "#waitingroomentry-content-holder button",
            'button:has-text("Join waiting room")',
            'button:has-text("Join Waiting Room")',
        ]

        button = None

        for selector in selectors:
            try:
                loc = page.locator(selector).first

                if (
                    loc.count() > 0
                    and loc.is_visible()
                ):
                    button = loc
                    break

            except Exception:
                pass

        if button is None:
            return False

        self.status(
            "🎟️ พบปุ่ม Join waiting room"
        )

        # --------------------------------------------------
        # CLICK ACTUAL BUTTON
        # --------------------------------------------------
        try:
            button.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            button.click(
                timeout=2500,
            )

            self.status(
                "✅ Click Join waiting room สำเร็จ"
            )

        except Exception as ex:
            self.status(
                f"⚠️ Normal Join click ไม่ผ่าน: {ex}"
            )

            try:
                button.evaluate(
                    """
                    el => el.dispatchEvent(
                        new MouseEvent("click", {
                            bubbles: true,
                            cancelable: true,
                            view: window
                        })
                    )
                    """
                )

                self.status(
                    "✅ Trigger Join waiting room สำเร็จ"
                )

            except Exception as ex2:
                self.status(
                    f"❌ Join waiting room ไม่สำเร็จ: {ex2}"
                )
                return False

        # --------------------------------------------------
        # WAIT FOR QUEUE / CHALLENGE
        # --------------------------------------------------
        for _ in range(60):  # ~6 sec
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            # Challenge / verification visible
            try:
                challenge = page.locator(
                    "#challenge-widget-container"
                ).first

                if (
                    challenge.count() > 0
                    and challenge.is_visible()
                ):
                    self._attach_queue_status_monitor(page)
                    self.status(
                        "🧩 พบ Visitor Verification / Challenge"
                    )
                    self.status(
                        "👤 กรุณาดำเนินการ Challenge บน Browser "
                        "หากเว็บร้องขอ"
                    )
                    return True
            except Exception:
                pass

            # Waiting room entry button disappeared = click accepted
            try:
                holder = page.locator(
                    "#waitingroomentry-content-holder"
                ).first

                if (
                    holder.count() == 0
                    or not holder.is_visible()
                ):
                    self._attach_queue_status_monitor(page)
                    self.status(
                        "⏳ เข้าสู่ Waiting Room แล้ว"
                    )
                    return True
            except Exception:
                pass

            # Queue page/state changed
            try:
                body = (
                    page.locator("body")
                    .inner_text(timeout=400)
                    .lower()
                )

                if any(
                    token in body
                    for token in [
                        "your queue",
                        "queue number",
                        "estimated wait",
                        "you are in line",
                        "หมายเลขคิว",
                        "อยู่ในคิว",
                        "กำลังรอคิว",
                    ]
                ):
                    self._attach_queue_status_monitor(page)
                    self.status(
                        "⏳ อยู่ใน Queue แล้ว"
                    )
                    return True

            except Exception:
                pass

            page.wait_for_timeout(100)

        # Click may still have been accepted even if DOM does not change fast.
        self._attach_queue_status_monitor(page)
        self.status(
            "ℹ️ กด Join waiting room แล้ว "
            "กำลังรอระบบ Queue"
        )
        return True

    def _queue_detected(self, page):
        """
        สำคัญ:
        ถ้าเข้า Queue จริงแล้ว จะไม่ refresh อัตโนมัติ
        เพื่อไม่เสี่ยงเสียลำดับคิว.
        """

        try:
            url = (page.url or "").lower()

            queue_url_tokens = [
                "queue",
                "waiting",
                "waitingroom",
                "busy.",
            ]

            if any(
                token in url
                for token in queue_url_tokens
            ):
                return True

            # Waiting Room Entry page
            try:
                join_btn = page.locator(
                    "#waitingroomentry-content-holder "
                    "button.botdetect-button.btn"
                ).first

                if (
                    join_btn.count() > 0
                    and join_btn.is_visible()
                ):
                    return True
            except Exception:
                pass

            body = (
                page.locator("body")
                .inner_text(timeout=600)
                .lower()
            )

            return any(
                token in body
                for token in [
                    "join waiting room",
                    "waiting room",
                    "queue number",
                    "your queue",
                    "หมายเลขคิว",
                    "กำลังรอคิว",
                    "อยู่ในคิว",
                    "รอคิว",
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

            if remain % 60 == 0 or remain < 60:
                self.status(
                    f"⏳ รอถึงช่วง Pre-login "
                    f"อีก {remain}s"
                )

            time.sleep(
                min(
                    30,
                    max(1, remain),
                )
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

            self.status(
                f"⏳ รอเปิดขาย {remain}s "
                f"| เช็ค Waiting Room ทุก "
                f"{preview_refresh}s"
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

            time.sleep(sleep_for)

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

            self.status(
                f"🔄 On-sale check "
                f"{attempt}/90"
            )

            time.sleep(
                self.onsale_refresh_seconds
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

    def handle_performance_page(self, page):
        """
        Performance / On-sale handler v2.1

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
        - ใช้เฉพาะ .row-enable เท่านั้น
        - เลือก event block จาก DOM จริง ไม่เก็บปุ่มซื้อบัตรจากส่วนอื่น
        - ถ้ามี ticket_type จะพยายามเลือก block ที่มีข้อความตรงกัน
        - กด anchor จริงด้วย Playwright เพื่อให้ Queue/JS ของเว็บทำงาน
        - ค่าว่างของ show_round = AUTO
        """

        url = (page.url or "").lower()

        if (
            "/performance/" not in url
            and "/concert/" not in url
        ):
            return False

        # ==========================================================
        # FIND ENABLED EVENT BLOCKS
        # ==========================================================

        event_blocks = page.locator(
            "#section-event-round .event-detail-item"
        )

        block_candidates = []

        for i in range(event_blocks.count()):
            block = event_blocks.nth(i)

            try:
                enabled_links = block.locator(
                    '.row.row-enable '
                    '.col-btn a.btn'
                    '[href*="/booking/3m/zones.php"]'
                    '[href*="query="]'
                )

                if enabled_links.count() == 0:
                    continue

                try:
                    block_text = (
                        block.inner_text(timeout=800)
                        or ""
                    ).strip()
                except Exception:
                    block_text = ""

                block_candidates.append(
                    {
                        "index": i,
                        "block": block,
                        "text": block_text,
                        "count": enabled_links.count(),
                    }
                )

            except Exception:
                continue

        # ยังไม่มี row-enable = ยังไม่เปิดให้กด
        if not block_candidates:
            return False

        # ==========================================================
        # CHOOSE EVENT BLOCK
        # ==========================================================

        chosen_block = None

        ticket_query = (
            self.ticket_type.strip().lower()
            if self.ticket_type
            else ""
        )

        if ticket_query:
            for item in block_candidates:
                if ticket_query in item["text"].lower():
                    chosen_block = item
                    self.status(
                        "🎫 Match Event Block จาก Ticket Type: "
                        f"{self.ticket_type}"
                    )
                    break

        if chosen_block is None:
            # ใช้ block แรกที่มี row-enable ตามลำดับ DOM
            # ป้องกันไปหยิบ CTA/recommendation อื่นนอก section
            chosen_block = block_candidates[0]

        block = chosen_block["block"]

        self.status(
            "🔥 SALE OPEN / พบ Event Round ที่กดได้"
        )

        # ==========================================================
        # READ ONLY ENABLED ROUND LINKS IN SELECTED BLOCK
        # ==========================================================

        links = block.locator(
            '.row.row-enable '
            '.col-btn a.btn'
            '[href*="/booking/3m/zones.php"]'
            '[href*="query="]'
        )

        rounds = []

        for i in range(links.count()):
            link = links.nth(i)

            try:
                href = link.get_attribute("href") or ""

                if not href:
                    continue

                query_match = re.search(
                    r"[?&]query=(\d+)",
                    href,
                    flags=re.I,
                )

                if not query_match:
                    continue

                rd_match = re.search(
                    r"[?&]rdId=(\d+)",
                    href,
                    flags=re.I,
                )

                # เวลาในปุ่ม ใช้ .item-show เป็นหลัก
                time_text = ""

                try:
                    show = link.locator(
                        ".item-show"
                    ).first

                    if show.count() > 0:
                        time_text = (
                            show.inner_text(timeout=500)
                            or ""
                        ).strip()
                except Exception:
                    pass

                if not time_text:
                    try:
                        time_text = (
                            link.inner_text(timeout=500)
                            or ""
                        ).strip()
                    except Exception:
                        time_text = ""

                # หา date label จาก row / DOM ใกล้เคียง
                date_text = ""

                try:
                    row = link.locator(
                        "xpath=ancestor::div[contains(@class,'row')][1]"
                    )

                    if row.count() > 0:
                        label = row.locator(
                            ".col-label"
                        ).first

                        if label.count() > 0:
                            date_text = (
                                label.inner_text(timeout=500)
                                or ""
                            ).strip()
                except Exception:
                    pass

                rounds.append(
                    {
                        "element": link,
                        "href": href,
                        "query": query_match.group(1),
                        "value": (
                            rd_match.group(1)
                            if rd_match
                            else ""
                        ),
                        "time": time_text,
                        "date": date_text,
                        "text": (
                            f"{date_text} {time_text}"
                        ).strip(),
                    }
                )

            except Exception:
                continue

        if not rounds:
            return False

        # Safety: ใช้ query หลักใน block นี้
        query_counter = Counter(
            item["query"]
            for item in rounds
        )

        event_query = (
            query_counter.most_common(1)[0][0]
        )

        rounds = [
            item
            for item in rounds
            if item["query"] == event_query
        ]

        self.status(
            f"🎫 Performance Event query={event_query}"
        )

        self.status(
            f"🎭 พบรอบที่กดได้ {len(rounds)} รอบ"
        )

        for item in rounds:
            rd_text = (
                f"rdId={item['value']}"
                if item["value"]
                else "Queue Link"
            )

            self.status(
                f"• {item['text']} | {rd_text}"
            )

        # ==========================================================
        # SELECT ROUND
        # ==========================================================

        target = None

        requested = (
            self.show_round.strip()
            if self.show_round
            else "AUTO"
        )

        if not requested:
            requested = "AUTO"

        if requested.upper() != "AUTO":
            q = requested.lower()

            # rdId exact
            target = next(
                (
                    x
                    for x in rounds
                    if x["value"]
                    and x["value"].lower() == q
                ),
                None,
            )

            # Full date/time exact
            if target is None:
                target = next(
                    (
                        x
                        for x in rounds
                        if x["text"].lower() == q
                    ),
                    None,
                )

            # Visible time exact เช่น 18:00
            if target is None:
                target = next(
                    (
                        x
                        for x in rounds
                        if x["time"].lower() == q
                    ),
                    None,
                )

            # Partial match
            if target is None:
                target = next(
                    (
                        x
                        for x in rounds
                        if q in x["text"].lower()
                    ),
                    None,
                )

            if target is None:
                raise Exception(
                    f"ไม่พบรอบการแสดง {self.show_round}"
                )

        if target is None:
            target = rounds[0]

        self.selected_round = {
            "value": target["value"],
            "text": target["text"],
        }

        self.status(
            "🎯 เลือกรอบ: "
            f"{target['text']}"
        )

        # ==========================================================
        # CLICK ACTUAL ON-SALE / QUEUE BUTTON
        # ==========================================================

        before_url = page.url
        target_link = target["element"]

        self.status(
            "🚀 Click รอบ / รับคิวซื้อบัตร"
        )

        try:
            target_link.scroll_into_view_if_needed()

            target_link.click(
                timeout=4000,
            )

        except Exception as ex:
            self.status(
                f"⚠️ Normal round click: {ex}"
            )

            # fallback DOM click - ยังใช้ element จริงของเว็บ
            target_link.evaluate(
                "el => el.click()"
            )

        # ==========================================================
        # WAIT QUEUE / BOOKING NAVIGATION
        # ==========================================================

        for _ in range(80):  # ~8 sec
            if self.should_stop():
                raise Exception("BOT_STOPPED")

            current_url = page.url

            if current_url != before_url:
                self.status(
                    "✅ รอบถูกกดแล้ว"
                )
                self.status(
                    f"🌐 {current_url}"
                )
                return True

            if self._queue_detected(page):
                self.status(
                    "🎟️ เข้าระบบ Queue แล้ว"
                )
                return True

            try:
                if (
                    page.locator("select#rdId").count() > 0
                    or page.locator(
                        'map area[onclick*="selectzone"]'
                    ).count() > 0
                    or page.locator(
                        "#tableseats"
                    ).count() > 0
                ):
                    self.status(
                        "✅ เข้าหน้า Booking แล้ว"
                    )
                    return True
            except Exception:
                pass

            page.wait_for_timeout(100)

        # ถ้าคลิกไม่เกิดอะไรจริง ๆ ให้ใช้ href จากปุ่มเดิม
        # เป็น fallback สุดท้ายเท่านั้น
        self.status(
            "⚠️ Click รอบยังไม่เปลี่ยนหน้า "
            "→ ใช้ href ของปุ่มเป็น fallback"
        )

        page.goto(
            target["href"],
            wait_until="domcontentloaded",
            timeout=30000,
        )

        self.status(
            f"🌐 {page.url}"
        )

        return True

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

            if second and second % 10 == 0:
                self.status(f"⏳ Waiting Login... {second}s")

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

                if not current:
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

        try:
            body = (
                page.locator("body")
                .inner_text(timeout=1500)
                .lower()
            )
        except Exception:
            body = ""

        # --------------------------------------------------
        # WAITING ROOM ENTRY
        # ต้องเช็กก่อน IN_QUEUE เพราะหน้า Entry มีคำว่า
        # "waiting room" อยู่ใน body เช่นกัน
        # --------------------------------------------------
        try:
            join_button = page.locator(
                "#waitingroomentry-content-holder "
                "button.botdetect-button.btn"
            ).first

            if (
                join_button.count() > 0
                and join_button.is_visible()
            ):
                return TTMState.QUEUE_ENTRY
        except Exception:
            pass

        if any(
            text in body
            for text in [
                "join waiting room",
                "get in line",
                "join queue",
                "กดเพื่อรอรับคิว",
                "เข้าคิว",
            ]
        ):
            return TTMState.QUEUE_ENTRY

        # Queue-it actual queue page
        try:
            current_url = (page.url or "").lower()

            if (
                "wait.thaiticketmajor.com" in current_url
                and (
                    "/view" in current_url
                    or "queue" in current_url
                )
            ):
                return TTMState.IN_QUEUE
        except Exception:
            pass

        if any(
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
                    "#section-event-round "
                    ".row.row-enable "
                    '.col-btn a.btn'
                    '[href*="/booking/3m/zones.php"]'
                    '[href*="query="]'
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

        rounds = []
        options = select.locator("option")

        for i in range(options.count()):
            option = options.nth(i)
            value = option.get_attribute("value") or ""
            text = (option.text_content() or "").strip()

            if not value:
                continue

            rounds.append(
                {
                    "value": value,
                    "text": text,
                }
            )

        return rounds

    def select_show_round(self, page):
        rounds = self.read_show_rounds(page)

        if not rounds:
            raise Exception("ไม่พบรอบการแสดง")

        self.status(f"🎭 พบรอบการแสดง {len(rounds)} รอบ")

        for item in rounds:
            self.status(f'• {item["value"]} | {item["text"]}')

        target = None

        if self.show_round and self.show_round.upper() != "AUTO":
            query = self.show_round.lower()

            target = next(
                (
                    x
                    for x in rounds
                    if x["value"].lower() == query
                ),
                None,
            )

            if target is None:
                target = next(
                    (
                        x
                        for x in rounds
                        if x["text"].lower() == query
                    ),
                    None,
                )

            if target is None:
                target = next(
                    (
                        x
                        for x in rounds
                        if query in x["text"].lower()
                    ),
                    None,
                )

            if target is None:
                raise Exception(
                    f"ไม่พบรอบการแสดง {self.show_round}"
                )

        if target is None:
            target = rounds[0]

        self.status(f'🎯 เลือกรอบ: {target["text"]}')

        select = page.locator("select#rdId").first

        try:
            current_value = (select.input_value() or "").strip()
        except Exception:
            current_value = ""

        # ถ้าค่าปัจจุบันกับ target ต่างกัน ใช้ select_option ตามปกติ
        if current_value != target["value"]:
            select.select_option(value=target["value"])

        else:
            # บางหน้า TTM default รอบแรกไว้แล้ว
            # ถ้าเลือกค่าเดิม browser อาจไม่ยิง onchange
            # จึง trigger change เอง เพื่อให้ round_change() ของเว็บทำงาน
            self.status(
                "ℹ️ Round นี้ถูกเลือกอยู่แล้ว → Trigger change event"
            )

            select.evaluate(
                """
                el => {
                    el.dispatchEvent(
                        new Event("change", {
                            bubbles: true
                        })
                    );
                }
                """
            )

        self.selected_round = target

        # รอหน้าเว็บประมวลผล round_change()
        page.wait_for_timeout(1500)

        self.status(
            f"✅ เลือกรอบการแสดงสำเร็จ | value={target['value']}"
        )

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
