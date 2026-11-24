import sys

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QApplication, QMessageBox

from license.license_manager import LicenseManager
from ui.main_window import MainWindow


def main():
    QLocale.setDefault(
        QLocale(
            QLocale.Language.English,
            QLocale.Country.UnitedStates,
        )
    )

    app = QApplication(sys.argv)

    result = LicenseManager.validate()

    if not result.valid:
        QMessageBox.critical(
            None,
            "License Error",
            result.message
            + "\n\nMachine ID:\n"
            + result.machine_id,
        )
        return 1

    window = MainWindow(result)
    window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
