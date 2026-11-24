import threading
import traceback

from PySide6.QtCore import QObject, Qt, Signal, QLocale
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QFrame,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from bots.thaiticketmajor import ThaiTicketMajorBot


class UiSignals(QObject):
    log = Signal(str)
    progress = Signal(str)
    finished = Signal()
    failed = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self, license_result=None):
        super().__init__()

        self.license_result = license_result

        self.bot = None
        self.bot_thread = None
        self.stop_requested = False

        self.signals = UiSignals()
        self.signals.log.connect(self.append_log)
        self.signals.progress.connect(self.set_progress)
        self.signals.finished.connect(self.on_finished)
        self.signals.failed.connect(self.on_failed)

        self.setWindowTitle("Ticket Assistant v3.1.1 Fast Smart TESTER")
        self.resize(980, 820)
        self.setMinimumSize(820, 760)

        self._build_ui()
        self._apply_style()
        self._update_mode()

    # ----------------------------------------------------------
    # LAYOUT HELPERS
    # ----------------------------------------------------------

    @staticmethod
    def _field(label_text, widget):
        """Small caption above an input, as one aligned unit."""
        box = QVBoxLayout()
        box.setSpacing(6)
        box.setContentsMargins(0, 0, 0, 0)

        label = QLabel(label_text)
        label.setObjectName("fieldLabel")
        # Thai and Latin captions have different natural heights
        label.setFixedHeight(20)

        # Same height for every input so rows line up exactly
        widget.setFixedHeight(40)

        box.addWidget(label)
        box.addWidget(widget)
        return box

    @staticmethod
    def _card(title_text):
        card = QFrame()
        card.setObjectName("card")

        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(12)

        title = QLabel(title_text)
        title.setObjectName("cardTitle")
        layout.addWidget(title)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)

        # Same 4-column proportions in every card so fields line up
        for column, stretch in enumerate((2, 4, 2, 2)):
            grid.setColumnStretch(column, stretch)

        layout.addLayout(grid)
        return card, layout, grid

    @staticmethod
    def _english_spinbox(minimum, maximum, step=1):
        spin = QSpinBox()
        spin.setLocale(
            QLocale(
                QLocale.Language.English,
                QLocale.Country.UnitedStates,
            )
        )
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return spin

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)

        # ======================================================
        # HEADER
        # ======================================================
        header = QHBoxLayout()
        header.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(2)

        title = QLabel("🎟️ Ticket Assistant Hybrid")
        title.setObjectName("title")
        titles.addWidget(title)

        subtitle = QLabel("ThaiTicketMajor  •  v3.1.1 Fast Snapshot")
        subtitle.setObjectName("subtitle")
        titles.addWidget(subtitle)

        header.addLayout(titles)
        header.addStretch(1)

        if self.license_result is not None:
            expiry = self.license_result.expire_date or ""
            if expiry:
                try:
                    y, m, d = expiry.split("-")
                    expiry = f"{d}/{m}/{y}"
                except Exception:
                    pass

            license_info = QLabel(
                f"🧪 TEST LICENSE  •  ถึง {expiry}  •  "
                f"เหลือ {self.license_result.days_left} วัน"
            )
            license_info.setObjectName("licenseInfo")
            header.addWidget(
                license_info, 0, Qt.AlignmentFlag.AlignVCenter
            )

        self.state_label = QLabel("● STOPPED")
        self.state_label.setObjectName("state")
        self.state_label.setProperty("running", False)
        header.addWidget(
            self.state_label, 0, Qt.AlignmentFlag.AlignVCenter
        )

        root.addLayout(header)

        # ======================================================
        # CARD 1: EVENT
        # ======================================================
        event_card, _, event_grid = self._card("ข้อมูลงาน")

        self.event_url = QLineEdit()
        self.event_url.setPlaceholderText(
            "https://www.thaiticketmajor.com/concert/... หรือ /performance/..."
        )

        self.quantity = self._english_spinbox(1, 20)
        self.quantity.setValue(1)
        self.quantity.setSuffix(" ใบ")

        self.show_round = QLineEdit()
        self.show_round.setPlaceholderText(
            "ว่าง = AUTO  เช่น 18:00 หรือ 16 ส.ค. 20:00"
        )

        self.ticket_type = QLineEdit()
        self.ticket_type.setPlaceholderText("เช่น VIP Standing")

        event_grid.addLayout(
            self._field("Event URL", self.event_url), 0, 0, 1, 4
        )
        event_grid.addLayout(
            self._field("จำนวนบัตร", self.quantity), 1, 0
        )
        event_grid.addLayout(
            self._field("รอบการแสดง", self.show_round), 1, 1
        )
        event_grid.addLayout(
            self._field("Ticket Type", self.ticket_type), 1, 2, 1, 2
        )

        root.addWidget(event_card)

        # ======================================================
        # CARD 2: SEAT SELECTION
        # ======================================================
        seat_card, seat_layout, seat_grid = self._card("การเลือกที่นั่ง")

        self.selection_mode = QComboBox()
        self.selection_mode.addItems(["AUTO", "ZONE", "PRICE"])
        self.selection_mode.currentTextChanged.connect(
            self._update_mode
        )

        self.zone_priority = QLineEdit()
        self.zone_priority.setPlaceholderText(
            "A1,A2,SB,SC,C,D,FF,HH"
        )

        self.min_price = self._english_spinbox(0, 100000, 100)
        self.min_price.setSpecialValueText("ไม่กำหนด")
        self.min_price.setSuffix(" THB")

        self.max_price = self._english_spinbox(0, 100000, 100)
        self.max_price.setSpecialValueText("ไม่กำหนด")
        self.max_price.setSuffix(" THB")

        seat_grid.addLayout(
            self._field("รูปแบบการเลือก", self.selection_mode), 0, 0
        )
        seat_grid.addLayout(
            self._field("Zone Priority", self.zone_priority), 0, 1
        )
        seat_grid.addLayout(
            self._field("ราคาต่ำสุด", self.min_price), 0, 2
        )
        seat_grid.addLayout(
            self._field("ราคาสูงสุด", self.max_price), 0, 3
        )

        help_label = QLabel(
            "Zone ใช้ , คั่นตามลำดับความสำคัญ  •  ราคา 0 = ไม่จำกัด  •  "
            "รอบว่าง = AUTO  •  Queue Entry = T-60 นาที  •  "
            "CAPTCHA / ชำระเงิน ทำเองบน Browser"
        )
        help_label.setWordWrap(True)
        help_label.setObjectName("help")
        seat_layout.addWidget(help_label)

        root.addWidget(seat_card)

        # ======================================================
        # ACTIONS
        # ======================================================
        buttons = QHBoxLayout()
        buttons.setSpacing(12)

        self.start_button = QPushButton("▶  START BOT")
        self.start_button.setObjectName("start")
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(self.start_bot)

        self.stop_button = QPushButton("■  STOP")
        self.stop_button.setObjectName("stop")
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_bot)

        buttons.addWidget(self.start_button, 3)
        buttons.addWidget(self.stop_button, 1)
        root.addLayout(buttons)

        # ======================================================
        # LOG
        # ======================================================
        log_card = QFrame()
        log_card.setObjectName("card")

        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(18, 12, 18, 16)
        log_layout.setSpacing(10)

        log_header = QHBoxLayout()

        log_title = QLabel("BOT STATUS / LOG")
        log_title.setObjectName("cardTitle")
        log_header.addWidget(log_title)
        log_header.addStretch(1)

        clear_button = QPushButton("ล้าง Log")
        clear_button.setObjectName("ghost")
        clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_button.clicked.connect(lambda: self.log_box.clear())
        log_header.addWidget(clear_button)

        log_layout.addLayout(log_header)

        # Live line for countdowns / queue progress: updated in place,
        # so the log below only collects real events.
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("live")
        self.progress_label.setWordWrap(True)
        self.progress_label.setVisible(False)
        log_layout.addWidget(self.progress_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(150)
        self.log_box.setObjectName("log")
        log_layout.addWidget(self.log_box, 1)

        root.addWidget(log_card, 1)

    def _set_running(self, running):
        self.state_label.setText(
            "● RUNNING" if running else "● STOPPED"
        )
        self.state_label.setProperty("running", running)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)

    def _update_mode(self):
        # Zone field can still be typed in AUTO for reference,
        # but becomes primary when mode=ZONE.
        is_zone = self.selection_mode.currentText().upper() == "ZONE"
        self.zone_priority.setEnabled(True)

        if is_zone:
            self.zone_priority.setPlaceholderText(
                "Priority เช่น A1,A2,SB,SC,C,D,FF,HH"
            )
        else:
            self.zone_priority.setPlaceholderText(
                "ใช้เมื่อเลือกโหมด ZONE"
            )

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget#central {
                background: #0b1220;
            }
            QWidget {
                color: #e2e8f0;
                font-family: "Segoe UI", "Leelawadee UI", "Tahoma";
                font-size: 14px;
            }
            QLabel {
                background: transparent;
            }
            QLabel#title {
                font-size: 24px;
                font-weight: 800;
                color: #f8fafc;
            }
            QLabel#subtitle {
                color: #7c8db0;
                font-size: 13px;
            }
            QLabel#licenseInfo {
                background: #1a2540;
                color: #c7d2fe;
                border: 1px solid #2b3a5e;
                border-radius: 13px;
                padding: 4px 12px;
                min-height: 18px;
                font-size: 12px;
                font-weight: 600;
            }
            QLabel#state {
                background: #1e293b;
                color: #94a3b8;
                border: 1px solid #334155;
                border-radius: 13px;
                padding: 4px 14px;
                min-height: 18px;
                font-size: 12px;
                font-weight: 800;
            }
            QLabel#state[running="true"] {
                background: #0f2f24;
                color: #34d399;
                border: 1px solid #166a4b;
            }
            QFrame#card {
                background: #111a2e;
                border: 1px solid #1f2b47;
                border-radius: 14px;
            }
            QLabel#cardTitle {
                color: #93a4c8;
                font-size: 12px;
                font-weight: 800;
                letter-spacing: 1px;
            }
            QLabel#fieldLabel {
                color: #b6c2da;
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#help {
                color: #6f80a3;
                font-size: 12px;
            }
            QLineEdit, QComboBox, QSpinBox {
                background: #0a1324;
                color: #f1f5f9;
                border: 1px solid #263552;
                border-radius: 9px;
                min-height: 38px;
                padding: 0 12px;
                selection-background-color: #2563eb;
            }
            QLineEdit:hover, QComboBox:hover, QSpinBox:hover {
                border: 1px solid #36507d;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
                border: 1px solid #3b82f6;
            }
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {
                color: #64748b;
                background: #0d1628;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                /* Qt gives the inner editor 1px width with NoButtons
                   unless the button subcontrols are anchored here */
                subcontrol-origin: border;
                width: 0;
                border: none;
            }
            QComboBox::drop-down {
                border: none;
                width: 28px;
            }
            QComboBox QAbstractItemView {
                background: #0f1930;
                color: #e2e8f0;
                border: 1px solid #263552;
                selection-background-color: #2563eb;
                outline: none;
            }
            QPushButton {
                border: none;
                border-radius: 10px;
                min-height: 44px;
                font-size: 15px;
                font-weight: 800;
                color: white;
            }
            QPushButton#start {
                background: #2563eb;
            }
            QPushButton#start:hover {
                background: #3b74f0;
            }
            QPushButton#start:pressed {
                background: #1d4ed8;
            }
            QPushButton#stop {
                background: #dc2626;
            }
            QPushButton#stop:hover {
                background: #e94343;
            }
            QPushButton#start:disabled, QPushButton#stop:disabled {
                background: #1e293b;
                color: #5b6b86;
            }
            QPushButton#ghost {
                background: transparent;
                color: #8ea0c4;
                border: 1px solid #263552;
                border-radius: 8px;
                min-height: 28px;
                padding: 0 12px;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton#ghost:hover {
                color: #e2e8f0;
                border: 1px solid #3b82f6;
            }
            QLabel#live {
                background: #10223f;
                color: #bfdbfe;
                border: 1px solid #1e3a66;
                border-radius: 9px;
                padding: 8px 12px;
                font-size: 14px;
                font-weight: 700;
            }
            QPlainTextEdit#log {
                background: #070d1a;
                color: #d7f3ff;
                border: 1px solid #1c2842;
                border-radius: 10px;
                padding: 10px;
                font-family: "Cascadia Mono", "Consolas", "Leelawadee UI";
                font-size: 12px;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 4px 2px;
            }
            QScrollBar::handle:vertical {
                background: #2a3a5c;
                border-radius: 3px;
                min-height: 30px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QToolTip {
                background: #0f1930;
                color: #e2e8f0;
                border: 1px solid #263552;
            }
            """
        )

    def start_bot(self):
        if self.bot_thread and self.bot_thread.is_alive():
            return

        event_url = self.event_url.text().strip()

        if not event_url:
            QMessageBox.warning(
                self,
                "ข้อมูลไม่ครบ",
                "กรุณาใส่ Event URL",
            )
            return

        min_price = self.min_price.value()
        max_price = self.max_price.value()

        if (
            min_price
            and max_price
            and min_price > max_price
        ):
            QMessageBox.warning(
                self,
                "ช่วงราคาไม่ถูกต้อง",
                "ราคาต่ำสุดต้องไม่มากกว่าราคาสูงสุด",
            )
            return

        zones = ",".join(
            z.strip()
            for z in self.zone_priority.text().split(",")
            if z.strip()
        )

        kwargs = dict(
            event_url=event_url,
            quantity=self.quantity.value(),
            selection_mode=self.selection_mode.currentText(),
            preferred_zone=zones,
            preferred_price=0,
            min_price=min_price,
            max_price=max_price,
            ticket_type=self.ticket_type.text().strip(),
            show_round=self.show_round.text().strip() or "AUTO",
            prelogin_minutes=60,
            status_callback=self.thread_log,
            progress_callback=self.thread_progress,
            stop_callback=self.should_stop,
        )

        self.stop_requested = False
        self.log_box.clear()
        self.set_progress("")

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._set_running(True)

        self.append_log("=" * 48)
        self.append_log("🎟️ TICKET ASSISTANT v3.1.1 FAST SMART TESTER")
        self.append_log("=" * 48)
        self.append_log(f"Quantity : {self.quantity.value()}")
        self.append_log(
            f"Round    : {self.show_round.text().strip() or 'AUTO'}"
        )

        if zones:
            self.append_log(f"Zones    : {zones}")

        if min_price or max_price:
            self.append_log(
                f"Price    : {min_price or 'ANY'} - "
                f"{max_price or 'ANY'} THB"
            )

        self.bot_thread = threading.Thread(
            target=self._run_bot,
            kwargs={"kwargs": kwargs},
            daemon=True,
        )
        self.bot_thread.start()

    def _run_bot(self, kwargs):
        try:
            self.bot = ThaiTicketMajorBot(**kwargs)
            self.bot.run()
        except Exception:
            self.signals.failed.emit(
                traceback.format_exc()
            )
        finally:
            self.signals.finished.emit()

    def stop_bot(self):
        self.stop_requested = True
        self.append_log("🛑 STOP requested...")

    def should_stop(self):
        return self.stop_requested

    def thread_log(self, message):
        self.signals.log.emit(str(message))

    def thread_progress(self, message):
        self.signals.progress.emit(str(message))

    def set_progress(self, message):
        message = str(message or "").strip()
        self.progress_label.setText(message)
        self.progress_label.setVisible(bool(message))

    def append_log(self, message):
        bar = self.log_box.verticalScrollBar()
        # Follow new lines only if the user is already at the bottom;
        # if they scrolled up to read, keep their position.
        at_bottom = bar.value() >= bar.maximum() - 4

        self.log_box.appendPlainText(str(message))

        if at_bottom:
            bar.setValue(bar.maximum())

    def on_failed(self, error):
        self.append_log(error)

    def on_finished(self):
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._set_running(False)
        self.set_progress("")
        self.bot = None
        self.bot_thread = None

    def closeEvent(self, event):
        self.stop_requested = True
        event.accept()
