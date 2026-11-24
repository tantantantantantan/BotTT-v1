import hashlib
import os
import platform
import subprocess
import winreg


def _machine_guid():
    try:
        access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            access,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except Exception:
        return ""


def _system_uuid():
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_ComputerSystemProduct).UUID",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return output.strip()
    except Exception:
        return ""


def get_machine_id():
    raw = "|".join(
        [
            _machine_guid(),
            _system_uuid(),
            platform.node(),
        ]
    ).strip("|")

    if not raw:
        raw = platform.platform()

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()

    # Example: 8A21-F392-17BC-CC11
    return "-".join(
        digest[i:i + 4]
        for i in range(0, 16, 4)
    )
