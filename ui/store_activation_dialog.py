from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QLabel, QPushButton, QVBoxLayout

from auth.store_license import StoreLicenseError, StoreLicenseService
from .pro_messagebox import QMessageBox
from .store_dialogs import ask_switch_to_lifetime


class StoreActivationDialog(QDialog):
    def __init__(
        self,
        *,
        service: StoreLicenseService,
        current_license: dict[str, Any] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self.license_payload = dict(current_license) if isinstance(current_license, dict) else None

        self.setWindowTitle("Auto-Cutter Entitlement")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, False)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setFixedSize(560, 360)

        self._build_ui()
        self._apply_styles()
        self._refresh_ui_from_license()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)

        card = QFrame()
        card.setObjectName("Card")
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(22, 22, 22, 22)
        card_l.setSpacing(10)

        title = QLabel("Microsoft Store Access Required")
        title.setObjectName("Title")

        self.subtitle = QLabel("")
        self.subtitle.setObjectName("Subtitle")
        self.subtitle.setWordWrap(True)

        self.btn_monthly = QPushButton("Start Monthly Plan")
        self.btn_monthly.setObjectName("Primary")
        self.btn_monthly.clicked.connect(self._buy_monthly)

        self.btn_lifetime = QPushButton("Unlock Lifetime")
        self.btn_lifetime.setObjectName("Primary")
        self.btn_lifetime.clicked.connect(self._buy_lifetime)

        self.btn_restore = QPushButton("Refresh Entitlement")
        self.btn_restore.setObjectName("Ghost")
        self.btn_restore.clicked.connect(self._restore)

        self.btn_store = QPushButton("Open Microsoft Store Billing")
        self.btn_store.setObjectName("Ghost")
        self.btn_store.clicked.connect(self._open_store)

        self.btn_exit = QPushButton("Exit")
        self.btn_exit.setObjectName("Danger")
        self.btn_exit.clicked.connect(self.reject)

        self.status = QLabel("")
        self.status.setObjectName("Status")
        self.status.setWordWrap(True)

        card_l.addWidget(title)
        card_l.addWidget(self.subtitle)
        card_l.addSpacing(8)
        card_l.addWidget(self.btn_monthly)
        card_l.addWidget(self.btn_lifetime)
        card_l.addWidget(self.btn_restore)
        card_l.addWidget(self.btn_store)
        card_l.addStretch(1)
        card_l.addWidget(self.status)
        card_l.addWidget(self.btn_exit, alignment=Qt.AlignRight)
        root.addWidget(card)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background: #0c0f16;
                color: #e6ebff;
                font-family: "Segoe UI Variable Text", "Segoe UI", "Bahnschrift", "Arial";
                font-size: 13px;
            }
            QFrame#Card {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #151a28, stop:1 #121623);
                border: 1px solid #2a3248;
                border-radius: 14px;
            }
            QLabel#Title {
                font-size: 24px;
                font-weight: 700;
                color: #f4f6ff;
            }
            QLabel#Subtitle {
                color: #b7c0dc;
                font-size: 13px;
            }
            QLabel#Status {
                color: #cf95ff;
                min-height: 34px;
            }
            QPushButton {
                border-radius: 8px;
                padding: 10px 14px;
                font-weight: 600;
            }
            QPushButton#Primary {
                background: #6f4eff;
                border: 1px solid #6f4eff;
                color: white;
            }
            QPushButton#Primary:hover {
                background: #8b70ff;
                border-color: #8b70ff;
            }
            QPushButton#Ghost {
                background: #171d2d;
                border: 1px solid #2f3b5a;
                color: #dbe3ff;
            }
            QPushButton#Ghost:hover {
                background: #242b42;
                border-color: #8166ff;
            }
            QPushButton#Danger {
                background: #2b1620;
                border: 1px solid #6c2f4a;
                color: #ffd6e2;
            }
            QPushButton#Danger:hover {
                background: #3a1d2a;
            }
            """
        )

    def _refresh_ui_from_license(self) -> None:
        payload = self.license_payload
        plan = str(payload.get("plan", "") if isinstance(payload, dict) else "").strip().lower()
        status = str(payload.get("status", "") if isinstance(payload, dict) else "").strip().lower()
        ends = payload.get("ends_at") if isinstance(payload, dict) else None

        if plan == "trial" and status == "active":
            msg = "Your free trial is active."
            if isinstance(ends, str) and ends:
                msg = f"Your free trial is active until {ends}."
            self.subtitle.setText(
                f"{msg}\nYou can continue using the app or unlock a paid plan now."
            )
            self.status.setText("You can choose a plan anytime from this dialog or the top-right account menu.")
        elif plan == "lifetime" and status == "active":
            self.subtitle.setText("Lifetime access is active.\nNo additional purchase is required.")
            self.status.setText("Your app is permanently unlocked on this Microsoft account.")
        else:
            self.subtitle.setText(
                "Your free trial has ended.\nChoose a paid plan to continue using Auto-Cutter."
            )
            self.status.setText("Monthly and Lifetime purchases are managed by Microsoft Store entitlement.")

        self.btn_monthly.setText(f"Buy Monthly ({self._service.monthly_offer_label})")
        self.btn_lifetime.setText(f"Buy Lifetime ({self._service.lifetime_offer_label})")
        can_monthly = bool(self._service.purchase_monthly_enabled)
        can_lifetime = bool(self._service.purchase_lifetime_enabled)
        if plan == "lifetime" and status == "active":
            can_monthly = False
            can_lifetime = False
        elif plan == "monthly" and status == "active":
            can_monthly = False
            if can_lifetime:
                self.btn_lifetime.setText(
                    f"Switch to Lifetime ({self._service.lifetime_offer_label}) - cancel monthly first"
                )
        self.btn_monthly.setEnabled(can_monthly)
        self.btn_lifetime.setEnabled(can_lifetime)

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _accept_if_unlocked(self) -> bool:
        if self._service.is_license_active(self.license_payload):
            self.accept()
            return True
        return False

    def _restore(self) -> None:
        self._set_status("Refreshing Microsoft Store entitlement...")
        try:
            self.license_payload = self._service.restore_purchases()
        except StoreLicenseError as err:
            self._set_status(err.detail)
            return
        self._refresh_ui_from_license()
        if not self._accept_if_unlocked():
            self._set_status("No active paid entitlement found yet.")

    def _open_store(self) -> None:
        self._service.open_store_subscription_page()
        self._set_status("Microsoft Store billing opened. Complete purchase, then click Refresh Entitlement.")

    def _buy_monthly(self) -> None:
        try:
            result = self._service.purchase_monthly()
        except StoreLicenseError as err:
            QMessageBox.warning(self, "Monthly purchase", err.detail)
            return
        self.license_payload = result.license_payload if isinstance(result.license_payload, dict) else self.license_payload
        self._refresh_ui_from_license()
        if result.success and self._accept_if_unlocked():
            return
        QMessageBox.information(self, "Monthly purchase", result.detail or "Purchase did not complete.")

    def _buy_lifetime(self) -> None:
        payload = self.license_payload if isinstance(self.license_payload, dict) else {}
        plan = str(payload.get("plan", "") or "").strip().lower()
        status = str(payload.get("status", "") or "").strip().lower()
        if plan == "monthly" and status == "active":
            choice = ask_switch_to_lifetime(self)
            if choice == "store":
                self._open_store()
                return
            if choice != "continue":
                return

        try:
            result = self._service.purchase_lifetime()
        except StoreLicenseError as err:
            QMessageBox.warning(self, "Lifetime purchase", err.detail)
            return
        self.license_payload = result.license_payload if isinstance(result.license_payload, dict) else self.license_payload
        self._refresh_ui_from_license()
        if result.success and self._accept_if_unlocked():
            return
        QMessageBox.information(self, "Lifetime purchase", result.detail or "Purchase did not complete.")
