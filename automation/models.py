from __future__ import annotations

import math
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class PipelineState(str, Enum):
    DISCOVERED = "discovered"
    WAITING_RANGE = "waiting_range"
    DOWNLOADING = "downloading"
    ANALYZING = "analyzing"
    READY_EXPORT = "ready_export"
    EXPORTING = "exporting"
    READY_UPLOAD = "ready_upload"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {PipelineState.DONE, PipelineState.FAILED, PipelineState.CANCELLED}

    @property
    def accepts_progress(self) -> bool:
        return self in {
            PipelineState.DOWNLOADING,
            PipelineState.ANALYZING,
            PipelineState.EXPORTING,
            PipelineState.UPLOADING,
        }


_NEXT_STATE: dict[PipelineState, PipelineState] = {
    PipelineState.DISCOVERED: PipelineState.WAITING_RANGE,
    PipelineState.WAITING_RANGE: PipelineState.DOWNLOADING,
    PipelineState.DOWNLOADING: PipelineState.ANALYZING,
    PipelineState.ANALYZING: PipelineState.READY_EXPORT,
    PipelineState.READY_EXPORT: PipelineState.EXPORTING,
    PipelineState.EXPORTING: PipelineState.READY_UPLOAD,
    PipelineState.READY_UPLOAD: PipelineState.UPLOADING,
    PipelineState.UPLOADING: PipelineState.DONE,
}


class PipelineTransitionError(ValueError):
    pass


