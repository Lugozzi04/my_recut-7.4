from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from .api_client import ApiError, AuthApiClient
from ui.pro_messagebox import QMessageBox


class LicenseMonitor(QObject):
    def __init__(
        self,
        *,
        api: AuthApiClient,
        initial_license: dict[str, Any] | None,
        parent_window: QWidget,
        on_license_updated: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(parent_window)
        self.api = api
        self.parent_window = parent_window
        self._on_license_updated = on_license_updated
        seed_license = initial_license if isinstance(initial_license, dict) else api.license_cache
        self.current_license = dict(seed_license) if isinstance(seed_license, dict) else None
        if self.current_license:
            # Keep the persisted "last online check" timestamp untouched on startup.
            self.api.set_license_cache(self.current_license, checked_now=False)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._run_check)

    def start(self) -> None:
        self._schedule_next()

    def set_current_license(self, license_payload: dict[str, Any] | None, *, checked_now: bool = False) -> None:
        if isinstance(license_payload, dict):
            self.current_license = dict(license_payload)
            self.api.set_license_cache(self.current_license, checked_now=checked_now)
            if self._on_license_updated:
                try:
                    self._on_license_updated(self.current_license)
                except Exception:
                    pass
        else:
            self.current_license = None
        self._schedule_next()

    def _schedule_next(self) -> None:
        interval_seconds = self.api.compute_license_check_interval_seconds(self.current_license)
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
            license_payload = self.api.ensure_active_license_cached(
                force=True,
                allow_start_trial=False,
            )
            self.current_license = license_payload
            if self._on_license_updated:
                try:
                    self._on_license_updated(license_payload)
                except Exception:
                    pass
        except ApiError as err:
            # Keep app usable offline, but close immediately on explicit invalid license/session.
            if err.status_code in {401, 403, 404}:
                self._close_for_license(
                    "Your license is no longer active. Please login again.",
                )
                return

            if err.status_code == 0:
                # Network issue: stay on cached license and retry on the same cadence.
                pass
            else:
                # Unknown server error: do not interrupt editing session.
                pass

        self._schedule_next()
