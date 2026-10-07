from __future__ import annotations

import errno
import importlib
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class FileLockError(RuntimeError):
    pass


class FileLockBusyError(FileLockError):
    pass


class _LockState:
    def __init__(self) -> None:
        self.local_lock = threading.RLock()
        self.depth = 0
        self.stream: BinaryIO | None = None


_registry_guard = threading.Lock()
_registry: dict[str, _LockState] = {}


def _state_for(path: Path) -> _LockState:
    key = os.path.normcase(str(path.resolve()))
    with _registry_guard:
        return _registry.setdefault(key, _LockState())


def _try_lock(stream: BinaryIO) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl = importlib.import_module("fcntl")

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EBUSY}:
            return False
        raise


def _unlock(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl = importlib.import_module("fcntl")

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ProcessFileLock:
    """A shared thread lock plus an OS lock released automatically on process exit.

    Lock files remain in place: deleting one could let another process lock a
    different file while an existing owner still holds the original handle.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()
        self._state = _state_for(self.path)

    @contextmanager
    def hold(
        self,
        *,
        blocking: bool = True,
        timeout: float | None = None,
        reentrant: bool = True,
    ) -> Iterator[None]:
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        if not blocking:
            acquired = self._state.local_lock.acquire(blocking=False)
        elif deadline is None:
            acquired = self._state.local_lock.acquire()
        else:
            acquired = self._state.local_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise FileLockBusyError(f"Lock is already held: {self.path}")

        entered = False
        try:
            if self._state.depth:
                if not reentrant:
                    raise FileLockBusyError(f"Lock is already held: {self.path}")
                self._state.depth += 1
                entered = True
            else:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    stream = self.path.open("a+b")
                    try:
                        stream.seek(0, os.SEEK_END)
                        if stream.tell() == 0:
                            stream.write(b"\0")
                            stream.flush()
                        while not _try_lock(stream):
                            if not blocking or (deadline is not None and time.monotonic() >= deadline):
                                raise FileLockBusyError(f"Lock is already held: {self.path}")
                            time.sleep(0.01)
                        self._state.stream = stream
                        self._state.depth = 1
                        entered = True
                    except BaseException:
                        stream.close()
                        raise
                except OSError as exc:
                    raise FileLockError(f"Cannot lock {self.path}: {exc}") from exc
            yield
        finally:
            try:
                if entered:
                    self._state.depth -= 1
                    if self._state.depth == 0:
                        held_stream = self._state.stream
                        self._state.stream = None
                        if held_stream is not None:
                            try:
                                _unlock(held_stream)
                            finally:
                                held_stream.close()
            finally:
                self._state.local_lock.release()
