from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import json
import math
import os
import re
import signal
import sys
import time
import webbrowser
from collections.abc import Callable, Iterator, Sequence
from typing import Any, NoReturn, TextIO


EXIT_SUCCESS = 0
EXIT_ARGUMENTS = 2
EXIT_DOWNLOAD = 10
EXIT_ANALYSIS = 20
EXIT_EXPORT = 30
EXIT_UPLOAD = 40
EXIT_AUTH = 50
EXIT_FILESYSTEM = 60
EXIT_CANCELLED = 70
EXIT_BUSY = 75


def parse_time(value: str) -> float:
    """Seconds, MM:SS or HH:MM:SS become finite, nonnegative seconds."""
    try:
        parts = str(value).strip().split(":")
        if len(parts) not in {1, 2, 3} or any(not part for part in parts):
            raise ValueError
        numbers = [float(part) for part in parts]
        if any(not math.isfinite(number) or number < 0 for number in numbers):
            raise ValueError
        if any(not number.is_integer() for number in numbers[:-1]):
            raise ValueError
        if len(numbers) > 1 and numbers[-1] >= 60:
            raise ValueError
        if len(numbers) == 3 and numbers[1] >= 60:
            raise ValueError
        seconds = sum(number * 60 ** index for index, number in enumerate(reversed(numbers)))
        if not math.isfinite(seconds):
            raise ValueError
        return seconds
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError("Time must be seconds, MM:SS or HH:MM:SS (for example 90, 01:30, 01:02:30).") from exc


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


class _ArgumentError(ValueError):
    pass


def _youtube_video_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise argparse.ArgumentTypeError("YouTube video ID must contain exactly 11 letters, digits, underscores or hyphens.")
    return value


def _json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="Write one machine-readable JSON result to stdout; progress stays on stderr.")


def _processing_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", type=parse_time, help="Range start: seconds, MM:SS or HH:MM:SS.")
    boundaries = parser.add_mutually_exclusive_group()
    boundaries.add_argument("--end", type=parse_time, help="Range end (default: source end).")
    boundaries.add_argument("--duration", type=parse_time, help="Length measured from --start; alternative to --end.")
    parser.add_argument("--preset", help="A real GUI preset name; default: preferred catalog default.")
    destinations = parser.add_mutually_exclusive_group()
    destinations.add_argument("--output", help="Output file; existing files get a numeric suffix unless --force.")
    destinations.add_argument("--output-dir", help="Output directory (default: user Videos/Auto Cutter/Exports).")
    parser.add_argument("--quality", choices=("maximum", "very_high", "high", "balanced", "compact"), help="Override saved GUI export quality.")
    parser.add_argument("--youtube", action="store_true", help="Upload the validated export to YouTube PRIVATE (authenticate first).")
    parser.add_argument("--title", help="YouTube title (default: source title).")
    parser.add_argument("--description", default="", help="YouTube description.")
    parser.add_argument("--thumbnail", help="Optional thumbnail image for the private YouTube video.")
    parser.add_argument("--keep-source", action="store_true", help="Keep the downloaded VOD after DONE; original local files are always retained.")
    parser.add_argument("--force", action="store_true", help="Create a fresh job and allow replacing the explicit output; sources are protected.")
    parser.add_argument("--dry-run", action="store_true", help="Show metadata, range, preset and destination without processing or creating a job.")
    _json_option(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="AutoCutter", description="Run Auto Cutter through the same pipeline used by its GUI.")
    _json_option(parser)
    parser.add_argument("--version", action="store_true", help="Show application version.")
    sub = parser.add_subparsers(dest="command")
    presets = sub.add_parser("presets", help="List the actual GUI presets.")
    _json_option(presets)
    jobs = sub.add_parser("jobs", help="List persisted jobs, or inspect one.")
    jobs.add_argument("--job", help="Inspect one job by ID.")
    _json_option(jobs)
    local = sub.add_parser("process", help="Analyze, cut, export and validate a local video.")
    local.add_argument("source", help="Local video path.")
    _processing_options(local)
    vod = sub.add_parser("vod", help="Download and process a selected Twitch VOD interval.")
    vod.add_argument("url", help="https://www.twitch.tv/videos/VIDEO_ID")
    _processing_options(vod)
    resume = sub.add_parser("resume", help="Resume or retry an existing job using its saved preset snapshot.")
    resume.add_argument("job_id")
    resume_mode = resume.add_mutually_exclusive_group()
    resume_mode.add_argument("--dry-run", action="store_true", help="Inspect the saved job without executing it.")
    resume_mode.add_argument("--youtube-video-id", type=_youtube_video_id,
                             help="Record the matching PRIVATE video ID manually verified in YouTube Studio before resuming an unknown upload outcome.")
    _json_option(resume)
    auth = sub.add_parser("auth", help="Authenticate Twitch or YouTube, inspect status, or log out.")
    _json_option(auth)
    providers = auth.add_subparsers(dest="provider", required=True)
    for provider in ("twitch", "youtube"):
        provider_parser = providers.add_parser(provider)
        operation = provider_parser.add_mutually_exclusive_group()
        operation.add_argument("--logout", action="store_true", help="Delete locally stored credentials and invalidate pending authorization.")
        operation.add_argument("--status", action="store_true", help="Validate existing credentials without starting login.")
        if provider == "twitch":
            provider_parser.add_argument("--client-id", help="Twitch app client ID (or environment/saved GUI client ID).")
        else:
            provider_parser.add_argument("--client-config", help="Google desktop OAuth client JSON (or AUTO_CUTTER_YOUTUBE_CLIENT_CONFIG).")
        _json_option(provider_parser)
    return parser


