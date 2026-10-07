from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from automation.cli import _ctrl_c, parse_time, run_cli
from automation.manager import PipelineJobBusyError, PipelineManager
from automation.runtime import PipelineCancellation, PipelineRuntime, PipelineRuntimeError
from automation.store import PipelineStore
from core.presets import PresetRepository, default_presets_catalog
from utils.ffmpeg import ffprobe_duration_seconds
from utils.subprocess_utils import popen_no_window


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {
            "AUTO_CUTTER_DATA_DIR": str(self.root),
            "AUTO_CUTTER_PIPELINE_STORE": str(self.root / "jobs.json"),
            "AUTO_CUTTER_CONFIG_DIR": str(self.root / "config"),
            "AUTO_CUTTER_OUTPUT_DIR": str(self.root / "output"),
            "AUTO_CUTTER_CREDENTIALS_DIR": str(self.root / "credentials"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.presets = PresetRepository(self.root / "config" / "presets.json")
        self.presets.save(default_presets_catalog())
        self.source = self.root / "local à 🎮.mp4"
        self.source.write_bytes(b"media" * 500)
        self.manager = PipelineManager(PipelineStore(self.root / "jobs.json"))
        self.runs = []

    def factory(self, *, on_event):
        runtime = PipelineRuntime(
            self.manager, self.presets, on_event,
            duration_probe=lambda _path: 10.0, video_probe=lambda _path: True,
            settings_loader=lambda: {},
            metadata_resolver=SimpleNamespace(metadata=lambda url: {"id": "123", "title": "VOD", "duration_s": 10.0}),
        )

        def fake_run(job_id, *, resume=False, cancellation=None):
            self.runs.append((job_id, resume))
            print("worker legacy diagnostic")
            on_event({"job_id": job_id, "stage": "analysis", "progress": 50})
            job = self.manager.get(job_id)
            if job.state.value == "done":
                return job
            project = self.root / (job.id + ".autocutter")
            project.write_text("project", encoding="utf-8")
            output = self.root / (job.id + ".mp4")
            output.write_bytes(b"video" * 500)
            if job.state.value == "downloading":
                self.manager.mark_downloaded(job_id, self.source)
            self.manager.mark_analyzed(job_id, project)
            self.manager.start_export(job_id)
            self.manager.mark_exported(job_id, output)
            return self.manager.complete_without_upload(job_id)

        runtime.run = fake_run
        return runtime

    def invoke(self, arguments, *, factory=None):
        out, err = io.StringIO(), io.StringIO()
        code = run_cli(arguments, runtime_factory=factory or self.factory, stdout=out, stderr=err)
        return code, out.getvalue(), err.getvalue()

    def test_ctrl_c_during_local_planning_probe_exits_70_without_job_or_gui(self):
        started = threading.Event()
        processes = []

        def spawn(_command, **kwargs):
            process = popen_no_window([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(30)"], **kwargs)
            processes.append(process)
            started.set()
            return process

        @contextlib.contextmanager
        def cancelled_ctrl_c(token):
            def cancel():
                if started.wait(2):
                    token.cancel()
            thread = threading.Thread(target=cancel, daemon=True)
            thread.start()
            try:
                yield
            finally:
                thread.join(2)

        def factory(**kwargs):
            return PipelineRuntime(self.manager, self.presets, duration_probe=ffprobe_duration_seconds,
                                   video_probe=lambda _path: True, settings_loader=lambda: {}, **kwargs)

        original = self.source.read_bytes()
        with patch("utils.ffmpeg.ensure_ffmpeg", return_value=("ffmpeg", "ffprobe")), patch("utils.ffmpeg.popen_no_window", side_effect=spawn), patch("automation.cli._ctrl_c", side_effect=cancelled_ctrl_c):
            code, out, _err = self.invoke(["process", str(self.source), "--json"], factory=factory)
        self.assertEqual(code, 70)
        self.assertEqual(json.loads(out)["exit_code"], 70)
        self.assertFalse(self.manager.store.path.exists())
        self.assertEqual(self.source.read_bytes(), original)
        self.assertIsNotNone(processes[0].poll())
        processes[0].wait(timeout=.2)

    def test_time_parser_accepts_all_requested_forms(self):
        self.assertEqual(parse_time("90"), 90.0)
        self.assertEqual(parse_time("01:30"), 90.0)
        self.assertEqual(parse_time("01:02:30"), 3750.0)
        self.assertEqual(parse_time("00:00:01.5"), 1.5)
        for value in ("", "-1", "NaN", "inf", "1:60", "1:60:00", "1:2:3:4", "1.5:00"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse_time(value)

    def test_presets_json_is_actual_catalog(self):
        code, out, err = self.invoke(["presets", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["presets"], self.presets.load())
        self.assertEqual(err, "")

    def test_json_allowed_before_or_after_command(self):
        for arguments in (["--json", "presets"], ["presets", "--json"]):
            code, out, _err = self.invoke(arguments)
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["ok"])

    def test_local_process_json_never_mixes_worker_output(self):
        code, out, err = self.invoke(["process", str(self.source), "--preset", "Balanced (Default)", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["job"]["state"], "done")
        self.assertNotIn("worker legacy diagnostic", out)
        self.assertIn("worker legacy diagnostic", err)
        self.assertIn("[Analysis] 50%", err)
        self.assertEqual(len(self.runs), 1)

    def test_vod_dry_run_is_read_only_and_interval_is_numeric(self):
        code, out, _err = self.invoke(["vod", "https://www.twitch.tv/videos/123", "--start", "00:02", "--duration", "4", "--youtube", "--dry-run", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["plan"]["range"], {"start_s": 2.0, "end_s": 6.0})
        self.assertTrue(payload["plan"]["delivery"]["youtube"])
        self.assertFalse(self.manager.store.path.exists())
        self.assertEqual(self.runs, [])

    def test_local_dry_run_human_shows_plan(self):
        code, out, err = self.invoke(["process", str(self.source), "--dry-run", "--end", "4"])
        self.assertEqual(code, 0)
        self.assertIn("Dry run complete", out)
        self.assertIn("Actual range: 0.000s to 4.000s", err)
        self.assertFalse(self.manager.store.path.exists())

    def test_invalid_time_unknown_preset_and_exclusive_boundaries_exit_two(self):
        requests = (["process", str(self.source), "--start", "broken"],
                    ["process", str(self.source), "--preset", "unknown"],
                    ["process", str(self.source), "--end", "4", "--duration", "2"],
                    ["process", str(self.source), "--output", "a.mp4", "--output-dir", "other"],
                    ["vod", "https://example.test/videos/123"])
        for request in requests:
            with self.subTest(request=request):
                code, out, err = self.invoke([*request, "--json"])
                self.assertEqual(code, 2)
                self.assertFalse(json.loads(out)["ok"])
                self.assertIn("Error", err)

    def test_resume_unknown_job_and_unknown_command_exit_two(self):
        for request in (["resume", "missing"], ["frobnicate"], []):
            code, out, _err = self.invoke([*request, "--json"])
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(out)["ok"])

    def test_jobs_and_resume_use_saved_job(self):
        code, out, _err = self.invoke(["process", str(self.source), "--json"])
        self.assertEqual(code, 0)
        job_id = json.loads(out)["job"]["id"]
        code, out, _err = self.invoke(["jobs", "--job", job_id, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["job"]["id"], job_id)
        code, out, _err = self.invoke(["resume", job_id, "--dry-run", "--json"])
        self.assertTrue(json.loads(out)["dry_run"])
        self.assertEqual(len(self.runs), 1)
        code, out, _err = self.invoke(["resume", job_id, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(self.runs[-1], (job_id, True))

    def test_help_and_version_do_not_require_runtime_factory(self):
        def forbidden(**kwargs):
            raise AssertionError("Runtime instantiated during help")
        for request in (["--help"], ["vod", "--help"], ["auth", "youtube", "--help"], ["--version"]):
            code, out, _err = self.invoke(request, factory=forbidden)
            self.assertEqual(code, 0)
            self.assertTrue(out)
        code, out, _err = self.invoke(["vod", "--help", "--json"], factory=forbidden)
        self.assertEqual(code, 0)
        self.assertIn("--start", json.loads(out)["help"])

    def test_json_unicode_path_round_trip_and_human_unicode_output(self):
        code, out, _err = self.invoke(["process", str(self.source), "--dry-run", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["plan"]["source"], str(self.source))
        self.assertTrue(out.isascii())
        code, _out, err = self.invoke(["process", str(self.source), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("à 🎮", err)

    def test_headless_entrypoint_from_different_cwd_and_no_display(self):
        root = Path(__file__).resolve().parents[1]
        source = f"import sys; sys.path.insert(0, {str(root)!r}); from automation.cli import run_cli; result=run_cli(['presets','--json']); assert 'PySide6.QtWidgets' not in sys.modules; assert 'ui.main_window' not in sys.modules; raise SystemExit(result)"
        result = subprocess.run([sys.executable, "-c", source], cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        for request in (["--help"], ["presets", "--json"], ["unknown", "--json"]):
            result = subprocess.run([sys.executable, str(root / "main.py"), *request], cwd=self.root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2 if request[0] == "unknown" else 0, result.stderr)

    def test_exit_codes_are_stable_and_secret_messages_redacted(self):
        for exit_code in (10, 20, 30, 40, 50, 60, 70):
            def failing(**kwargs):
                raise PipelineRuntimeError("test_error", "Authorization: Bearer super-secret", exit_code)
            code, out, err = self.invoke(["process", str(self.source), "--json"], factory=failing)
            self.assertEqual(code, exit_code)
            self.assertEqual(json.loads(out)["exit_code"], exit_code)
            self.assertNotIn("super-secret", out + err)
        def busy(**kwargs):
            raise PipelineJobBusyError("busy")
        code, out, _err = self.invoke(["process", str(self.source), "--json"], factory=busy)
        self.assertEqual(code, 75)
        self.assertEqual(json.loads(out)["error"]["code"], "job_busy")

    def test_ctrl_c_propagates_and_restores_handler(self):
        cancellation = PipelineCancellation()
        captures = []
        callback_calls = []
        previous = object()
        def install(signum, callback):
            captures.append((signum, callback))
            return previous
        with patch("automation.cli.signal.signal", side_effect=install):
            with _ctrl_c(cancellation, on_cancel=lambda: callback_calls.append(True)):
                captures[0][1](signal.SIGINT, None)
        self.assertTrue(cancellation.cancelled)
        self.assertEqual(callback_calls, [True])
        self.assertIs(captures[-1][1], previous)

    def test_twitch_auth_status_and_missing_client_id(self):
        with patch("core.config.get_setting", return_value=""), patch.dict(os.environ, {"AUTO_CUTTER_TWITCH_CLIENT_ID": ""}):
            code, out, _err = self.invoke(["auth", "twitch", "--status", "--json"])
        self.assertEqual(code, 50)
        self.assertEqual(json.loads(out)["error"]["code"], "twitch_client_id_missing")
        identity = SimpleNamespace(login="test-user", user_id="123")
        with patch("automation.twitch_auth.TwitchSession") as session:
            session.return_value.validated_context.return_value = (object(), identity)
            code, out, _err = self.invoke(["auth", "twitch", "--client-id", "configured-client", "--status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["login"], "test-user")

    def test_youtube_auth_lazy_adapter_and_optional_client_config(self):
        session = SimpleNamespace(validated_credentials=lambda: object(), disconnect=lambda: None)
        module = SimpleNamespace(YouTubeSession=lambda **kwargs: session)
        with patch("automation.cli.importlib.import_module", return_value=module):
            code, out, _err = self.invoke(["auth", "youtube", "--client-config", "desktop-client.json", "--status", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["authenticated"])
        with patch("automation.cli.importlib.import_module", return_value=module):
            code, out, _err = self.invoke(["auth", "youtube", "--logout", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["logged_out"])

    def _unknown_upload_job(self):
        job, _created = self.manager.create_configured_job(
            vod_id="unknown-upload", vod_url=self.source.as_uri(), start_s=0, end_s=10,
            source_title="Private video", local_source_path=self.source,
            metadata={"source_kind": "local", "delivery": {"youtube": True, "title": "Private video"}},
        )
        project = self.root / "upload.autocutter"
        project.write_text("project", encoding="utf-8")
        output = self.root / "uploaded.mp4"
        output.write_bytes(b"video" * 500)
        stat = output.stat()
        output.with_suffix(".mp4.automation.json").write_text(json.dumps({
            "format": "autocutter_automation_export", "version": 1, "signature": "fixture-render",
            "duration_s": 10.0, "require_audio": False,
            "output_size": stat.st_size, "output_mtime_ns": stat.st_mtime_ns,
        }), encoding="utf-8")
        self.manager.mark_analyzed(job.id, project)
        self.manager.start_export(job.id)
        self.manager.mark_exported(job.id, output)
        self.manager.set_upload_metadata(job.id, title="Private video")
        self.manager.start_upload(job.id)
        return self.manager.fail(job.id, "upload_outcome_unknown", "Resolve the accepted video ID manually.")

    def test_manually_confirmed_youtube_id_is_durable_before_resume_and_skips_insert(self):
        from automation.uploader import PipelineUploadService
        from integrations.youtube.uploader import UploadResult
        job = self._unknown_upload_job()
        seen_ids = []

        class ExistingVideoUploader:
            def upload(_self, current, *, cancellation=None, on_progress=None,
                       on_video_created=None, on_attempt_started=None):
                # The store was committed before execution; no insert/attempt is issued.
                seen_ids.append(self.manager.get(current.id).youtube_video_id)
                self.assertEqual(current.youtube_video_id, "abcdefghijk")
                return UploadResult(current.youtube_video_id)

        def factory(*, on_event):
            return PipelineRuntime(
                self.manager, self.presets, on_event,
                upload_service_factory=lambda manager: PipelineUploadService(manager, uploader_factory=ExistingVideoUploader),
            )

        code, out, _err = self.invoke(["resume", job.id, "--youtube-video-id", "abcdefghijk", "--json"], factory=factory)
        self.assertEqual(code, 0)
        self.assertEqual(seen_ids, ["abcdefghijk"])
        payload = json.loads(out)
        self.assertEqual(payload["job"]["state"], "done")
        self.assertEqual(payload["youtube_video_id"], "abcdefghijk")

    def test_manual_youtube_id_invalid_or_dry_run_conflict_never_mutates_job(self):
        job = self._unknown_upload_job()
        original = self.manager.get(job.id).to_mapping()
        for arguments in (
            ["--youtube-video-id", "too-short"],
            ["--youtube-video-id", "abc/defghij"],
            ["--youtube-video-id", "abcdefghijk", "--dry-run"],
        ):
            code, out, _err = self.invoke(["resume", job.id, *arguments, "--json"])
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(out)["ok"])
            self.assertEqual(self.manager.get(job.id).to_mapping(), original)

    def test_manual_youtube_id_conflicting_id_or_wrong_stage_is_rejected(self):
        job = self._unknown_upload_job()
        self.manager.record_youtube_video_id(job.id, "abcdefghijk")
        original = self.manager.get(job.id).to_mapping()
        code, _out, _err = self.invoke(["resume", job.id, "--youtube-video-id", "lmnopqrstuv", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(self.manager.get(job.id).to_mapping(), original)
        local = self.factory(on_event=lambda event: None).create_job(self.factory(on_event=lambda event: None).plan_local(self.source))
        original = self.manager.get(local.id).to_mapping()
        code, _out, _err = self.invoke(["resume", local.id, "--youtube-video-id", "abcdefghijk", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(self.manager.get(local.id).to_mapping(), original)

    def test_typed_youtube_auth_and_storage_exit_codes_survive_runtime_wrapper(self):
        from integrations.youtube.auth import YouTubeAuthError, YouTubeCredentialStoreError
        for error_type, expected in ((YouTubeAuthError, 50), (YouTubeCredentialStoreError, 60)):
            with self.subTest(error_type=error_type):
                job = self._unknown_upload_job()
                self.manager.retry(job.id)
                calls = []

                class BrokenUploadService:
                    def execute(_self, job_id, **kwargs):
                        calls.append(job_id)
                        raise error_type("typed_error", "Typed upload prerequisite failure")

                def factory(*, on_event):
                    return PipelineRuntime(
                        self.manager, self.presets, on_event,
                        duration_probe=lambda path: 10.0, video_probe=lambda path: True,
                        upload_service_factory=lambda manager: BrokenUploadService(),
                    )

                code, out, _err = self.invoke(["resume", job.id, "--json"], factory=factory)
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(out)["exit_code"], expected)
                self.assertEqual(calls, [job.id])
                self.assertEqual(self.manager.get(job.id).state.value, "failed")
                self.manager.delete(job.id)

    def test_busy_output_exit_75_preserves_export_stage_for_later_resume(self):
        from automation.locking import FileLockBusyError
        runtime = PipelineRuntime(self.manager, self.presets, duration_probe=lambda path: 10.0,
                                  video_probe=lambda path: True, settings_loader=lambda: {})
        job = runtime.create_job(runtime.plan_local(self.source))
        project = self.root / "pending.autocutter"
        project.write_text("project", encoding="utf-8")
        ready = self.manager.mark_analyzed(job.id, project)

        class BusyOutput:
            def hold(self, **kwargs):
                raise FileLockBusyError("Another executor owns the output destination.")

        with patch.object(self.manager, "reconcile_artifacts", return_value=ready), patch("automation.runtime.output_execution_lock", return_value=BusyOutput()):
            code, out, _err = self.invoke(["resume", job.id, "--json"], factory=lambda **kwargs: runtime)
        self.assertEqual(code, 75)
        self.assertEqual(json.loads(out)["error"]["code"], "job_busy")
        self.assertEqual(self.manager.get(job.id).state.value, "ready_export")


if __name__ == "__main__":
    unittest.main()
