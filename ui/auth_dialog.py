from __future__ import annotations

import time
import webbrowser
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QFrame, QLabel, QPushButton, QVBoxLayout

from auth.api_client import ApiError, AuthApiClient


class AuthDialog(QDialog):
    def __init__(self, api: AuthApiClient, parent=None):
        super().__init__(parent)
        self.api = api
        self.user: dict[str, Any] | None = None
        self.license: dict[str, Any] | None = None

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_desktop_auth)
        self._pending_request_id: str | None = None
        self._pending_device_secret: str | None = None
        self._pending_login_url: str | None = None
        self._pending_browser_url: str | None = None
        self._poll_deadline_ts: float = 0.0
        self._is_busy: bool = False

        self.setWindowTitle("Auto-Cutter Login")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, False)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setFixedSize(560, 380)

        self._build_ui()
        self._apply_styles()
        QTimer.singleShot(0, self._try_auto_login)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)

        card = QFrame()
        card.setObjectName("AuthCard")
        card_l = QVBoxLayout(card)
        card_l.setContentsMargins(22, 22, 22, 22)
        card_l.setSpacing(10)

        title = QLabel("Auto-Cutter")
        title.setObjectName("Title")
        subtitle = QLabel(
            "Continue securely in your browser. "
            "After login or signup, this app unlocks automatically."
        )
        subtitle.setObjectName("Subtitle")
        subtitle.setWordWrap(True)

        self.btn_continue = QPushButton("Continue with Auto-Cutter Account")
        self.btn_continue.setObjectName("Primary")
        self.btn_continue.clicked.connect(self._on_continue_clicked)

        self.btn_signup = QPushButton("Don't have an account? Sign up first")
        self.btn_signup.setObjectName("Link")
        self.btn_signup.clicked.connect(self._on_signup_clicked)

        self.btn_cancel = QPushButton("Cancel current login request")
        self.btn_cancel.setObjectName("Ghost")
        self.btn_cancel.clicked.connect(self._on_cancel_flow_clicked)
        self.btn_cancel.setVisible(False)

        self.btn_reopen = QPushButton("I closed browser, reopen login page")
        self.btn_reopen.setObjectName("Ghost")
        self.btn_reopen.clicked.connect(self._on_reopen_browser_clicked)
        self.btn_reopen.setVisible(False)

        self.status = QLabel("Checking saved session...")
        self.status.setObjectName("Status")
        self.status.setWordWrap(True)

        card_l.addWidget(title)
        card_l.addWidget(subtitle)
        card_l.addSpacing(8)
        card_l.addWidget(self.btn_continue)
        card_l.addWidget(self.btn_signup, alignment=Qt.AlignLeft)
        card_l.addWidget(self.btn_reopen, alignment=Qt.AlignLeft)
        card_l.addWidget(self.btn_cancel, alignment=Qt.AlignLeft)
        card_l.addStretch(1)
        card_l.addWidget(self.status)
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
            QFrame#AuthCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #151a28, stop:1 #121623);
                border: 1px solid #2a3248;
                border-radius: 14px;
            }
            QLabel#Title {
                font-size: 28px;
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
                padding: 11px 14px;
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
            QPushButton#Primary:pressed {
                background: #5f3fff;
                border-color: #5f3fff;
            }
            QPushButton#Primary:disabled {
                background: #343a4d;
                border-color: #343a4d;
                color: #aeb6d1;
            }
            QPushButton#Link {
                background: transparent;
                border: none;
                padding: 0;
                color: #b59bff;
                text-decoration: underline;
                font-weight: 600;
            }
            QPushButton#Link:hover {
                color: #d0c2ff;
            }
            QPushButton#Ghost {
                background: #171d2d;
                border: 1px solid #2f3b5a;
                color: #dbe3ff;
                padding: 8px 12px;
            }
            QPushButton#Ghost:hover {
                background: #242b42;
                border-color: #8166ff;
            }
            """
        )

    def _set_status(self, message: str, ok: bool = False) -> None:
        color = "#9df5b1" if ok else "#cf95ff"
        self.status.setStyleSheet(f"color: {color}; min-height: 34px;")
        self.status.setText(message)

    def _sync_action_buttons(self) -> None:
        has_pending = bool(self._pending_request_id and self._pending_device_secret)
        if self._is_busy:
            self.btn_continue.setEnabled(False)
            self.btn_signup.setEnabled(False)
            self.btn_cancel.setEnabled(False)
            self.btn_reopen.setEnabled(False)
            return

        if has_pending:
            self.btn_continue.setEnabled(False)
            self.btn_signup.setEnabled(False)
            self.btn_cancel.setEnabled(True)
            self.btn_reopen.setEnabled(bool(self._pending_browser_url))
            return

        self.btn_continue.setEnabled(True)
        self.btn_signup.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.btn_reopen.setEnabled(False)

    def _set_busy(self, busy: bool) -> None:
        self._is_busy = busy
        self.setCursor(Qt.WaitCursor if busy else Qt.ArrowCursor)
        self._sync_action_buttons()

    def _reset_pending_flow(self) -> None:
        self._poll_timer.stop()
        self._pending_request_id = None
        self._pending_device_secret = None
        self._pending_login_url = None
        self._pending_browser_url = None
        self._poll_deadline_ts = 0.0
        self.btn_cancel.setVisible(False)
        self.btn_cancel.setEnabled(False)
        self.btn_reopen.setVisible(False)
        self.btn_reopen.setEnabled(False)
        self._sync_action_buttons()

    def _open_url(self, url: str) -> None:
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass

    def _derive_signup_url(self, login_url: str) -> str:
        if "/auth/login/" in login_url:
            return login_url.replace("/auth/login/", "/auth/signup/")
        return "https://auto-cutter.com/en/auth/signup/"

    def _try_auto_login(self) -> None:
        self._set_busy(True)
        self._set_status("Checking existing desktop session...")
        try:
            token_payload = self.api.try_restore_session()
            self._finalize_authenticated(token_payload)
        except ApiError:
            self._reset_pending_flow()
            self._set_status("No active session. Continue in browser to login or sign up.")
        finally:
            self._set_busy(False)

    def _start_browser_flow(self, *, open_signup: bool) -> None:
        self._reset_pending_flow()
        self._set_busy(True)
        self._set_status("Creating secure browser login request...")
        try:
            payload = self.api.desktop_start()
        except ApiError as err:
            self._set_status(err.detail)
            self._set_busy(False)
            return

        request_id = payload.get("request_id")
        device_secret = payload.get("device_secret")
        verification_url = payload.get("verification_url")
        expires_in = int(payload.get("expires_in") or 0)
        interval = int(payload.get("poll_interval_seconds") or 2)
        if not isinstance(request_id, str) or not isinstance(device_secret, str) or not isinstance(verification_url, str):
            self._set_status("Unexpected desktop auth response from server.")
            self._set_busy(False)
            return

        self._pending_request_id = request_id
        self._pending_device_secret = device_secret
        self._pending_login_url = verification_url
        self._poll_deadline_ts = time.monotonic() + max(30, expires_in)
        self.btn_cancel.setVisible(True)
        self.btn_cancel.setEnabled(True)
        self.btn_reopen.setVisible(True)
        self.btn_reopen.setEnabled(True)

        browser_url = verification_url
        if open_signup:
            browser_url = self._derive_signup_url(verification_url)
            self._open_url(browser_url)
            self._set_status("Browser opened on signup. If you closed it, use the reopen button.")
        else:
            self._open_url(browser_url)
            self._set_status("Browser opened. If you closed it, use the reopen button.")
        self._pending_browser_url = browser_url

        self._set_busy(False)
        self._poll_timer.start(max(1000, interval * 1000))

    def _on_continue_clicked(self) -> None:
        self._start_browser_flow(open_signup=False)

    def _on_signup_clicked(self) -> None:
        self._start_browser_flow(open_signup=True)

    def _on_cancel_flow_clicked(self) -> None:
        self._reset_pending_flow()
        self._set_busy(False)
        self._set_status("Login request cancelled. Click Continue to start again.")

    def _on_reopen_browser_clicked(self) -> None:
        if not self._pending_browser_url:
            self._set_status("No browser URL available. Start a new login request.")
            return
        self._open_url(self._pending_browser_url)
        self._set_status("Browser reopened. Complete authentication there, then return to this app.")

    def _poll_desktop_auth(self) -> None:
        if not self._pending_request_id or not self._pending_device_secret:
            self._poll_timer.stop()
            self._set_busy(False)
            return

        if time.monotonic() >= self._poll_deadline_ts:
            self._reset_pending_flow()
            self._set_busy(False)
            self._set_status("Desktop login request expired. Click Continue to start again.")
            return

        try:
            payload = self.api.desktop_poll(
                request_id=self._pending_request_id,
                device_secret=self._pending_device_secret,
            )
        except ApiError as err:
            if err.status_code == 0:
                self._set_status("Temporary network issue. You can reopen browser or wait.")
                return
            self._reset_pending_flow()
            self._set_busy(False)
            self._set_status(err.detail)
            return

        state = str(payload.get("status") or "").lower()
        if state == "pending":
            self._set_status("Waiting for browser authentication... (reopen browser only if you closed it)")
            return
        if state == "authorized":
            self._reset_pending_flow()
            self._finalize_authenticated(payload)
            return
        if state == "expired":
            self._reset_pending_flow()
            self._set_busy(False)
            self._set_status("Desktop login expired. Click Continue to start again.")
            return
        if state == "canceled":
            self._reset_pending_flow()
            self._set_busy(False)
            self._set_status("Desktop login canceled. Start again from this window.")
            return
        if state == "consumed":
            self._reset_pending_flow()
            self._set_busy(False)
            self._set_status("Desktop login already consumed. Start a new login request.")
            return

        self._set_status("Waiting for browser authentication... (reopen browser only if you closed it)")

    def _finalize_authenticated(self, token_payload: dict[str, Any]) -> None:
        self._reset_pending_flow()
        self._set_status("Checking active license...")
        try:
            license_payload = self.api.ensure_active_license_cached(
                force=False,
                allow_start_trial=True,
            )
        except ApiError as err:
            self._set_busy(False)
            self._set_status(err.detail)
            return

        self.user = token_payload.get("user") if isinstance(token_payload.get("user"), dict) else self.api.user
        self.license = license_payload
        plan = str(license_payload.get("plan", "active"))
        self._set_status(f"Authenticated. Active plan: {plan}.", ok=True)
        self.accept()
