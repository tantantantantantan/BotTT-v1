import base64
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .machine_id import get_machine_id
from .public_key import PUBLIC_KEY_B64


@dataclass
class LicenseResult:
    valid: bool
    message: str
    customer: str = ""
    machine_id: str = ""
    expire_date: str = ""
    days_left: int = 0


class LicenseManager:
    PRODUCT = "TicketAssistantHybrid"

    @staticmethod
    def app_dir():
        # PyInstaller one-file: license.json should sit beside the EXE.
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent.parent

    @staticmethod
    def license_path():
        return LicenseManager.app_dir() / "license.json"

    @staticmethod
    def state_path():
        base = Path(
            os.environ.get(
                "LOCALAPPDATA",
                str(Path.home()),
            )
        )

        folder = base / "TicketAssistantHybrid"
        folder.mkdir(parents=True, exist_ok=True)

        return folder / ".license_state.json"

    @staticmethod
    def _canonical_payload(payload):
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _verify_signature(payload, signature_b64):
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(PUBLIC_KEY_B64)
        )

        public_key.verify(
            base64.b64decode(signature_b64),
            LicenseManager._canonical_payload(payload),
        )

    @staticmethod
    def _read_last_run():
        path = LicenseManager.state_path()

        if not path.exists():
            return None

        try:
            data = json.loads(
                path.read_text(encoding="utf-8")
            )
            value = data.get("last_run_date")

            if value:
                return date.fromisoformat(value)
        except Exception:
            pass

        return None

    @staticmethod
    def _write_last_run(today):
        try:
            LicenseManager.state_path().write_text(
                json.dumps(
                    {
                        "last_run_date": today.isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            # State protection is best-effort.
            pass

    @staticmethod
    def validate():
        machine = get_machine_id()
        license_path = LicenseManager.license_path()

        if not license_path.exists():
            return LicenseResult(
                False,
                "ไม่พบ license.json",
                machine_id=machine,
            )

        try:
            document = json.loads(
                license_path.read_text(
                    encoding="utf-8"
                )
            )

            payload = document["payload"]
            signature = document["signature"]

        except Exception:
            return LicenseResult(
                False,
                "รูปแบบ License ไม่ถูกต้อง",
                machine_id=machine,
            )

        try:
            LicenseManager._verify_signature(
                payload,
                signature,
            )
        except Exception:
            return LicenseResult(
                False,
                "License ถูกแก้ไขหรือ Signature ไม่ถูกต้อง",
                machine_id=machine,
            )

        if payload.get("product") != LicenseManager.PRODUCT:
            return LicenseResult(
                False,
                "License ไม่ตรงกับโปรแกรมนี้",
                machine_id=machine,
            )

        licensed_machine = str(
            payload.get("machine_id", "")
        ).strip()

        if (
            licensed_machine
            and licensed_machine != "*"
            and licensed_machine.upper() != machine.upper()
        ):
            return LicenseResult(
                False,
                "License นี้ใช้กับเครื่องอื่น",
                customer=str(payload.get("customer", "")),
                machine_id=machine,
                expire_date=str(payload.get("expire_date", "")),
            )

        try:
            expiry = date.fromisoformat(
                str(payload["expire_date"])
            )
        except Exception:
            return LicenseResult(
                False,
                "Expire Date ใน License ไม่ถูกต้อง",
                customer=str(payload.get("customer", "")),
                machine_id=machine,
            )

        today = date.today()

        # Offline rollback detection:
        # If the machine date moves backwards after successful use, block.
        last_run = LicenseManager._read_last_run()

        if last_run and today < last_run:
            return LicenseResult(
                False,
                "ตรวจพบวันที่เครื่องถูกย้อนกลับ (Clock Rollback)",
                customer=str(payload.get("customer", "")),
                machine_id=machine,
                expire_date=expiry.isoformat(),
            )

        # Valid through the expire date, expires the following day.
        if today > expiry:
            return LicenseResult(
                False,
                f"License หมดอายุแล้ว ({expiry.strftime('%d/%m/%Y')})",
                customer=str(payload.get("customer", "")),
                machine_id=machine,
                expire_date=expiry.isoformat(),
                days_left=0,
            )

        LicenseManager._write_last_run(today)

        days_left = (expiry - today).days

        return LicenseResult(
            True,
            "License Active",
            customer=str(payload.get("customer", "")),
            machine_id=machine,
            expire_date=expiry.isoformat(),
            days_left=days_left,
        )
