import sys

from PySide6.QtCore import QSettings
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog

from auth.store_license import StoreLicenseError, StoreLicenseService
from auth.store_license_monitor import StoreLicenseMonitor
from ui.main_window import MainWindow
from ui.pro_messagebox import QMessageBox
from ui.store_activation_dialog import StoreActivationDialog
from utils.runtime_paths import resource_path


def _resolve_store_session(service: StoreLicenseService) -> tuple[dict | None, dict | None]:
    try:
        license_payload = service.refresh_license(force=True)
    except StoreLicenseError as err:
        QMessageBox.critical(
            None,
            "License check failed",
            err.detail,
        )
        return None, None

    if service.is_license_active(license_payload):
        return dict(service.user), license_payload

    gate = StoreActivationDialog(service=service, current_license=license_payload)
    if gate.exec() != QDialog.Accepted:
        return None, None
    license_payload = gate.license_payload if isinstance(gate.license_payload, dict) else None
    if not isinstance(license_payload, dict):
        try:
            license_payload = service.refresh_license(force=True)
        except StoreLicenseError:
            return None, None
    if not service.is_license_active(license_payload):
        return None, None
    return dict(service.user), license_payload


def main() -> int:
    app = QApplication([])
    try:
        # Force a consistent cross-PC widget style (do not depend on OS theme/style).
        app.setStyle("Fusion")
    except Exception:
        pass
    # Ensure Qt standard data paths (QStandardPaths.AppDataLocation) use the
    # app name instead of the Python interpreter name ("python").
    app.setApplicationName("Auto Cutter")
    logo = resource_path("icons", "logo", "logo.png")
    if logo.exists():
        app.setWindowIcon(QIcon(str(logo)))

    try:
        service = StoreLicenseService()
        user_payload, license_payload = _resolve_store_session(service)
    except Exception as err:
        QMessageBox.critical(None, "Startup failed", str(err))
        return 1
    if not isinstance(user_payload, dict) or not isinstance(license_payload, dict):
        # Avoid the perception of a random crash/close when entitlement is missing.
        QMessageBox.information(
            None,
            "Session not started",
            "No active Microsoft Store session/license found.\nThe app will close.",
        )
        return 0

    w = MainWindow()
    w.set_auth_context(
        api=service,
        user=user_payload,
        license_payload=license_payload,
    )
    identity_label = user_payload.get("display_name") or user_payload.get("email")
    if isinstance(identity_label, str) and identity_label:
        w.setWindowTitle(f"Auto-Cutter - {identity_label}")

    monitor = StoreLicenseMonitor(
        service=service,
        initial_license=license_payload,
        parent_window=w,
        on_license_updated=w._on_license_updated,
    )
    monitor.start()
    w._license_monitor = monitor

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