class ProgressPrinter:
    _names = {"download": "Download", "analysis": "Analysis", "cuts": "Cuts",
              "export": "Export", "validation": "Validation", "youtube": "YouTube PRIVATE",
              "warning": "Warning", "done": "DONE"}

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self._last: dict[str, int] = {}

    def __call__(self, event: dict[str, Any]) -> None:
        from utils.redaction import redact_secrets
        stage = str(event.get("stage", "pipeline"))
        name = self._names.get(stage, stage)
        if "cuts_count" in event:
            print(f"[{name}] {event['cuts_count']} cuts; {float(event.get('removed_s', 0)) / 60:.1f} minutes removed", file=self.stream)
        elif "progress" in event:
            progress = int(event["progress"])
            previous = self._last.get(stage, -10)
            if progress not in {0, 100} and progress - previous < 5:
                return
            if previous == progress:
                return
            self._last[stage] = progress
            print(f"[{name}] {progress}%", file=self.stream)
        elif event.get("message"):
            message = redact_secrets(str(event["message"]))
            if stage == "export":
                if message.startswith("automation_export_config effective="):
                    try:
                        settings = json.loads(message.split("effective=", 1)[1])
                        message = f"codec: {settings['codec']}; method: {settings['method']}"
                    except (ValueError, KeyError):
                        return
                elif message.startswith("export_summary "):
                    method = re.search(r"\bmethod=(\S+)", message)
                    codec = re.search(r"\bcodec=(\S+)", message)
                    message = f"{method.group(1) if method else 'complete'}; {codec.group(1) if codec else ''}"
                elif not message.startswith("automation_export_codec_fallback"):
                    return
            print(f"[{name}] {message}", file=self.stream)
        self.stream.flush()


@contextlib.contextmanager
def _ctrl_c(cancellation: Any, *, on_cancel: Callable[[], None] | None = None) -> Iterator[None]:
    def interrupt(_signum: int, _frame: Any) -> None:
        cancellation.cancel()
        if on_cancel is not None:
            on_cancel()
    try:
        previous = signal.signal(signal.SIGINT, interrupt)
    except ValueError:  # Library invocation from a non-main thread.
        yield
        return
    try:
        yield
    except Exception as exc:
        if cancellation.cancelled:
            from automation.runtime import PipelineRuntimeError
            raise PipelineRuntimeError("cancelled", "Operation cancelled.", EXIT_CANCELLED) from exc
        raise
    finally:
        signal.signal(signal.SIGINT, previous)


