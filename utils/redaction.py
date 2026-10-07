from __future__ import annotations

import re
import json


_REDACTED = "[REDACTED]"
_SECRET_NAMES = (
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|device[_-]?code|"
    r"user[_-]?code|client[_-]?secret|oauth[_-]?(?:token|code)|"
    r"authorization|auth[_-]?token|token|code|upload[_-]?id|"
    r"sig|signature|state"
)
_QUERY_SECRET = re.compile(
    rf"(?P<prefix>[?&](?:{_SECRET_NAMES})=)[^&#\s\"'<>]*",
    re.IGNORECASE,
)
_ASSIGNED_SECRET = re.compile(
    rf"(?P<prefix>(?<![\w-])[\"']?(?:{_SECRET_NAMES})[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"(?:Bearer|Basic)\s+[^\s,;}\]\"']+|\[REDACTED\]|[^\s,;&}\]\"']+)",
    re.IGNORECASE,
)
_COOKIE = re.compile(
    r"(?P<prefix>(?<![\w-])[\"']?(?:cookie|set-cookie|cookies)[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def _replace_value(match: re.Match[str]) -> str:
    value = match.group("value")
    if value.startswith(("'", '"')):
        return match.group("prefix") + value[0] + _REDACTED + value[0]
    return match.group("prefix") + _REDACTED


def _redact_text(text: str, depth: int = 0) -> str:
    output = _COOKIE.sub(_replace_value, text)
    output = _QUERY_SECRET.sub(lambda match: match.group("prefix") + _REDACTED, output)
    output = _ASSIGNED_SECRET.sub(_replace_value, output)
    output = _BEARER.sub("Bearer " + _REDACTED, output)
    if depth < 8:
        def redact_json_string(match: re.Match[str]) -> str:
            literal = match.group(0)
            if "\\" not in literal:
                return literal
            try:
                decoded = json.loads(literal)
            except ValueError:
                return literal
            cleaned = _redact_text(decoded, depth + 1)
            # Preserve spelling/formatting unless there was a secret inside a
            # serialized message, including several layers of JSON escaping.
            return json.dumps(cleaned, ensure_ascii=True) if cleaned != decoded else literal

        output = _JSON_STRING.sub(redact_json_string, output)
    return output


def redact_secrets(text: str) -> str:
    """Remove OAuth credentials and authenticated URL/header values from text.

    This operates on messages, tracebacks, JSON and Python-dict renderings. It
    deliberately preserves parameter/key names, never reads a credential store,
    and treats resumable session identifiers as credentials.
    """
    return _redact_text(str(text or ""))
