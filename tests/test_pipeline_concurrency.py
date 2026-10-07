from __future__ import annotations

import subprocess
import site
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from automation.manager import PipelineJobBusyError, PipelineManager
from automation.models import PipelineJob, PipelineState
from automation.store import PipelineStore, PipelineStoreError


ROOT = Path(__file__).resolve().parents[1]
CHILD_PYTHON = getattr(sys, "_base_executable", sys.executable)
# Windows venv executables are launchers; kill the actual interpreter when
# testing abrupt owner death, while retaining this environment's dependencies.
CHILD_BOOTSTRAP = "import site\n" + "".join(f"site.addsitedir({path!r})\n" for path in site.getsitepackages())
_HOLD_JOB = """
import sys
from pathlib import Path
from automation.manager import PipelineManager
from automation.store import PipelineStore
manager = PipelineManager(PipelineStore(sys.argv[1]))
with manager.execution_lock(sys.argv[2]):
    Path(sys.argv[3]).write_text('ready', encoding='utf-8')
    sys.stdin.readline()
"""


class PipelineConcurrencyTests(unittest.TestCase):
    def manager(self, root: Path) -> PipelineManager:
        return PipelineManager(PipelineStore(root / "jobs.json"))

    def discover(self, manager: PipelineManager, vod_id: str = "vod-1") -> PipelineJob:
        job, _ = manager.discover_vod(vod_id=vod_id, vod_url=f"https://www.twitch.tv/videos/{vod_id}")
        return job

    @contextmanager
    def child_owner(self, manager: PipelineManager, job_id: str, root: Path) -> Iterator[subprocess.Popen[str]]:
        ready = root / "owner-ready"
        process = subprocess.Popen(
            [CHILD_PYTHON, "-c", CHILD_BOOTSTRAP + _HOLD_JOB, str(manager.store.path), job_id, str(ready)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10.0
            while not ready.exists():
                if process.poll() is not None:
                    output, error = process.communicate()
                    self.fail(f"Lock owner exited early: {output} {error}")
                if time.monotonic() >= deadline:
                    self.fail("Timed out waiting for the lock owner.")
                time.sleep(0.01)
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5.0)

    def test_transaction_rolls_back_callback_failure_and_corrupt_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp))
            job = self.discover(manager)
            before = manager.store.path.read_bytes()

            def broken(jobs: list[PipelineJob]) -> None:
                jobs[0].metadata["not_committed"] = True
                raise OSError("callback failed")

            with self.assertRaisesRegex(OSError, "callback failed"):
                manager.store.transaction(broken)
            self.assertEqual(manager.store.path.read_bytes(), before)
            manager.store.path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(PipelineStoreError):
                manager.update_metadata(job.id, {"preset": "balanced"})
            with self.assertRaises(PipelineStoreError):
                manager.store.save([])
            self.assertEqual(manager.store.path.read_text(encoding="utf-8"), "{broken")

    def test_two_processes_preserve_updates_to_different_jobs(self) -> None:
        code = """
import sys, time
from automation.store import PipelineStore
store = PipelineStore(sys.argv[1])
for _ in range(20):
    def increment(jobs):
        job = next(item for item in jobs if item.id == sys.argv[2])
        job.metadata['counter'] = job.metadata.get('counter', 0) + 1
        time.sleep(0.002)
    store.transaction(increment)
"""
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp))
            jobs = [self.discover(manager, "first"), self.discover(manager, "second")]
            processes = [
                subprocess.Popen(
                    [CHILD_PYTHON, "-c", CHILD_BOOTSTRAP + code, str(manager.store.path), job.id],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                for job in jobs
            ]
            try:
                for process in processes:
                    output, error = process.communicate(timeout=15.0)
                    self.assertEqual(process.returncode, 0, f"{output} {error}")
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5.0)
            restored = manager.list_jobs()
            self.assertEqual(len(restored), 2)
            self.assertEqual([job.metadata["counter"] for job in restored], [20, 20])

    def test_concurrent_discovery_creates_one_job(self) -> None:
        code = """
import sys
from automation.manager import PipelineManager
from automation.store import PipelineStore
_, created = PipelineManager(PipelineStore(sys.argv[1])).discover_vod(
    vod_id='same-vod', vod_url='https://www.twitch.tv/videos/same-vod')
print(int(created))
"""
        with tempfile.TemporaryDirectory() as tmp:
            manager = self.manager(Path(tmp))
            processes = [
                subprocess.Popen(
                    [CHILD_PYTHON, "-c", CHILD_BOOTSTRAP + code, str(manager.store.path)],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                for _ in range(2)
            ]
            created = []
            try:
                for process in processes:
                    output, error = process.communicate(timeout=15.0)
                    self.assertEqual(process.returncode, 0, error)
                    created.append(int(output.strip()))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=5.0)
            self.assertEqual(sorted(created), [0, 1])
            self.assertEqual(len(manager.list_jobs()), 1)

    def test_live_process_blocks_same_job_but_not_other_jobs_and_death_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.manager(root)
            owned = self.discover(manager, "owned")
            other = self.discover(manager, "other")
            with self.child_owner(manager, owned.id, root) as process:
                with self.assertRaises(PipelineJobBusyError):
                    with self.manager(root).execution_lock(owned.id):
                        self.fail("A second process must not execute the owned job.")
                with manager.execution_lock(other.id):
                    manager.update_metadata(other.id, {"available": True})
                process.terminate()
                process.wait(timeout=5.0)
                with manager.execution_lock(owned.id):
                    manager.update_metadata(owned.id, {"owner_released": True})
            self.assertTrue(manager.get(owned.id).metadata["owner_released"])

    def test_different_instances_share_thread_locks_and_support_nested_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.manager(root)
            second = self.manager(root)
            job = self.discover(first)
            errors: list[BaseException] = []

            def compete() -> None:
                try:
                    with second.execution_lock(job.id):
                        errors.append(AssertionError("A competing thread acquired the job."))
                except PipelineJobBusyError:
                    pass
                except BaseException as exc:
                    errors.append(exc)

            with first.execution_lock(job.id):
                with second.execution_lock(job.id):
                    self.assertEqual(first.get(job.id).id, job.id)
                with self.assertRaises(PipelineJobBusyError):
                    with second.execution_lock(job.id, reentrant=False):
                        pass
                thread = threading.Thread(target=compete)
                thread.start()
                thread.join(timeout=5.0)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_recovery_skips_live_owner_and_recovers_only_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = self.manager(root)
            owned = self.discover(manager, "owned")
            orphan = self.discover(manager, "orphan")
            for job in (owned, orphan):
                manager.request_range(job.id)
                manager.select_range(job.id, 0.0, 30.0)
            with self.child_owner(manager, owned.id, root):
                recovered = self.manager(root).recover_interrupted_jobs()
                self.assertEqual([job.id for job in recovered], [orphan.id])
                self.assertEqual(manager.get(owned.id).state, PipelineState.DOWNLOADING)
                self.assertEqual(manager.get(orphan.id).error_code, "interrupted")
            self.assertEqual([job.id for job in manager.recover_interrupted_jobs()], [owned.id])


if __name__ == "__main__":
    unittest.main()
