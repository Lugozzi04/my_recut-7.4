from __future__ import annotations

import json
import hashlib
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, TypeVar

from automation.locking import FileLockError, ProcessFileLock
from automation.models import PipelineFormatError, PipelineJob


PIPELINE_STORE_FORMAT = "auto_cutter_pipeline_store"
PIPELINE_STORE_VERSION = 1
_T = TypeVar("_T")


class PipelineStoreError(RuntimeError):
    pass


def default_pipeline_store_path() -> Path:
    custom = str(os.environ.get("AUTO_CUTTER_PIPELINE_STORE", "") or "").strip()
    if custom:
        return Path(custom).expanduser().resolve()

    if str(os.environ.get("AUTO_CUTTER_DATA_DIR", "") or "").strip():
        from utils.runtime_paths import data_root
        return data_root() / "pipeline" / "jobs.json"

    if os.name == "nt":
        base = str(os.environ.get("LOCALAPPDATA", "") or "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "Auto Cutter" / "pipeline" / "jobs.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Auto Cutter" / "pipeline" / "jobs.json"

    base = str(os.environ.get("XDG_DATA_HOME", "") or "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "auto-cutter" / "pipeline" / "jobs.json"


class PipelineStore:
    """Versioned JSON with short process-safe transactions and atomic replacement."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = (Path(path) if path is not None else default_pipeline_store_path()).expanduser().resolve()
        self._lock = ProcessFileLock(self.path.with_name(f"{self.path.name}.lock"))

    @contextmanager
    def _store_lock(self) -> Iterator[None]:
        try:
            with self._lock.hold(timeout=10.0):
                yield
        except FileLockError as exc:
            raise PipelineStoreError(str(exc)) from exc

    def load(self) -> list[PipelineJob]:
        with self._store_lock():
            return self._load_unlocked()

    def _load_unlocked(self) -> list[PipelineJob]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._decode_payload(raw)
        except PipelineStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, PipelineFormatError) as exc:
            raise PipelineStoreError(f"Cannot read pipeline store {self.path}: {exc}") from exc

    def transaction(self, mutation: Callable[[list[PipelineJob]], _T]) -> _T:
        """Read the latest jobs, apply one mutation, and durably commit it.

        A raised exception leaves the previous JSON unchanged. Callbacks must
        only perform short in-memory work; media processing uses a job lock.
        """
        with self._store_lock():
            jobs = self._load_unlocked()
            before = self._serialize(jobs)
            result = mutation(jobs)
            after = self._serialize(jobs)
            if after != before:
                self._write_unlocked(after)
            return result

    def job_lock(self, job_id: str) -> ProcessFileLock:
        requested = str(job_id).strip()
        if not requested:
            raise PipelineStoreError("Job id is required for an execution lock.")
        digest = hashlib.sha256(requested.encode("utf-8")).hexdigest()
        directory = self.path.with_name(f"{self.path.name}.locks")
        return ProcessFileLock(directory / f"{digest}.lock")

    def save(self, jobs: Iterable[PipelineJob]) -> None:
        with self._store_lock():
            # An explicit replacement still must not overwrite corrupt input.
            self._load_unlocked()
            self._write_unlocked(self._serialize(list(jobs)))

    def _serialize(self, jobs: list[PipelineJob]) -> str:
        try:
            self._validate_unique(jobs)
            payload = {
                "format": PIPELINE_STORE_FORMAT,
                "version": PIPELINE_STORE_VERSION,
                "jobs": [job.to_mapping() for job in jobs],
            }
            return json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        except (AttributeError, PipelineFormatError, TypeError, ValueError) as exc:
            raise PipelineStoreError(f"Cannot serialize pipeline store: {exc}") from exc

    def _write_unlocked(self, serialized: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise PipelineStoreError(f"Cannot write pipeline store {self.path}: {exc}") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, job_id: str) -> PipelineJob | None:
        requested = str(job_id).strip()
        with self._store_lock():
            for job in self._load_unlocked():
                if job.id == requested:
                    return job
        return None

    def upsert(self, job: PipelineJob) -> None:
        def apply(jobs: list[PipelineJob]) -> None:
            for index, current in enumerate(jobs):
                if current.id == job.id:
                    jobs[index] = job
                    break
            else:
                jobs.append(job)
        self.transaction(apply)

    def delete(self, job_id: str) -> bool:
        requested = str(job_id).strip()
        def apply(jobs: list[PipelineJob]) -> bool:
            remaining = [job for job in jobs if job.id != requested]
            if len(remaining) == len(jobs):
                return False
            jobs[:] = remaining
            return True
        return self.transaction(apply)

    def _decode_payload(self, raw: object) -> list[PipelineJob]:
        if not isinstance(raw, dict):
            raise PipelineStoreError("Pipeline store root must be an object.")
        if raw.get("format") != PIPELINE_STORE_FORMAT:
            raise PipelineStoreError("Unsupported pipeline store format.")
        try:
            version = int(raw.get("version", 0))
        except (TypeError, ValueError) as exc:
            raise PipelineStoreError("Pipeline store version is invalid.") from exc
        if version != PIPELINE_STORE_VERSION:
            raise PipelineStoreError(
                f"Unsupported pipeline store version {version}; supported version is {PIPELINE_STORE_VERSION}."
            )
        raw_jobs = raw.get("jobs")
        if not isinstance(raw_jobs, list):
            raise PipelineStoreError("Pipeline store jobs must be a list.")

        jobs: list[PipelineJob] = []
        for index, item in enumerate(raw_jobs):
            if not isinstance(item, dict):
                raise PipelineStoreError(f"Pipeline job at index {index} must be an object.")
            try:
                jobs.append(PipelineJob.from_mapping(item))
            except PipelineFormatError as exc:
                raise PipelineStoreError(f"Invalid pipeline job at index {index}: {exc}") from exc
        self._validate_unique(jobs)
        return jobs

    @staticmethod
    def _validate_unique(jobs: Iterable[PipelineJob]) -> None:
        ids: set[str] = set()
        vod_ids: set[str] = set()
        for job in jobs:
            job.validate()
            if job.id in ids:
                raise PipelineStoreError(f"Duplicate pipeline job id: {job.id}")
            if job.vod_id in vod_ids:
                raise PipelineStoreError(f"Duplicate VOD id: {job.vod_id}")
            ids.add(job.id)
            vod_ids.add(job.vod_id)
