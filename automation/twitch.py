from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


TWITCH_API_BASE = "https://api.twitch.tv/helix"
TWITCH_ID_BASE = "https://id.twitch.tv/oauth2"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
_DURATION_PATTERN = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class TwitchError(RuntimeError):
    pass


class TwitchNetworkError(TwitchError):
    pass


class TwitchProtocolError(TwitchError):
    pass


class TwitchApiError(TwitchError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Twitch API returned HTTP {status}: {message}")
        self.status = int(status)
        self.message = str(message)


class TwitchAuthorizationPending(TwitchApiError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status: int
    payload: object


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        timeout: float = 15.0,
    ) -> HttpResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        timeout: float = 15.0,
    ) -> HttpResponse:
        body = urllib.parse.urlencode(dict(form)).encode("utf-8") if form is not None else None
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "Auto-Cutter/automation",
            **dict(headers or {}),
        }
        if body is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=float(timeout)) as response:
                status = int(response.status)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raw = exc.read()
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            raise TwitchNetworkError(f"Cannot reach Twitch: {reason}") from exc
        except OSError as exc:
            raise TwitchNetworkError(f"Cannot reach Twitch: {exc}") from exc

        if not raw:
            payload: object = {}
        else:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise TwitchProtocolError("Twitch returned an invalid JSON response.") from exc
        return HttpResponse(status=status, payload=payload)


