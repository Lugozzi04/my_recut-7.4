from __future__ import annotations

import unittest
import urllib.parse
from collections.abc import Mapping

from automation.twitch import (
    DEVICE_GRANT_TYPE,
    HttpResponse,
    TwitchApiClient,
    TwitchApiError,
    TwitchAuthorizationPending,
    TwitchProtocolError,
    parse_twitch_duration,
)


class FakeTransport:
    def __init__(self, *responses: HttpResponse) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str] | None = None,
        timeout: float = 15.0,
    ) -> HttpResponse:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "form": dict(form or {}),
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("No fake Twitch response queued.")
        return self.responses.pop(0)


class TwitchClientTests(unittest.TestCase):
    def test_duration_parser_supports_twitch_format(self) -> None:
        self.assertEqual(parse_twitch_duration("6h26m14s"), 23174)
        self.assertEqual(parse_twitch_duration("26m14s"), 1574)
        self.assertEqual(parse_twitch_duration("45s"), 45)
        with self.assertRaises(TwitchProtocolError):
            parse_twitch_duration("01:30")

    def test_get_archived_videos_uses_helix_headers_and_parses_result(self) -> None:
        transport = FakeTransport(
            HttpResponse(
                200,
                {
                    "data": [
                        {
                            "id": "987",
                            "user_id": "42",
                            "user_login": "streamer",
                            "user_name": "Streamer",
                            "title": "Live title",
                            "description": "",
                            "created_at": "2026-09-28T10:00:00Z",
                            "published_at": "2026-09-28T10:00:00Z",
                            "url": "https://www.twitch.tv/videos/987",
                            "thumbnail_url": "https://example.invalid/thumb.jpg",
                            "duration": "2h3m4s",
                            "language": "it",
                            "view_count": 12,
                        }
                    ],
                    "pagination": {},
                },
            )
        )
        client = TwitchApiClient("client-1", transport=transport)

        videos = client.get_archived_videos("secret-token", "42", first=5)

        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0].duration_s, 7384)
        request = transport.requests[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(str(request["url"])).query)
        self.assertEqual(query["type"], ["archive"])
        self.assertEqual(query["sort"], ["time"])
        self.assertEqual(query["first"], ["5"])
        self.assertEqual(request["headers"]["Client-Id"], "client-1")  # type: ignore[index]
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret-token")  # type: ignore[index]

    def test_validate_token_checks_client_ownership(self) -> None:
        transport = FakeTransport(
            HttpResponse(
                200,
                {
                    "client_id": "another-client",
                    "user_id": "42",
                    "login": "streamer",
                    "scopes": [],
                    "expires_in": 3600,
                },
            )
        )
        client = TwitchApiClient("client-1", transport=transport)

        with self.assertRaises(TwitchProtocolError):
            client.validate_access_token("secret-token")

    def test_device_flow_handles_pending_then_returns_token(self) -> None:
        transport = FakeTransport(
            HttpResponse(
                200,
                {
                    "device_code": "device-code",
                    "user_code": "ABCD1234",
                    "verification_uri": "https://www.twitch.tv/activate",
                    "expires_in": 600,
                    "interval": 5,
                },
            ),
            HttpResponse(400, {"status": 400, "message": "authorization_pending"}),
            HttpResponse(
                200,
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 14400,
                    "scope": [],
                    "token_type": "bearer",
                },
            ),
        )
        client = TwitchApiClient("client-1", transport=transport, clock=lambda: 1000.0)
        authorization = client.start_device_authorization()

        with self.assertRaises(TwitchAuthorizationPending):
            client.poll_device_authorization(authorization)
        token = client.poll_device_authorization(authorization)

        self.assertEqual(token.access_token, "access")
        self.assertEqual(token.expires_at, 15400.0)
        self.assertEqual(transport.requests[1]["form"]["grant_type"], DEVICE_GRANT_TYPE)  # type: ignore[index]

    def test_refresh_rotates_both_tokens_without_client_secret(self) -> None:
        transport = FakeTransport(
            HttpResponse(
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "scope": [],
                    "token_type": "bearer",
                },
            )
        )
        client = TwitchApiClient("public-client", transport=transport, clock=lambda: 10.0)

        token = client.refresh_access_token("old-refresh")

        request_form = transport.requests[0]["form"]
        self.assertEqual(token.refresh_token, "new-refresh")
        self.assertEqual(request_form["client_id"], "public-client")  # type: ignore[index]
        self.assertNotIn("client_secret", request_form)  # type: ignore[operator]

    def test_api_error_never_includes_access_token(self) -> None:
        transport = FakeTransport(HttpResponse(401, {"message": "invalid access token"}))
        client = TwitchApiClient("client-1", transport=transport)

        with self.assertRaises(TwitchApiError) as caught:
            client.get_archived_videos("super-secret", "42")

        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("super-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
