import threading
import traceback

from PySide6.QtCore import QObject, Signal, QLocale
from PySide6.QtWidgets import (
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
        self.signals.finished.connect(self.on_finished)
        self.signals.failed.connect(self.on_failed)

        self.setWindowTitle("Ticket Assistant v3.1.1 Fast Smart TESTER")
        self.resize(940, 830)
        self.setMinimumSize(860, 710)

        self._build_ui()
        self._apply_style()
        self._update_mode()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(28, 20, 28, 22)
        root.setSpacing(11)

        title = QLabel("🎟️ Ticket Assistant Hybrid")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "ThaiTicketMajor • v3.1.1 Fast Snapshot • Conditions Fix"
        )
        subtitle.setObjectName("subtitle")
        root.addWidget(subtitle)

        if self.license_result is not None:
            expiry = self.license_result.expire_date or ""
            if expiry:
                try:
                    y, m, d = expiry.split("-")
                    expiry = f"{d}/{m}/{y}"
                except Exception:
                    pass

            license_info = QLabel(
                f"🧪 TEST LICENSE • "
                f"ใช้ได้ถึง {expiry} • "
                f"เหลือ {self.license_result.days_left} วัน"
            )
            license_info.setObjectName("licenseInfo")
            root.addWidget(license_info)

        root.addWidget(QLabel("Event URL"))
        self.event_url = QLineEdit()
        self.event_url.setPlaceholderText(
            "https://www.thaiticketmajor.com/concert/... หรือ /performance/..."
        )
        root.addWidget(self.event_url)

        # Row 1
        grid1 = QGridLayout()
        grid1.setHorizontalSpacing(10)

        grid1.addWidget(QLabel("จำนวนบัตร"), 0, 0)
        grid1.addWidget(QLabel("รอบการแสดง"), 0, 1)

        self.quantity = QSpinBox()
        self.quantity.setLocale(
            QLocale(
                QLocale.Language.English,
                QLocale.Country.UnitedStates,
            )
        )
        self.quantity.setRange(1, 20)
        self.quantity.setValue(1)

        self.show_round = QLineEdit()
        self.show_round.setPlaceholderText(
            "ค่าว่าง = AUTO เช่น 18:00 หรือ rdId"
        )

        grid1.addWidget(self.quantity, 1, 0)
        grid1.addWidget(self.show_round, 1, 1)

        grid1.setColumnStretch(0, 1)
        grid1.setColumnStretch(1, 4)

        root.addLayout(grid1)

        # Row 2
        grid2 = QGridLayout()
        grid2.setHorizontalSpacing(10)

        headers = [
            "รูปแบบการเลือก",
            "Zone Priority",
            "ราคาต่ำสุด",
            "ราคาสูงสุด",
            "Ticket Type",
        ]

        for i, text in enumerate(headers):
            grid2.addWidget(QLabel(text), 0, i)

        self.selection_mode = QComboBox()
        self.selection_mode.addItems(["AUTO", "ZONE", "PRICE"])
        self.selection_mode.currentTextChanged.connect(
            self._update_mode
        )

        self.zone_priority = QLineEdit()
        self.zone_priority.setPlaceholderText(
            "A1,A2,SB,SC,C,D,FF,HH"
        )

        self.min_price = QSpinBox()
        self.min_price.setLocale(
            QLocale(
                QLocale.Language.English,
                QLocale.Country.UnitedStates,
            )
        )
        self.min_price.setRange(0, 100000)
        self.min_price.setSingleStep(100)
        self.min_price.setSpecialValueText("ไม่กำหนด")
        self.min_price.setSuffix(" THB")

        self.max_price = QSpinBox()
        self.max_price.setLocale(
            QLocale(
                QLocale.Language.English,
                QLocale.Country.UnitedStates,
            )
        )
        self.max_price.setRange(0, 100000)
        self.max_price.setSingleStep(100)
        self.max_price.setSpecialValueText("ไม่กำหนด")
        self.max_price.setSuffix(" THB")

        self.ticket_type = QLineEdit()
        self.ticket_type.setPlaceholderText("เช่น VIP Standing")

        grid2.addWidget(self.selection_mode, 1, 0)
        grid2.addWidget(self.zone_priority, 1, 1)
        grid2.addWidget(self.min_price, 1, 2)
        grid2.addWidget(self.max_price, 1, 3)
        grid2.addWidget(self.ticket_type, 1, 4)

        grid2.setColumnStretch(0, 2)
        grid2.setColumnStretch(1, 3)
        grid2.setColumnStretch(2, 2)
        grid2.setColumnStretch(3, 2)
        grid2.setColumnStretch(4, 3)

        root.addLayout(grid2)

        help_label = QLabel(
            "Zone Priority ใช้ , คั่น เช่น A1,A2,A3,SB,SC,C,D,FF,HH • "
            "รองรับ Zone ตัวเดียว / 2 ตัว / ตัวอักษร+ตัวเลข • "
            "ราคา 0 = ไม่จำกัด • รอบว่าง = AUTO • Queue Entry = T-60 นาที"
        )
        help_label.setWordWrap(True)
        help_label.setObjectName("help")
        root.addWidget(help_label)

        # Buttons
        buttons = QHBoxLayout()

        self.start_button = QPushButton("▶ START BOT")
        self.start_button.setObjectName("start")
        self.start_button.clicked.connect(self.start_bot)

        self.stop_button = QPushButton("■ STOP")
        self.stop_button.setObjectName("stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_bot)

        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        root.addLayout(buttons)

        self.state_label = QLabel("● STOPPED")
        self.state_label.setObjectName("state")
        root.addWidget(self.state_label)

        root.addWidget(QLabel("BOT STATUS / ERROR"))

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(330)
        self.log_box.setObjectName("log")
        root.addWidget(self.log_box, 1)

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
            QMainWindow, QWidget {
                background: #0f1a30;
                color: #f8fafc;
                font-family: "Segoe UI";
                font-size: 14px;
            }
            QLabel#title {
                font-size: 27px;
                font-weight: 800;
            }
            QLabel#subtitle, QLabel#help {
                color: #8294b5;
            }
            QLineEdit, QComboBox, QSpinBox {
                background: #020a1c;
                color: white;
                border: 1px solid #30415f;
                border-radius: 9px;
                min-height: 42px;
                padding: 0 10px;
            }
            QComboBox QAbstractItemView {
                background: #0b1428;
                color: white;
                selection-background-color: #2563eb;
            }
            QPushButton {
                border: none;
                border-radius: 9px;
                min-height: 44px;
                font-weight: 800;
            }
            QPushButton#start {
                background: #2563eb;
                color: white;
            }
            QPushButton#stop {
                background: #c62828;
                color: white;
            }
            QPushButton:disabled {
                background: #42516b;
                color: #8a98ad;
            }
            QLabel#state {
                color: #38bdf8;
                font-weight: 800;
            }
            QPlainTextEdit#log {
                background: #02091a;
                color: #e5f9ff;
                border: 1px solid #30415f;
                border-radius: 10px;
                padding: 10px;
                font-family: "Consolas";
                font-size: 12px;
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
            stop_callback=self.should_stop,
        )

        self.stop_requested = False
        self.log_box.clear()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.state_label.setText("● RUNNING")

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

    def append_log(self, message):
        self.log_box.appendPlainText(str(message))
        bar = self.log_box.verticalScrollBar()
        bar.setValue(bar.maximum())

    def on_failed(self, error):
        self.append_log(error)

    def on_finished(self):
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.state_label.setText("● STOPPED")
        self.bot = None
        self.bot_thread = None

    def closeEvent(self, event):
        self.stop_requested = True
        event.accept()