@dataclass(frozen=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class TwitchToken:
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: tuple[str, ...] = ()
    token_type: str = "bearer"

    def expires_within(self, seconds: float, *, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return self.expires_at <= current + max(0.0, float(seconds))

    def to_mapping(self) -> dict[str, object]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "token_type": self.token_type,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "TwitchToken":
        scopes_raw = raw.get("scopes", [])
        if not isinstance(scopes_raw, list) or not all(isinstance(item, str) for item in scopes_raw):
            raise TwitchProtocolError("Stored Twitch token scopes are invalid.")
        expires_raw = raw.get("expires_at", 0.0)
        if isinstance(expires_raw, bool) or not isinstance(expires_raw, (str, int, float)):
            raise TwitchProtocolError("Stored Twitch token expiration is invalid.")
        try:
            token = cls(
                access_token=str(raw.get("access_token", "")).strip(),
                refresh_token=str(raw.get("refresh_token", "")).strip(),
                expires_at=float(expires_raw),
                scopes=tuple(scopes_raw),
                token_type=str(raw.get("token_type", "bearer")).strip().lower(),
            )
        except (TypeError, ValueError) as exc:
            raise TwitchProtocolError("Stored Twitch token is invalid.") from exc
        if not token.access_token or not token.refresh_token or token.expires_at <= 0.0:
            raise TwitchProtocolError("Stored Twitch token is incomplete.")
        return token


@dataclass(frozen=True)
class TwitchIdentity:
    client_id: str
    user_id: str
    login: str
    scopes: tuple[str, ...]
    expires_in: int


@dataclass(frozen=True)
class TwitchVideo:
    id: str
    user_id: str
    user_login: str
    user_name: str
    title: str
    description: str
    created_at: str
    published_at: str
    url: str
    thumbnail_url: str
    duration_text: str
    duration_s: int
    language: str
    view_count: int


def parse_twitch_duration(value: str) -> int:
    match = _DURATION_PATTERN.fullmatch(str(value).strip().lower())
    if match is None or not any(match.groups()):
        raise TwitchProtocolError(f"Invalid Twitch video duration: {value}")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


class TwitchApiClient:
    def __init__(
        self,
        client_id: str,
        *,
        transport: HttpTransport | None = None,
        timeout: float = 15.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = str(client_id).strip()
        if not self.client_id:
            raise ValueError("Twitch client id is required.")
        self.transport = transport or UrllibTransport()
        self.timeout = max(1.0, float(timeout))
        self._clock = clock

    def start_device_authorization(self, scopes: Sequence[str] = ()) -> DeviceAuthorization:
        payload = self._request_object(
            "POST",
            f"{TWITCH_ID_BASE}/device",
            form={"client_id": self.client_id, "scopes": " ".join(scopes)},
        )
        try:
            authorization = DeviceAuthorization(
                device_code=str(payload["device_code"]).strip(),
                user_code=str(payload["user_code"]).strip(),
                verification_uri=str(payload["verification_uri"]).strip(),
                expires_in=int(payload["expires_in"]),
                interval=max(1, int(payload["interval"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TwitchProtocolError("Twitch device authorization response is incomplete.") from exc
        if not authorization.device_code or not authorization.user_code or not authorization.verification_uri:
            raise TwitchProtocolError("Twitch device authorization response is incomplete.")
        return authorization

    def poll_device_authorization(
        self,
        authorization: DeviceAuthorization,
        scopes: Sequence[str] = (),
    ) -> TwitchToken:
        response = self.transport.request(
            "POST",
            f"{TWITCH_ID_BASE}/token",
            form={
                "client_id": self.client_id,
                "scopes": " ".join(scopes),
                "device_code": authorization.device_code,
                "grant_type": DEVICE_GRANT_TYPE,
            },
            timeout=self.timeout,
        )
        if response.status == 400 and self._error_message(response.payload).lower() == "authorization_pending":
            raise TwitchAuthorizationPending(response.status, "authorization_pending")
        payload = self._successful_object(response)
        return self._parse_token(payload)

    def refresh_access_token(self, refresh_token: str) -> TwitchToken:
        clean_refresh_token = str(refresh_token).strip()
        if not clean_refresh_token:
            raise ValueError("Twitch refresh token is required.")
        payload = self._request_object(
            "POST",
            f"{TWITCH_ID_BASE}/token",
            form={
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": clean_refresh_token,
            },
        )
        return self._parse_token(payload)

    def validate_access_token(self, access_token: str) -> TwitchIdentity:
        token = self._clean_access_token(access_token)
        payload = self._request_object(
            "GET",
            f"{TWITCH_ID_BASE}/validate",
            headers={"Authorization": f"Bearer {token}"},
        )
        scopes_raw = payload.get("scopes", [])
        if not isinstance(scopes_raw, list) or not all(isinstance(item, str) for item in scopes_raw):
            raise TwitchProtocolError("Twitch token validation returned invalid scopes.")
        try:
            identity = TwitchIdentity(
                client_id=str(payload["client_id"]).strip(),
                user_id=str(payload["user_id"]).strip(),
                login=str(payload["login"]).strip(),
                scopes=tuple(scopes_raw),
                expires_in=int(payload["expires_in"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TwitchProtocolError("Twitch token validation response is incomplete.") from exc
        if not identity.client_id or not identity.user_id or not identity.login:
            raise TwitchProtocolError("Twitch token is not associated with a user.")
        if identity.client_id != self.client_id:
            raise TwitchProtocolError("Twitch token belongs to a different client id.")
        return identity

    def get_archived_videos(
        self,
        access_token: str,
        user_id: str,
        *,
        first: int = 1,
    ) -> list[TwitchVideo]:
        token = self._clean_access_token(access_token)
        broadcaster_id = str(user_id).strip()
        if not broadcaster_id:
            raise ValueError("Twitch user id is required.")
        count = max(1, min(100, int(first)))
        query = urllib.parse.urlencode(
            {
                "user_id": broadcaster_id,
                "type": "archive",
                "sort": "time",
                "first": str(count),
            }
        )
        payload = self._request_object(
            "GET",
            f"{TWITCH_API_BASE}/videos?{query}",
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Id": self.client_id,
            },
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise TwitchProtocolError("Twitch videos response does not contain a data list.")
        videos: list[TwitchVideo] = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise TwitchProtocolError(f"Twitch video at index {index} is invalid.")
            videos.append(self._parse_video(item, index))
        return videos

    def _request_object(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self.transport.request(
            method,
            url,
            headers=headers,
            form=form,
            timeout=self.timeout,
        )
        return self._successful_object(response)

    def _successful_object(self, response: HttpResponse) -> dict[str, Any]:
        if not 200 <= response.status < 300:
            raise TwitchApiError(response.status, self._error_message(response.payload))
        if not isinstance(response.payload, dict):
            raise TwitchProtocolError("Twitch response root must be an object.")
        return response.payload

    def _parse_token(self, payload: Mapping[str, Any]) -> TwitchToken:
        scopes_raw = payload.get("scope", [])
        if isinstance(scopes_raw, str):
            scopes = tuple(part for part in scopes_raw.split(" ") if part)
        elif isinstance(scopes_raw, list) and all(isinstance(item, str) for item in scopes_raw):
            scopes = tuple(scopes_raw)
        else:
            raise TwitchProtocolError("Twitch token response contains invalid scopes.")
        try:
            expires_in = int(payload["expires_in"])
            token = TwitchToken(
                access_token=str(payload["access_token"]).strip(),
                refresh_token=str(payload["refresh_token"]).strip(),
                expires_at=float(self._clock()) + expires_in,
                scopes=scopes,
                token_type=str(payload.get("token_type", "bearer")).strip().lower(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TwitchProtocolError("Twitch token response is incomplete.") from exc
        if not token.access_token or not token.refresh_token or expires_in <= 0:
            raise TwitchProtocolError("Twitch token response is incomplete.")
        return token

    @staticmethod
    def _parse_video(raw: Mapping[str, Any], index: int) -> TwitchVideo:
        try:
            duration_text = str(raw["duration"]).strip()
            video = TwitchVideo(
                id=str(raw["id"]).strip(),
                user_id=str(raw["user_id"]).strip(),
                user_login=str(raw.get("user_login", "")).strip(),
                user_name=str(raw.get("user_name", "")).strip(),
                title=str(raw.get("title", "")).strip(),
                description=str(raw.get("description", "")),
                created_at=str(raw["created_at"]).strip(),
                published_at=str(raw["published_at"]).strip(),
                url=str(raw["url"]).strip(),
                thumbnail_url=str(raw.get("thumbnail_url", "")).strip(),
                duration_text=duration_text,
                duration_s=parse_twitch_duration(duration_text),
                language=str(raw.get("language", "")).strip(),
                view_count=int(raw.get("view_count", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TwitchProtocolError(f"Twitch video at index {index} is incomplete.") from exc
        if not video.id or not video.user_id or not video.url or not video.published_at:
            raise TwitchProtocolError(f"Twitch video at index {index} is incomplete.")
        return video

    @staticmethod
    def _clean_access_token(value: str) -> str:
        token = str(value).strip()
        if not token:
            raise ValueError("Twitch access token is required.")
        return token

    @staticmethod
    def _error_message(payload: object) -> str:
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            if message:
                return str(message)
        return "request failed"
