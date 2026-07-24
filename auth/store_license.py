from __future__ import annotations

import asyncio
import json
import os
import time
import webbrowser
import getpass
import subprocess
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .session_store import storage_dir
from utils.subprocess_utils import run_no_window


STATE_FILENAME = "store_license_state.json"
CHECK_INTERVAL_DEFAULT_SECONDS = 24 * 60 * 60
CHECK_INTERVAL_NEAR_EXPIRY_SECONDS = 60 * 60
NEAR_EXPIRY_WINDOW_SECONDS = 24 * 60 * 60
DEFAULT_TRIAL_DAYS = 3
DEFAULT_MONTHLY_STORE_ID = "9PHZ3F113134"
DEFAULT_LIFETIME_STORE_ID = "9NQGFMV3NKGB"


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(str(raw).strip())
    except Exception:
        return int(default)


@dataclass
class StoreLicenseError(Exception):
    status_code: int
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass
class StorePurchaseResult:
    success: bool
    already_owned: bool
    status: str
    detail: str
    license_payload: dict[str, Any] | None = None


class StoreLicenseService:
    supports_logout = False

    def __init__(self) -> None:
        self.monthly_store_id = str(
            os.getenv("AUTOCUTTER_STORE_MONTHLY_STORE_ID", DEFAULT_MONTHLY_STORE_ID) or DEFAULT_MONTHLY_STORE_ID
        ).strip()
        self.lifetime_store_id = str(
            os.getenv("AUTOCUTTER_STORE_LIFETIME_STORE_ID", DEFAULT_LIFETIME_STORE_ID) or DEFAULT_LIFETIME_STORE_ID
        ).strip()
        self.monthly_offer_label = str(os.getenv("AUTOCUTTER_STORE_MONTHLY_LABEL", "EUR 6.99 / month") or "EUR 6.99 / month").strip()
        self.lifetime_offer_label = str(os.getenv("AUTOCUTTER_STORE_LIFETIME_LABEL", "EUR 50.00 one-time") or "EUR 50.00 one-time").strip()
        self.trial_days = max(1, _int_env("AUTOCUTTER_STORE_TRIAL_DAYS", DEFAULT_TRIAL_DAYS))
        self._allow_unpacked_store = str(os.getenv("AUTOCUTTER_STORE_ALLOW_UNPACKAGED", "0")).strip().lower() in {"1", "true", "yes", "on"}

        requested_mode = str(os.getenv("AUTOCUTTER_LICENSE_MODE", "auto") or "auto").strip().lower()
        if requested_mode not in {"auto", "store", "simulator"}:
            requested_mode = "auto"
        self.mode_requested = requested_mode

        self._StoreContext = None
        self._StorePurchaseStatus = None
        self._store_supported = False
        self._package_identity = False
        self._init_store_runtime()
        self.mode = self._resolve_mode()

        self._state_path = self._state_file_path()
        self._state = self._load_state()

        cached = self._state.get("license_cache")
        self.license_cache: dict[str, Any] | None = dict(cached) if isinstance(cached, dict) else None
        raw_checked_at = self._state.get("license_checked_at")
        try:
            self.license_checked_at = float(raw_checked_at) if raw_checked_at is not None else None
        except (TypeError, ValueError):
            self.license_checked_at = None

        self.user = self._resolve_user_profile()

    @property
    def purchase_monthly_enabled(self) -> bool:
        if self.mode == "simulator":
            return True
        return bool(self.monthly_store_id and self._store_supported and (self._package_identity or self._allow_unpacked_store))

    @property
    def purchase_lifetime_enabled(self) -> bool:
        if self.mode == "simulator":
            return True
        return bool(self.lifetime_store_id and self._store_supported and (self._package_identity or self._allow_unpacked_store))

    @property
    def manage_store_enabled(self) -> bool:
        return bool(self._store_supported or self.mode == "simulator")

    def _resolve_mode(self) -> str:
        if self.mode_requested == "simulator":
            return "simulator"
        if self.mode_requested == "store":
            if self._store_supported and (self._package_identity or self._allow_unpacked_store):
                return "store"
            return "simulator"
        # auto
        if self._store_supported and (self._package_identity or self._allow_unpacked_store):
            return "store"
        return "simulator"

    def _init_store_runtime(self) -> None:
        if os.name != "nt":
            self._store_supported = False
            self._package_identity = False
            return
        try:
            from winsdk.windows.services.store import StoreContext, StorePurchaseStatus
            from winsdk.windows.applicationmodel import Package
        except Exception:
            self._store_supported = False
            self._package_identity = False
            return

        self._StoreContext = StoreContext
        self._StorePurchaseStatus = StorePurchaseStatus
        self._store_supported = True

        try:
            _ = Package.current.id.family_name
            self._package_identity = True
        except Exception:
            self._package_identity = False

    @staticmethod
    def _windows_display_name() -> str:
        if os.name != "nt":
            return ""
        try:
            # EXTENDED_NAME_FORMAT.NameDisplay = 3
            NameDisplay = 3
            size = wintypes.ULONG(0)
            ctypes.windll.secur32.GetUserNameExW(NameDisplay, None, ctypes.byref(size))
            if int(size.value) <= 0:
                return ""
            buf = ctypes.create_unicode_buffer(int(size.value) + 2)
            ok = ctypes.windll.secur32.GetUserNameExW(NameDisplay, buf, ctypes.byref(size))
            if not ok:
                return ""
            return str(buf.value or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _windows_upn() -> str:
        if os.name != "nt":
            return ""
        try:
            p = run_no_window(
                ["whoami", "/upn"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if int(p.returncode) != 0:
                return ""
            out = str(p.stdout or "").strip()
            if not out:
                return ""
            first = out.splitlines()[0].strip()
            if "@" not in first:
                return ""
            return first
        except Exception:
            return ""

    def _resolve_user_profile(self) -> dict[str, Any]:
        display_name = self._windows_display_name()
        upn = self._windows_upn()
        fallback = (
            str(os.getenv("USERNAME", "") or "").strip()
            or str(getpass.getuser() or "").strip()
            or "Microsoft Store User"
        )
        label = display_name or upn or fallback
        return {
            "email": label,
            "display_name": display_name or label,
            "upn": upn,
            "provider": "microsoft_store",
        }

    @staticmethod
    def _state_file_path() -> Path:
        return storage_dir() / STATE_FILENAME

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self) -> None:
        payload = dict(self._state)
        payload["license_cache"] = self.license_cache if isinstance(self.license_cache, dict) else {}
        payload["license_checked_at"] = self.license_checked_at
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
        tmp.replace(self._state_path)

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _to_iso_utc(value: datetime | None) -> str | None:
        if value is None:
            return None
        dt = value.astimezone(timezone.utc)
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

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
        ends_at = StoreLicenseService._parse_iso_utc(license_payload.get("ends_at"))
        if ends_at and StoreLicenseService._now_utc() >= ends_at:
            return False
        return bool(license_payload.get("is_currently_active", True))

    def is_license_active(self, license_payload: dict[str, Any] | None) -> bool:
        return bool(isinstance(license_payload, dict) and self._license_is_active_locally(license_payload))

    def compute_license_check_interval_seconds(self, license_payload: dict[str, Any] | None = None) -> int:
        payload = license_payload if isinstance(license_payload, dict) else self.license_cache
        if not isinstance(payload, dict):
            return CHECK_INTERVAL_DEFAULT_SECONDS

        plan = str(payload.get("plan", "")).strip().lower()
        if plan not in {"trial", "monthly"}:
            return CHECK_INTERVAL_DEFAULT_SECONDS

        ends_at = self._parse_iso_utc(payload.get("ends_at"))
        if not ends_at:
            return CHECK_INTERVAL_DEFAULT_SECONDS

        remaining_seconds = (ends_at - self._now_utc()).total_seconds()
        if remaining_seconds <= NEAR_EXPIRY_WINDOW_SECONDS:
            return CHECK_INTERVAL_NEAR_EXPIRY_SECONDS
        return CHECK_INTERVAL_DEFAULT_SECONDS

    def should_revalidate_license_now(self, license_payload: dict[str, Any] | None = None) -> bool:
        if self.license_checked_at is None:
            return True
        elapsed = time.time() - self.license_checked_at
        interval = self.compute_license_check_interval_seconds(license_payload)
        return elapsed >= interval

    def set_license_cache(self, license_payload: dict[str, Any], *, checked_now: bool = False) -> None:
        self.license_cache = dict(license_payload)
        if checked_now:
            self.license_checked_at = time.time()
        self._save_state()

    def _build_payload(
        self,
        *,
        plan: str,
        status: str,
        ends_at: datetime | None,
        source: str,
    ) -> dict[str, Any]:
        active = status == "active" and (ends_at is None or self._now_utc() < ends_at)
        return {
            "plan": plan,
            "status": status,
            "source": source,
            "starts_at": self._to_iso_utc(self._now_utc()),
            "ends_at": self._to_iso_utc(ends_at),
            "is_currently_active": active,
        }

    def _trial_payload(self) -> dict[str, Any]:
        started = self._parse_iso_utc(self._state.get("trial_started_at"))
        if started is None:
            started = self._now_utc()
            self._state["trial_started_at"] = self._to_iso_utc(started)

        ends_at = started + timedelta(days=self.trial_days)
        active = self._now_utc() < ends_at
        payload = self._build_payload(
            plan="trial",
            status="active" if active else "expired",
            ends_at=ends_at,
            source="microsoft_store_trial",
        )
        payload["starts_at"] = self._to_iso_utc(started)
        self._state["trial_expires_at"] = self._to_iso_utc(ends_at)
        return payload

    def _simulator_payload(self) -> dict[str, Any]:
        if bool(self._state.get("sim_lifetime_owned", False)):
            return self._build_payload(
                plan="lifetime",
                status="active",
                ends_at=None,
                source="simulator",
            )

        monthly_expires = self._parse_iso_utc(self._state.get("sim_monthly_expires_at"))
        if monthly_expires and self._now_utc() < monthly_expires:
            return self._build_payload(
                plan="monthly",
                status="active",
                ends_at=monthly_expires,
                source="simulator",
            )

        return self._trial_payload()

    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _query_store_state_async(self) -> dict[str, Any]:
        if self._StoreContext is None:
            raise StoreLicenseError(status_code=500, detail="winsdk StoreContext not available")

        ctx = self._StoreContext.get_default()
        app_license = await ctx.get_app_license_async()
        add_ons: dict[str, dict[str, Any]] = {}

        add_on_map = getattr(app_license, "add_on_licenses", None)
        if add_on_map is not None:
            try:
                keys = list(add_on_map.keys())
            except Exception:
                keys = []
            for key in keys:
                store_id = str(key)
                try:
                    lic = add_on_map[key]
                except Exception:
                    try:
                        lic = add_on_map.lookup(key)
                    except Exception:
                        lic = None
                if lic is None:
                    continue
                exp_dt = getattr(lic, "expiration_date", None)
                add_ons[store_id] = {
                    "is_active": bool(getattr(lic, "is_active", False)),
                    "expiration_date": self._to_iso_utc(exp_dt if isinstance(exp_dt, datetime) else None),
                }

        return {
            "app_is_active": bool(getattr(app_license, "is_active", False)),
            "app_is_trial": bool(getattr(app_license, "is_trial", False)),
            "add_ons": add_ons,
        }

    def _resolve_store_plan(self, add_ons: dict[str, dict[str, Any]]) -> tuple[str | None, datetime | None]:
        # 1) explicit mapping from env store IDs (recommended)
        if self.lifetime_store_id:
            lic = add_ons.get(self.lifetime_store_id)
            if isinstance(lic, dict) and bool(lic.get("is_active", False)):
                return "lifetime", None

        if self.monthly_store_id:
            lic = add_ons.get(self.monthly_store_id)
            if isinstance(lic, dict) and bool(lic.get("is_active", False)):
                exp = self._parse_iso_utc(lic.get("expiration_date"))
                return "monthly", exp

        # 2) fallback heuristic only when IDs are not configured yet
        if not self.lifetime_store_id or not self.monthly_store_id:
            monthly_candidate: datetime | None = None
            for store_id, lic in add_ons.items():
                if not isinstance(lic, dict) or not bool(lic.get("is_active", False)):
                    continue
                exp = self._parse_iso_utc(lic.get("expiration_date"))
                sid = store_id.lower()
                if "life" in sid or "perpetual" in sid:
                    return "lifetime", None
                if "month" in sid or "sub" in sid:
                    return "monthly", exp
                if exp is None or exp.year >= 9999:
                    return "lifetime", None
                if monthly_candidate is None or exp > monthly_candidate:
                    monthly_candidate = exp
            if monthly_candidate is not None:
                return "monthly", monthly_candidate

        return None, None

    def _store_payload(self) -> dict[str, Any]:
        if not self._store_supported:
            raise StoreLicenseError(status_code=500, detail="Microsoft Store API unavailable")
        try:
            data = self._run_async(self._query_store_state_async())
        except Exception as exc:
            raise StoreLicenseError(status_code=0, detail=f"Store check failed: {exc}") from None

        add_ons = data.get("add_ons")
        add_ons = add_ons if isinstance(add_ons, dict) else {}
        plan, ends_at = self._resolve_store_plan(add_ons)
        if plan == "lifetime":
            return self._build_payload(
                plan="lifetime",
                status="active",
                ends_at=None,
                source="microsoft_store",
            )
        if plan == "monthly":
            active = ends_at is None or self._now_utc() < ends_at
            return self._build_payload(
                plan="monthly",
                status="active" if active else "expired",
                ends_at=ends_at,
                source="microsoft_store",
            )

        # No add-on purchase: local trial gate.
        return self._trial_payload()

    def refresh_license(self, *, force: bool = False) -> dict[str, Any]:
        if (
            not force
            and isinstance(self.license_cache, dict)
            and self.is_license_active(self.license_cache)
            and not self.should_revalidate_license_now(self.license_cache)
        ):
            return dict(self.license_cache)

        if self.mode == "store":
            try:
                payload = self._store_payload()
            except StoreLicenseError as exc:
                if exc.status_code == 0 and self.is_license_active(self.license_cache):
                    # Offline/network issue: keep locally valid entitlement.
                    return dict(self.license_cache or {})
                if self.is_license_active(self.license_cache):
                    return dict(self.license_cache or {})
                # First run with a store check issue: still allow local trial bootstrap.
                payload = self._trial_payload()
        else:
            payload = self._simulator_payload()

        self.set_license_cache(payload, checked_now=True)
        return dict(payload)

    async def _request_purchase_async(self, store_id: str):
        if self._StoreContext is None:
            raise StoreLicenseError(status_code=500, detail="winsdk StoreContext not available")
        ctx = self._StoreContext.get_default()
        return await ctx.request_purchase_async(store_id)

    def _purchase_via_store(self, store_id: str) -> StorePurchaseResult:
        if not self._store_supported:
            raise StoreLicenseError(status_code=500, detail="Microsoft Store API unavailable on this machine")
        if not (self._package_identity or self._allow_unpacked_store):
            raise StoreLicenseError(
                status_code=400,
                detail="Store purchase requires packaged MSIX identity.",
            )
        try:
            result = self._run_async(self._request_purchase_async(store_id))
        except Exception as exc:
            raise StoreLicenseError(status_code=0, detail=f"Purchase failed: {exc}") from None

        status = getattr(result, "status", None)
        status_name = str(getattr(status, "name", status))
        ext_err = getattr(result, "extended_error", None)
        ext_text = ""
        if ext_err is not None:
            try:
                ext_val = int(getattr(ext_err, "value", 0))
            except Exception:
                ext_val = 0
            if ext_val:
                ext_text = f" (0x{ext_val & 0xFFFFFFFF:08X})"

        s = self._StorePurchaseStatus
        if status in {s.SUCCEEDED, s.ALREADY_PURCHASED}:
            payload = self.refresh_license(force=True)
            return StorePurchaseResult(
                success=True,
                already_owned=bool(status == s.ALREADY_PURCHASED),
                status=status_name,
                detail="Purchase completed." if status == s.SUCCEEDED else "Already owned.",
                license_payload=payload,
            )

        if status == s.NOT_PURCHASED:
            return StorePurchaseResult(
                success=False,
                already_owned=False,
                status=status_name,
                detail=f"Purchase canceled by user{ext_text}.",
                license_payload=self.license_cache,
            )
        if status == s.NETWORK_ERROR:
            return StorePurchaseResult(
                success=False,
                already_owned=False,
                status=status_name,
                detail=f"Network error during purchase{ext_text}.",
                license_payload=self.license_cache,
            )
        if status == s.SERVER_ERROR:
            return StorePurchaseResult(
                success=False,
                already_owned=False,
                status=status_name,
                detail=f"Store server error{ext_text}.",
                license_payload=self.license_cache,
            )

        return StorePurchaseResult(
            success=False,
            already_owned=False,
            status=status_name,
            detail=f"Purchase failed ({status_name}){ext_text}.",
            license_payload=self.license_cache,
        )

    def purchase_monthly(self) -> StorePurchaseResult:
        if self.mode == "simulator":
            exp = self._now_utc() + timedelta(days=30)
            self._state["sim_monthly_expires_at"] = self._to_iso_utc(exp)
            self._save_state()
            payload = self.refresh_license(force=True)
            return StorePurchaseResult(
                success=True,
                already_owned=False,
                status="SIMULATED",
                detail="Simulated monthly purchase applied.",
                license_payload=payload,
            )

        if not self.monthly_store_id:
            raise StoreLicenseError(
                status_code=400,
                detail="Missing AUTOCUTTER_STORE_MONTHLY_STORE_ID.",
            )
        return self._purchase_via_store(self.monthly_store_id)

    def purchase_lifetime(self) -> StorePurchaseResult:
        if self.mode == "simulator":
            self._state["sim_lifetime_owned"] = True
            self._save_state()
            payload = self.refresh_license(force=True)
            return StorePurchaseResult(
                success=True,
                already_owned=False,
                status="SIMULATED",
                detail="Simulated lifetime purchase applied.",
                license_payload=payload,
            )

        if not self.lifetime_store_id:
            raise StoreLicenseError(
                status_code=400,
                detail="Missing AUTOCUTTER_STORE_LIFETIME_STORE_ID.",
            )
        return self._purchase_via_store(self.lifetime_store_id)

    def restore_purchases(self) -> dict[str, Any]:
        return self.refresh_license(force=True)

    def open_store_subscription_page(self) -> None:
        # Prefer monthly offer page for subscription management.
        if self.monthly_store_id:
            url = f"ms-windows-store://pdp/?productid={self.monthly_store_id}"
        elif self.lifetime_store_id:
            url = f"ms-windows-store://pdp/?productid={self.lifetime_store_id}"
        else:
            url = "ms-windows-store://home"
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass
