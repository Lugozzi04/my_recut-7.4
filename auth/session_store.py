from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


APP_DIR_NAME = "AutoCutterDesktop"
TOKEN_FILENAME = "session.json"
COOKIE_FILENAME = "cookies.txt"
DEVICE_ID_FILENAME = "device_id.txt"


def storage_dir() -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    if not base:
        base = str(Path.home() / ".autocutter")
    target = Path(base) / APP_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def token_file_path() -> Path:
    return storage_dir() / TOKEN_FILENAME


def cookie_file_path() -> Path:
    return storage_dir() / COOKIE_FILENAME


def device_id_file_path() -> Path:
    return storage_dir() / DEVICE_ID_FILENAME


def load_session() -> dict[str, Any]:
    token_path = token_file_path()
    if not token_path.exists():
        return {}
    try:
        return json.loads(token_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_session_payload(payload: dict[str, Any]) -> None:
    path = token_file_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def update_session(**fields: Any) -> dict[str, Any]:
    payload = load_session()
    payload.update(fields)
    save_session_payload(payload)
    return payload


def save_session(*, access_token: str, user: dict[str, Any]) -> None:
    update_session(
        access_token=access_token,
        refresh_token="",
        user=user or {},
    )


def save_desktop_session(*, access_token: str, refresh_token: str, user: dict[str, Any]) -> None:
    update_session(
        access_token=access_token,
        refresh_token=refresh_token,
        user=user or {},
    )


def clear_session() -> None:
    for path in (token_file_path(), cookie_file_path()):
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


def load_or_create_device_id() -> str:
    path = device_id_file_path()
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    except Exception:
        pass

    value = uuid.uuid4().hex
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(value, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception:
        # Fallback: still return a value even if persistence fails.
        return value
    return value