class PipelineFormatError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise PipelineFormatError(f"{field_name} must be a number or null.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise PipelineFormatError(f"{field_name} must be a number or null.") from exc
    if not math.isfinite(result):
        raise PipelineFormatError(f"{field_name} must be finite.")
    return result


@dataclass
class PipelineJob:
    id: str
    vod_id: str
    vod_url: str
    channel_id: str = ""
    source_title: str = ""
    state: PipelineState = PipelineState.DISCOVERED
    range_start_s: float | None = None
    range_end_s: float | None = None
    local_source_path: str | None = None
    project_path: str | None = None
    export_path: str | None = None
    youtube_video_id: str | None = None
    progress: float = 0.0
    retry_state: PipelineState | None = None
    retry_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        vod_id: str,
        vod_url: str,
        channel_id: str = "",
        source_title: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "PipelineJob":
        job = cls(
            id=uuid.uuid4().hex,
            vod_id=str(vod_id).strip(),
            vod_url=str(vod_url).strip(),
            channel_id=str(channel_id).strip(),
            source_title=str(source_title).strip(),
            metadata=deepcopy(dict(metadata or {})),
        )
        job.validate()
        return job

    def validate(self) -> None:
        if not self.id.strip():
            raise PipelineFormatError("Job id is required.")
        if not self.vod_id.strip():
            raise PipelineFormatError("VOD id is required.")
        if not self.vod_url.strip():
            raise PipelineFormatError("VOD URL is required.")
        if not self.created_at or not self.updated_at:
            raise PipelineFormatError("Job timestamps are required.")
        if not math.isfinite(float(self.progress)) or not 0.0 <= float(self.progress) <= 100.0:
            raise PipelineFormatError("Progress must be between 0 and 100.")
        if self.retry_count < 0:
            raise PipelineFormatError("Retry count cannot be negative.")
        if not isinstance(self.metadata, dict):
            raise PipelineFormatError("Metadata must be an object.")
        if (self.range_start_s is None) != (self.range_end_s is None):
            raise PipelineFormatError("Both range boundaries must be set together.")
        if self.range_start_s is not None and self.range_end_s is not None:
            if self.range_start_s < 0.0 or self.range_end_s <= self.range_start_s:
                raise PipelineFormatError("The selected range is invalid.")
        if self.state == PipelineState.FAILED and self.retry_state is None:
            raise PipelineFormatError("A failed job must include its retry state.")
        if self.state != PipelineState.FAILED and self.retry_state is not None:
            raise PipelineFormatError("Only failed jobs can include a retry state.")
        if self.state == PipelineState.DONE and float(self.progress) != 100.0:
            raise PipelineFormatError("A completed job must have 100% progress.")

    def transition(self, target: PipelineState, *, now: str | None = None) -> None:
        if self.state in {PipelineState.FAILED, PipelineState.CANCELLED, PipelineState.DONE}:
            raise PipelineTransitionError(f"Cannot transition a terminal job from {self.state.value}.")
        expected = _NEXT_STATE.get(self.state)
        if target != expected:
            expected_name = expected.value if expected is not None else "none"
            raise PipelineTransitionError(
                f"Invalid transition {self.state.value} -> {target.value}; expected {expected_name}."
            )
        if target == PipelineState.DOWNLOADING and self.range_start_s is None:
            raise PipelineTransitionError("A time range must be selected before download.")

        self.state = target
        self.progress = 100.0 if target == PipelineState.DONE else 0.0
        self.error_code = None
        self.error_message = None
        self.updated_at = now or utc_now()
        self.validate()

    def select_range(self, start_s: float, end_s: float, *, now: str | None = None) -> None:
        if self.state != PipelineState.WAITING_RANGE:
            raise PipelineTransitionError("A range can only be selected while waiting for user input.")
        start = float(start_s)
        end = float(end_s)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0.0 or end <= start:
            raise PipelineTransitionError("The selected range is invalid.")
        self.range_start_s = start
        self.range_end_s = end
        self.transition(PipelineState.DOWNLOADING, now=now)

    def update_progress(self, value: float, *, now: str | None = None) -> None:
        if not self.state.accepts_progress:
            raise PipelineTransitionError(f"State {self.state.value} does not accept progress updates.")
        progress = float(value)
        if not math.isfinite(progress) or not 0.0 <= progress <= 100.0:
            raise PipelineTransitionError("Progress must be between 0 and 100.")
        if progress < self.progress:
            raise PipelineTransitionError("Progress cannot move backwards within a stage.")
        self.progress = progress
        self.updated_at = now or utc_now()

    def fail(self, code: str, message: str, *, now: str | None = None) -> None:
        if self.state.is_terminal:
            raise PipelineTransitionError(f"Cannot fail a terminal job from {self.state.value}.")
        error_code = str(code).strip()
        error_message = str(message).strip()
        if not error_code or not error_message:
            raise PipelineTransitionError("Failure code and message are required.")
        self.retry_state = self.state
        self.state = PipelineState.FAILED
        self.error_code = error_code
        self.error_message = error_message
        self.updated_at = now or utc_now()
        self.validate()

    def retry(self, *, now: str | None = None) -> None:
        if self.state != PipelineState.FAILED or self.retry_state is None:
            raise PipelineTransitionError("Only failed jobs can be retried.")
        target = self.retry_state
        self.state = target
        self.retry_state = None
        self.retry_count += 1
        self.progress = 0.0
        self.error_code = None
        self.error_message = None
        self.updated_at = now or utc_now()
        self.validate()

    def cancel(self, *, now: str | None = None) -> None:
        if self.state == PipelineState.CANCELLED:
            return
        if self.state == PipelineState.DONE:
            raise PipelineTransitionError(f"Cannot cancel a terminal job from {self.state.value}.")
        previous = self.retry_state if self.state == PipelineState.FAILED else self.state
        if previous is None:
            raise PipelineTransitionError("The failed job has no stage to cancel.")
        self.metadata["cancelled_from"] = previous.value
        self.state = PipelineState.CANCELLED
        self.retry_state = None
        self.progress = 0.0
        self.error_code = None
        self.error_message = None
        self.updated_at = now or utc_now()
        self.validate()

    def resume_cancelled(self, *, now: str | None = None) -> None:
        if self.state != PipelineState.CANCELLED:
            raise PipelineTransitionError("Only cancelled jobs can be resumed.")
        try:
            previous = PipelineState(str(self.metadata.get("cancelled_from", "")))
        except ValueError as exc:
            raise PipelineTransitionError("The cancelled job has no valid stage to resume.") from exc
        if previous.is_terminal:
            raise PipelineTransitionError("The cancelled job has no valid stage to resume.")
        target = {
            PipelineState.EXPORTING: PipelineState.READY_EXPORT,
            PipelineState.UPLOADING: PipelineState.READY_UPLOAD,
        }.get(previous, previous)
        self.state = target
        self.retry_state = None
        self.retry_count += 1
        self.progress = 0.0
        self.error_code = None
        self.error_message = None
        self.metadata.pop("cancelled_from", None)
        self.updated_at = now or utc_now()
        self.validate()

    def complete_without_upload(self, *, now: str | None = None) -> None:
        if self.state != PipelineState.READY_UPLOAD:
            raise PipelineTransitionError("Only a completed export can finish without uploading.")
        self.state = PipelineState.DONE
        self.progress = 100.0
        self.retry_state = None
        self.error_code = None
        self.error_message = None
        self.updated_at = now or utc_now()
        self.validate()

    def to_mapping(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.id,
            "vod_id": self.vod_id,
            "vod_url": self.vod_url,
            "channel_id": self.channel_id,
            "source_title": self.source_title,
            "state": self.state.value,
            "range_start_s": self.range_start_s,
            "range_end_s": self.range_end_s,
            "local_source_path": self.local_source_path,
            "project_path": self.project_path,
            "export_path": self.export_path,
            "youtube_video_id": self.youtube_video_id,
            "progress": self.progress,
            "retry_state": self.retry_state.value if self.retry_state is not None else None,
            "retry_count": self.retry_count,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": deepcopy(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PipelineJob":
        try:
            state = PipelineState(str(raw.get("state", "")))
            retry_raw = raw.get("retry_state")
            retry_state = PipelineState(str(retry_raw)) if retry_raw is not None else None
            metadata_raw = raw.get("metadata", {})
            if not isinstance(metadata_raw, dict):
                raise PipelineFormatError("Metadata must be an object.")
            job = cls(
                id=str(raw.get("id", "")).strip(),
                vod_id=str(raw.get("vod_id", "")).strip(),
                vod_url=str(raw.get("vod_url", "")).strip(),
                channel_id=str(raw.get("channel_id", "")).strip(),
                source_title=str(raw.get("source_title", "")).strip(),
                state=state,
                range_start_s=_optional_float(raw.get("range_start_s"), "range_start_s"),
                range_end_s=_optional_float(raw.get("range_end_s"), "range_end_s"),
                local_source_path=_optional_text(raw.get("local_source_path")),
                project_path=_optional_text(raw.get("project_path")),
                export_path=_optional_text(raw.get("export_path")),
                youtube_video_id=_optional_text(raw.get("youtube_video_id")),
                progress=float(raw.get("progress", 0.0)),
                retry_state=retry_state,
                retry_count=int(raw.get("retry_count", 0)),
                error_code=_optional_text(raw.get("error_code")),
                error_message=_optional_text(raw.get("error_message")),
                created_at=str(raw.get("created_at", "")).strip(),
                updated_at=str(raw.get("updated_at", "")).strip(),
                metadata=deepcopy(metadata_raw),
            )
        except PipelineFormatError:
            raise
        except (TypeError, ValueError) as exc:
            raise PipelineFormatError(f"Invalid pipeline job: {exc}") from exc
        job.validate()
        return job
