from __future__ import annotations

import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
from typing import Any


_HTTP_STATUS = re.compile(r"(?:HTTP(?:\s+Error)?|server returned|status(?: code)?)[\s:]+([1-5]\d\d)\b", re.I)
_TRANSIENT = re.compile(r"timed? out|timeout|connection (?:reset|refused|aborted)|temporary failure|network is unreachable", re.I)
_AUTOMATIC_RETRY_ALLOWED: ContextVar[bool] = ContextVar("autocutter_automatic_retry_allowed", default=False)


@contextmanager
def automatic_retry_scope(allowed: bool) -> Iterator[None]:
    token = _AUTOMATIC_RETRY_ALLOWED.set(allowed)
    try:
        yield
    finally:
        _AUTOMATIC_RETRY_ALLOWED.reset(token)


def automatic_retry_allowed() -> bool:
    return _AUTOMATIC_RETRY_ALLOWED.get()


def transient_network_error(error: object) -> bool:
    """Retry transport failures, 429 and 5xx, never a permanent HTTP response."""
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    message = str(error)
    statuses = [int(value) for value in _HTTP_STATUS.findall(message)]
    if statuses:
        return all(value == 429 or 500 <= value <= 599 for value in statuses)
    return bool(_TRANSIENT.search(message))


def wait_for_retry(manager: Any, job_id: str, cancellation: Any, delay_s: float) -> None:
    """Keep transient work recoverable if shutdown or a crash interrupts backoff."""
    manager.prepare_automatic_retry(job_id)
    deadline = time.monotonic() + max(0.0, delay_s)
    try:
        while time.monotonic() < deadline:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    except BaseException:
        cancelled = cancellation is not None and bool(getattr(cancellation, "is_cancelled", getattr(cancellation, "cancelled", False)))
        if cancelled:
            # Explicit user cancellation has already persisted CANCELLED and
            # interrupt() preserves it. Queue shutdown retains a retryable job.
            manager.interrupt(job_id)
        raise
