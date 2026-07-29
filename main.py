import sys

from PySide6.QtCore import QSettings
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from utils.app_version import app_version
from utils.crash_handler import install_global_exception_handler
from utils.runtime_paths import resource_path


def main() -> int:
    install_global_exception_handler()
    app = QApplication(sys.argv)
    # Import the heavy Qt/WebEngine UI only after crash logging is active.
    from ui.main_window import MainWindow

    try:
        # Force a consistent cross-PC widget style (do not depend on OS theme/style).
        app.setStyle("Fusion")
    except Exception:
        pass
    # Ensure Qt standard data paths (QStandardPaths.AppDataLocation) use the
    # app name instead of the Python interpreter name ("python").
    app.setApplicationName("Auto Cutter")
    app.setApplicationVersion(app_version())
    app.setOrganizationName("Auto Cutter")
    logo = resource_path("icons", "logo", "logo.png")
    if logo.exists():
        app.setWindowIcon(QIcon(str(logo)))

    w = MainWindow()
    if "--smoke-test" in sys.argv:
        w.close()
        return 0

    # Respect user's last window mode when available.
    try:
        s = QSettings("Auto Cutter", "Auto Cutter")
        start_maximized = bool(int(s.value("window_maximized", 1) or 1))
    except Exception:
        start_maximized = True
    if start_maximized:
        w.showMaximized()
    else:
        w.show()
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
