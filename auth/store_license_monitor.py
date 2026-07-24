from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from .store_license import StoreLicenseError, StoreLicenseService
from ui.pro_messagebox import QMessageBox


class StoreLicenseMonitor(QObject):
    def __init__(
        self,
        *,
        service: StoreLicenseService,
        initial_license: dict[str, Any] | None,
        parent_window: QWidget,
        on_license_updated: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(parent_window)
        self.service = service
        self.parent_window = parent_window
        self._on_license_updated = on_license_updated
        self.current_license = dict(initial_license) if isinstance(initial_license, dict) else service.license_cache

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run_check)

    def start(self) -> None:
        self._schedule_next()

    def set_current_license(self, license_payload: dict[str, Any] | None, *, checked_now: bool = False) -> None:
        if isinstance(license_payload, dict):
            self.current_license = dict(license_payload)
            self.service.set_license_cache(self.current_license, checked_now=checked_now)
            if self._on_license_updated:
                try:
                    self._on_license_updated(self.current_license)
                except Exception:
                    pass
        else:
            self.current_license = None
        self._schedule_next()

    def _schedule_next(self) -> None:
        interval_seconds = self.service.compute_license_check_interval_seconds(self.current_license)
        interval_ms = max(60, int(interval_seconds)) * 1000
        self._timer.start(interval_ms)

    def _close_for_license(self, message: str) -> None:
        QMessageBox.warning(
            self.parent_window,
            "License Required",
            message,
        )
        QApplication.quit()

    def _run_check(self) -> None:
        try:
            payload = self.service.refresh_license(force=True)
            self.current_license = dict(payload)
            if not self.service.is_license_active(payload):
                self._close_for_license(
                    "Your trial/subscription is no longer active. Renew from Microsoft Store to continue.",
                )
                return
            if self._on_license_updated:
                try:
                    self._on_license_updated(payload)
                except Exception:
                    pass
        except StoreLicenseError as err:
            if err.status_code == 0:
                # Temporary network/store issue: keep cached local entitlement.
                pass
            elif self.current_license and self.service.is_license_active(self.current_license):
                # Keep editing session if we still have a locally valid entitlement.
                pass
            else:
                self._close_for_license(
                    "Unable to validate Microsoft Store entitlement. Please reopen the app online.",
                )
                return

        self._schedule_next()
