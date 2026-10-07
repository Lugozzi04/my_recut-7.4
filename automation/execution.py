from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from automation.retry import automatic_retry_scope, wait_for_retry
from utils.ffmpeg import FFprobeCancelled, cancellable_probe_scope


T = TypeVar("T")


class _ProbeStageCancelled(RuntimeError):
    exit_code = 70

    def __init__(self, code: str) -> None:
        super().__init__("Media probe cancelled.")
        self.code = code


def job_execution(method: Callable[..., T]) -> Callable[..., T]:
    """Protect the existing stage services across GUI and terminal processes."""
    @wraps(method)
    def execute(service: Any, job_id: str, *args: Any, **kwargs: Any) -> T:
        # A coordinator may already own this lock on the same thread. The
        # manager supports that nesting while rejecting another executor.
        token = kwargs.get("cancellation")
        with service.manager.execution_lock(job_id), cancellable_probe_scope(token):
            for attempt in range(3):
                try:
                    with automatic_retry_scope(attempt < 2):
                        return method(service, job_id, *args, **kwargs)
                except FFprobeCancelled as exc:
                    current = service.manager.get(job_id)
                    stage = {
                        "downloading": "download", "analyzing": "analysis",
                        "ready_export": "export", "exporting": "export",
                        "ready_upload": "upload", "uploading": "upload",
                    }.get(current.state.value, "probe")
                    error: Exception = _ProbeStageCancelled(f"{stage}_cancelled")
                    if token is not None:
                        try:
                            token.raise_if_cancelled()
                        except Exception as cancelled:
                            error = cancelled
                    if not current.state.is_terminal:
                        service.manager.fail(job_id, f"{stage}_cancelled", str(error))
                    # Queue loops catch Exception. Convert only here, after
                    # the validators' broad corrupt-artifact handlers unwind.
                    raise error from exc
                except Exception as exc:
                    if not getattr(exc, "retryable", False) or attempt == 2:
                        raise
                    wait_for_retry(service.manager, job_id, token, 0.5 * (2 ** attempt))
            raise AssertionError("Unreachable retry state")
    return execute
