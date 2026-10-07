import sys

def main() -> int:
    args = sys.argv[1:]
    if args and "--smoke-test" not in args:
        from automation.cli import run_cli

        return run_cli(args)
    return run_gui()


def run_gui() -> int:
    from utils.console import detach_gui_console

    detach_gui_console()
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from utils.app_version import app_version
    from utils.crash_handler import install_global_exception_handler
    from utils.runtime_paths import initialize_qt_settings, resource_path

    install_global_exception_handler()
    initialize_qt_settings()
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
