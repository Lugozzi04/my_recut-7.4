from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

from .session_store import clear_session, load_or_create_device_id, load_session, save_desktop_session, update_session


DEFAULT_API_BASE = "https://auto-cutter.com/api"
CHECK_INTERVAL_DEFAULT_SECONDS = 24 * 60 * 60
CHECK_INTERVAL_NEAR_EXPIRY_SECONDS = 60 * 60
NEAR_EXPIRY_WINDOW_SECONDS = 24 * 60 * 60


@dataclass
class ApiError(Exception):
    status_code: int
    detail: str

    def __str__(self) -> str:
        return self.detail


class AuthApiClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.getenv("AUTOCUTTER_API_BASE") or DEFAULT_API_BASE).rstrip("/")
        saved = load_session()
        self.access_token = str(saved.get("access_token") or "") or None
        self.refresh_token = str(saved.get("refresh_token") or "") or None
        raw_access_exp = saved.get("access_token_expires_at")
        try:
            self.access_token_expires_at = float(raw_access_exp) if raw_access_exp is not None else None
        except (TypeError, ValueError):
            self.access_token_expires_at = None
        saved_user = saved.get("user")
        self.user: dict[str, Any] | None = saved_user if isinstance(saved_user, dict) else None
        saved_license = saved.get("license")
        self.license_cache: dict[str, Any] | None = saved_license if isinstance(saved_license, dict) else None
        raw_checked_at = saved.get("license_checked_at")
        try:
            self.license_checked_at = float(raw_checked_at) if raw_checked_at is not None else None
        except (TypeError, ValueError):
            self.license_checked_at = None
        try:
            self.device_fingerprint = f"desktop:{load_or_create_device_id()}"
        except Exception:
            self.device_fingerprint = None

    def _parse_http_error(self, status_code: int, raw_body: str, reason: Any) -> ApiError:
        detail = str(reason) if reason else "Request failed"
        try:
            payload = json.loads(raw_body)
            parsed = payload.get("detail", detail)
            if isinstance(parsed, list):
                parsed = ", ".join(str(item) for item in parsed)
            detail = str(parsed)
        except Exception:
            if raw_body.strip():
                detail = raw_body.strip()
        return ApiError(status_code=status_code, detail=detail)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        with_auth: bool = False,
        timeout: int = 15,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        if self.device_fingerprint:
            headers["X-Device-Fingerprint"] = str(self.device_fingerprint)
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if with_auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        req = request.Request(url=url, data=body, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                decoded = json.loads(raw)
                return decoded if isinstance(decoded, dict) else {}
        except error.HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            raise self._parse_http_error(exc.code, raw_body, exc.reason) from None
        except error.URLError as exc:
            raise ApiError(status_code=0, detail=f"Network error: {exc.reason}") from None

    def _store_desktop_tokens(self, payload: dict[str, Any]) -> dict[str, Any]:
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        user = payload.get("user")
        expires_in_raw = payload.get("expires_in")
        if not isinstance(access_token, str) or not access_token:
            raise ApiError(status_code=500, detail="Missing access token in API response")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ApiError(status_code=500, detail="Missing refresh token in API response")
        if not isinstance(user, dict):
            raise ApiError(status_code=500, detail="Missing user payload in API response")

        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user = user
        expires_in = int(expires_in_raw) if isinstance(expires_in_raw, int) else 900
        self.access_token_expires_at = max(time.time() + expires_in - 20, time.time() + 30)
        save_desktop_session(access_token=access_token, refresh_token=refresh_token, user=user)
        update_session(access_token_expires_at=self.access_token_expires_at)
        return payload

    @staticmethod
    def _parse_iso_utc(value: Any) -> datetime | None:
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _license_is_active_locally(license_payload: dict[str, Any]) -> bool:
        status_value = str(license_payload.get("status", "")).strip().lower()
        if status_value != "active":
            return False
        ends_at = AuthApiClient._parse_iso_utc(license_payload.get("ends_at"))
        if ends_at and datetime.now(timezone.utc) >= ends_at:
            return False
        return True

    @staticmethod
    def _license_plan(license_payload: dict[str, Any]) -> str:
        return str(license_payload.get("plan", "")).strip().lower()

    def compute_license_check_interval_seconds(self, license_payload: dict[str, Any] | None = None) -> int:
        payload = license_payload if isinstance(license_payload, dict) else self.license_cache
        if not isinstance(payload, dict):
            return CHECK_INTERVAL_DEFAULT_SECONDS

        plan = self._license_plan(payload)
        if plan not in {"trial", "monthly"}:
            return CHECK_INTERVAL_DEFAULT_SECONDS

        ends_at = self._parse_iso_utc(payload.get("ends_at"))
        if not ends_at:
            return CHECK_INTERVAL_DEFAULT_SECONDS

        remaining_seconds = (ends_at - datetime.now(timezone.utc)).total_seconds()
        if remaining_seconds <= NEAR_EXPIRY_WINDOW_SECONDS:
            return CHECK_INTERVAL_NEAR_EXPIRY_SECONDS
        return CHECK_INTERVAL_DEFAULT_SECONDS

    def should_revalidate_license_now(self, license_payload: dict[str, Any] | None = None) -> bool:
        if self.license_checked_at is None:
            return True
        elapsed = time.time() - self.license_checked_at
        interval = self.compute_license_check_interval_seconds(license_payload)
        return elapsed >= interval

    def _persist_license_cache(self) -> None:
        update_session(
            license=self.license_cache or {},
            license_checked_at=self.license_checked_at,
        )

    def set_license_cache(self, license_payload: dict[str, Any], *, checked_now: bool = False) -> None:
        self.license_cache = dict(license_payload)
        if checked_now:
            self.license_checked_at = time.time()
        self._persist_license_cache()

    def _validate_active_license_payload(self, license_payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(license_payload, dict):
            raise ApiError(status_code=500, detail="Invalid license payload")
        if not bool(license_payload.get("is_currently_active", self._license_is_active_locally(license_payload))):
            plan = str(license_payload.get("plan", "")).strip() or "your plan"
            raise ApiError(status_code=403, detail=f"License not active ({plan}).")
        return license_payload

    def _clear_local_session(self) -> None:
        self.access_token = None
        self.refresh_token = None
        self.access_token_expires_at = None
        self.user = None
        self.license_cache = None
        self.license_checked_at = None
        clear_session()

    def _access_token_stale(self) -> bool:
        if not self.access_token:
            return True
        if self.access_token_expires_at is None:
            return False
        return time.time() >= self.access_token_expires_at

    def desktop_start(self) -> dict[str, Any]:
        return self._request("POST", "/auth/desktop/start")

    def desktop_poll(self, *, request_id: str, device_secret: str) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/auth/desktop/poll",
            payload={"request_id": request_id, "device_secret": device_secret},
        )
        if payload.get("status") == "authorized":
            self._store_desktop_tokens(payload)
        return payload

    def desktop_refresh(self) -> dict[str, Any]:
        if not self.refresh_token:
            raise ApiError(status_code=401, detail="No local desktop refresh token")
        payload = self._request(
            "POST",
            "/auth/desktop/refresh",
            payload={"refresh_token": self.refresh_token},
        )
        return self._store_desktop_tokens(payload)

    def logout(self) -> None:
        if self.refresh_token:
            try:
                self._request(
                    "POST",
                    "/auth/desktop/logout",
                    payload={"refresh_token": self.refresh_token},
                )
            except ApiError:
                pass
        self._clear_local_session()

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/auth/me", with_auth=True)

    def get_license(self) -> dict[str, Any]:
        return self._request("GET", "/licenses/me", with_auth=True)

    def start_trial(self) -> dict[str, Any]:
        return self._request("POST", "/licenses/start-trial", with_auth=True)

    def ensure_active_license(self, *, allow_start_trial: bool = True) -> dict[str, Any]:
        if self._access_token_stale() and self.refresh_token:
            self.desktop_refresh()

        try:
            license_payload = self.get_license()
        except ApiError as exc:
            if exc.status_code == 401:
                self.desktop_refresh()
                license_payload = self.get_license()
            elif exc.status_code == 404 and allow_start_trial:
                license_payload = self.start_trial()
            else:
                raise

        validated = self._validate_active_license_payload(license_payload)
        self.set_license_cache(validated, checked_now=True)
        return validated

    def ensure_active_license_cached(self, *, force: bool, allow_start_trial: bool) -> dict[str, Any]:
        if self.license_cache:
            self._validate_active_license_payload(self.license_cache)
            needs_revalidate = force or self.should_revalidate_license_now(self.license_cache)
            if not needs_revalidate:
                return self.license_cache
            try:
                return self.ensure_active_license(allow_start_trial=allow_start_trial)
            except ApiError as exc:
                if exc.status_code == 0:
                    # Offline: keep using last locally valid entitlement.
                    return self.license_cache
                raise
        return self.ensure_active_license(allow_start_trial=allow_start_trial)

    def try_restore_session(self) -> dict[str, Any]:
        if (
            self.user
            and self.license_cache
            and self._license_is_active_locally(self.license_cache)
            and not self._access_token_stale()
            and not self.should_revalidate_license_now(self.license_cache)
        ):
            return {
                "access_token": self.access_token or "",
                "refresh_token": self.refresh_token or "",
                "user": self.user,
            }

        try:
            return self.desktop_refresh()
        except ApiError as exc:
            if (
                exc.status_code == 0
                and self.user
                and self.license_cache
                and self._license_is_active_locally(self.license_cache)
            ):
                # Offline fallback: allow opening app with last known valid entitlement.
                return {
                    "access_token": self.access_token or "",
                    "refresh_token": self.refresh_token or "",
                    "user": self.user,
                }
            if exc.status_code not in {0}:
                self._clear_local_session()
            raise