def _human_plan(plan: dict[str, Any], stream: TextIO) -> None:
    media = plan["media"]
    selected = plan["range"]
    print(f"Source: {plan['source']}\nTitle: {media['title']}\nDuration: {media['duration_s']:.3f}s", file=stream)
    print(f"Requested range: {json.dumps(plan['requested_range'])}", file=stream)
    print(f"Actual range: {selected['start_s']:.3f}s to {selected['end_s']:.3f}s\nPreset: {plan['preset']['name']}", file=stream)
    print(f"Output: {plan['delivery']['output_path']}\nYouTube: {'PRIVATE' if plan['delivery']['youtube'] else 'disabled'}", file=stream)
    for warning in plan.get("warnings", []):
        print(f"Warning: {warning}", file=stream)


def _auth_twitch(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    from automation.runtime import PipelineCancellation, PipelineRuntimeError
    from automation.twitch import TwitchApiClient, TwitchAuthorizationPending
    from automation.twitch_auth import TwitchSession, TwitchTokenStore
    from core.config import get_setting, set_setting
    if args.logout:
        # Logout does not need a configured application ID or a network request.
        TwitchTokenStore().delete()
        return {"authenticated": False, "logged_out": True}
    client_id = str(args.client_id or os.environ.get("AUTO_CUTTER_TWITCH_CLIENT_ID") or get_setting("automation/twitch/client_id", "")).strip()
    if not client_id:
        raise PipelineRuntimeError("twitch_client_id_missing", "Set --client-id, AUTO_CUTTER_TWITCH_CLIENT_ID, or the client ID in the GUI.", EXIT_AUTH)
    session = TwitchSession(TwitchApiClient(client_id))
    if args.status:
        _token, identity = session.validated_context()
        return {"authenticated": True, "login": identity.login, "user_id": identity.user_id}
    cancellation = PipelineCancellation()
    with _ctrl_c(cancellation, on_cancel=session.cancel_authorization):
        authorization = session.begin_device_authorization()
        print(f"Open: {authorization.verification_uri}\nEnter this temporary authorization code: {authorization.user_code}", file=stream, flush=True)
        webbrowser.open(authorization.verification_uri)
        deadline = time.monotonic() + authorization.expires_in
        while True:
            cancellation.raise_if_cancelled()
            if time.monotonic() >= deadline:
                session.cancel_authorization()
                raise PipelineRuntimeError("authorization_expired", "Twitch authorization expired. Run auth twitch again.", EXIT_AUTH)
            try:
                session.complete_device_authorization(authorization)
                break
            except TwitchAuthorizationPending:
                # Event waits allow Ctrl+C to interrupt polling promptly.
                remaining = max(1, authorization.interval)
                for _tick in range(remaining * 10):
                    cancellation.raise_if_cancelled()
                    time.sleep(.1)
        cancellation.raise_if_cancelled()
        _token, identity = session.validated_context()
        set_setting("automation/twitch/client_id", client_id)
        return {"authenticated": True, "login": identity.login, "user_id": identity.user_id}


def _auth_youtube(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    from automation.runtime import PipelineCancellation, PipelineRuntimeError
    try:
        module = importlib.import_module("integrations.youtube.auth")
    except ImportError as exc:
        raise PipelineRuntimeError("youtube_dependency_missing", "Install the YouTube OAuth runtime dependencies.", EXIT_AUTH) from exc
    session = module.YouTubeSession(client_config_path=args.client_config)
    if args.logout:
        session.disconnect()
        return {"authenticated": False, "logged_out": True}
    if args.status:
        session.validated_credentials()
        return {"authenticated": True}
    cancellation = PipelineCancellation()
    with _ctrl_c(cancellation, on_cancel=session.cancel_authorization):
        session.connect(cancellation=cancellation,
                        on_authorization_url=lambda _url: print("Complete Google OAuth in the opened browser.", file=stream, flush=True))
        cancellation.raise_if_cancelled()
    return {"authenticated": True}


def run_cli(argv: Sequence[str] | None = None, *, runtime_factory: Callable[..., Any] | None = None,
            stdout: TextIO | None = None, stderr: TextIO | None = None) -> int:
    """Parse first, import only the requested core, and emit one stable result."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    for standard in (sys.stdout, sys.stderr):
        reconfigure = getattr(standard, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    output = stdout or sys.stdout
    progress = stderr or sys.stderr
    json_mode = "--json" in arguments
    command = ""
    parser = build_parser()
    captured_help = io.StringIO() if json_mode and any(arg in {"--help", "-h"} for arg in arguments) else None
    try:
        # argparse help/version must never import the analysis engines or GUI.
        with contextlib.redirect_stdout(captured_help or output), contextlib.redirect_stderr(progress):
            args = parser.parse_args(arguments)
        json_mode = getattr(args, "json", False)
        command = args.command or ""
        if args.version:
            from utils.app_version import app_version
            result: dict[str, Any] = {"version": app_version()}
        elif not command:
            raise _ArgumentError("Choose a command; use --help to list commands.")
        else:
            # Existing third-party/worker debug output belongs on stderr even in JSON mode.
            with contextlib.redirect_stdout(progress):
                result = _execute(args, progress, runtime_factory)
        payload = {"ok": True, "command": command or "version", **result}
        if json_mode:
            print(json.dumps(payload, ensure_ascii=True, allow_nan=False), file=output)
        else:
            _human_result(command, result, output)
        return EXIT_SUCCESS
    except SystemExit as exc:
        if captured_help is not None and not exc.code:
            print(json.dumps({"ok": True, "help": captured_help.getvalue()}, ensure_ascii=True), file=output)
        return int(exc.code or 0)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt,)) or str(getattr(exc, "code", "")).endswith("cancelled"):
            code, error_code, message = EXIT_CANCELLED, "cancelled", "Operation cancelled."
        elif isinstance(exc, _ArgumentError):
            code, error_code, message = EXIT_ARGUMENTS, "invalid_arguments", str(exc)
        elif isinstance(exc, Exception):
            code, error_code, message = _error_info(exc, command)
        else:
            raise
        from utils.redaction import redact_secrets
        clean = redact_secrets(message)
        if json_mode:
            print(json.dumps({"ok": False, "command": command, "error": {"code": error_code, "message": clean}, "exit_code": code}, ensure_ascii=True), file=output)
        print(f"Error [{error_code}]: {clean}", file=progress)
        return code


def _execute(args: argparse.Namespace, progress: TextIO, runtime_factory: Callable[..., Any] | None) -> dict[str, Any]:
    command = args.command
    if command == "presets":
        from core.presets import PresetRepository
        return {"presets": PresetRepository().load()}
    if command == "jobs":
        from automation.manager import PipelineManager
        manager = PipelineManager()
        if args.job:
            return {"job": manager.get(args.job).to_mapping()}
        return {"jobs": [job.to_mapping() for job in manager.list_jobs()]}
    if command == "auth":
        return {"provider": args.provider, **(_auth_twitch(args, progress) if args.provider == "twitch" else _auth_youtube(args, progress))}
    from automation.runtime import PipelineCancellation, PipelineRuntime
    from utils.ffmpeg import cancellable_probe_scope
    factory = runtime_factory or PipelineRuntime
    runtime = factory(on_event=ProgressPrinter(progress))
    if command == "resume":
        job = runtime.manager.get(args.job_id)
        if args.dry_run:
            return {"dry_run": True, "job": job.to_mapping()}
        cancellation = PipelineCancellation()
        with _ctrl_c(cancellation):
            with runtime.manager.execution_lock(job.id):
                if args.youtube_video_id:
                    runtime.manager.record_youtube_video_id(job.id, args.youtube_video_id)
                completed = runtime.run(job.id, resume=True, cancellation=cancellation)
        return {"job": completed.to_mapping(), "output": completed.export_path, "youtube_video_id": completed.youtube_video_id}
    options = {key: getattr(args, key) for key in (
        "start", "end", "duration", "preset", "output", "output_dir", "youtube", "title",
        "description", "thumbnail", "keep_source", "force", "quality",
    )}
    cancellation = PipelineCancellation()
    with _ctrl_c(cancellation), cancellable_probe_scope(cancellation):
        try:
            plan = runtime.plan_vod(args.url, **options) if command == "vod" else runtime.plan_local(args.source, **options)
        except Exception:
            cancellation.raise_if_cancelled()
            raise
        cancellation.raise_if_cancelled()
        if args.dry_run:
            if not getattr(args, "json", False):
                _human_plan(plan, progress)
            return {"dry_run": True, "plan": plan}
        job = runtime.create_job(plan)
        print(f"Job: {job.id}", file=progress, flush=True)
        print(f"Source: {plan['media']['title']}\nPreset: {plan['preset']['name']}", file=progress, flush=True)
        completed = runtime.run(job.id, cancellation=cancellation)
    return {"job": completed.to_mapping(), "output": completed.export_path, "youtube_video_id": completed.youtube_video_id}


def _error_info(exc: Exception, command: str) -> tuple[int, str, str]:
    code = getattr(exc, "code", type(exc).__name__)
    explicit = getattr(exc, "exit_code", None)
    if explicit is not None:
        return int(explicit), str(code), str(exc)
    name = type(exc).__name__
    if str(code).endswith("cancelled") or "Cancelled" in name:
        return EXIT_CANCELLED, "cancelled", str(exc)
    if name == "PipelineJobBusyError" or name == "FileLockBusyError":
        return EXIT_BUSY, "job_busy", str(exc)
    if name == "PipelineJobNotFoundError":
        return EXIT_ARGUMENTS, "job_not_found", str(exc)
    if isinstance(exc, OSError) or name.endswith("StoreError"):
        return EXIT_FILESYSTEM, str(code), str(exc)
    if command == "auth" or name in {"TwitchAuthRequired", "TwitchApiError"} or "Auth" in name:
        return EXIT_AUTH, str(code), str(exc)
    if "Download" in name:
        return EXIT_DOWNLOAD, str(code), str(exc)
    if "Analysis" in name:
        return EXIT_ANALYSIS, str(code), str(exc)
    if "Export" in name:
        return EXIT_EXPORT, str(code), str(exc)
    if "Upload" in name or "YouTube" in name:
        return EXIT_UPLOAD, str(code), str(exc)
    if isinstance(exc, (ValueError, KeyError)):
        return EXIT_ARGUMENTS, str(code), str(exc)
    return EXIT_ARGUMENTS, str(code), str(exc)


def _human_result(command: str, result: dict[str, Any], stream: TextIO) -> None:
    if "version" in result:
        print(result["version"], file=stream)
    elif command == "presets":
        for name in result["presets"]:
            print(name, file=stream)
    elif command == "jobs" and "jobs" in result:
        for job in result["jobs"]:
            print(f"{job['id']}  {job['state']:14}  {job['source_title']}", file=stream)
    elif result.get("dry_run"):
        if "job" in result:
            print(json.dumps(result["job"], ensure_ascii=False, indent=2), file=stream)
        else:
            print("Dry run complete. No job was created.", file=stream)
    elif "output" in result:
        print(f"DONE\nOutput:\n{result['output']}", file=stream)
        if result.get("youtube_video_id"):
            print(f"YouTube video (PRIVATE):\n{result['youtube_video_id']}", file=stream)
    elif "job" in result:
        print(json.dumps(result["job"], ensure_ascii=False, indent=2), file=stream)
    elif command == "auth":
        print(f"{result['provider']}: {'authenticated' if result['authenticated'] else 'logged out'}", file=stream)
